"""
Tests for helios.mail_backend: one log line per email, recipients masked.
"""
import smtplib

from django.core import mail
from django.core.mail.backends.base import BaseEmailBackend
from django.test import TestCase, override_settings

from helios.mail_backend import mask_address

LOGGING_BACKEND = 'helios.mail_backend.LoggingEmailBackend'


class FailingBackend(BaseEmailBackend):
  def send_messages(self, email_messages):
    if self.fail_silently:
      return 0
    raise smtplib.SMTPRecipientsRefused({'erika@example.org': (550, b'no such user')})


@override_settings(EMAIL_BACKEND=LOGGING_BACKEND, HELIOS_EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class MailLogTests(TestCase):

  def test_mask_address(self):
    self.assertEqual(mask_address('Erika Mustermann <erika@example.org>'), 'e***@example.org')
    self.assertEqual(mask_address('erika@example.org'), 'e***@example.org')
    self.assertEqual(mask_address('not an address'), '***')

  def test_sent_mail_is_logged_and_delivered(self):
    with self.assertLogs('helios.mail', level='INFO') as logs:
      count = mail.send_mail('Election XY - vote cast', 'body', 'helios@example.org',
                             ['Erika Mustermann <erika@example.org>'])
    self.assertEqual(count, 1)
    self.assertEqual(len(mail.outbox), 1)   # handed on to the real backend
    self.assertEqual(logs.output, ['INFO:helios.mail:email sent: to=e***@example.org subject="Election XY - vote cast"'])
    self.assertNotIn('erika@', logs.output[0])

  def test_one_line_per_mail(self):
    messages = [mail.EmailMessage('s%d' % i, 'b', 'helios@example.org', ['v%d@example.org' % i]) for i in range(3)]
    with self.assertLogs('helios.mail', level='INFO') as logs:
      self.assertEqual(mail.get_connection().send_messages(messages), 3)
    self.assertEqual(len(logs.output), 3)

  @override_settings(HELIOS_EMAIL_BACKEND='helios.tests_mail.FailingBackend')
  def test_failure_is_logged_and_raised(self):
    with self.assertLogs('helios.mail', level='ERROR') as logs:
      with self.assertRaises(smtplib.SMTPRecipientsRefused):
        mail.send_mail('subject', 'body', 'helios@example.org', ['erika@example.org'])
    self.assertIn('email NOT sent: to=e***@example.org subject="subject" error=SMTPRecipientsRefused', logs.output[0])

  @override_settings(HELIOS_EMAIL_BACKEND='helios.tests_mail.FailingBackend')
  def test_silenced_failure_is_still_logged(self):
    # helios.utils.send_email uses fail_silently=True; the failure must not vanish
    with self.assertLogs('helios.mail', level='WARNING') as logs:
      self.assertEqual(mail.send_mail('subject', 'body', 'helios@example.org', ['erika@example.org'], fail_silently=True), 0)
    self.assertIn('email NOT sent: to=e***@example.org', logs.output[0])
