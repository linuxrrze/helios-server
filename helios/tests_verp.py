"""
Tests for VERP envelope senders (helios.verp) and the SMTP backend that sends
with them and asks for DSNs (helios.mail_backend.SMTPBackend). The mail server
is a fake smtplib.SMTP that records what it is given.
"""
import email
import uuid
from unittest import mock

from django.conf import settings
from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings

from helios import mail as helios_mail
from helios import verp
from helios.mail_backend import SMTPBackend
from helios.models import Election, OutgoingEmail, Voter
from helios_auth.models import User

BOUNCE_SETTINGS = dict(HELIOS_BOUNCE_DOMAIN='bounces.example.org', HELIOS_BOUNCE_SECRET='test-bounce-secret-0123456789')


class FakeSMTP:
  """smtplib.SMTP as far as Django's backend uses it; ESMTP extensions as given"""
  instances = []
  extensions = ('dsn',)

  def __init__(self, host, port, local_hostname=None, timeout=None, **kwargs):
    self.sent = []
    self.helo_done = False
    FakeSMTP.instances.append(self)

  def ehlo_or_helo_if_needed(self):
    self.helo_done = True

  def has_extn(self, name):
    # as smtplib: only known after EHLO
    return self.helo_done and name.lower() in FakeSMTP.extensions

  def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
    self.ehlo_or_helo_if_needed()
    self.sent.append({'from': from_addr, 'to': to_addrs, 'msg': msg,
                      'mail_options': list(mail_options), 'rcpt_options': list(rcpt_options)})
    return {}

  def quit(self):
    pass

  def close(self):
    pass

  @classmethod
  def all_sent(cls):
    return [s for i in cls.instances for s in i.sent]


@override_settings(**BOUNCE_SETTINGS)
class TokenTests(SimpleTestCase):

  def test_token_and_address(self):
    token = verp.token(4711)
    self.assertRegex(token, r'^4711-[a-z2-7]{16}$')
    self.assertEqual(verp.token(4711), token)   # deterministic
    self.assertNotEqual(verp.token(4712).split('-')[1], token.split('-')[1])
    self.assertEqual(verp.address(token), 'bounce+%s@bounces.example.org' % token)
    # only lower-case letters, digits, '+', '-', '@', '.'
    self.assertRegex(verp.address(token), r'^[a-z0-9+\-@.]+$')

  def test_verification(self):
    token = verp.token(4711)
    self.assertEqual(verp.email_id_from_token(token), 4711)
    # mail servers that change the case of the local part
    self.assertEqual(verp.email_id_from_token(token.upper()), 4711)
    self.assertEqual(verp.email_ids_in('Delivered-To: %s' % verp.address(token).upper()), [4711])
    self.assertEqual(verp.email_ids_in('<%s>' % verp.address(token)), [4711])

  def test_tampered_or_forged_tokens_are_rejected(self):
    token = verp.token(4711)
    mac = token.split('-')[1]
    tampered = mac[:-1] + ('a' if mac[-1] != 'a' else 'b')
    self.assertIsNone(verp.email_id_from_token('4711-%s' % tampered))
    # a valid MAC of another email
    self.assertIsNone(verp.email_id_from_token('4712-%s' % mac))
    self.assertIsNone(verp.email_id_from_token('4711-%s' % mac[:-1]))
    self.assertIsNone(verp.email_id_from_token('4711'))
    self.assertEqual(verp.email_ids_in('bounce+4711-%s@bounces.example.org' % tampered), [])
    # another domain
    self.assertEqual(verp.email_ids_in('bounce+%s@elsewhere.example.net' % token), [])
    with override_settings(HELIOS_BOUNCE_SECRET='another-secret-0123456789'):
      self.assertIsNone(verp.email_id_from_token(token))

  def test_enabled(self):
    with override_settings(EMAIL_BACKEND='helios.mail_backend.LoggingEmailBackend',
                           HELIOS_EMAIL_BACKEND='helios.mail_backend.SMTPBackend'):
      self.assertTrue(verp.enabled())
      with override_settings(HELIOS_BOUNCE_SECRET=''):
        self.assertFalse(verp.enabled())
      with override_settings(HELIOS_BOUNCE_DOMAIN=''):
        self.assertFalse(verp.enabled())
    # other backends (Mailgun, SES, console): no VERP, no bounce handling
    with override_settings(EMAIL_BACKEND='helios.mail_backend.LoggingEmailBackend',
                           HELIOS_EMAIL_BACKEND='anymail.backends.mailgun.EmailBackend'):
      self.assertFalse(verp.enabled())
    with override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
      self.assertFalse(verp.enabled())


@mock.patch('django.core.mail.backends.smtp.smtplib.SMTP', FakeSMTP)
class SMTPBackendTests(SimpleTestCase):

  def setUp(self):
    FakeSMTP.instances = []
    FakeSMTP.extensions = ('dsn',)

  def message(self, **attributes):
    message = mail.EmailMessage('Election XY', 'Your password: s3cret', '"Helios" <helios@example.org>',
                                ['Erika Mustermann <erika@example.net>'])
    for name, value in attributes.items():
      setattr(message, name, value)
    return message

  def test_envelope_sender_and_dsn(self):
    message = self.message(envelope_from='bounce+7-abcdefghijklmnop@bounces.example.org',
                           envelope_id='7-abcdefghijklmnop')
    self.assertEqual(SMTPBackend().send_messages([message]), 1)
    sent, = FakeSMTP.all_sent()
    self.assertEqual(sent['from'], 'bounce+7-abcdefghijklmnop@bounces.example.org')
    self.assertEqual(sent['to'], ['Erika Mustermann <erika@example.net>'])
    self.assertEqual(sent['mail_options'], ['RET=HDRS', 'ENVID=7-abcdefghijklmnop'])
    self.assertEqual(sent['rcpt_options'], ['NOTIFY=FAILURE,DELAY'])
    # the header From is unchanged
    parsed = email.message_from_bytes(sent['msg'])
    self.assertEqual(parsed['From'], '"Helios" <helios@example.org>')
    self.assertNotIn('bounce+', parsed.as_string())

  def test_no_dsn_options_without_dsn_extension(self):
    FakeSMTP.extensions = ('8bitmime',)
    SMTPBackend().send_messages([self.message(envelope_from='bounce+7-abcdefghijklmnop@bounces.example.org')])
    sent, = FakeSMTP.all_sent()
    self.assertEqual(sent['from'], 'bounce+7-abcdefghijklmnop@bounces.example.org')
    self.assertEqual((sent['mail_options'], sent['rcpt_options']), ([], []))

  def test_without_envelope_from_as_django(self):
    SMTPBackend().send_messages([self.message()])
    sent, = FakeSMTP.all_sent()
    self.assertEqual(sent['from'], 'Helios <helios@example.org>')
    self.assertEqual((sent['mail_options'], sent['rcpt_options']), ([], []))

  def test_unsafe_envelope_id_is_left_out(self):
    SMTPBackend().send_messages([self.message(envelope_from='bounce+7-abcdefghijklmnop@bounces.example.org',
                                              envelope_id='a b+c')])
    self.assertEqual(FakeSMTP.all_sent()[0]['mail_options'], ['RET=HDRS'])


@override_settings(EMAIL_BACKEND='helios.mail_backend.LoggingEmailBackend',
                   HELIOS_EMAIL_BACKEND='helios.mail_backend.SMTPBackend', **BOUNCE_SETTINGS)
@mock.patch('django.core.mail.backends.smtp.smtplib.SMTP', FakeSMTP)
class VERPSendingTests(TestCase):
  """helios.mail sends with the VERP envelope sender and keeps the token"""

  def setUp(self):
    FakeSMTP.instances = []
    FakeSMTP.extensions = ('dsn',)
    admin = User.objects.create(user_type='password', user_id='admin@example.org', name='Ada Admin',
                                info={'password': 'x'})
    self.election = Election.objects.create(admin=admin, uuid=str(uuid.uuid4()), short_name='board',
                                            name='Board election', election_type='election')
    self.voter = Voter.objects.create(uuid=str(uuid.uuid4()), election=self.election, voter_login_id='v1',
                                      voter_email='voter1@example.net', voter_name='Voter One',
                                      voter_password='pw')
    self.batch = helios_mail.create_batch(self.election, 'email/simple_subject.txt', 'email/simple_body.txt',
                                          {'custom_subject': 'Board', 'custom_message': 'Please vote.'},
                                          language='en')

  def test_bulk_email_goes_out_with_verp(self):
    from helios.tests import run_queued_tasks
    email_obj = helios_mail.queue(OutgoingEmail.KIND_BULK, voter=self.voter, batch=self.batch)
    run_queued_tasks()
    email_obj.refresh_from_db()
    self.assertEqual(email_obj.status, OutgoingEmail.SENT)
    self.assertEqual(email_obj.bounce_token, verp.token(email_obj.id))
    sent, = FakeSMTP.all_sent()
    self.assertEqual(sent['from'], 'bounce+%s@bounces.example.org' % email_obj.bounce_token)
    self.assertEqual(sent['mail_options'], ['RET=HDRS', 'ENVID=%s' % email_obj.bounce_token])
    parsed = email.message_from_bytes(sent['msg'])
    self.assertEqual(parsed['From'], settings.SERVER_EMAIL)

  def test_off_without_secret(self):
    from helios.tests import run_queued_tasks
    with override_settings(HELIOS_BOUNCE_SECRET=''):
      email_obj = helios_mail.queue(OutgoingEmail.KIND_BULK, voter=self.voter, batch=self.batch)
      run_queued_tasks()
    email_obj.refresh_from_db()
    self.assertEqual((email_obj.status, email_obj.bounce_token), (OutgoingEmail.SENT, ''))
    sent, = FakeSMTP.all_sent()
    self.assertNotIn('bounce', sent['from'])
    self.assertEqual(sent['mail_options'], [])
