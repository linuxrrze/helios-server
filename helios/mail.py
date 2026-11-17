"""
Sending emails to voters and election administrators, in one place.

Every such email gets an OutgoingEmail row that records what became of it
(sent, failed, not sent because the voter opted out, ...); the emails of a bulk
mailing also belong to an EmailBatch. Subject and body are not stored: they are
rendered when the email is sent, as some contain the voter's password.

The auth systems only say where an email to one of their users goes, with
get_email_address(user_id, name, user_info) -> '"Name" <address>' or None. An
auth system without it still sends with its own send_message (whose result
cannot be checked: the email counts as sent unless it raises).

  email = mail.new_email(OutgoingEmail.KIND_VOTE_CAST, election=election, voter=voter, user=user)
  if not mail.skip_opted_out(email, voter):
    mail.deliver(email, user, subject, body)

The email itself goes through helios.mail_backend.LoggingEmailBackend like any
other (log line with a masked address, sender name and Reply-To of the election's
organization inside organizations.mail_context).
"""
import logging
import smtplib
from email.utils import make_msgid

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.mail.utils import DNS_NAME
from django.utils import timezone

from helios_auth.auth_systems import AUTH_SYSTEMS
from .models import EmailBatch, EmailOptOut, OutgoingEmail

logger = logging.getLogger(__name__)


def voter_email_address(voter):
  """the bare address of the voter (to check opt-outs with), or None"""
  return voter.voter_email or (voter.user and voter.user.user_id)


def voter_opted_out(voter):
  """True if the voter's address has opted out of all Helios emails."""
  address = voter_email_address(voter)
  return bool(address) and EmailOptOut.is_opted_out(address)


def create_batch(election, subject_template, body_template, extra_vars, language='', triggered_by=None):
  """a new bulk mailing; its emails are added by helios.tasks.voters_email"""
  return EmailBatch.objects.create(
    election=election, subject_template=subject_template, body_template=body_template,
    extra_vars=extra_vars or {}, language=language or '',
    triggered_by=triggered_by if triggered_by and triggered_by.pk else None)


def new_email(kind, election=None, voter=None, user=None, batch=None):
  """a new (queued) email, to the voter or else the user"""
  if election is None and voter is not None:
    election = voter.election
  if user is None and voter is not None:
    user = voter.user
  return OutgoingEmail.objects.create(
    kind=kind, election=election, voter=voter, batch=batch,
    # voters without an account get an unsaved stub user (Voter.get_user)
    user=user if user is not None and user.pk else None)


def skip_opted_out(email, voter):
  """
  True (and the email marked as not sent) if the voter has opted out. The
  address is not stored then: only its hash is kept of an opted-out address.
  """
  if not voter_opted_out(voter):
    return False
  logger.info("Skipping email to opted-out voter %s", voter.uuid)
  _record(email, OutgoingEmail.SKIPPED_OPTOUT)
  return True


def send_now(kind, user, subject, body, election=None, voter=None):
  """a new email to USER (or the voter), sent right away; raises like deliver()"""
  email = new_email(kind, election=election, voter=voter, user=user)
  deliver(email, user if user is not None else voter.get_user(), subject, body)
  return email


def deliver(email, user, subject, body):
  """
  Send EMAIL (an OutgoingEmail) to USER and record the result. A failure of the
  mail server is recorded and raised, as before there were OutgoingEmails.
  No address (e.g. a GitHub voter who never logged in) is recorded, not raised.

  The email is marked "sending" before it goes to the mail server and "sent"
  right after: if the worker dies in between, the task runs again and sends a
  duplicate rather than losing the email.
  """
  # subject up until the first newline (as helios_auth.models.User.send_message)
  subject = subject.split("\n")[0]

  email.status = OutgoingEmail.SENDING
  email.attempts += 1
  email.save(update_fields=['status', 'attempts'])

  auth_system = AUTH_SYSTEMS.get(user.user_type)
  if auth_system is None:
    logger.warning("email NOT sent: unknown auth system %s", user.user_type)
    _record(email, OutgoingEmail.SKIPPED_NOADDRESS, error_text='unknown auth system %s' % user.user_type)
    return

  if not hasattr(auth_system, 'get_email_address'):
    # the auth system sends its emails itself
    try:
      auth_system.send_message(user.user_id, user.name, user.info, subject, body)
    except Exception as e:
      _record_failure(email, e)
      raise
    _record(email, OutgoingEmail.SENT, sent_at=timezone.now())
    return

  recipient = auth_system.get_email_address(user.user_id, user.name, user.info)
  if not recipient:
    logger.warning("email NOT sent: %s user without an email address", user.user_type)
    _record(email, OutgoingEmail.SKIPPED_NOADDRESS, error_text='no email address on file')
    return

  message_id = make_msgid(domain=DNS_NAME)
  # as django.core.mail.send_mail, which the auth systems used
  message = EmailMultiAlternatives(subject, body, settings.SERVER_EMAIL, [recipient], headers={'Message-ID': message_id})
  email.recipient = recipient
  try:
    message.send(fail_silently=False)
  except Exception as e:
    _record_failure(email, e)
    raise
  _record(email, OutgoingEmail.SENT, sent_at=timezone.now(), message_id=message_id)


def _record(email, status, sent_at=None, message_id='', error_code='', error_text=''):
  email.status = status
  email.sent_at = sent_at
  email.message_id = message_id
  if error_code or error_text:
    email.last_error_code = error_code[:32]
    email.last_error_text = error_text
  email.save(update_fields=['status', 'sent_at', 'message_id', 'recipient', 'last_error_code', 'last_error_text'])


def _record_failure(email, exception):
  _record(email, OutgoingEmail.FAILED, error_code=error_code(exception),
          error_text=('%s: %s' % (type(exception).__name__, exception))[:2000])


def error_code(exception):
  """the SMTP reply code of a mail server error ('550'), else the name of the exception"""
  if isinstance(exception, smtplib.SMTPResponseException):
    return str(exception.smtp_code)
  if isinstance(exception, smtplib.SMTPRecipientsRefused) and exception.recipients:
    code, _message = next(iter(exception.recipients.values()))
    return str(code)
  return type(exception).__name__
