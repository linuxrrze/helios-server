"""
Sending emails, in one place: to voters, election administrators and trustees,
opt-out/opt-in confirmations and account password reminders.

Every email is an OutgoingEmail row, sent by its own background task
(helios.tasks.send_outgoing_email), which records what became of it: sent,
deferred to try again, failed, or not sent (opted out, no address, no longer
applicable). The emails of a bulk mailing also belong to an EmailBatch. Subject
and body are rendered when the email is sent (helios.mail_messages), as some
contain a password or a secret link.

  mail.queue(OutgoingEmail.KIND_VOTE_CAST, election=election, voter=voter,
             params={'cast_vote_id': cast_vote.id})

Right before sending, the task checks again whether the email is still to go
out (the voter opted out or was removed, the election was deleted, voting closed
for an invitation to vote, ...). Errors of the mail server are judged by
helios.mail_errors: a permanent one fails the email, a temporary one defers it
and the task queues itself again for later (the n-th retry after
min(HELIOS_EMAIL_RETRY_BASE * 2^n, HELIOS_EMAIL_RETRY_CAP) * random(0.5, 1.5)
seconds), until HELIOS_EMAIL_MAX_ATTEMPTS or HELIOS_EMAIL_MAX_AGE (not counting the
time sending was paused, EmailPausePeriod). When the mail
server cannot be reached or refuses our login, sending is paused for all emails
(EmailSendingPause); the first email after the pause tests the server again.

The email is marked "sending" before it goes to the mail server and "sent" right
after: if the worker dies in between, it is sent again (after SENDING_LEASE)
rather than lost.

The auth systems only say where an email to one of their users goes, with
get_email_address(user_id, name, user_info) -> '"Name" <address>' or None. An
auth system without it still sends with its own send_message (whose result
cannot be checked: the email counts as sent unless it raises).

The email itself goes through helios.mail_backend.LoggingEmailBackend like any
other (log line with a masked address, sender name and Reply-To of the election's
organization inside organizations.mail_context).
"""
import contextlib
import datetime
import logging
import random
from email.utils import make_msgid
from typing import Callable, NamedTuple, Optional

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, mail_admins
from django.core.mail.utils import DNS_NAME
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone, translation

from helios_auth.auth_systems import AUTH_SYSTEMS
from . import mail_errors
from . import mail_messages
from . import organizations
from .mail_backend import mask_address
from .models import EmailBatch, EmailOptOut, EmailPausePeriod, EmailSendingPause, OutgoingEmail

logger = logging.getLogger(__name__)

# an email still "sending" after this has died with its worker, and is sent again
SENDING_LEASE = datetime.timedelta(minutes=15)
# a task that finds its email deferred to later than this is not the current one
STALE_AFTER = datetime.timedelta(minutes=1)
# configuration errors wait as long as after this many more failed attempts (8 * base)
CONFIG_EXTRA_STEPS = 3
# single emails go ahead of bulk mailings (cast-vote verification: 10)
PRIORITY = 5
BULK_PRIORITY = 0

# not sent to voters who opted out. Not so the notice that an administrator removed
# a voter's vote (KIND_VOTE_REMOVED): it is always sent.
OPTOUT_KINDS = (OutgoingEmail.KIND_BULK, OutgoingEmail.KIND_VOTER_PASSWORD, OutgoingEmail.KIND_VOTE_CAST)
# not sent once their voter is removed
VOTER_KINDS = OPTOUT_KINDS
# asking voters to vote: not sent once voting has closed
VOTING_TEMPLATES = ('email/vote_body.txt', 'email/password_resend_body.txt')

voter_email_address = mail_messages.voter_email_address


def voter_opted_out(voter):
  """True if the voter's address has opted out of all Helios emails."""
  address = voter_email_address(voter)
  return bool(address) and EmailOptOut.is_opted_out(address)


def suppressed(email, recipient):
  """
  True if the address is not to be mailed (hard bounces, complaints). A hook for
  the suppression list, which does not exist yet.
  """
  return False


##
## queueing
##

def create_batch(election, subject_template, body_template, extra_vars, language='', triggered_by=None,
                 voter_constraints=None):
  """
  a new bulk mailing; its emails are added by helios.tasks.voters_email.
  VOTER_CONSTRAINTS (include, exclude): the voters it is for, checked again before
  each email is sent (None: not checked, e.g. a mailing to one voter)
  """
  if voter_constraints is not None:
    include, exclude = voter_constraints
    voter_constraints = {'include': include or None, 'exclude': exclude or None}
  return EmailBatch.objects.create(
    election=election, subject_template=subject_template, body_template=body_template,
    extra_vars=extra_vars or {}, language=language or '', voter_constraints=voter_constraints,
    triggered_by=triggered_by if triggered_by and triggered_by.pk else None)


def new_email(kind, election=None, voter=None, user=None, batch=None, trustee=None, recipient='', params=None,
              language=None):
  """a new (queued) email, to the voter, else the user, trustee or recipient; not sent yet (see queue)"""
  if election is None and voter is not None:
    election = voter.election
  if election is None and trustee is not None:
    election = trustee.election
  if user is None and voter is not None:
    user = voter.user
  if language is None:
    # of a bulk mailing: its batch's
    language = '' if batch is not None else (translation.get_language() or '')
  return OutgoingEmail.objects.create(
    kind=kind, election=election, voter=voter, batch=batch, trustee=trustee,
    # voters without an account get an unsaved stub user (Voter.get_user)
    user=user if user is not None and user.pk else None,
    recipient=recipient or '', params=params or {}, language=language)


def enqueue(email_id, kind, run_after=None):
  """the task sending the email (again), at RUN_AFTER or now"""
  from .tasks import send_outgoing_email
  options = {'priority': BULK_PRIORITY if kind == OutgoingEmail.KIND_BULK else PRIORITY}
  if run_after is not None:
    options['run_after'] = run_after
  send_outgoing_email.using(**options).enqueue(email_id)


def queue(kind, **kwargs):
  """
  A new email (arguments as new_email), sent in the background with retries.
  Its task is queued in the same transaction (the queue is in the database).
  """
  with transaction.atomic():
    email = new_email(kind, **kwargs)
    enqueue(email.id, kind)
  return email


def recipient_of(user):
  """the address of USER as their auth system gives it, or '' (unknown, or the auth system sends itself)"""
  auth_system = AUTH_SYSTEMS.get(user.user_type)
  if auth_system is None or not hasattr(auth_system, 'get_email_address'):
    return ''
  return auth_system.get_email_address(user.user_id, user.name, user.info) or ''


##
## backoff and pause
##

def retry_delay(steps):
  """seconds to wait before retry number STEPS (0: the first): min(base * 2^steps, cap) * random(0.5, 1.5)"""
  base, cap = settings.HELIOS_EMAIL_RETRY_BASE, settings.HELIOS_EMAIL_RETRY_CAP
  return min(base * 2 ** min(max(steps, 0), 32), cap) * random.uniform(0.5, 1.5)


def _pause(error, now):
  """pause sending after ERROR (SYSTEM or CONFIG); True if this begins a run of failures"""
  if not EmailSendingPause.objects.filter(pk=1).exists():
    try:
      with transaction.atomic():   # another worker may create it at the same time
        EmailSendingPause.objects.create(pk=1)
    except IntegrityError:
      pass
  pause = EmailSendingPause.objects.select_for_update().get(pk=1)
  if pause.paused_until and pause.paused_until > now:
    # another worker ran into it at the same time
    return False
  begins = not pause.failures
  pause.failures += 1
  steps = pause.failures - 1 + (CONFIG_EXTRA_STEPS if error.category == mail_errors.CONFIG else 0)
  pause.paused_until = now + datetime.timedelta(seconds=retry_delay(steps))
  if begins:
    pause.since = now
    if not EmailPausePeriod.objects.filter(ended_at__isnull=True).exists():
      EmailPausePeriod.objects.create(started_at=now)
  pause.error_code, pause.error_text = error.code[:32], error.text
  pause.save()
  logger.warning("email sending paused until %s: %s", pause.paused_until, error.text)
  return begins


def _close_pause_periods(now):
  EmailPausePeriod.objects.filter(ended_at__isnull=True).update(ended_at=now)


def _end_pause():
  if EmailSendingPause.objects.filter(pk=1, failures__gt=0).update(failures=0, paused_until=None, since=None):
    _close_pause_periods(timezone.now())
    logger.info("email sending resumed: the mail server took an email again")


def resume_sending():
  """end a pause now (e.g. after fixing the configuration); emails deferred by it are queued again"""
  now = timezone.now()
  EmailSendingPause.objects.filter(pk=1).update(failures=0, paused_until=None, since=None)
  _close_pause_periods(now)
  count = 0
  for email_id, kind in OutgoingEmail.objects.filter(status=OutgoingEmail.DEFERRED).values_list('id', 'kind'):
    with transaction.atomic():
      if OutgoingEmail.objects.filter(id=email_id, status=OutgoingEmail.DEFERRED).update(next_attempt_at=now):
        enqueue(email_id, kind)
        count += 1
  return count


def paused_seconds(since, until):
  """seconds between SINCE and UNTIL during which sending was paused (a pause still going on counts until UNTIL)"""
  periods = EmailPausePeriod.objects.filter(started_at__lt=until).filter(
    Q(ended_at__isnull=True) | Q(ended_at__gt=since))
  total = 0.0
  for started_at, ended_at in periods.values_list('started_at', 'ended_at'):
    total += max((min(ended_at or until, until) - max(started_at, since)).total_seconds(), 0)
  return total


def max_age_deadline(email, now):
  """
  when EMAIL counts as too old to be sent: HELIOS_EMAIL_MAX_AGE after it was
  queued, plus the time sending was paused since then (an outage of the mail
  server does not use up the time of the emails waiting for it)
  """
  return email.created_at + datetime.timedelta(
    seconds=settings.HELIOS_EMAIL_MAX_AGE + paused_seconds(email.created_at, now))


def _report_pause(error):
  """the mail server refuses us: tell the site administrators (as far as that works) and Rollbar"""
  if error.category == mail_errors.CONFIG:
    problem = "refuses the login or sender address of Helios (check EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, DEFAULT_FROM_EMAIL)"
  else:
    problem = "cannot be reached (check EMAIL_HOST, EMAIL_PORT and the mail server)"
  message = ("Helios has paused sending emails: the mail server %s.\n\n%s\n\n"
             "Emails are deferred and tried again automatically. After fixing the problem, "
             "`manage.py resume_email` sends them right away." % (problem, error.text))
  logger.error(message)
  if error.category != mail_errors.CONFIG:
    return
  mail_admins("Emails paused: mail server refuses Helios", message, fail_silently=True)
  rollbar_settings = getattr(settings, 'ROLLBAR', None)
  if rollbar_settings:
    try:
      import rollbar
      if not rollbar._initialized:
        rollbar.init(**rollbar_settings)
      rollbar.report_message(message, 'error')
    except Exception:
      logger.exception("could not report to Rollbar")


##
## sending (the task send_outgoing_email)
##

class _Message(NamedTuple):
  subject: str
  body: str
  recipient: str
  # the auth system sends it itself (no get_email_address)
  send_with: Optional[Callable] = None


class _NoAddress(Exception):
  pass


def _context(email):
  """the language of the email, and the mail context of its election (sender name, Reply-To, signature)"""
  stack = contextlib.ExitStack()
  language = email.language or (email.batch.language if email.batch_id else '') or settings.LANGUAGE_CODE
  stack.enter_context(translation.override(language))
  if email.election_id:
    stack.enter_context(organizations.mail_context(email.election))
  return stack


def _obsolete_reason(email):
  """why the email no longer makes sense (for last_error_code), or None"""
  election = email.election
  if election is not None and election.deleted_at:
    return 'election_deleted'
  if email.kind in VOTER_KINDS and email.voter_id is None:
    return 'voter_removed'
  if email.kind == OutgoingEmail.KIND_TRUSTEE and email.trustee_id is None:
    return 'trustee_removed'
  if email.kind in (OutgoingEmail.KIND_ADMIN_INFO, OutgoingEmail.KIND_PASSWORD_REMINDER) and email.user_id is None \
     and not email.recipient:
    return 'user_removed'
  if email.kind in (OutgoingEmail.KIND_BULK, OutgoingEmail.KIND_VOTER_PASSWORD) and election is not None:
    if email.kind == OutgoingEmail.KIND_BULK and not election.can_send_voter_emails()[0]:
      return 'voter_email_cutoff'
    if mail_messages.voter_templates(email)[1] in VOTING_TEMPLATES and election.voting_has_stopped():
      return 'voting_closed'
    # e.g. a reminder to those who have not voted, to a voter who has voted since
    if email.batch_id and not email.batch.selects(email.voter):
      return 'no_longer_selected'
  return None


def _destination(email, recipient):
  """(recipient, send_with) of the email; raises _NoAddress"""
  if recipient:
    return recipient, None
  if email.kind in VOTER_KINDS:
    # the voter's current account, or a stub password user for voters without one
    user = email.voter.get_user()
  else:
    user = email.user
  if user is None:
    raise _NoAddress('no recipient')
  auth_system = AUTH_SYSTEMS.get(user.user_type)
  if auth_system is None:
    raise _NoAddress('unknown auth system %s' % user.user_type)
  if not hasattr(auth_system, 'get_email_address'):
    def send_with(subject, body):
      auth_system.send_message(user.user_id, user.name, user.info, subject, body)
    return '', send_with
  recipient = auth_system.get_email_address(user.user_id, user.name, user.info)
  if not recipient:
    raise _NoAddress('no email address on file')
  return recipient, None


def _finish(email, status, sent_at=None, message_id='', error_code=None, error_text=None):
  """the email is final: no longer sent, its params (only needed to send it) dropped"""
  email.status = status
  email.sent_at = sent_at
  email.message_id = message_id
  email.next_attempt_at = None
  if error_code is not None:
    email.last_error_code = error_code[:32]
  if error_text is not None:
    email.last_error_text = error_text
  email.params = {}
  if status == OutgoingEmail.SKIPPED_OPTOUT:
    # the address of an opted-out voter is not stored
    email.recipient = ''
  elif email.kind in OutgoingEmail.KINDS_ADDRESS_NOT_KEPT and email.recipient:
    email.recipient = mask_address(email.recipient)
  email.save(update_fields=['status', 'sent_at', 'message_id', 'next_attempt_at', 'last_error_code',
                            'last_error_text', 'params', 'recipient'])


def _defer(email, next_attempt_at):
  email.status = OutgoingEmail.DEFERRED
  email.next_attempt_at = next_attempt_at
  email.save(update_fields=['status', 'next_attempt_at', 'last_error_code', 'last_error_text'])
  enqueue(email.id, email.kind, run_after=next_attempt_at)


def _claim(email_id, now):
  """
  Lock the email, check that it is still to be sent and render it; mark it
  "sending" (committed when this returns). Returns (email, message, pause_seen)
  or None if there is nothing to send now.
  """
  with transaction.atomic():
    email = OutgoingEmail.objects.select_for_update().filter(id=email_id).first()
    if email is None:
      logger.info("email %s no longer exists (its election was deleted)", email_id)
      return None
    if email.status not in OutgoingEmail.SENDABLE:
      # already sent (or not to be sent): this task ran before
      logger.info("email %s already %s", email.id, email.status)
      return None
    if email.next_attempt_at and email.next_attempt_at > now + STALE_AFTER:
      if email.status == OutgoingEmail.SENDING:
        # another worker is sending it; should it have died, send it after the lease
        enqueue(email.id, email.kind, run_after=email.next_attempt_at)
      # else a later task is queued for it
      return None

    reason = _obsolete_reason(email)
    if reason:
      logger.info("email %s (%s) not sent: %s", email.id, email.kind, reason)
      _finish(email, OutgoingEmail.SKIPPED_OBSOLETE, error_code=reason, error_text='')
      return None
    if email.kind in OPTOUT_KINDS and voter_opted_out(email.voter):
      logger.info("Skipping email to opted-out voter %s", email.voter.uuid)
      _finish(email, OutgoingEmail.SKIPPED_OPTOUT)
      return None
    if email.status != OutgoingEmail.QUEUED and now >= max_age_deadline(email, now):
      _finish(email, OutgoingEmail.FAILED, error_code=email.last_error_code or 'max_age',
              error_text='given up after %d attempts (too old): %s' % (email.attempts, email.last_error_text))
      return None

    pause = EmailSendingPause.objects.filter(pk=1).first()
    if pause and pause.paused_until and pause.paused_until > now:
      # spread out, so that not all of them test the mail server at once
      _defer(email, pause.paused_until + datetime.timedelta(seconds=random.uniform(0, settings.HELIOS_EMAIL_RETRY_BASE)))
      return None

    try:
      with _context(email):
        subject, body, recipient = mail_messages.compose(email)
        recipient, send_with = _destination(email, recipient)
    except mail_messages.Obsolete as e:
      _finish(email, OutgoingEmail.SKIPPED_OBSOLETE, error_code=e.args[0], error_text='')
      return None
    except _NoAddress as e:
      logger.warning("email %s NOT sent: %s", email.id, e)
      _finish(email, OutgoingEmail.SKIPPED_NOADDRESS, error_text=str(e))
      return None
    except Exception as e:
      logger.exception("email %s (%s) could not be rendered", email.id, email.kind)
      _finish(email, OutgoingEmail.FAILED, error_code='render_error', error_text='%s: %s' % (type(e).__name__, e))
      return None

    if suppressed(email, recipient):
      _finish(email, OutgoingEmail.SKIPPED_SUPPRESSED)
      return None

    email.status = OutgoingEmail.SENDING
    email.attempts += 1
    email.next_attempt_at = now + SENDING_LEASE
    email.recipient = recipient
    email.save(update_fields=['status', 'attempts', 'next_attempt_at', 'recipient'])
    return email, _Message(subject, body, recipient, send_with), bool(pause and pause.failures)


def _send(message):
  """hand the email to the mail server; its Message-ID"""
  if message.send_with is not None:
    message.send_with(message.subject, message.body)
    return ''
  message_id = make_msgid(domain=DNS_NAME)
  # as django.core.mail.send_mail, which the auth systems used
  EmailMultiAlternatives(message.subject, message.body, settings.SERVER_EMAIL, [message.recipient],
                         headers={'Message-ID': message_id}).send(fail_silently=False)
  return message_id


def _after_failure(email, error):
  now = timezone.now()
  begins = False
  with transaction.atomic():
    email.last_error_code, email.last_error_text = error.code[:32], error.text
    if error.category == mail_errors.PERMANENT:
      logger.warning("email %s failed: %s", email.id, error.text)
      _finish(email, OutgoingEmail.FAILED)
    else:
      steps = email.attempts - 1 + (CONFIG_EXTRA_STEPS if error.category == mail_errors.CONFIG else 0)
      next_attempt_at = now + datetime.timedelta(seconds=retry_delay(steps))
      # the mail server is down or refuses us: the email waits for the pause, whose time
      # does not count (checked when it is due), only its attempts do
      too_old = (error.category == mail_errors.TEMPORARY and next_attempt_at > max_age_deadline(email, now))
      if email.attempts >= settings.HELIOS_EMAIL_MAX_ATTEMPTS or too_old:
        logger.warning("email %s failed after %d attempts: %s", email.id, email.attempts, error.text)
        _finish(email, OutgoingEmail.FAILED,
                error_text='given up after %d attempts: %s' % (email.attempts, error.text))
      else:
        logger.info("email %s deferred until %s: %s", email.id, next_attempt_at, error.text)
        _defer(email, next_attempt_at)
    if error.category in (mail_errors.SYSTEM, mail_errors.CONFIG):
      begins = _pause(error, now)
  if begins:
    _report_pause(error)


def send_queued(email_id):
  """
  Send the OutgoingEmail EMAIL_ID, if it is (still) to be sent, and record the
  result; on a temporary error, queue the task again for later. Safe to run
  twice: an email already sent (or deferred to later) is left alone.
  """
  claimed = _claim(email_id, timezone.now())
  if claimed is None:
    return
  email, message, pause_seen = claimed
  # committed "sending": a worker dying from here on leaves it to be sent again
  try:
    with _context(email):
      message_id = _send(message)
  except Exception as e:
    _after_failure(email, mail_errors.classify(e))
    return
  _finish(email, OutgoingEmail.SENT, sent_at=timezone.now(), message_id=message_id)
  if pause_seen:
    _end_pause()


def error_code(exception):
  """the SMTP reply code of a mail server error ('550', '550 5.1.1'), else the name of the exception"""
  return mail_errors.classify(exception).code
