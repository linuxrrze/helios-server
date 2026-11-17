"""
Tests for helios.mail_backend (one log line per email, recipients masked) and
helios.mail (an OutgoingEmail per email, an EmailBatch per bulk mailing).
"""
import datetime
import smtplib
import uuid
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.core import mail
from django.core.mail.backends.base import BaseEmailBackend
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from django_tasks import TaskResultStatus
from django_tasks_db.management.commands.db_worker import Worker
from django_tasks_db.models import DBTaskResult

from helios import mail as helios_mail
from helios import tasks
from helios.mail_backend import mask_address
from helios.models import CastVote, Election, EmailBatch, EmailOptOut, OutgoingEmail, Voter
from helios_auth.auth_systems import AUTH_SYSTEMS
from helios_auth.models import User

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
    # a caller passing fail_silently=True must not make the failure vanish
    with self.assertLogs('helios.mail', level='WARNING') as logs:
      self.assertEqual(mail.send_mail('subject', 'body', 'helios@example.org', ['erika@example.org'], fail_silently=True), 0)
    self.assertIn('email NOT sent: to=e***@example.org', logs.output[0])


##
## helios.mail: an OutgoingEmail per email, an EmailBatch per bulk mailing
##


def run_tasks_allowing_failures():
  """like helios.tests.run_queued_tasks, but leaves failed tasks to the test"""
  worker = Worker(queue_names=['*'], excluded_queue_names=[], interval=0, batch=True, backend_name='default',
                  startup_delay=False, max_tasks=None, worker_id='test-worker')
  with mock.patch('django_tasks_db.management.commands.db_worker.close_old_connections'):
    worker.run()


class OutgoingEmailTests(TestCase):

  def setUp(self):
    self.admin = User.objects.create(user_type='password', user_id='admin@example.org', name='Ada Admin',
                                     info={'password': 'x'})
    self.election = Election.objects.create(admin=self.admin, uuid=str(uuid.uuid4()), short_name='board',
                                            name='Board election', election_type='election')
    self.voters = [self.make_voter('voter%d@example.net' % i) for i in range(3)]

  def make_voter(self, email, user=None):
    voter = Voter(uuid=str(uuid.uuid4()), election=self.election, voter_login_id=email,
                  voter_email=email if user is None else None, voter_name='Voter %s' % email, user=user)
    if user is None:
      voter.generate_password()
    voter.save()
    return voter

  def make_batch(self):
    return helios_mail.create_batch(self.election, 'email/simple_subject.txt', 'email/simple_body.txt',
                                    {'custom_subject': 'Board election', 'custom_message': 'Please vote.'},
                                    language='en', triggered_by=self.admin)

  def send_batch(self, batch, **constraints):
    from helios.tests import run_queued_tasks
    tasks.voters_email.enqueue(election_id=self.election.id, batch_id=batch.id, language='en', **constraints)
    run_queued_tasks()

  def test_one_email_per_voter(self):
    batch = self.make_batch()
    self.send_batch(batch)
    self.assertEqual(batch.triggered_by, self.admin)
    emails = batch.emails.order_by('id')
    self.assertEqual(sorted(e.voter_id for e in emails), sorted(v.id for v in self.voters))
    self.assertEqual(len(mail.outbox), 3)
    for email in emails:
      self.assertEqual(email.kind, OutgoingEmail.KIND_BULK)
      self.assertEqual(email.election, self.election)
      self.assertEqual(email.status, OutgoingEmail.SENT)
      self.assertEqual(email.attempts, 1)
      self.assertIsNotNone(email.sent_at)
      self.assertIn(email.voter.voter_email, email.recipient)
    # the Message-ID of the email is recorded
    self.assertEqual(sorted(m.extra_headers['Message-ID'] for m in mail.outbox),
                     sorted(e.message_id for e in emails))
    self.assertTrue(all(e.message_id.startswith('<') for e in emails))

  def test_body_is_not_stored(self):
    batch = self.make_batch()
    self.send_batch(batch)
    password = self.voters[0].voter_password
    self.assertIn(password, ''.join(m.body for m in mail.outbox))
    for email in OutgoingEmail.objects.values():
      self.assertNotIn(password, repr(email))
    self.assertNotIn(password, repr(EmailBatch.objects.values().get()))

  def test_running_again_sends_only_missing_emails(self):
    batch = self.make_batch()
    self.send_batch(batch)
    self.assertEqual(len(mail.outbox), 3)
    # e.g. requeue_stuck_tasks after the worker died: nothing twice
    self.send_batch(batch)
    self.assertEqual(batch.emails.count(), 3)
    self.assertEqual(len(mail.outbox), 3)
    # a voter missing from the first run gets the email
    late = self.make_voter('late@example.net')
    self.send_batch(batch)
    self.assertEqual(batch.emails.count(), 4)
    self.assertEqual(len(mail.outbox), 4)
    self.assertEqual(mail.outbox[-1].to, ['"Voter late@example.net" <late@example.net>'])
    self.assertTrue(batch.emails.filter(voter=late, status=OutgoingEmail.SENT).exists())

  def test_task_run_twice_sends_once(self):
    batch = self.make_batch()
    email = helios_mail.new_email(OutgoingEmail.KIND_BULK, voter=self.voters[0], batch=batch)
    tasks.single_voter_email.call(self.voters[0].uuid, outgoing_email_id=email.id)
    tasks.single_voter_email.call(self.voters[0].uuid, outgoing_email_id=email.id)
    self.assertEqual(len(mail.outbox), 1)
    # the worker died while sending: sent again rather than lost
    email.refresh_from_db()
    email.status = OutgoingEmail.SENDING
    email.save()
    tasks.single_voter_email.call(self.voters[0].uuid, outgoing_email_id=email.id)
    self.assertEqual(len(mail.outbox), 2)
    email.refresh_from_db()
    self.assertEqual((email.status, email.attempts), (OutgoingEmail.SENT, 2))

  def test_once_per_batch_and_voter(self):
    from django.db import IntegrityError, transaction
    batch = self.make_batch()
    helios_mail.new_email(OutgoingEmail.KIND_BULK, voter=self.voters[0], batch=batch)
    with transaction.atomic(), self.assertRaises(IntegrityError):
      helios_mail.new_email(OutgoingEmail.KIND_BULK, voter=self.voters[0], batch=batch)
    # without a batch, any number
    helios_mail.new_email(OutgoingEmail.KIND_VOTE_CAST, voter=self.voters[0])
    helios_mail.new_email(OutgoingEmail.KIND_VOTE_CAST, voter=self.voters[0])

  def test_opted_out_voter_is_skipped(self):
    EmailOptOut.add_opt_out('voter1@example.net')
    batch = self.make_batch()
    self.send_batch(batch)
    self.assertEqual(len(mail.outbox), 2)
    email = batch.emails.get(voter=self.voters[1])
    self.assertEqual(email.status, OutgoingEmail.SKIPPED_OPTOUT)
    self.assertEqual(email.recipient, '')   # the address of an opted-out voter is not stored
    self.assertIsNone(email.sent_at)

  @override_settings(EMAIL_BACKEND='helios.tests_mail.FailingBackend')
  def test_mail_server_error_fails_the_email_not_the_task(self):
    batch = self.make_batch()
    tasks.voters_email.enqueue(election_id=self.election.id, batch_id=batch.id, language='en')
    run_tasks_allowing_failures()
    self.assertFalse(DBTaskResult.objects.failed().exists())
    self.assertEqual(DBTaskResult.objects.filter(task_path='helios.tasks.send_outgoing_email').count(), 3)
    for email in batch.emails.all():
      # 550: permanent, not tried again
      self.assertEqual(email.status, OutgoingEmail.FAILED)
      self.assertEqual(email.last_error_code, '550')
      self.assertIn('SMTPRecipientsRefused', email.last_error_text)
      self.assertIsNone(email.sent_at)
      self.assertEqual(email.message_id, '')

  def test_auth_system_without_get_email_address(self):
    def send_message(user_id, name, info, subject, body):
      mail.send_mail(subject, body, 'helios@example.org', [info['address']])
    legacy = SimpleNamespace(send_message=send_message)
    user = User.objects.create(user_type='legacy', user_id='u1', name='Lea Legacy', info={'address': 'lea@example.net'})
    voter = self.make_voter('lea', user=user)
    batch = self.make_batch()
    with mock.patch.dict(AUTH_SYSTEMS, {'legacy': legacy}):
      self.send_batch(batch, voter_constraints_include={'id': voter.id})
    self.assertEqual([m.to for m in mail.outbox], [['lea@example.net']])
    email = batch.emails.get()
    self.assertEqual((email.status, email.user), (OutgoingEmail.SENT, user))
    self.assertIsNotNone(email.sent_at)

  def test_no_address_is_recorded(self):
    user = User.objects.create(user_type='github', user_id='octo', name='Octo', info={})
    voter = self.make_voter('octo', user=user)
    batch = self.make_batch()
    self.send_batch(batch, voter_constraints_include={'id': voter.id})
    self.assertEqual(len(mail.outbox), 0)
    email = batch.emails.get()
    self.assertEqual(email.status, OutgoingEmail.SKIPPED_NOADDRESS)

  def test_older_queued_tasks(self):
    """tasks queued before EmailBatch existed pass the templates"""
    from helios.tests import run_queued_tasks
    tasks.voters_email.enqueue(self.election.id, 'email/simple_subject.txt', 'email/simple_body.txt',
                               {'custom_subject': 'x', 'custom_message': 'y'}, None, None, language='en')
    tasks.single_voter_email.enqueue(self.voters[0].uuid, 'email/simple_subject.txt', 'email/simple_body.txt',
                                     {'custom_subject': 'x', 'custom_message': 'y'}, language='en')
    run_queued_tasks()
    self.assertEqual(len(mail.outbox), 4)
    self.assertEqual(EmailBatch.objects.get().emails.count(), 3)
    self.assertEqual(OutgoingEmail.objects.filter(batch=None, status=OutgoingEmail.SENT).count(), 1)

  def test_vote_cast_confirmation(self):
    from helios.tests import run_queued_tasks
    voter = self.voters[0]
    cast_vote = CastVote.objects.create(voter=voter, vote_hash='HASH123', vote_tinyhash='tiny123')
    with mock.patch.object(CastVote, 'verify_and_store', return_value=True):
      tasks.cast_vote_verify_and_store.enqueue(cast_vote.id)
      run_queued_tasks()
    self.assertEqual(len(mail.outbox), 1)
    email = OutgoingEmail.objects.get()
    self.assertEqual((email.kind, email.status, email.voter, email.election, email.user),
                     (OutgoingEmail.KIND_VOTE_CAST, OutgoingEmail.SENT, voter, self.election, None))
    self.assertEqual(email.message_id, mail.outbox[0].extra_headers['Message-ID'])

  @override_settings(EMAIL_BACKEND='helios.tests_mail.FailingBackend')
  def test_vote_cast_confirmation_failure_keeps_the_vote(self):
    from helios.tests import run_queued_tasks
    cast_vote = CastVote.objects.create(voter=self.voters[0], vote_hash='HASH123', vote_tinyhash='tiny123')
    with mock.patch.object(CastVote, 'verify_and_store', return_value=True):
      tasks.cast_vote_verify_and_store.enqueue(cast_vote.id)
      run_queued_tasks()   # no task fails
    self.assertEqual(OutgoingEmail.objects.get().status, OutgoingEmail.FAILED)

  def test_vote_cast_confirmation_opted_out(self):
    from helios.tests import run_queued_tasks
    EmailOptOut.add_opt_out('voter0@example.net')
    cast_vote = CastVote.objects.create(voter=self.voters[0], vote_hash='HASH123', vote_tinyhash='tiny123')
    with mock.patch.object(CastVote, 'verify_and_store', return_value=True):
      tasks.cast_vote_verify_and_store.enqueue(cast_vote.id)
      run_queued_tasks()
    self.assertEqual(len(mail.outbox), 0)
    self.assertEqual(OutgoingEmail.objects.get().status, OutgoingEmail.SKIPPED_OPTOUT)

  def test_admin_information(self):
    from helios.tests import run_queued_tasks
    tasks.election_notify_admin.enqueue(election_id=self.election.id, subject='voter file processed', body='Done.')
    run_queued_tasks()
    self.assertEqual(mail.outbox[0].to, ['"Ada Admin" <admin@example.org>'])
    email = OutgoingEmail.objects.get()
    self.assertEqual((email.kind, email.status, email.user, email.election, email.voter),
                     (OutgoingEmail.KIND_ADMIN_INFO, OutgoingEmail.SENT, self.admin, self.election, None))
    self.assertEqual(email.recipient, '"Ada Admin" <admin@example.org>')

  @override_settings(EMAIL_BACKEND='helios.tests_mail.FailingBackend')
  def test_admin_information_failure_is_recorded(self):
    from helios.tests import run_queued_tasks
    tasks.election_notify_admin.call(election_id=self.election.id, subject='s', body='b')
    run_queued_tasks()
    email = OutgoingEmail.objects.get()
    self.assertEqual((email.status, email.last_error_code), (OutgoingEmail.FAILED, '550'))
    self.assertEqual(email.params, {})   # subject and body are not kept

  def test_subject_is_one_line(self):
    from helios.tests import run_queued_tasks
    helios_mail.queue(OutgoingEmail.KIND_ADMIN_INFO, user=self.admin, election=self.election,
                      params={'subject': 'first\nsecond', 'body': 'body'})
    run_queued_tasks()
    self.assertEqual(mail.outbox[0].subject, 'first')

  def test_error_code(self):
    self.assertEqual(helios_mail.error_code(smtplib.SMTPDataError(451, b'try later')), '451')
    self.assertEqual(helios_mail.error_code(smtplib.SMTPServerDisconnected('gone')), 'SMTPServerDisconnected')


class RequeueBulkMailingTests(TestCase):

  def _task_result(self, kwargs):
    return DBTaskResult.objects.create(
      status=TaskResultStatus.RUNNING, task_path='helios.tasks.voters_email', args_kwargs={'args': [], 'kwargs': kwargs},
      backend_name='default', run_after=timezone.now(), started_at=timezone.now() - datetime.timedelta(minutes=180))

  def test_bulk_mailing_with_batch_is_requeued(self):
    with_batch = self._task_result({'election_id': 1, 'batch_id': 7})
    older = self._task_result({'election_id': 1, 'subject_template': 's'})
    out = StringIO()
    call_command('requeue_stuck_tasks', stdout=out)
    with_batch.refresh_from_db()
    older.refresh_from_db()
    self.assertEqual(with_batch.status, TaskResultStatus.READY)
    # queued before batches: running it again would email every voter again
    self.assertEqual(older.status, TaskResultStatus.RUNNING)
    self.assertIn('not requeued: %s' % older.id, out.getvalue())
    self.assertIn('requeued 1 tasks', out.getvalue())
