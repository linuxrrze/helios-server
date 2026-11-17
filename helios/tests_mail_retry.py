"""
Tests for sending emails with retries (helios.mail, helios.mail_errors): what an
error of the mail server means, backoff, deferring and sending again, giving up,
pausing all sending, checks right before sending, the new kinds of email and
requeue_stuck_tasks for emails whose task is gone. The mail server is a fake
backend that raises the smtplib exceptions it is given.
"""
import datetime
import smtplib
import socket
import uuid
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.core import mail
from django.core.mail.backends.base import BaseEmailBackend
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django_tasks import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from helios import mail as helios_mail
from helios import mail_errors, tasks
from helios.mail_errors import CONFIG, PERMANENT, SYSTEM, TEMPORARY
from helios.models import (CastVote, Election, EmailOptOut, EmailPausePeriod, EmailSendingPause, OutgoingEmail,
                           Trustee, Voter)
from helios_auth.models import User

SCRIPTED_BACKEND = 'helios.tests_mail_retry.ScriptedBackend'


class ScriptedBackend(BaseEmailBackend):
  """the mail server: raises the next of OUTCOMES for each email (None or none left: takes it)"""
  outcomes = []
  calls = 0

  def send_messages(self, email_messages):
    for message in email_messages:
      ScriptedBackend.calls += 1
      outcome = ScriptedBackend.outcomes.pop(0) if ScriptedBackend.outcomes else None
      if outcome is not None:
        raise outcome
      mail.outbox.append(message)
    return len(email_messages)


def refused(code, text):
  return smtplib.SMTPRecipientsRefused({'voter@example.net': (code, text)})


class HTTPError(Exception):
  """as anymail's errors: the HTTP status of the mail service"""
  def __init__(self, status_code):
    super().__init__('HTTP %s' % status_code)
    self.status_code = status_code


##
## helios.mail_errors
##

class ErrorClassificationTests(SimpleTestCase):

  def test_table(self):
    from botocore.exceptions import ClientError
    table = [
      # recipient refused: the enhanced status code wins over the reply code
      (refused(550, b'5.1.1 <voter@example.net>: Recipient address rejected'), PERMANENT, '550 5.1.1'),
      (refused(550, b'mailbox unavailable'), PERMANENT, '550'),
      (refused(552, b'4.2.2 Mailbox full, try again later'), TEMPORARY, '552 4.2.2'),
      (refused(452, b'4.2.2 Mailbox full'), TEMPORARY, '452 4.2.2'),
      (refused(450, b'4.7.1 Greylisted, please try again'), TEMPORARY, '450 4.7.1'),
      (refused(451, b'Too many messages, slow down'), TEMPORARY, '451'),
      (refused(450, b'5.1.1 no such user'), PERMANENT, '450 5.1.1'),
      # refused after DATA
      (smtplib.SMTPDataError(554, b'5.7.1 Message rejected as spam'), PERMANENT, '554 5.7.1'),
      (smtplib.SMTPDataError(451, b'try later'), TEMPORARY, '451'),
      # the server goes away: all emails
      (smtplib.SMTPResponseException(421, b'4.3.2 Service shutting down'), SYSTEM, '421 4.3.2'),
      (smtplib.SMTPServerDisconnected('Connection unexpectedly closed'), SYSTEM, 'SMTPServerDisconnected'),
      (smtplib.SMTPConnectError(421, b'too busy'), SYSTEM, '421'),
      (TimeoutError('timed out'), SYSTEM, 'TimeoutError'),
      (socket.timeout('timed out'), SYSTEM, 'TimeoutError'),
      (ConnectionRefusedError(111, 'Connection refused'), SYSTEM, 'ConnectionRefusedError'),
      (socket.gaierror(-2, 'Name or service not known'), SYSTEM, 'gaierror'),
      # our configuration
      (smtplib.SMTPAuthenticationError(535, b'5.7.8 Authentication credentials invalid'), CONFIG, '535 5.7.8'),
      (smtplib.SMTPSenderRefused(553, b'5.7.1 Sender address rejected: not owned by user', 'helios@example.org'),
       CONFIG, '553 5.7.1'),
      (smtplib.SMTPNotSupportedError('STARTTLS extension not supported by server.'), CONFIG, 'SMTPNotSupportedError'),
      # HTTP mail services
      (HTTPError(429), TEMPORARY, '429'),
      (HTTPError(503), TEMPORARY, '503'),
      (HTTPError(400), PERMANENT, '400'),
      (HTTPError(401), CONFIG, '401'),
      (ClientError({'Error': {'Code': 'Throttling', 'Message': 'Maximum sending rate exceeded.'},
                    'ResponseMetadata': {'HTTPStatusCode': 400}}, 'SendRawEmail'), TEMPORARY, '400 Throttling'),
      (ClientError({'Error': {'Code': 'MessageRejected', 'Message': 'Email address is not verified.'},
                    'ResponseMetadata': {'HTTPStatusCode': 400}}, 'SendRawEmail'), PERMANENT, '400 MessageRejected'),
      # anything else: tried again rather than lost
      (ValueError('something odd'), TEMPORARY, 'ValueError'),
    ]
    for exception, category, code in table:
      with self.subTest(exception=repr(exception)):
        error = mail_errors.classify(exception)
        self.assertEqual((error.category, error.code), (category, code))
        self.assertIn(type(exception).__name__, error.text)

  def test_enhanced_code(self):
    self.assertEqual(mail_errors.enhanced_code(b'5.1.1 <x@example.org>: unknown'), '5.1.1')
    self.assertEqual(mail_errors.enhanced_code('  4.2.2 full'), '4.2.2')
    self.assertEqual(mail_errors.enhanced_code(b'4.7.0 '), '4.7.0')
    # only at the start, and not part of an address or version
    self.assertIsNone(mail_errors.enhanced_code(b'rejected by 192.5.10.20'))
    self.assertIsNone(mail_errors.enhanced_code(b'5.1.1.4 what'))
    self.assertIsNone(mail_errors.enhanced_code(b'3.1.1 not a class'))
    self.assertIsNone(mail_errors.enhanced_code(None))


@override_settings(HELIOS_EMAIL_RETRY_BASE=300, HELIOS_EMAIL_RETRY_CAP=21600)
class BackoffTests(SimpleTestCase):

  def test_bounds(self):
    for steps, expected in [(0, 300), (1, 600), (2, 1200), (6, 19200), (7, 21600), (40, 21600)]:
      with self.subTest(steps=steps):
        with mock.patch('helios.mail.random.uniform', side_effect=lambda low, high: low):
          self.assertEqual(helios_mail.retry_delay(steps), expected * 0.5)
        with mock.patch('helios.mail.random.uniform', side_effect=lambda low, high: high):
          self.assertEqual(helios_mail.retry_delay(steps), expected * 1.5)
        for _ in range(20):
          self.assertTrue(expected * 0.5 <= helios_mail.retry_delay(steps) <= expected * 1.5)


##
## sending with retries
##

def run_tasks():
  from helios.tests import run_queued_tasks
  run_queued_tasks()


def time_passes():
  """everything waiting for later is due now: deferred emails, their tasks and a pause"""
  past = timezone.now() - datetime.timedelta(seconds=1)
  DBTaskResult.objects.filter(status=TaskResultStatus.READY).update(run_after=past)
  OutgoingEmail.objects.filter(status=OutgoingEmail.DEFERRED).update(next_attempt_at=past)
  EmailSendingPause.objects.filter(paused_until__isnull=False).update(paused_until=past)


def waiting_tasks():
  return DBTaskResult.objects.filter(status=TaskResultStatus.READY, task_path='helios.tasks.send_outgoing_email')


@override_settings(EMAIL_BACKEND=SCRIPTED_BACKEND, HELIOS_EMAIL_RETRY_BASE=300, HELIOS_EMAIL_RETRY_CAP=21600,
                   HELIOS_EMAIL_MAX_ATTEMPTS=8, HELIOS_EMAIL_MAX_AGE=172800, ADMINS=[])
class RetryTests(TestCase):

  def setUp(self):
    ScriptedBackend.outcomes = []
    ScriptedBackend.calls = 0
    self.admin = User.objects.create(user_type='password', user_id='admin@example.org', name='Ada Admin',
                                     info={'password': 'x'})
    self.election = Election.objects.create(admin=self.admin, uuid=str(uuid.uuid4()), short_name='board',
                                            name='Board election', election_type='election')
    self.voter = self.make_voter('voter0@example.net')

  def make_voter(self, address):
    voter = Voter(uuid=str(uuid.uuid4()), election=self.election, voter_login_id=address, voter_email=address,
                  voter_name='Voter %s' % address)
    voter.generate_password()
    voter.save()
    return voter

  def queue_bulk(self, template='simple', voter=None):
    batch = helios_mail.create_batch(self.election, 'email/%s_subject.txt' % template, 'email/%s_body.txt' % template,
                                     {'custom_subject': 'Board election', 'custom_message': 'Please vote.'},
                                     language='en', triggered_by=self.admin)
    return helios_mail.queue(OutgoingEmail.KIND_BULK, voter=voter or self.voter, batch=batch)

  def reload(self, email):
    email.refresh_from_db()
    return email

  def test_temporary_error_is_deferred_and_sent_later(self):
    ScriptedBackend.outcomes = [refused(450, b'4.7.1 Greylisted, try again in 5 minutes')]
    with mock.patch('helios.mail.random.uniform', return_value=1.0):
      email = self.queue_bulk()
      before = timezone.now()
      run_tasks()   # no task fails
    self.reload(email)
    self.assertEqual((email.status, email.attempts, email.last_error_code),
                     (OutgoingEmail.DEFERRED, 1, '450 4.7.1'))
    self.assertIn('Greylisted', email.last_error_text)
    self.assertAlmostEqual((email.next_attempt_at - before).total_seconds(), 300, delta=5)
    self.assertEqual(email.recipient, '"Voter voter0@example.net" <voter0@example.net>')
    self.assertEqual(len(mail.outbox), 0)
    # the task queued itself again, for then (state is in the email, not the task)
    task_result = waiting_tasks().get()
    self.assertEqual(task_result.args_kwargs['args'], [email.id])
    self.assertEqual(task_result.run_after, email.next_attempt_at)
    run_tasks()
    self.assertEqual(len(mail.outbox), 0)   # not yet
    time_passes()
    run_tasks()
    self.reload(email)
    self.assertEqual((email.status, email.attempts, len(mail.outbox)), (OutgoingEmail.SENT, 2, 1))
    self.assertIsNone(email.next_attempt_at)
    self.assertFalse(EmailSendingPause.objects.filter(failures__gt=0).exists())

  def test_backoff_grows(self):
    ScriptedBackend.outcomes = [refused(451, b'4.3.0 try later')] * 3
    email = self.queue_bulk()
    delays = []
    with mock.patch('helios.mail.random.uniform', return_value=1.0):
      for _ in range(3):
        start = timezone.now()
        run_tasks()
        delays.append(round((self.reload(email).next_attempt_at - start).total_seconds() / 60))
        time_passes()
    self.assertEqual(delays, [5, 10, 20])

  @override_settings(HELIOS_EMAIL_MAX_ATTEMPTS=3)
  def test_max_attempts(self):
    ScriptedBackend.outcomes = [refused(452, b'4.2.2 Mailbox full')] * 5
    email = self.queue_bulk()
    for _ in range(5):
      run_tasks()
      time_passes()
    self.reload(email)
    self.assertEqual((email.status, email.attempts, email.last_error_code), (OutgoingEmail.FAILED, 3, '452 4.2.2'))
    self.assertIn('given up after 3 attempts', email.last_error_text)
    self.assertEqual(ScriptedBackend.calls, 3)
    self.assertFalse(waiting_tasks().exists())

  @override_settings(HELIOS_EMAIL_MAX_AGE=60)
  def test_max_age(self):
    # the next attempt would come too late
    ScriptedBackend.outcomes = [refused(452, b'4.2.2 Mailbox full')]
    email = self.queue_bulk()
    run_tasks()
    self.assertEqual(self.reload(email).status, OutgoingEmail.FAILED)
    self.assertFalse(waiting_tasks().exists())
    # deferred (e.g. by a pause) until it is too old
    other = self.queue_bulk(voter=self.make_voter('voter1@example.net'))
    OutgoingEmail.objects.filter(id=other.id).update(status=OutgoingEmail.DEFERRED, attempts=1,
                                                     created_at=timezone.now() - datetime.timedelta(seconds=61),
                                                     last_error_code='SMTPServerDisconnected')
    run_tasks()
    self.reload(other)
    self.assertEqual((other.status, other.last_error_code), (OutgoingEmail.FAILED, 'SMTPServerDisconnected'))
    self.assertIn('too old', other.last_error_text)
    self.assertEqual(ScriptedBackend.calls, 1)

  @override_settings(HELIOS_EMAIL_MAX_AGE=3600)
  def test_time_paused_does_not_count_towards_max_age(self):
    now = timezone.now()
    hours_ago = now - datetime.timedelta(hours=2)
    waited = self.queue_bulk()
    control = self.queue_bulk(voter=self.make_voter('voter1@example.net'))
    OutgoingEmail.objects.filter(id__in=[waited.id, control.id]).update(
      status=OutgoingEmail.DEFERRED, attempts=1, created_at=hours_ago, next_attempt_at=now - datetime.timedelta(minutes=1))
    # an outage of 1h45 since: 15 minutes of its hour used up
    EmailPausePeriod.objects.create(started_at=now - datetime.timedelta(minutes=110),
                                    ended_at=now - datetime.timedelta(minutes=5))
    self.assertAlmostEqual(helios_mail.paused_seconds(hours_ago, now), 105 * 60, delta=1)
    # the control email is of another time: from before the outage only part counts
    OutgoingEmail.objects.filter(id=control.id).update(created_at=now - datetime.timedelta(hours=5))
    run_tasks()
    self.assertEqual(self.reload(waited).status, OutgoingEmail.SENT)
    self.assertEqual(self.reload(control).status, OutgoingEmail.FAILED)   # 5h - 1h45 > 1h

  @override_settings(HELIOS_EMAIL_MAX_AGE=60)
  def test_emails_outlast_an_outage(self):
    ScriptedBackend.outcomes = [smtplib.SMTPServerDisconnected('gone'), smtplib.SMTPServerDisconnected('gone')]
    with self.assertLogs('helios.mail', level='ERROR'):
      email = self.queue_bulk()
      other = self.queue_bulk(voter=self.make_voter('voter1@example.net'))
      run_tasks()
    # the mail server is down: no age limit for the next attempt, only the attempts count
    self.assertEqual((self.reload(email).status, self.reload(other).status),
                     (OutgoingEmail.DEFERRED, OutgoingEmail.DEFERRED))
    self.assertEqual((email.attempts, other.attempts), (1, 0))
    period = EmailPausePeriod.objects.get()
    self.assertIsNone(period.ended_at)
    # long after MAX_AGE, the outage is still going on
    two_days_ago = timezone.now() - datetime.timedelta(days=2)
    OutgoingEmail.objects.update(created_at=two_days_ago)
    EmailPausePeriod.objects.update(started_at=two_days_ago)
    time_passes()
    run_tasks()
    self.assertEqual((self.reload(email).status, email.attempts), (OutgoingEmail.DEFERRED, 2))
    self.assertEqual(self.reload(other).status, OutgoingEmail.DEFERRED)
    # the mail server is back: they go out, and the outage is over
    time_passes()
    run_tasks()
    self.assertEqual((self.reload(email).status, self.reload(other).status), (OutgoingEmail.SENT, OutgoingEmail.SENT))
    self.assertIsNotNone(EmailPausePeriod.objects.get().ended_at)

  def test_permanent_error_fails_at_once(self):
    ScriptedBackend.outcomes = [refused(550, b'5.1.1 <voter0@example.net>: Recipient address rejected')]
    email = self.queue_bulk()
    run_tasks()
    self.reload(email)
    self.assertEqual((email.status, email.attempts, email.last_error_code), (OutgoingEmail.FAILED, 1, '550 5.1.1'))
    self.assertIsNone(email.sent_at)
    self.assertFalse(waiting_tasks().exists())
    self.assertFalse(EmailSendingPause.objects.exists())   # one recipient, not the mail server

  @override_settings(ADMINS=[('Ops', 'ops@example.org')])
  def test_config_error_waits_longer_pauses_and_tells_the_administrators(self):
    ScriptedBackend.outcomes = [smtplib.SMTPAuthenticationError(535, b'5.7.8 Authentication credentials invalid')]
    with mock.patch('helios.mail.random.uniform', return_value=1.0), \
         self.assertLogs('helios.mail', level='ERROR') as logs:
      email = self.queue_bulk()
      start = timezone.now()
      run_tasks()
    self.reload(email)
    self.assertEqual((email.status, email.last_error_code), (OutgoingEmail.DEFERRED, '535 5.7.8'))
    # as if 3 more attempts had failed: 8 * 5 minutes
    self.assertAlmostEqual((email.next_attempt_at - start).total_seconds(), 2400, delta=5)
    pause = EmailSendingPause.objects.get()
    self.assertEqual((pause.failures, pause.error_code), (1, '535 5.7.8'))
    self.assertAlmostEqual((pause.paused_until - start).total_seconds(), 2400, delta=5)
    self.assertIn('paused sending emails', logs.output[0])
    # the administrators get an email (as far as the mail server takes it)
    self.assertEqual(len(mail.outbox), 1)
    self.assertEqual(mail.outbox[0].to, ['ops@example.org'])
    self.assertIn('EMAIL_HOST_PASSWORD', mail.outbox[0].body)

    # meanwhile, other emails wait for the pause without an attempt
    calls = ScriptedBackend.calls
    other = self.queue_bulk(voter=self.make_voter('voter1@example.net'))
    run_tasks()
    self.reload(other)
    self.assertEqual((other.status, other.attempts), (OutgoingEmail.DEFERRED, 0))
    self.assertGreaterEqual(other.next_attempt_at, pause.paused_until)
    self.assertEqual(ScriptedBackend.calls, calls)

    # once it is over, the first email through ends the pause
    time_passes()
    run_tasks()
    self.assertEqual(self.reload(email).status, OutgoingEmail.SENT)
    self.assertEqual(self.reload(other).status, OutgoingEmail.SENT)
    self.assertEqual(EmailSendingPause.objects.get().failures, 0)
    self.assertIsNone(EmailSendingPause.objects.get().paused_until)

  @override_settings(ADMINS=[('Ops', 'ops@example.org')])
  def test_unreachable_mail_server_pauses_longer_each_time(self):
    ScriptedBackend.outcomes = [smtplib.SMTPServerDisconnected('Connection unexpectedly closed'),
                                ConnectionRefusedError(111, 'Connection refused')]
    with mock.patch('helios.mail.random.uniform', return_value=1.0), self.assertLogs('helios.mail', level='ERROR'):
      email = self.queue_bulk()
      start = timezone.now()
      run_tasks()
      pause = EmailSendingPause.objects.get()
      self.assertAlmostEqual((pause.paused_until - start).total_seconds(), 300, delta=5)
      self.assertEqual(len(mail.outbox), 0)   # no email to the administrators: it would not get through
      time_passes()
      start = timezone.now()
      run_tasks()
    pause.refresh_from_db()
    self.assertEqual((pause.failures, pause.error_code), (2, 'ConnectionRefusedError'))
    self.assertAlmostEqual((pause.paused_until - start).total_seconds(), 600, delta=5)
    self.assertEqual((self.reload(email).status, email.attempts), (OutgoingEmail.DEFERRED, 2))

  def test_resume_email_command(self):
    ScriptedBackend.outcomes = [smtplib.SMTPServerDisconnected('gone')]
    with self.assertLogs('helios.mail', level='ERROR'):
      email = self.queue_bulk()
      run_tasks()
    out = StringIO()
    call_command('resume_email', stdout=out)
    self.assertIn('pause ended, 1 deferred emails queued', out.getvalue())
    run_tasks()   # right away
    self.assertEqual(self.reload(email).status, OutgoingEmail.SENT)
    # the older task, still waiting, finds it sent
    time_passes()
    run_tasks()
    self.assertEqual(len(mail.outbox), 1)

  def test_no_double_send(self):
    email = self.queue_bulk()
    tasks.send_outgoing_email.call(email.id)
    tasks.send_outgoing_email.call(email.id)
    run_tasks()
    self.assertEqual(len(mail.outbox), 1)
    self.assertEqual(self.reload(email).attempts, 1)

  def test_task_of_an_email_deferred_to_later_does_nothing(self):
    email = self.queue_bulk()
    later = timezone.now() + datetime.timedelta(hours=1)
    OutgoingEmail.objects.filter(id=email.id).update(status=OutgoingEmail.DEFERRED, next_attempt_at=later)
    tasks.send_outgoing_email.call(email.id)
    self.assertEqual(len(mail.outbox), 0)
    self.assertEqual(self.reload(email).next_attempt_at, later)

  def test_email_being_sent_by_another_worker(self):
    email = self.queue_bulk()
    DBTaskResult.objects.all().delete()
    lease = timezone.now() + datetime.timedelta(minutes=10)
    OutgoingEmail.objects.filter(id=email.id).update(status=OutgoingEmail.SENDING, attempts=1, next_attempt_at=lease)
    tasks.send_outgoing_email.call(email.id)
    self.assertEqual(len(mail.outbox), 0)
    # should that worker have died, it is sent after the lease
    self.assertEqual(waiting_tasks().get().run_after, lease)
    OutgoingEmail.objects.filter(id=email.id).update(next_attempt_at=timezone.now() - datetime.timedelta(minutes=1))
    time_passes()
    run_tasks()
    self.reload(email)
    self.assertEqual((email.status, email.attempts, len(mail.outbox)), (OutgoingEmail.SENT, 2, 1))

  ##
  ## checks right before sending
  ##

  def test_opted_out_between_queueing_and_sending(self):
    ScriptedBackend.outcomes = [refused(450, b'4.7.1 Greylisted')]
    email = self.queue_bulk()
    run_tasks()
    self.assertEqual(self.reload(email).recipient, '"Voter voter0@example.net" <voter0@example.net>')
    EmailOptOut.add_opt_out('voter0@example.net')
    time_passes()
    run_tasks()
    self.reload(email)
    self.assertEqual((email.status, email.recipient), (OutgoingEmail.SKIPPED_OPTOUT, ''))
    self.assertEqual(len(mail.outbox), 0)

  def test_invitation_to_vote_after_voting_closed(self):
    invitation = self.queue_bulk('vote')
    information = self.queue_bulk('info', voter=self.make_voter('voter1@example.net'))
    Election.objects.filter(id=self.election.id).update(
      voting_ended_at=datetime.datetime.utcnow() - datetime.timedelta(minutes=1))
    run_tasks()
    self.reload(invitation)
    self.assertEqual((invitation.status, invitation.last_error_code), (OutgoingEmail.SKIPPED_OBSOLETE, 'voting_closed'))
    self.assertEqual(self.reload(information).status, OutgoingEmail.SENT)
    self.assertEqual(len(mail.outbox), 1)

  def test_voter_password_after_voting_closed(self):
    email = helios_mail.queue(OutgoingEmail.KIND_VOTER_PASSWORD, voter=self.voter, params={
      'subject_template': 'email/password_resend_subject.txt', 'body_template': 'email/password_resend_body.txt',
      'extra_vars': {'election_vote_url': 'https://helios.example.org/vote'}})
    Election.objects.filter(id=self.election.id).update(
      voting_ended_at=datetime.datetime.utcnow() - datetime.timedelta(minutes=1))
    run_tasks()
    self.assertEqual(self.reload(email).last_error_code, 'voting_closed')

  def test_voter_password_resent(self):
    email = helios_mail.queue(OutgoingEmail.KIND_VOTER_PASSWORD, voter=self.voter, params={
      'subject_template': 'email/password_resend_subject.txt', 'body_template': 'email/password_resend_body.txt',
      'extra_vars': {'election_vote_url': 'https://helios.example.org/vote'}})
    run_tasks()
    self.assertEqual(self.reload(email).status, OutgoingEmail.SENT)
    self.assertIn(self.voter.voter_password, mail.outbox[0].body)
    self.assertNotIn(self.voter.voter_password, repr(OutgoingEmail.objects.values().get()))

  def test_after_the_voter_email_cutoff(self):
    email = self.queue_bulk('result')
    Election.objects.filter(id=self.election.id).update(
      tallying_finished_at=datetime.datetime.utcnow() - datetime.timedelta(weeks=4))
    run_tasks()
    self.assertEqual(self.reload(email).last_error_code, 'voter_email_cutoff')

  def send_mailing(self, voter_constraints_include, voter_constraints_exclude):
    """a mailing as the "email voters" page sends it, its emails added and queued (not sent yet)"""
    batch = helios_mail.create_batch(self.election, 'email/vote_subject.txt', 'email/vote_body.txt',
                                     {'custom_subject': 'Reminder', 'custom_message': 'Please vote.'}, language='en',
                                     voter_constraints=(voter_constraints_include, voter_constraints_exclude))
    tasks.voters_email.call(election_id=self.election.id, batch_id=batch.id, language='en',
                            voter_constraints_include=voter_constraints_include,
                            voter_constraints_exclude=voter_constraints_exclude)
    return batch

  def test_reminder_to_a_voter_who_has_voted_since(self):
    other = self.make_voter('voter1@example.net')
    batch = self.send_mailing({'vote_hash': None}, None)   # to those who have not voted
    self.assertEqual(batch.voter_constraints, {'include': {'vote_hash': None}, 'exclude': None})
    self.assertEqual(batch.emails.count(), 2)
    # voter0 votes before the emails go out
    Voter.objects.filter(id=self.voter.id).update(vote_hash='HASH')
    run_tasks()
    email = batch.emails.get(voter=self.voter)
    self.assertEqual((email.status, email.last_error_code), (OutgoingEmail.SKIPPED_OBSOLETE, 'no_longer_selected'))
    self.assertEqual(batch.emails.get(voter=other).status, OutgoingEmail.SENT)
    self.assertEqual([m.to for m in mail.outbox], [['"Voter voter1@example.net" <voter1@example.net>']])

  def test_mailing_to_all_or_without_recorded_choice(self):
    voted = self.make_voter('voter1@example.net')
    Voter.objects.filter(id=voted.id).update(vote_hash='HASH')
    # to all voters: recorded, everyone stays chosen
    batch = self.send_mailing(None, None)
    self.assertEqual(batch.voter_constraints, {'include': None, 'exclude': None})
    run_tasks()
    self.assertEqual(batch.emails.filter(status=OutgoingEmail.SENT).count(), 2)
    # a mailing from before the choice was recorded (or to one voter): not checked
    old = self.queue_bulk('vote', voter=voted)
    self.assertIsNone(old.batch.voter_constraints)
    run_tasks()
    self.assertEqual(self.reload(old).status, OutgoingEmail.SENT)

  def test_election_deleted_or_voter_removed(self):
    email = self.queue_bulk()
    other = self.queue_bulk(voter=self.make_voter('voter1@example.net'))
    Voter.objects.filter(id=other.voter_id).delete()
    run_tasks()
    self.assertEqual(self.reload(other).last_error_code, 'voter_removed')
    self.assertEqual(self.reload(email).status, OutgoingEmail.SENT)
    third = self.queue_bulk(voter=self.make_voter('voter2@example.net'))
    self.election.soft_delete()
    run_tasks()
    self.reload(third)
    self.assertEqual((third.status, third.last_error_code), (OutgoingEmail.SKIPPED_OBSOLETE, 'election_deleted'))

  ##
  ## vote-cast confirmation: queued by the cast-vote task, sent by its own
  ##

  def test_vote_cast_confirmation_failure_does_not_fail_the_verification(self):
    ScriptedBackend.outcomes = [smtplib.SMTPServerDisconnected('Connection unexpectedly closed')]
    cast_vote = CastVote.objects.create(voter=self.voter, vote_hash='HASH123', vote_tinyhash='tiny123')
    with mock.patch.object(CastVote, 'verify_and_store', return_value=True), \
         self.assertLogs('helios.mail', level='ERROR'):
      tasks.cast_vote_verify_and_store.enqueue(cast_vote.id, language='de')
      run_tasks()   # neither task fails
    verification = DBTaskResult.objects.get(task_path='helios.tasks.cast_vote_verify_and_store')
    self.assertEqual(verification.status, TaskResultStatus.SUCCESSFUL)
    email = OutgoingEmail.objects.get()
    self.assertEqual((email.kind, email.status, email.language, email.params),
                     (OutgoingEmail.KIND_VOTE_CAST, OutgoingEmail.DEFERRED, 'de', {'cast_vote_id': cast_vote.id}))
    time_passes()
    run_tasks()
    self.assertEqual(self.reload(email).status, OutgoingEmail.SENT)
    self.assertEqual(mail.outbox[0].subject, 'Board election – Stimme abgegeben')
    self.assertIn('HASH123', mail.outbox[0].body)
    self.assertEqual(self.reload(email).params, {})

  def test_vote_cast_confirmation_priority(self):
    cast_vote = CastVote.objects.create(voter=self.voter, vote_hash='HASH123', vote_tinyhash='tiny123')
    helios_mail.queue(OutgoingEmail.KIND_VOTE_CAST, voter=self.voter, params={'cast_vote_id': cast_vote.id})
    self.queue_bulk()
    priorities = dict(waiting_tasks().values_list('args_kwargs__args__0', 'priority'))
    self.assertEqual(sorted(priorities.values()), [helios_mail.BULK_PRIORITY, helios_mail.PRIORITY])

  ##
  ## the other kinds
  ##

  def test_trustee_link(self):
    trustee = Trustee.objects.create(election=self.election, uuid=str(uuid.uuid4()), name='Tara Trustee',
                                     email='trustee@example.net', secret='s3cret')
    email = helios_mail.queue(OutgoingEmail.KIND_TRUSTEE, election=self.election, trustee=trustee)
    run_tasks()
    self.reload(email)
    self.assertEqual((email.status, email.trustee, email.recipient),
                     (OutgoingEmail.SENT, trustee, 'Tara Trustee <trustee@example.net>'))
    self.assertIn('/t/board/trustee@example.net/s3cret', mail.outbox[0].body)
    self.assertNotIn('s3cret', repr(OutgoingEmail.objects.values().get()))
    # a trustee removed before it is sent
    other = Trustee.objects.create(election=self.election, uuid=str(uuid.uuid4()), name='Tom', email='tom@example.net',
                                   secret='x')
    email = helios_mail.queue(OutgoingEmail.KIND_TRUSTEE, election=self.election, trustee=other)
    other.delete()
    run_tasks()
    self.assertEqual(self.reload(email).last_error_code, 'trustee_removed')

  def test_trustee_link_view(self):
    trustee = Trustee.objects.create(election=self.election, uuid=str(uuid.uuid4()), name='Tara Trustee',
                                     email='trustee@example.net', secret='s3cret')
    ScriptedBackend.outcomes = [smtplib.SMTPRecipientsRefused({'trustee@example.net': (451, b'4.3.0 later')})]
    self.client.get('/')
    session = self.client.session
    session['user'] = {'type': 'password', 'user_id': 'admin@example.org'}
    session['csrf_token'] = 'tok'
    session.save()
    self.client.post('/helios/elections/%s/trustees/%s/sendurl' % (self.election.uuid, trustee.uuid),
                     {'csrf_token': 'tok'})
    run_tasks()
    email = OutgoingEmail.objects.get(kind=OutgoingEmail.KIND_TRUSTEE)
    self.assertEqual(email.status, OutgoingEmail.DEFERRED)   # formerly lost (fail_silently)
    time_passes()
    run_tasks()
    self.assertEqual(self.reload(email).status, OutgoingEmail.SENT)

  def test_optout_confirmation_keeps_no_address(self):
    ScriptedBackend.outcomes = [refused(451, b'4.3.0 later'), refused(550, b'5.1.1 unknown')]
    self.client.post('/optout/', {'email': 'erika@example.org'})
    email = OutgoingEmail.objects.get()
    # only while it is to be sent
    self.assertEqual((email.kind, email.recipient), (OutgoingEmail.KIND_OPTOUT_CONFIRM, 'erika@example.org'))
    run_tasks()
    self.assertEqual(self.reload(email).recipient, 'erika@example.org')
    time_passes()
    run_tasks()
    self.reload(email)
    self.assertEqual((email.status, email.recipient), (OutgoingEmail.FAILED, 'e***@example.org'))
    self.client.post('/optin/', {'email': 'erika@example.org'})   # not opted out: nothing sent
    EmailOptOut.add_opt_out('erika@example.org')
    self.client.post('/optin/', {'email': 'erika@example.org'})
    run_tasks()   # to an opted-out address, of course
    email = OutgoingEmail.objects.get(kind=OutgoingEmail.KIND_OPTIN_CONFIRM)
    self.assertEqual((email.status, email.recipient), (OutgoingEmail.SENT, 'e***@example.org'))
    self.assertIn('/optin/confirm/erika@example.org/', mail.outbox[0].body)
    self.assertNotIn('erika@', repr(list(OutgoingEmail.objects.values())))

  def test_password_reminder(self):
    user = User.objects.create(user_type='password', user_id='jdoe', name='Jane',
                               info={'password': 'pw-secret', 'email': 'jane@example.net', 'name': 'Jane'})
    email = helios_mail.queue(OutgoingEmail.KIND_PASSWORD_REMINDER, user=user, language='en')
    run_tasks()
    self.reload(email)
    self.assertEqual((email.status, email.user, email.recipient),
                     (OutgoingEmail.SENT, user, '"Jane" <jane@example.net>'))
    self.assertIn('pw-secret', mail.outbox[0].body)
    self.assertNotIn('pw-secret', repr(OutgoingEmail.objects.values().get()))
    # an account without an email address: its user id
    user = User.objects.create(user_type='password', user_id='joe@example.net', name='Joe', info={'password': 'pw'})
    helios_mail.queue(OutgoingEmail.KIND_PASSWORD_REMINDER, user=user)
    run_tasks()
    self.assertEqual(mail.outbox[1].to, ['"Joe" <joe@example.net>'])

  def test_vote_removed_after_the_voter_is_gone(self):
    email = helios_mail.queue(OutgoingEmail.KIND_VOTE_REMOVED, election=self.election, voter=self.voter,
                              recipient=helios_mail.recipient_of(self.voter.get_user()),
                              params={'subject': 'Your vote has been removed', 'body': 'Sorry.'})
    self.voter.delete()
    run_tasks()
    self.reload(email)
    self.assertEqual((email.status, email.voter, email.params), (OutgoingEmail.SENT, None, {}))
    self.assertEqual(mail.outbox[0].to, ['"Voter voter0@example.net" <voter0@example.net>'])

  def test_auth_system_sending_itself(self):
    from helios_auth.auth_systems import AUTH_SYSTEMS
    def send_message(user_id, name, info, subject, body):
      raise smtplib.SMTPServerDisconnected('gone')
    user = User.objects.create(user_type='legacy', user_id='u1', name='Lea', info={})
    with mock.patch.dict(AUTH_SYSTEMS, {'legacy': SimpleNamespace(send_message=send_message)}), \
         self.assertLogs('helios.mail', level='ERROR'):
      email = helios_mail.queue(OutgoingEmail.KIND_ADMIN_INFO, election=self.election, user=user,
                                params={'subject': 's', 'body': 'b'})
      run_tasks()
    self.assertEqual(self.reload(email).status, OutgoingEmail.DEFERRED)

  def test_render_error_fails_the_email(self):
    email = helios_mail.queue(OutgoingEmail.KIND_ADMIN_INFO, election=self.election, user=self.admin, params={})
    with self.assertLogs('helios.mail', level='ERROR'):
      run_tasks()
    self.reload(email)
    self.assertEqual((email.status, email.last_error_code), (OutgoingEmail.FAILED, 'render_error'))

  ##
  ## tasks queued before send_outgoing_email existed
  ##

  def test_older_single_voter_email_tasks(self):
    ScriptedBackend.outcomes = [refused(450, b'4.7.1 Greylisted')]
    tasks.single_voter_email.enqueue(self.voter.uuid, 'email/simple_subject.txt', 'email/simple_body.txt',
                                     {'custom_subject': 'x', 'custom_message': 'y'}, language='en')
    run_tasks()
    email = OutgoingEmail.objects.get()
    self.assertEqual((email.kind, email.status), (OutgoingEmail.KIND_BULK, OutgoingEmail.DEFERRED))
    self.assertEqual(waiting_tasks().count(), 1)
    time_passes()
    run_tasks()
    self.assertEqual((self.reload(email).status, len(mail.outbox)), (OutgoingEmail.SENT, 1))
    self.assertEqual(email.params, {})


##
## requeue_stuck_tasks: emails whose task is gone
##

class RequeueLostEmailsTests(TestCase):

  def setUp(self):
    self.admin = User.objects.create(user_type='password', user_id='admin@example.org', name='Ada', info={'password': 'x'})
    self.election = Election.objects.create(admin=self.admin, uuid=str(uuid.uuid4()), short_name='board',
                                            name='Board election', election_type='election')

  def email(self, status, created_ago=None, next_attempt_ago=None):
    now = timezone.now()
    email = OutgoingEmail.objects.create(kind=OutgoingEmail.KIND_ADMIN_INFO, election=self.election, user=self.admin,
                                         status=status, params={'subject': 's', 'body': 'b'})
    fields = {}
    if created_ago is not None:
      fields['created_at'] = now - datetime.timedelta(minutes=created_ago)
    if next_attempt_ago is not None:
      fields['next_attempt_at'] = now - datetime.timedelta(minutes=next_attempt_ago)
    OutgoingEmail.objects.filter(id=email.id).update(**fields)
    return email

  def test_emails_without_a_task_are_queued_again(self):
    lost_queued = self.email(OutgoingEmail.QUEUED, created_ago=180)
    lost_sending = self.email(OutgoingEmail.SENDING, created_ago=300, next_attempt_ago=150)   # its worker died
    lost_deferred = self.email(OutgoingEmail.DEFERRED, created_ago=300, next_attempt_ago=130)
    # not lost
    self.email(OutgoingEmail.QUEUED, created_ago=10)        # its task may still come
    self.email(OutgoingEmail.DEFERRED, created_ago=300, next_attempt_ago=-60)   # later
    self.email(OutgoingEmail.SENT, created_ago=300)
    with_task = self.email(OutgoingEmail.QUEUED, created_ago=180)
    helios_mail.enqueue(with_task.id, with_task.kind)
    older_task = self.email(OutgoingEmail.QUEUED, created_ago=180)   # of a bulk mailing before send_outgoing_email
    tasks.single_voter_email.enqueue('some-voter-uuid', language='en', outgoing_email_id=older_task.id)

    out = StringIO()
    call_command('requeue_stuck_tasks', '--dry-run', stdout=out)
    self.assertIn('would requeue 3 emails', out.getvalue())
    call_command('requeue_stuck_tasks', stdout=out)
    self.assertIn('requeued 3 emails', out.getvalue())
    queued = sorted(task_result.args_kwargs['args'][0] for task_result in waiting_tasks())
    self.assertEqual(queued, sorted([lost_queued.id, lost_sending.id, lost_deferred.id, with_task.id]))
    # nothing twice
    out = StringIO()
    call_command('requeue_stuck_tasks', stdout=out)
    self.assertIn('requeued 0 emails', out.getvalue())

    with override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
      DBTaskResult.objects.filter(task_path='helios.tasks.single_voter_email').delete()
      run_tasks()
    for email in (lost_queued, lost_sending, lost_deferred, with_task):
      email.refresh_from_db()
      self.assertEqual(email.status, OutgoingEmail.SENT)
    self.assertEqual(len(mail.outbox), 4)

  def test_running_task_is_not_lost(self):
    email = self.email(OutgoingEmail.SENDING, created_ago=300, next_attempt_ago=150)
    DBTaskResult.objects.create(
      status=TaskResultStatus.RUNNING, task_path='helios.tasks.send_outgoing_email',
      args_kwargs={'args': [email.id], 'kwargs': {}}, backend_name='default', run_after=timezone.now(),
      started_at=timezone.now() - datetime.timedelta(minutes=180))
    out = StringIO()
    call_command('requeue_stuck_tasks', stdout=out)
    # the stuck task itself is requeued, the email not a second time
    self.assertIn('requeued 1 tasks', out.getvalue())
    self.assertIn('requeued 0 emails', out.getvalue())
