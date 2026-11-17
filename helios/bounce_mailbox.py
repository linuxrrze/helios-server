"""
Reading the bounce mailbox over IMAP (`manage.py process_bounces`, run every few
minutes by a systemd timer or cron): each mail of HELIOS_BOUNCE_IMAP_FOLDER
(read or not: UID SEARCH ALL) is judged by helios.bounces and moved to one of the
folders

  processed  a bounce, delay notice or complaint about one of our emails
  ignored    an automatic reply (out of office) or other mail about one of them
  unmatched  no valid VERP token (spam to the bounce address, forged bounces,
             bounces of emails sent before bounce handling or with another secret)
  error      could not be read (malformed, too large); see the log

which are created if missing. Mails in these folders are deleted
HELIOS_BOUNCE_RETENTION_DAYS after they arrived. TLS only, with certificate
checks (implicit TLS, or STARTTLS with HELIOS_BOUNCE_IMAP_STARTTLS=1).

Each mail is handled once: the mail, by folder, UIDVALIDITY and UID and by its
Message-ID, is recorded (ProcessedBounceMail) in the same transaction as its
effect. A mail found again (moving it failed, two runs at once) is only moved.
One bad mail does not stop the run.
"""
import datetime
import email
import imaplib
import logging
import re
import ssl
from typing import NamedTuple

from django.conf import settings
from django.db import IntegrityError, transaction

from . import bounces
from .models import ProcessedBounceMail

logger = logging.getLogger(__name__)

# larger mails are not fetched (a bounce with RET=HDRS is a few kilobytes)
MAX_SIZE = 5 * 1024 * 1024
TIMEOUT = 60

_ATOM = re.compile(r'^[A-Za-z0-9._/-]+$')


class NotConfigured(Exception):
  pass


def configured():
  return bool(settings.HELIOS_BOUNCE_IMAP_HOST and settings.HELIOS_BOUNCE_SECRET)


def _quote(name):
  if _ATOM.match(name):
    return name
  return '"%s"' % name.replace('\\', '\\\\').replace('"', '\\"')


def _check(result, what):
  typ, data = result
  if typ != 'OK':
    raise imaplib.IMAP4.error('%s: %s %r' % (what, typ, data))
  return data


class Mailbox:
  """the few IMAP operations process_bounces needs, on an imaplib connection"""

  def __init__(self, connection):
    self.conn = connection
    self.deleted = False

  @classmethod
  def connect(cls):
    if not configured():
      raise NotConfigured("set HELIOS_BOUNCE_IMAP_HOST and HELIOS_BOUNCE_SECRET (and the other HELIOS_BOUNCE_* settings)")
    context = ssl.create_default_context()   # verifies the certificate and the host name
    host, port = settings.HELIOS_BOUNCE_IMAP_HOST, settings.HELIOS_BOUNCE_IMAP_PORT
    if settings.HELIOS_BOUNCE_IMAP_STARTTLS:
      conn = imaplib.IMAP4(host, port, timeout=TIMEOUT)
      conn.starttls(ssl_context=context)
    else:
      conn = imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=TIMEOUT)
    _check(conn.login(settings.HELIOS_BOUNCE_IMAP_USER, settings.HELIOS_BOUNCE_IMAP_PASSWORD), 'LOGIN')
    return cls(conn)

  def capable(self, name):
    return name.upper() in {str(c).upper() for c in (self.conn.capabilities or ())}

  def folders(self):
    names = set()
    for line in _check(self.conn.list(), 'LIST') or []:
      if not line:
        continue
      if isinstance(line, tuple):
        line = b' '.join(line)
      text = line.decode('utf-8', 'replace') if isinstance(line, bytes) else str(line)
      match = re.match(r'\((?P<flags>[^)]*)\) (?P<sep>"[^"]*"|NIL) (?P<name>.+)$', text)
      if match:
        name = match.group('name').strip()
        if name.startswith('"') and name.endswith('"'):
          name = name[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        names.add(name)
    return names

  def ensure_folders(self, names):
    existing = {n.lower() for n in self.folders()}
    for name in names:
      if name.lower() not in existing:
        logger.info("bounce mailbox: creating folder %s", name)
        _check(self.conn.create(_quote(name)), 'CREATE %s' % name)

  def select(self, folder):
    """select FOLDER (read-write); its UIDVALIDITY"""
    _check(self.conn.select(_quote(folder)), 'SELECT %s' % folder)
    self.deleted = False
    data = _check(self.conn.status(_quote(folder), '(UIDVALIDITY)'), 'STATUS %s' % folder)
    match = re.search(rb'UIDVALIDITY (\d+)', b' '.join(d if isinstance(d, bytes) else str(d).encode() for d in data))
    if not match:
      raise imaplib.IMAP4.error('no UIDVALIDITY for %s' % folder)
    return int(match.group(1))

  def search(self, *criteria):
    data = _check(self.conn.uid('SEARCH', None, *criteria), 'SEARCH')
    return [int(uid) for uid in b' '.join(d for d in data if d).split()]

  def size(self, uid):
    data = _check(self.conn.uid('FETCH', str(uid), '(RFC822.SIZE)'), 'FETCH SIZE')
    for item in data:
      text = item[0] if isinstance(item, tuple) else item
      match = re.search(rb'RFC822\.SIZE (\d+)', text or b'')
      if match:
        return int(match.group(1))
    return None

  def fetch(self, uid):
    """the mail UID (without marking it read), or None if it is gone"""
    data = _check(self.conn.uid('FETCH', str(uid), '(BODY.PEEK[])'), 'FETCH')
    for item in data:
      if isinstance(item, tuple) and len(item) == 2:
        return item[1]
    return None

  def move(self, uid, folder):
    if self.capable('MOVE'):
      _check(self.conn.uid('MOVE', str(uid), _quote(folder)), 'MOVE')
      return
    _check(self.conn.uid('COPY', str(uid), _quote(folder)), 'COPY')
    self.delete(uid)

  def delete(self, uid):
    _check(self.conn.uid('STORE', str(uid), '+FLAGS', '(\\Deleted)'), 'STORE')
    self.deleted = True

  def expunge(self):
    if self.deleted:
      _check(self.conn.expunge(), 'EXPUNGE')
      self.deleted = False

  def logout(self):
    try:
      self.expunge()
      self.conn.logout()
    except Exception:
      logger.warning("bounce mailbox: logout failed", exc_info=True)


class Stats(NamedTuple):
  results: dict
  deleted: int


def _message_id(msg):
  try:
    return re.sub(r'\s+', '', str(msg.get('Message-ID', '') or ''))[:255]
  except Exception:
    return ''


def _handle(msg):
  """the folder for MSG, with its effect recorded (helios.bounces)"""
  email_id = bounces.find_email_id(msg)
  if email_id is None:
    logger.info("bounce mailbox: mail without a valid VERP token (%s)", bounces._one_line(msg.get('Subject', ''), 100))
    return bounces.UNMATCHED
  return bounces.record(email_id, bounces.classify(msg))


def process_one(mailbox, folder, uidvalidity, uid):
  """handle the mail UID of FOLDER; the folder it was moved to"""
  done = ProcessedBounceMail.objects.filter(folder=folder, uidvalidity=uidvalidity, uid=uid).first()
  if done:
    # handled before, but still here: moving it failed
    mailbox.move(uid, done.result)
    return done.result

  message_id, result = '', bounces.ERROR
  try:
    size = mailbox.size(uid)
    if size is not None and size > MAX_SIZE:
      logger.warning("bounce mailbox: mail %s is too large (%d bytes)", uid, size)
    else:
      raw = mailbox.fetch(uid)
      if raw is None:
        return None
      msg = email.message_from_bytes(raw)
      message_id = _message_id(msg)
      previous = message_id and ProcessedBounceMail.objects.filter(message_id=message_id).first()
      if previous:
        # the same mail once more (e.g. UIDVALIDITY changed)
        logger.info("bounce mailbox: mail %s handled before (%s)", uid, message_id)
        result = previous.result
        ProcessedBounceMail.objects.get_or_create(folder=folder, uidvalidity=uidvalidity, uid=uid,
                                                  defaults={'message_id': message_id, 'result': result})
      else:
        with transaction.atomic():
          result = _handle(msg)
          # in the same transaction: once recorded, its effect is there too (and only once)
          ProcessedBounceMail.objects.create(folder=folder, uidvalidity=uidvalidity, uid=uid,
                                             message_id=message_id, result=result)
  except IntegrityError:
    # another run handled it at the same time
    done = ProcessedBounceMail.objects.filter(folder=folder, uidvalidity=uidvalidity, uid=uid).first()
    result = done.result if done else bounces.ERROR
  except imaplib.IMAP4.abort:
    raise
  except Exception:
    logger.exception("bounce mailbox: mail %s could not be handled", uid)
    result = bounces.ERROR
  if result == bounces.ERROR:
    ProcessedBounceMail.objects.get_or_create(folder=folder, uidvalidity=uidvalidity, uid=uid,
                                              defaults={'message_id': message_id, 'result': result})
  mailbox.move(uid, result)
  return result


def delete_old(mailbox, now, inbox=None):
  """
  delete the mails of the Helios folders older than HELIOS_BOUNCE_RETENTION_DAYS, and
  the records of handled mails as old, except those of mails still in the inbox
  (INBOX: folder, UIDVALIDITY, UIDs), which would be handled again; how many mails
  """
  cutoff = now - datetime.timedelta(days=settings.HELIOS_BOUNCE_RETENTION_DAYS)
  date = '%02d-%s-%04d' % (cutoff.day, ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov',
                                        'Dec')[cutoff.month - 1], cutoff.year)
  count = 0
  for folder in bounces.FOLDERS:
    mailbox.select(folder)
    uids = mailbox.search('BEFORE', date)
    for uid in uids:
      mailbox.delete(uid)
    mailbox.expunge()
    count += len(uids)
  old = ProcessedBounceMail.objects.filter(processed_at__lt=cutoff)
  if inbox:
    folder, uidvalidity, uids = inbox
    old = old.exclude(folder=folder, uidvalidity=uidvalidity, uid__in=uids)
  old.delete()
  return count


def process(mailbox=None, limit=None, now=None):
  """handle the mails of the bounce mailbox (at most LIMIT) and delete old ones; Stats"""
  own = mailbox is None
  mailbox = mailbox or Mailbox.connect()
  now = now or datetime.datetime.utcnow()
  results = {folder: 0 for folder in bounces.FOLDERS}
  try:
    mailbox.ensure_folders(bounces.FOLDERS)
    folder = settings.HELIOS_BOUNCE_IMAP_FOLDER
    uidvalidity = mailbox.select(folder)
    # all of them, also those someone has read: handled ones are known by UIDVALIDITY and UID
    all_uids = mailbox.search('ALL')
    uids = all_uids[:limit] if limit else all_uids
    for uid in uids:
      result = process_one(mailbox, folder, uidvalidity, uid)
      if result:
        results[result] += 1
    mailbox.expunge()
    deleted = delete_old(mailbox, now, inbox=(folder, uidvalidity, all_uids))
  finally:
    if own:
      mailbox.logout()
  logger.info("bounce mailbox: %s, %d old mails deleted",
              ', '.join('%d %s' % (n, folder) for folder, n in results.items()), deleted)
  return Stats(results, deleted)
