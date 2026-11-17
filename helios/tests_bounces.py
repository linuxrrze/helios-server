"""
Tests for bounce handling: classifying the samples in helios/fixtures/bounces
(helios.bounces), reading the bounce mailbox with a fake IMAP server
(helios.bounce_mailbox, `manage.py process_bounces`) and the suppression list
(EmailSuppression, helios.mail.suppressed).
"""
import datetime
import email
import imaplib
import os
import re
import ssl
import uuid
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from helios import bounce_mailbox, bounces, verp
from helios import mail as helios_mail
from helios import utils
from helios.models import (BounceEvent, Election, EmailOptOut, EmailSuppression, OutgoingEmail, ProcessedBounceMail,
                           Voter)
from helios_auth.models import User

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'bounces')

BOUNCE_SETTINGS = dict(
  HELIOS_BOUNCE_DOMAIN='bounces.example.org', HELIOS_BOUNCE_SECRET='test-bounce-secret-0123456789',
  HELIOS_BOUNCE_IMAP_HOST='imap.example.org', HELIOS_BOUNCE_IMAP_PORT=993, HELIOS_BOUNCE_IMAP_STARTTLS=False,
  HELIOS_BOUNCE_IMAP_USER='bounce', HELIOS_BOUNCE_IMAP_PASSWORD='imap-password', HELIOS_BOUNCE_IMAP_FOLDER='INBOX',
  HELIOS_BOUNCE_RETENTION_DAYS=30, HELIOS_BOUNCE_SOFT_LIMIT=3, HELIOS_BOUNCE_SOFT_DAYS=14,
  HELIOS_BOUNCE_COMPLAINT_OPTOUT=False)


def sample(name, email_id=42, message_id='<20261117090210.1@vote.example.org>'):
  """the bytes of a sample bounce, for the email EMAIL_ID"""
  with open(os.path.join(FIXTURES, name), 'rb') as f:
    raw = f.read()
  token = verp.token(email_id)
  return (raw.replace(b'{{token}}', token.encode()).replace(b'{{TOKEN}}', token.upper().encode())
          .replace(b'{{message_id}}', message_id.encode()))


# file, kind, status code
SAMPLES = [
  ('postfix_user_unknown.eml', bounces.HARD, '5.1.1'),
  ('postfix_expired.eml', bounces.SOFT, '4.2.2'),
  ('postfix_delayed.eml', bounces.DELAYED, '4.4.1'),
  ('exim_plain.eml', bounces.HARD, '5.1.1'),
  ('exim_dsn.eml', bounces.HARD, '5.0.0'),
  ('sendmail_dsn.eml', bounces.HARD, '5.1.1'),
  ('exchange_old.eml', bounces.HARD, '5.1.1'),
  ('exchange_new.eml', bounces.HARD, '5.1.10'),
  ('gmail_user_unknown.eml', bounces.HARD, '5.1.1'),
  ('gmail_over_quota.eml', bounces.SOFT, '4.2.2'),
  ('qmail.eml', bounces.HARD, '5.1.1'),
  ('freemail_unclear.eml', bounces.UNKNOWN, ''),
  ('arf_complaint.eml', bounces.COMPLAINT, ''),
  ('ooo_exchange.eml', bounces.AUTO_REPLY, ''),
  ('ooo_vacation.eml', bounces.AUTO_REPLY, ''),
  ('human_reply.eml', bounces.OTHER, ''),
]


@override_settings(**BOUNCE_SETTINGS)
class ClassificationTests(SimpleTestCase):

  def test_samples(self):
    for name, kind, status in SAMPLES:
      with self.subTest(sample=name):
        msg = email.message_from_bytes(sample(name, email_id=4711))
        self.assertEqual(bounces.find_email_id(msg), 4711)
        finding = bounces.classify(msg)
        self.assertEqual((finding.kind, finding.status), (kind, status))
        self.assertLessEqual(len(finding.diagnostic), bounces.DIAGNOSTIC_LENGTH)

  def test_diagnostics(self):
    finding = bounces.classify(email.message_from_bytes(sample('postfix_user_unknown.eml')))
    self.assertIn('User unknown', finding.diagnostic)
    self.assertNotIn('\n', finding.diagnostic)
    self.assertEqual(finding.recipient, 'rfc822; voter@example.net')
    # the returned copy of our email (here with something looking like codes) is not searched
    finding = bounces.classify(email.message_from_bytes(sample('qmail.eml')))
    self.assertEqual(finding.diagnostic, 'Sorry, no mailbox here by that name. (#5.1.1)')

  def test_token_sources(self):
    # in the DSN's ENVID only
    raw = sample('exim_dsn.eml', email_id=7).replace(b'Envelope-to:', b'X-Other:').replace(
      b'To: bounce+', b'X-To: bounce+').replace(b'Return-path: <bounce+', b'Return-path: <x+')
    self.assertEqual(bounces.find_email_id(email.message_from_bytes(raw)), 7)
    # upper-cased by a mail server
    self.assertEqual(bounces.find_email_id(email.message_from_bytes(sample('sendmail_dsn.eml', email_id=8))), 8)
    # a complaint: only in the returned headers of our email
    self.assertEqual(bounces.find_email_id(email.message_from_bytes(sample('arf_complaint.eml', email_id=9))), 9)

  def test_forged_and_missing_tokens(self):
    self.assertIsNone(bounces.find_email_id(email.message_from_bytes(sample('forged_token.eml'))))
    raw = sample('postfix_user_unknown.eml', email_id=5)
    with override_settings(HELIOS_BOUNCE_SECRET='another-secret-0123456789'):
      self.assertIsNone(bounces.find_email_id(email.message_from_bytes(raw)))
    # a valid token of another bounce domain
    with override_settings(HELIOS_BOUNCE_DOMAIN='bounces.example.com'):
      msg = email.message_from_bytes(raw.replace(b'Original-Envelope-Id', b'X-Envid'))
      self.assertIsNone(bounces.find_email_id(msg))

  def test_malformed_mail_does_not_raise(self):
    msg = email.message_from_bytes(sample('malformed.eml'))
    self.assertEqual(bounces.find_email_id(msg), 42)
    self.assertIn(bounces.classify(msg).kind, (bounces.UNKNOWN, bounces.OTHER))
    for raw in (b'', b'\x00\xff\xfe', b'Content-Type: multipart/report\n\n--x\n'):
      bounces.classify(email.message_from_bytes(raw))

  def test_feedback_types(self):
    raw = sample('arf_complaint.eml')
    self.assertEqual(bounces.classify(email.message_from_bytes(raw.replace(b'Feedback-Type: abuse',
                                                                           b'Feedback-Type: not-spam'))).kind,
                     bounces.OTHER)


##
## a fake IMAP server, as imaplib.IMAP4_SSL
##

class FakeFolder:
  def __init__(self, uidvalidity):
    self.uidvalidity = uidvalidity
    self.next_uid = 1
    self.messages = {}   # uid -> [raw, flags, internal date]

  def add(self, raw, flags=(), date=None):
    uid = self.next_uid
    self.next_uid += 1
    self.messages[uid] = [raw, set(flags), date or datetime.datetime.utcnow()]
    return uid


class FakeServer:
  def __init__(self, capabilities=('IMAP4REV1', 'MOVE', 'UIDPLUS')):
    self.capabilities = tuple(capabilities)
    self.folders = {'INBOX': FakeFolder(1000)}
    self.connections = []
    self.fail_moves = 0

  def deliver(self, raw, folder='INBOX', **kwargs):
    return self.folders[folder].add(raw, **kwargs)

  def contents(self, folder):
    return [m[0] for m in self.folders[folder].messages.values()] if folder in self.folders else []


class FakeIMAP:
  server = None
  error = imaplib.IMAP4.error
  abort = imaplib.IMAP4.abort

  def __init__(self, host, port=None, ssl_context=None, timeout=None):
    self.server = FakeIMAP.server
    self.server.connections.append(self)
    self.host, self.port, self.ssl_context, self.timeout = host, port, ssl_context, timeout
    self.capabilities = self.server.capabilities
    self.selected = None
    self.logged_in = False
    self.started_tls = None

  @staticmethod
  def _name(name):
    name = name.decode() if isinstance(name, bytes) else name
    return name[1:-1] if name.startswith('"') else name

  def starttls(self, ssl_context=None):
    self.started_tls = ssl_context
    return 'OK', [b'Begin TLS']

  def login(self, user, password):
    self.logged_in = (user, password)
    return 'OK', [b'Logged in']

  def list(self, directory='', pattern='*'):
    return 'OK', [b'(\\HasNoChildren) "/" "%s"' % n.encode() for n in self.server.folders]

  def create(self, name):
    self.server.folders.setdefault(self._name(name), FakeFolder(2000 + len(self.server.folders)))
    return 'OK', [b'CREATE completed']

  def select(self, name):
    name = self._name(name)
    if name not in self.server.folders:
      return 'NO', [b'no such folder']
    self.selected = self.server.folders[name]
    return 'OK', [str(len(self.selected.messages)).encode()]

  def status(self, name, names):
    folder = self.server.folders[self._name(name)]
    return 'OK', [b'"%s" (UIDVALIDITY %d)' % (self._name(name).encode(), folder.uidvalidity)]

  def uid(self, command, *args):
    command = command.upper()
    messages = self.selected.messages
    if command == 'SEARCH':
      criteria = args[1:]
      if criteria == ('UNSEEN',):
        uids = [uid for uid, m in messages.items() if '\\Seen' not in m[1]]
      elif criteria[0] == 'BEFORE':
        before = datetime.datetime.strptime(criteria[1], '%d-%b-%Y')
        uids = [uid for uid, m in messages.items() if m[2] < before]
      else:
        raise AssertionError(criteria)
      return 'OK', [' '.join(str(u) for u in uids).encode()]
    uid = int(args[0])
    if command == 'FETCH':
      if uid not in messages:
        return 'OK', [None]
      raw = messages[uid][0]
      if args[1] == '(RFC822.SIZE)':
        return 'OK', [b'1 (UID %d RFC822.SIZE %d)' % (uid, len(raw))]
      assert args[1] == '(BODY.PEEK[])', args
      return 'OK', [(b'1 (UID %d BODY[] {%d}' % (uid, len(raw)), raw), b')']
    if command in ('MOVE', 'COPY'):
      if command == 'MOVE' and 'MOVE' not in self.capabilities:
        return 'BAD', [b'unknown command']
      if self.server.fail_moves:
        self.server.fail_moves -= 1
        return 'NO', [b'move failed']
      raw, flags, date = messages[uid]
      self.server.folders[self._name(args[1])].add(raw, flags - {'\\Deleted'}, date)
      if command == 'MOVE':
        del messages[uid]
      return 'OK', [b'done']
    if command == 'STORE':
      assert args[1:] == ('+FLAGS', '(\\Deleted)'), args
      messages[uid][1].add('\\Deleted')
      return 'OK', [b'done']
    raise AssertionError(command)

  def expunge(self):
    for uid in [u for u, m in self.selected.messages.items() if '\\Deleted' in m[1]]:
      del self.selected.messages[uid]
    return 'OK', [b'']

  def logout(self):
    return 'BYE', [b'']


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', **BOUNCE_SETTINGS)
class BounceTestCase(TestCase):

  def setUp(self):
    self.server = FakeIMAP.server = FakeServer()
    patcher = mock.patch('helios.bounce_mailbox.imaplib.IMAP4_SSL', FakeIMAP)
    patcher.start()
    self.addCleanup(patcher.stop)
    self.admin = User.objects.create(user_type='password', user_id='admin@example.org', name='Ada Admin',
                                     info={'password': 'x'})
    self.election = self.make_election('board')
    self.voter = self.make_voter(self.election, 'voter@example.net')

  def make_election(self, short_name):
    return Election.objects.create(admin=self.admin, uuid=str(uuid.uuid4()), short_name=short_name,
                                   name='Election %s' % short_name, election_type='election')

  def make_voter(self, election, address, login_id=None):
    return Voter.objects.create(uuid=str(uuid.uuid4()), election=election, voter_login_id=login_id or address,
                                voter_email=address, voter_name='Voter %s' % address, voter_password='pw')

  def sent_email(self, voter=None, kind=OutgoingEmail.KIND_BULK):
    """an email as helios.mail leaves it once the mail server took it"""
    voter = voter or self.voter
    email_obj = OutgoingEmail.objects.create(kind=kind, election=voter.election, voter=voter, status=OutgoingEmail.SENT,
                                             recipient='"%s" <%s>' % (voter.voter_name, voter.voter_email))
    email_obj.bounce_token = verp.token(email_obj.id)
    email_obj.save()
    return email_obj

  def run_command(self):
    out = StringIO()
    call_command('process_bounces', stdout=out)
    return out.getvalue()

  def queue_and_send(self, voter):
    from helios.tests import run_queued_tasks
    batch = helios_mail.create_batch(voter.election, 'email/simple_subject.txt', 'email/simple_body.txt',
                                     {'custom_subject': 'Board', 'custom_message': 'Please vote.'}, language='en')
    email_obj = helios_mail.queue(OutgoingEmail.KIND_BULK, voter=voter, batch=batch)
    run_queued_tasks()
    email_obj.refresh_from_db()
    return email_obj


class ProcessBouncesTests(BounceTestCase):

  def test_hard_bounce(self):
    email_obj = self.sent_email()
    self.server.deliver(sample('postfix_user_unknown.eml', email_obj.id))
    output = self.run_command()
    self.assertIn('1 processed', output)
    email_obj.refresh_from_db()
    self.assertEqual((email_obj.status, email_obj.last_error_code), (OutgoingEmail.BOUNCED_HARD, '5.1.1'))
    event = BounceEvent.objects.get()
    self.assertEqual((event.outgoing_email, event.bounce_type, event.status_code),
                     (email_obj, BounceEvent.HARD, '5.1.1'))
    self.assertEqual(event.email_hash, utils.hash_email('voter@example.net'))
    self.assertIn('User unknown', event.diagnostic)
    suppression = EmailSuppression.for_address('Voter@Example.net')
    self.assertEqual((suppression.source, suppression.status_code), (EmailSuppression.SOURCE_HARD_BOUNCE, '5.1.1'))
    # moved out of the inbox; folders created
    self.assertEqual(self.server.contents('INBOX'), [])
    self.assertEqual(len(self.server.contents('processed')), 1)
    self.assertTrue({'processed', 'ignored', 'unmatched', 'error'} <= set(self.server.folders))
    # TLS with certificate checks
    connection = self.server.connections[0]
    self.assertEqual((connection.host, connection.port), ('imap.example.org', 993))
    self.assertEqual(connection.ssl_context.verify_mode, ssl.CERT_REQUIRED)
    self.assertTrue(connection.ssl_context.check_hostname)
    self.assertEqual(connection.logged_in, ('bounce', 'imap-password'))

  def test_every_sample(self):
    delivered = {}
    for name, kind, _status in SAMPLES + [('forged_token.eml', None, ''), ('malformed.eml', None, '')]:
      email_obj = self.sent_email(self.make_voter(self.election, '%s@example.net' % name.split('.')[0]))
      delivered[name] = sample(name, email_obj.id, message_id='<%s@vote.example.org>' % name)
      self.server.deliver(delivered[name])
    self.run_command()
    folders = {name: folder for folder in bounces.FOLDERS for raw in self.server.contents(folder)
               for name in delivered if delivered[name] == raw}
    self.assertEqual(self.server.contents('INBOX'), [])
    self.assertEqual(folders, {
      name: 'unmatched' if name == 'forged_token.eml' else
            'ignored' if kind in (bounces.AUTO_REPLY, bounces.OTHER) else 'processed'
      for name, kind, _status in SAMPLES + [('forged_token.eml', None, ''), ('malformed.eml', None, '')]})
    self.assertEqual(len(self.server.contents('processed')), 14)   # 12 bounces, delay notice, complaint
    self.assertEqual(BounceEvent.objects.filter(bounce_type=BounceEvent.HARD).count(), 8)
    self.assertEqual(BounceEvent.objects.filter(bounce_type=BounceEvent.SOFT).count(), 2)
    self.assertEqual(BounceEvent.objects.filter(bounce_type=BounceEvent.COMPLAINT).count(), 1)
    # the unclear freemail bounce and the malformed mail: unknown (counts as soft)
    self.assertEqual(BounceEvent.objects.filter(bounce_type=BounceEvent.UNKNOWN).count(), 2)
    self.assertEqual(ProcessedBounceMail.objects.count(), 18)

  def test_runs_twice_without_doing_anything_twice(self):
    email_obj = self.sent_email()
    self.server.deliver(sample('gmail_over_quota.eml', email_obj.id))
    self.run_command()
    self.run_command()
    self.assertEqual(BounceEvent.objects.count(), 1)

  def test_failed_move_is_repeated_but_not_the_bounce(self):
    email_obj = self.sent_email()
    self.server.deliver(sample('gmail_over_quota.eml', email_obj.id))
    self.server.fail_moves = 1
    with self.assertRaises(CommandError):
      self.run_command()
    self.assertEqual(len(self.server.contents('INBOX')), 1)   # still there
    self.assertEqual(BounceEvent.objects.count(), 1)           # but recorded
    self.run_command()
    self.assertEqual(BounceEvent.objects.count(), 1)
    self.assertEqual((len(self.server.contents('INBOX')), len(self.server.contents('processed'))), (0, 1))

  def test_same_mail_after_uidvalidity_changed(self):
    email_obj = self.sent_email()
    raw = sample('gmail_over_quota.eml', email_obj.id)
    self.server.deliver(raw)
    self.run_command()
    # the mailbox was rebuilt: new UIDVALIDITY, the mail once more
    self.server.folders['INBOX'] = FakeFolder(5000)
    self.server.deliver(raw)
    self.run_command()
    self.assertEqual(BounceEvent.objects.count(), 1)
    self.assertEqual(len(self.server.contents('processed')), 2)

  def test_read_mails_are_left_alone(self):
    email_obj = self.sent_email()
    self.server.deliver(sample('gmail_over_quota.eml', email_obj.id), flags={'\\Seen'})
    self.run_command()
    self.assertEqual((len(self.server.contents('INBOX')), BounceEvent.objects.count()), (1, 0))

  def test_without_move_capability(self):
    self.server.capabilities = ('IMAP4REV1',)
    email_obj = self.sent_email()
    self.server.deliver(sample('qmail.eml', email_obj.id))
    self.server.deliver(sample('forged_token.eml'))
    self.run_command()
    self.assertEqual(self.server.contents('INBOX'), [])
    self.assertEqual((len(self.server.contents('processed')), len(self.server.contents('unmatched'))), (1, 1))

  def test_bad_mail_does_not_stop_the_run(self):
    first, second = self.sent_email(), self.sent_email()
    self.server.deliver(sample('qmail.eml', first.id))
    self.server.deliver(sample('postfix_user_unknown.eml', second.id))
    real_classify = bounces.classify

    def classify(msg):
      if 'qmail' in str(msg.get('Received', '')):
        raise ValueError('cannot read this')
      return real_classify(msg)
    with mock.patch('helios.bounces.classify', side_effect=classify), self.assertLogs('helios.bounce_mailbox', 'ERROR'):
      self.run_command()
    self.assertEqual((len(self.server.contents('error')), len(self.server.contents('processed'))), (1, 1))
    self.assertEqual(BounceEvent.objects.get().outgoing_email, second)
    first.refresh_from_db()
    self.assertEqual(first.status, OutgoingEmail.SENT)
    # recorded as handled: not tried again
    self.server.folders['INBOX'].messages.update(self.server.folders['error'].messages)
    self.run_command()
    self.assertEqual(BounceEvent.objects.count(), 1)

  def test_too_large(self):
    with mock.patch('helios.bounce_mailbox.MAX_SIZE', 100):
      self.server.deliver(sample('qmail.eml', self.sent_email().id))
      self.run_command()
    self.assertEqual(len(self.server.contents('error')), 1)
    self.assertFalse(BounceEvent.objects.exists())

  def test_retention(self):
    now = datetime.datetime.utcnow()
    self.run_command()   # creates the folders
    old = self.server.deliver(b'Subject: old\n\nx', folder='processed', date=now - datetime.timedelta(days=31))
    self.server.deliver(b'Subject: old\n\nx', folder='unmatched', date=now - datetime.timedelta(days=40))
    recent = self.server.deliver(b'Subject: new\n\nx', folder='processed', date=now - datetime.timedelta(days=29))
    ProcessedBounceMail.objects.create(folder='INBOX', uidvalidity=1, uid=1, result='processed')
    ProcessedBounceMail.objects.filter(uid=1).update(processed_at=now - datetime.timedelta(days=31))
    output = self.run_command()
    self.assertIn('2 old mails deleted', output)
    self.assertEqual(list(self.server.folders['processed'].messages), [recent])
    self.assertNotIn(old, self.server.folders['processed'].messages)
    self.assertEqual(self.server.contents('unmatched'), [])
    self.assertFalse(ProcessedBounceMail.objects.exists())

  def test_bounce_of_deleted_email(self):
    email_obj = self.sent_email()
    self.server.deliver(sample('qmail.eml', email_obj.id))
    email_obj.delete()
    self.run_command()
    self.assertEqual(len(self.server.contents('processed')), 1)
    self.assertFalse(BounceEvent.objects.exists())

  def test_starttls(self):
    with override_settings(HELIOS_BOUNCE_IMAP_STARTTLS=True, HELIOS_BOUNCE_IMAP_PORT=143), \
         mock.patch('helios.bounce_mailbox.imaplib.IMAP4', FakeIMAP):
      self.run_command()
    connection = self.server.connections[0]
    self.assertEqual(connection.port, 143)
    self.assertEqual(connection.started_tls.verify_mode, ssl.CERT_REQUIRED)

  def test_not_configured(self):
    for name in ('HELIOS_BOUNCE_IMAP_HOST', 'HELIOS_BOUNCE_SECRET'):
      with override_settings(**{name: ''}), self.assertRaisesMessage(CommandError, 'not configured'):
        self.run_command()

  def test_mailbox_unreachable(self):
    with mock.patch('helios.bounce_mailbox.imaplib.IMAP4_SSL', side_effect=ConnectionRefusedError(111, 'refused')):
      with self.assertRaisesMessage(CommandError, 'ConnectionRefusedError'):
        self.run_command()


class SuppressionTests(BounceTestCase):

  def bounce(self, name, email_obj):
    # each bounce mail has a Message-ID of its own
    self.count = getattr(self, 'count', 0) + 1
    raw = re.sub(rb'(?m)^Message-I[Dd]: .*$', b'Message-ID: <bounce.%d@mx.example.net>' % self.count,
                 sample(name, email_obj.id), count=1)
    self.server.deliver(raw)
    self.run_command()
    email_obj.refresh_from_db()
    return email_obj

  def test_hard_bounce_suppresses_the_next_email_in_every_election(self):
    first = self.queue_and_send(self.voter)
    self.assertEqual(first.status, OutgoingEmail.SENT)
    self.bounce('postfix_user_unknown.eml', first)
    # the same address in another election, and other kinds of email
    other = self.make_voter(self.make_election('council'), 'VOTER@example.net', login_id='v2')
    email_obj = self.queue_and_send(other)
    self.assertEqual((email_obj.status, email_obj.last_error_code),
                     (OutgoingEmail.SKIPPED_SUPPRESSED, EmailSuppression.SOURCE_HARD_BOUNCE))
    self.assertEqual(len(mail.outbox), 1)
    # another address is not affected
    self.assertEqual(self.queue_and_send(self.make_voter(self.election, 'other@example.net')).status,
                     OutgoingEmail.SENT)

  def test_soft_bounces_count_up_to_the_limit(self):
    emails = [self.sent_email() for _ in range(3)]
    self.bounce('gmail_over_quota.eml', emails[0])
    self.bounce('freemail_unclear.eml', emails[1])   # unknown counts as soft
    self.assertEqual((emails[0].status, emails[1].status), (OutgoingEmail.BOUNCED_SOFT, OutgoingEmail.BOUNCED_SOFT))
    self.assertFalse(EmailSuppression.is_suppressed('voter@example.net'))
    # delays and auto-replies do not count
    self.bounce('postfix_delayed.eml', emails[2])
    self.bounce('ooo_vacation.eml', emails[2])
    self.assertFalse(EmailSuppression.is_suppressed('voter@example.net'))
    self.assertEqual(emails[2].status, OutgoingEmail.SENT)
    self.bounce('postfix_expired.eml', emails[2])
    self.assertEqual(emails[2].status, OutgoingEmail.BOUNCED_HARD)   # treated as hard
    self.assertEqual(EmailSuppression.for_address('voter@example.net').source, EmailSuppression.SOURCE_SOFT_BOUNCES)
    self.assertEqual(self.queue_and_send(self.voter).status, OutgoingEmail.SKIPPED_SUPPRESSED)

  def test_old_soft_bounces_do_not_count(self):
    emails = [self.sent_email() for _ in range(3)]
    self.bounce('gmail_over_quota.eml', emails[0])
    self.bounce('gmail_over_quota.eml', emails[1])
    BounceEvent.objects.update(created_at=datetime.datetime.utcnow() - datetime.timedelta(days=15))
    self.bounce('gmail_over_quota.eml', emails[2])
    self.assertFalse(EmailSuppression.is_suppressed('voter@example.net'))
    self.assertEqual(bounces.soft_bounces_count(utils.hash_email('voter@example.net')), 1)

  def test_soft_bounce_does_not_weaken_a_hard_one(self):
    email_obj = self.bounce('postfix_user_unknown.eml', self.sent_email())
    email_obj = self.bounce('gmail_over_quota.eml', email_obj)
    self.assertEqual(email_obj.status, OutgoingEmail.BOUNCED_HARD)

  def test_lifting(self):
    self.bounce('postfix_user_unknown.eml', self.sent_email())
    self.assertTrue(EmailSuppression.lift('voter@example.net', user=self.admin))
    self.assertFalse(EmailSuppression.lift('voter@example.net'))   # nothing left to lift
    suppression = EmailSuppression.objects.get()
    self.assertEqual(suppression.lifted_by, self.admin)
    self.assertEqual(self.queue_and_send(self.voter).status, OutgoingEmail.SENT)
    # soft bounces before the lift no longer count
    for _ in range(2):
      self.bounce('gmail_over_quota.eml', self.sent_email())
    EmailSuppression.objects.update(lifted_at=datetime.datetime.utcnow())
    self.bounce('gmail_over_quota.eml', self.sent_email())
    self.assertFalse(EmailSuppression.is_suppressed('voter@example.net'))
    # suppressed again
    self.bounce('qmail.eml', self.sent_email())
    suppression.refresh_from_db()
    self.assertEqual((suppression.lifted_at, suppression.lifted_by), (None, None))
    self.assertTrue(EmailSuppression.is_suppressed('voter@example.net'))

  def test_complaint(self):
    email_obj = self.bounce('arf_complaint.eml', self.sent_email())
    self.assertEqual(email_obj.status, OutgoingEmail.SENT)   # it was delivered
    self.assertEqual(EmailSuppression.for_address('voter@example.net').source, EmailSuppression.SOURCE_COMPLAINT)
    self.assertFalse(EmailOptOut.is_opted_out('voter@example.net'))
    with override_settings(HELIOS_BOUNCE_COMPLAINT_OPTOUT=True):
      self.bounce('arf_complaint.eml', self.sent_email())
    self.assertTrue(EmailOptOut.is_opted_out('voter@example.net'))
    # hashed, as every opt-out
    self.assertEqual(EmailOptOut.objects.get().email_hash, utils.hash_email('voter@example.net'))

  def test_no_address_kept(self):
    # opt-out confirmations keep only a masked address: nothing to suppress
    email_obj = OutgoingEmail.objects.create(kind=OutgoingEmail.KIND_OPTOUT_CONFIRM, status=OutgoingEmail.SENT,
                                             recipient='v***@example.net')
    self.bounce('qmail.eml', email_obj)
    self.assertEqual(email_obj.status, OutgoingEmail.BOUNCED_HARD)
    self.assertFalse(EmailSuppression.objects.exists())
    self.assertEqual(BounceEvent.objects.get().email_hash, '')
