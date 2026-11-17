"""
Email backend that logs one line per email and hands it on to the real backend
(SMTP, console, Mailgun or SES; see HELIOS_EMAIL_BACKEND in settings.py).

  email sent: to=e***@example.org subject="Election XY - vote cast"
  email NOT sent: to=e***@example.org subject="..." error=SMTPRecipientsRefused: ...

Recipient addresses are masked (first character of the local part and the
domain), so the log shows where mail went without listing every voter's
address. "sent" means the mail server accepted the email; delivery after that
is up to the mail server.

Emails sent for an election (inside organizations.mail_context) get the sender
name and Reply-To of the election's organization here, in one place for all auth
systems and background tasks.

SMTPBackend, the default real backend, is Django's SMTP backend with the
envelope sender of helios.verp (bounce+<id>-<mac>@<HELIOS_BOUNCE_DOMAIN>) and
DSN options, for emails that carry an envelope_from (see helios.mail._send).
"""
import logging
import re
import smtplib

from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.smtp import EmailBackend as DjangoSMTPBackend
from django.core.mail.message import sanitize_address

from helios import organizations

logger = logging.getLogger('helios.mail')


def mask_address(address):
  """'Erika Mustermann <erika@example.org>' -> 'e***@example.org'"""
  from email.utils import parseaddr
  _name, addr = parseaddr(str(address))
  local, at, domain = addr.rpartition('@')
  if not at:
    return '***'
  return '%s***@%s' % (local[:1], domain)


def _describe(message):
  recipients = ','.join(mask_address(r) for r in message.recipients()) or '-'
  subject = (message.subject or '').replace('"', "'")
  return 'to=%s subject="%s"' % (recipients, subject)


class LoggingEmailBackend(BaseEmailBackend):

  def __init__(self, fail_silently=False, **kwargs):
    super().__init__(fail_silently=fail_silently)
    self.inner = get_connection(settings.HELIOS_EMAIL_BACKEND, fail_silently=fail_silently, **kwargs)

  def open(self):
    return self.inner.open()

  def close(self):
    return self.inner.close()

  def send_messages(self, email_messages):
    if not email_messages:
      return 0
    sent = 0
    # one at a time, so that the log says which email failed
    for message in email_messages:
      message = organizations.apply_mail_identity(message)
      try:
        count = self.inner.send_messages([message])
      except Exception as e:
        logger.error('email NOT sent: %s error=%s: %s', _describe(message), type(e).__name__, e)
        raise
      if count:
        sent += count
        logger.info('email sent: %s', _describe(message))
      else:
        # the inner backend swallowed an error (fail_silently=True)
        logger.warning('email NOT sent: %s (error suppressed by the caller, see fail_silently)', _describe(message))
    return sent


# an ENVID is xtext (RFC 3461); ours is the VERP token, letters, digits and '-'
_ENVID = re.compile(r'[A-Za-z0-9-]{1,100}')


class SMTPBackend(DjangoSMTPBackend):
  """
  Django's SMTP backend; an email with an `envelope_from` attribute is sent with
  that envelope sender (MAIL FROM) instead of its From address, which stays in
  the header. If the mail server offers DSN (RFC 3461), such an email asks for
  failure and delay notices with only the headers returned (RET=HDRS: never the
  body, which may contain the voter's password) and gives `envelope_id` as ENVID.
  Emails without envelope_from go out exactly as with Django's backend.
  """

  def dsn_options(self, email_message):
    """(mail_options, rcpt_options) for an email with a VERP envelope sender"""
    self.connection.ehlo_or_helo_if_needed()
    if not self.connection.has_extn('dsn'):
      return [], []
    mail_options = ['RET=HDRS']
    envelope_id = getattr(email_message, 'envelope_id', '') or ''
    if _ENVID.fullmatch(envelope_id):
      mail_options.append('ENVID=%s' % envelope_id)
    return mail_options, ['NOTIFY=FAILURE,DELAY']

  def _send(self, email_message):
    envelope_from = getattr(email_message, 'envelope_from', None)
    if not envelope_from:
      return super()._send(email_message)
    if not email_message.recipients():
      return False
    encoding = email_message.encoding or settings.DEFAULT_CHARSET
    envelope_from = sanitize_address(envelope_from, encoding)
    recipients = [sanitize_address(addr, encoding) for addr in email_message.recipients()]
    message = email_message.message()
    try:
      mail_options, rcpt_options = self.dsn_options(email_message)
      self.connection.sendmail(envelope_from, recipients, message.as_bytes(linesep='\r\n'),
                               mail_options=mail_options, rcpt_options=rcpt_options)
    except smtplib.SMTPException:
      if not self.fail_silently:
        raise
      return False
    return True
