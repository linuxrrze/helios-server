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
"""
import logging

from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend

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
