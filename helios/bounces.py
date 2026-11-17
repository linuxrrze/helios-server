"""
What a mail in the bounce mailbox says, and what follows from it (see
helios.verp for the envelope senders, helios.bounce_mailbox for reading the
mailbox, `manage.py process_bounces`).

  find_email_id(msg)  the OutgoingEmail it is about: by a valid VERP token in
                      Delivered-To / X-Original-To / To, the DSN's
                      Original-Envelope-Id or the returned headers; else None
  classify(msg)       a Finding: what kind of mail it is
  record(id, finding) the BounceEvent, the email's status, the suppression list;
                      returns the folder the mail goes to

Kinds of mail:

  hard       failed for good (DSN Action: failed, Status 5.x.x; or a 5xx code or
             a typical phrase in a non-standard bounce): the email bounced_hard,
             the address suppressed
  soft       failed for now (failed 4.x.x, e.g. mailbox full, expired in the
             queue): bounced_soft; HELIOS_BOUNCE_SOFT_LIMIT of them within
             HELIOS_BOUNCE_SOFT_DAYS suppress the address like a hard bounce
  unknown    a bounce whose kind cannot be told: counts as soft
  delayed    still being tried (Action: delayed): only logged
  complaint  the recipient marked the email as spam (ARF, RFC 5965): the address
             suppressed, with HELIOS_BOUNCE_COMPLAINT_OPTOUT also opted out
  auto_reply out-of-office and other automatic replies: ignored
  other      neither (a person writing to the bounce address, a success notice):
             ignored

Standard bounces are DSNs (multipart/report; report-type=delivery-status, RFC
3464), read with Python's email package. Older or unusual mail servers (old
Exchange, qmail, Exim without DSN, freemail providers) send plain text: there
the recipient is known from VERP anyway, and only the kind is guessed from
status codes and phrases. The bounce mail itself is never stored, only what it
said (status code, a shortened diagnostic text).
"""
import datetime
import logging
import re
from email.message import Message
from email.parser import HeaderParser
from email.utils import parseaddr
from typing import NamedTuple

from django.conf import settings
from django.db import transaction

from . import utils, verp
from .models import BounceEvent, EmailOptOut, EmailSuppression, OutgoingEmail

logger = logging.getLogger(__name__)

HARD = BounceEvent.HARD
SOFT = BounceEvent.SOFT
UNKNOWN = BounceEvent.UNKNOWN
COMPLAINT = BounceEvent.COMPLAINT
DELAYED = 'delayed'
DELIVERED = 'delivered'
AUTO_REPLY = 'auto_reply'
OTHER = 'other'

# the folders of the bounce mailbox a mail ends up in
PROCESSED = 'processed'
IGNORED = 'ignored'
UNMATCHED = 'unmatched'
ERROR = 'error'
FOLDERS = (PROCESSED, IGNORED, UNMATCHED, ERROR)

DIAGNOSTIC_LENGTH = 500
# how much text of a bounce is searched for codes and phrases
TEXT_LIMIT = 20000


class Finding(NamedTuple):
  kind: str
  status: str = ''        # '5.1.1', or an SMTP reply code '550'
  diagnostic: str = ''    # what the remote server said, shortened
  recipient: str = ''     # Final-Recipient of a DSN, for the log


##
## reading the mail
##

def _header_values(msg, name):
  return [str(value) for value in (msg.get_all(name) or [])]


def _one_line(text, limit=DIAGNOSTIC_LENGTH):
  return re.sub(r'\s+', ' ', str(text or '')).strip()[:limit]


def _decoded(part):
  """the text of a text/* part, decoded (as far as possible)"""
  payload = part.get_payload(decode=True)
  if payload is None:
    payload = part.get_payload()
    return payload if isinstance(payload, str) else ''
  charset = part.get_content_charset() or 'utf-8'
  try:
    return payload.decode(charset, 'replace')
  except LookupError:
    return payload.decode('utf-8', 'replace')


def _report_type(msg):
  if msg.get_content_type() != 'multipart/report':
    return ''
  return (msg.get_param('report-type') or '').lower()


def _parts(msg, content_type):
  return [part for part in msg.walk() if part.get_content_type() == content_type]


def _field_blocks(part):
  """the field blocks of a message/delivery-status (or feedback-report) part, as Messages"""
  payload = part.get_payload()
  if isinstance(payload, list):
    blocks = [block for block in payload if isinstance(block, Message)]
    if blocks:
      return blocks
    payload = ''
  if isinstance(payload, bytes):
    payload = payload.decode('utf-8', 'replace')
  # not parsed (e.g. base64-encoded): the blocks are separated by empty lines
  text = _decoded(part) if part.get('Content-Transfer-Encoding') else (payload or '')
  parser = HeaderParser()
  return [parser.parsestr(block.strip() + '\n') for block in re.split(r'\r?\n\s*\r?\n', text) if block.strip()]


ENHANCED = re.compile(r'(?<![\d.])([245])\.(\d{1,3})\.(\d{1,3})(?![\d.])')
SMTP_CODE = re.compile(r'(?<![\d.])([45]\d\d)(?:[ -]|$)(?![\d.])', re.MULTILINE)


def _enhanced(text):
  match = ENHANCED.search(text or '')
  return '.'.join(match.groups()) if match else ''


##
## which email a mail is about
##

DELIVERY_HEADERS = ('Delivered-To', 'X-Original-To', 'Envelope-To', 'X-Envelope-To', 'X-Delivered-To', 'To', 'Cc')


def find_email_id(msg):
  """the id of the OutgoingEmail the mail is about (by a valid VERP token), or None"""
  # where the mailbox received it: the envelope recipient was our VERP address
  for name in DELIVERY_HEADERS:
    for value in _header_values(msg, name):
      ids = verp.email_ids_in(value)
      if ids:
        return ids[0]
  # the DSN's ENVID (we send the token as ENVID)
  for part in _parts(msg, 'message/delivery-status'):
    for block in _field_blocks(part)[:1]:
      email_id = verp.email_id_from_token(str(block.get('Original-Envelope-Id', '')))
      if email_id:
        return email_id
  # the returned headers of our email (Return-Path), e.g. in a complaint (ARF)
  for part in msg.walk():
    content_type = part.get_content_type()
    if content_type == 'text/rfc822-headers':
      ids = verp.email_ids_in(_decoded(part))
    elif content_type == 'message/rfc822' and isinstance(part.get_payload(), list) and part.get_payload():
      inner = part.get_payload()[0]
      ids = verp.email_ids_in('\n'.join('%s: %s' % (k, v) for k, v in inner.items()))
    else:
      continue
    if ids:
      return ids[0]
  return None


##
## what kind of mail it is
##

SEVERITY = {HARD: 5, SOFT: 4, UNKNOWN: 3, DELAYED: 2, DELIVERED: 1}


def _dsn_finding(msg):
  """the Finding of a DSN (RFC 3464), or None if it has no usable recipient fields"""
  findings = []
  for part in _parts(msg, 'message/delivery-status'):
    for block in _field_blocks(part)[1:] or _field_blocks(part):
      action = str(block.get('Action', '')).strip().lower()
      status = _enhanced(str(block.get('Status', '')))
      diagnostic = _one_line(block.get('Diagnostic-Code', ''))
      recipient = _one_line(block.get('Final-Recipient', '') or block.get('Original-Recipient', ''), 250)
      if not action and not status:
        continue
      if action.startswith('delayed'):
        kind = DELAYED
      elif action in ('delivered', 'relayed', 'expanded'):
        kind = DELIVERED
      else:
        # failed (or no action given)
        status = status or _enhanced(diagnostic)
        if status.startswith('5'):
          kind = HARD
        elif status.startswith('4'):
          kind = SOFT
        else:
          code = SMTP_CODE.search(diagnostic)
          if code:
            status = code.group(1)
            kind = HARD if status.startswith('5') else SOFT
          else:
            kind = _phrase_kind(diagnostic) or UNKNOWN
      findings.append(Finding(kind, status, diagnostic, recipient))
  if not findings:
    return None
  return max(findings, key=lambda f: SEVERITY[f.kind])


COMPLAINT_TYPES = ('abuse', 'fraud', 'other', '')


def _complaint_finding(msg):
  for part in _parts(msg, 'message/feedback-report'):
    for block in _field_blocks(part):
      feedback_type = str(block.get('Feedback-Type', '')).strip().lower()
      if feedback_type in COMPLAINT_TYPES:
        return Finding(COMPLAINT, '', _one_line('complaint: %s' % (feedback_type or 'abuse')))
      return Finding(OTHER, '', _one_line('feedback report: %s' % feedback_type))
  return Finding(COMPLAINT, '', 'complaint')


BOUNCE_SENDER = re.compile(r'^(mailer-daemon|postmaster|mail-daemon|mailerdaemon|bounces?|nobody)@', re.I)
BOUNCE_SUBJECT = re.compile(
  r'undeliver|undelivered|delivery (status notification|failure|failed|has failed|problem|notification)'
  r'|failure notice|returned mail|returned to sender|mail delivery (failed|failure|system|subsystem)'
  r'|could not be delivered|non-?delivery|nondeliverable|unzustellbar|nicht zustellbar|delivery delayed'
  r'|delayed mail|warning: message .* delayed|zustellung (verz|fehl)', re.I)
AUTO_REPLY_SUBJECT = re.compile(
  r'out of (the )?office|automatic reply|auto(matic)?[- ]?(reply|response|antwort)|automatische antwort'
  r'|abwesen|vacation|urlaub|\booo\b|away from (the )?office|nicht im b(ü|ue)ro', re.I)


def _looks_like_bounce(msg):
  if _report_type(msg):
    return True
  if msg.get('X-Failed-Recipients'):
    return True
  sender = parseaddr(str(msg.get('From', '')))
  if BOUNCE_SENDER.match(sender[1] or '') or re.search(r'mail delivery (sub)?system|mailer-daemon', sender[0] or '', re.I):
    return True
  return bool(BOUNCE_SUBJECT.search(_one_line(msg.get('Subject', ''), 300)))


def _is_auto_reply(msg):
  auto_submitted = str(msg.get('Auto-Submitted', '')).strip().lower()
  if auto_submitted and auto_submitted != 'no':
    return True
  if msg.get('X-Autoreply') or msg.get('X-Autorespond') or msg.get('X-Auto-Reply'):
    return True
  if str(msg.get('Precedence', '')).strip().lower() in ('auto_reply', 'auto-reply'):
    return True
  if msg.get('X-Auto-Response-Suppress') and AUTO_REPLY_SUBJECT.search(_one_line(msg.get('Subject', ''), 300)):
    return True
  return bool(AUTO_REPLY_SUBJECT.search(_one_line(msg.get('Subject', ''), 300)))


# where the returned copy of our email starts in a plain-text bounce (not searched)
ORIGINAL_MARKERS = re.compile(
  r'^.*(below this line is a copy of the message|this is a copy of the message|original message follows'
  r'|----- original message -----|------ this is a copy|--- the header of the original message'
  r'|original message headers).*$', re.I | re.MULTILINE)

HARD_PHRASES = re.compile(
  r'user unknown|unknown user|no such (user|mailbox|recipient|address)|mailbox (unavailable|not found|does not exist)'
  r'|does not exist|doesn.t exist|recipient (address )?rejected|address rejected|invalid (recipient|mailbox|address)'
  r'|unrouteable address|no mailbox here|account (has been |is )?(disabled|deactivated|closed|inactive)'
  r'|recipient not found|unknown (or illegal )?alias|not a valid (recipient|mailbox)|host or domain name not found'
  r'|domain (not found|does not exist)|unbekannt|existiert nicht|permanent (error|failure|fatal)'
  r'|this is a permanent error|i.ve given up|could not be found', re.I)
SOFT_PHRASES = re.compile(
  r'mailbox (is )?full|over ?quota|quota exceeded|exceeded (the|their|its) (storage|quota)|insufficient storage'
  r'|out of storage|temporar(y|ily)|try again later|postfach (ist )?voll|speicherplatz', re.I)
DELAY_PHRASES = re.compile(
  r'has not (yet )?been delivered|not been delivered yet|will (continue to |keep )?(re)?try|still (being )?(re)?tr(y|ied)'
  r'|delivery (is )?delayed|delivery delayed|warning: message .* delayed|zustellung verz', re.I)


def _phrase_kind(text):
  if HARD_PHRASES.search(text or ''):
    return HARD
  if SOFT_PHRASES.search(text or ''):
    return SOFT
  return None


def _bounce_text(msg):
  """the explaining text of a non-standard bounce, without the returned copy of our email"""
  texts = []
  for part in msg.walk():
    if part.get_content_type() in ('message/rfc822', 'text/rfc822-headers'):
      break
    if part.get_content_maintype() == 'text' and part.get_content_type() != 'text/rfc822-headers':
      text = _decoded(part)
      if part.get_content_subtype() == 'html':
        text = re.sub(r'<[^>]+>', ' ', text)
      texts.append(text)
    if sum(len(t) for t in texts) > TEXT_LIMIT:
      break
  text = '\n'.join(texts)[:TEXT_LIMIT]
  marker = ORIGINAL_MARKERS.search(text)
  return text[:marker.start()] if marker else text


def _line_with(text, start):
  begin = text.rfind('\n', 0, start) + 1
  end = text.find('\n', start)
  return _one_line(text[begin:end if end >= 0 else len(text)])


def _text_finding(msg):
  """the Finding of a plain-text bounce: the kind is guessed from codes and phrases"""
  subject = _one_line(msg.get('Subject', ''), 300)
  text = _bounce_text(msg)
  if DELAY_PHRASES.search(subject) or (DELAY_PHRASES.search(text) and not HARD_PHRASES.search(text)):
    return Finding(DELAYED, '', _one_line(subject))
  for match in ENHANCED.finditer(text):
    if match.group(1) in '45':
      status = '.'.join(match.groups())
      return Finding(HARD if status.startswith('5') else SOFT, status, _line_with(text, match.start()))
  match = SMTP_CODE.search(text)
  if match:
    status = match.group(1)
    return Finding(HARD if status.startswith('5') else SOFT, status, _line_with(text, match.start()))
  for kind, phrases in ((HARD, HARD_PHRASES), (SOFT, SOFT_PHRASES)):
    match = phrases.search(text)
    if match:
      return Finding(kind, '', _line_with(text, match.start()))
  return Finding(UNKNOWN, '', _one_line(subject))


def classify(msg):
  """the Finding of a mail of the bounce mailbox (an email.message.Message)"""
  report_type = _report_type(msg)
  if report_type == 'feedback-report':
    return _complaint_finding(msg)
  if report_type == 'delivery-status':
    finding = _dsn_finding(msg)
    if finding is not None:
      return finding
  bounce = _looks_like_bounce(msg)
  if not bounce:
    return Finding(AUTO_REPLY if _is_auto_reply(msg) else OTHER, '', _one_line(msg.get('Subject', ''), 200))
  return _text_finding(msg)


##
## what follows from it
##

def _address(email):
  """the bare address the email went to, or '' (not kept, or only masked)"""
  address = parseaddr(email.recipient or '')[1]
  return '' if not address or '***' in address else address


def soft_bounces_count(email_hash, now=None):
  """temporary bounces of the address within HELIOS_BOUNCE_SOFT_DAYS (since its suppression was lifted)"""
  now = now or datetime.datetime.utcnow()
  since = now - datetime.timedelta(days=settings.HELIOS_BOUNCE_SOFT_DAYS)
  lifted = EmailSuppression.objects.filter(email_hash=email_hash, lifted_at__isnull=False).values_list(
    'lifted_at', flat=True).first()
  if lifted and lifted > since:
    since = lifted
  return BounceEvent.objects.filter(email_hash=email_hash, bounce_type__in=(SOFT, UNKNOWN),
                                    created_at__gt=since).count()


def record(email_id, finding):
  """
  What FINDING means for the OutgoingEmail EMAIL_ID: a BounceEvent, its status,
  the suppression list. Returns the folder for the mail (PROCESSED, IGNORED).
  """
  if finding.kind in (AUTO_REPLY, OTHER, DELIVERED):
    logger.info("bounce mailbox: %s about email %s ignored", finding.kind, email_id)
    return IGNORED
  if finding.kind == DELAYED:
    logger.info("bounce mailbox: email %s delayed (%s)", email_id, finding.diagnostic)
    return PROCESSED

  with transaction.atomic():
    email = OutgoingEmail.objects.select_for_update().filter(id=email_id).first()
    if email is None:
      # its election has been deleted since
      logger.info("bounce mailbox: %s about email %s, which no longer exists", finding.kind, email_id)
      return PROCESSED
    address = _address(email)
    email_hash = utils.hash_email(address) if address else ''
    BounceEvent.objects.create(outgoing_email=email, email_hash=email_hash or '', bounce_type=finding.kind,
                               status_code=finding.status[:16], diagnostic=finding.diagnostic[:DIAGNOSTIC_LENGTH])
    logger.info("bounce mailbox: email %s (%s) %s %s", email.id, email.kind, finding.kind, finding.status)

    if finding.kind == COMPLAINT:
      if email_hash:
        EmailSuppression.suppress(email_hash=email_hash, source=EmailSuppression.SOURCE_COMPLAINT,
                                  reason=finding.diagnostic)
        if settings.HELIOS_BOUNCE_COMPLAINT_OPTOUT:
          EmailOptOut.add_opt_out(address)
      return PROCESSED

    new_status = OutgoingEmail.BOUNCED_HARD if finding.kind == HARD else OutgoingEmail.BOUNCED_SOFT
    if email_hash:
      if finding.kind == HARD:
        EmailSuppression.suppress(email_hash=email_hash, source=EmailSuppression.SOURCE_HARD_BOUNCE,
                                  status_code=finding.status, reason=finding.diagnostic)
      elif soft_bounces_count(email_hash) >= settings.HELIOS_BOUNCE_SOFT_LIMIT:
        logger.info("bounce mailbox: %d temporary bounces of the address of email %s: suppressed",
                    settings.HELIOS_BOUNCE_SOFT_LIMIT, email.id)
        EmailSuppression.suppress(email_hash=email_hash, source=EmailSuppression.SOURCE_SOFT_BOUNCES,
                                  status_code=finding.status, reason=finding.diagnostic)
        new_status = OutgoingEmail.BOUNCED_HARD
    # a bounce after delivery: of an email the mail server took (never weaker than before)
    if email.status == OutgoingEmail.SENT or (email.status == OutgoingEmail.BOUNCED_SOFT
                                              and new_status == OutgoingEmail.BOUNCED_HARD):
      email.status = new_status
      email.last_error_code = (finding.status or finding.kind)[:32]
      email.last_error_text = finding.diagnostic
      email.save(update_fields=['status', 'last_error_code', 'last_error_text'])
  return PROCESSED
