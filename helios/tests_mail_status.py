"""
Tests for the "email delivery" page of an election (helios.email_views,
helios.mail_status): permissions, counts, the voters not reached and why, CSV,
sending a mailing again to them, correcting an address and lifting a
suppression; the global suppression list of site administrators; the summary
of a mailing for its administrator.
"""
import csv
import datetime
import io
import uuid

from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings
from django_tasks import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from helios import mail as helios_mail
from helios import mail_status
from helios.models import Election, EmailBatch, EmailOptOut, EmailSuppression, OutgoingEmail, Voter
from helios_auth.models import User


def run_tasks():
  from helios.tests import run_queued_tasks
  run_queued_tasks()


class ReasonTests(SimpleTestCase):

  def test_codes_in_words(self):
    def email(status, code):
      return OutgoingEmail(status=status, last_error_code=code)
    self.assertEqual(mail_status.reason(email(OutgoingEmail.BOUNCED_HARD, '5.1.1')), '5.1.1 recipient unknown')
    self.assertEqual(mail_status.reason(email(OutgoingEmail.FAILED, '550 5.1.1')), '5.1.1 recipient unknown')
    self.assertEqual(mail_status.reason(email(OutgoingEmail.BOUNCED_SOFT, '4.2.2')), '4.2.2 mailbox full')
    self.assertEqual(mail_status.reason(email(OutgoingEmail.BOUNCED_HARD, '5.9.9')), '5.9.9 permanent delivery failure')
    self.assertEqual(mail_status.reason(email(OutgoingEmail.FAILED, '550')), '550 permanent delivery failure')
    self.assertEqual(mail_status.reason(email(OutgoingEmail.BOUNCED_SOFT, 'unknown')), 'bounced, reason unknown')
    self.assertIn('too long', mail_status.reason(email(OutgoingEmail.FAILED, 'max_age')))
    self.assertIn('permanent bounce', mail_status.reason(email(OutgoingEmail.SKIPPED_SUPPRESSED, 'hard_bounce')))
    self.assertIn('spam', mail_status.reason(email(OutgoingEmail.SKIPPED_SUPPRESSED, 'complaint')))

  def test_german(self):
    from django.utils import translation
    with translation.override('de'):
      self.assertEqual(mail_status.reason(OutgoingEmail(status=OutgoingEmail.BOUNCED_HARD, last_error_code='5.1.1')),
                       '5.1.1 Empfänger unbekannt')


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', HELIOS_EMAIL_BATCH_SUMMARY=True)
class EmailStatusTestCase(TestCase):

  def setUp(self):
    self.admin = User.objects.create(user_type='password', user_id='admin@example.org', name='Ada Admin',
                                     info={'password': 'x', 'email': 'admin@example.org'})
    self.other = User.objects.create(user_type='password', user_id='other@example.org', name='Otto Other',
                                     info={'password': 'x'})
    self.election = Election.objects.create(admin=self.admin, uuid=str(uuid.uuid4()), short_name='board',
                                            name='Board election', election_type='election',
                                            frozen_at=datetime.datetime.utcnow())
    self.voters = [self.make_voter('voter%d@example.net' % i) for i in range(6)]
    self.batch = helios_mail.create_batch(self.election, 'email/vote_subject.txt', 'email/vote_body.txt',
                                          {'custom_subject': 'Board election', 'custom_message': 'Please vote.'},
                                          language='en', triggered_by=self.admin, voter_constraints=(None, None))

  def make_voter(self, address):
    return Voter.objects.create(uuid=str(uuid.uuid4()), election=self.election, voter_login_id=address,
                                voter_email=address, voter_name='Voter %s' % address.split('@')[0], voter_password='pw')

  def add_email(self, voter, status, code='', batch='mailing', kind=OutgoingEmail.KIND_BULK):
    return OutgoingEmail.objects.create(batch=self.batch if batch == 'mailing' else batch, election=self.election,
                                        voter=voter, kind=kind, status=status, last_error_code=code,
                                        recipient='"%s" <%s>' % (voter.voter_name, voter.voter_email))

  def mailing(self):
    """sent, sent, retrying, failed (5.1.1), bounced (4.2.2), suppressed"""
    v = self.voters
    self.add_email(v[0], OutgoingEmail.SENT)
    self.add_email(v[1], OutgoingEmail.SENT)
    self.add_email(v[2], OutgoingEmail.DEFERRED, '451 4.3.0')
    self.add_email(v[3], OutgoingEmail.FAILED, '550 5.1.1')
    self.add_email(v[4], OutgoingEmail.BOUNCED_SOFT, '4.2.2')
    EmailSuppression.suppress(v[5].voter_email, source=EmailSuppression.SOURCE_HARD_BOUNCE, status_code='5.1.1')
    self.add_email(v[5], OutgoingEmail.SKIPPED_SUPPRESSED, EmailSuppression.SOURCE_HARD_BOUNCE)
    # a confirmation outside of mailings
    self.add_email(v[0], OutgoingEmail.SENT, batch=None, kind=OutgoingEmail.KIND_VOTE_CAST)

  def login(self, user):
    self.client.get('/')
    session = self.client.session
    session['user'] = {'type': user.user_type, 'user_id': user.user_id}
    session['csrf_token'] = 'tok'
    session.save()

  def url(self, path=''):
    return '/helios/elections/%s/emails/%s' % (self.election.uuid, path)


class PermissionTests(EmailStatusTestCase):

  def test_only_election_administrators(self):
    voter = self.voters[0]
    pages = [('get', self.url(), {}), ('get', self.url('csv'), {}), ('get', self.url('voters/%s' % voter.uuid), {}),
             ('post', self.url('resend'), {'csrf_token': 'tok', 'batch_id': self.batch.id}),
             ('post', self.url('voters/%s' % voter.uuid), {'csrf_token': 'tok', 'email': 'x@example.net'}),
             ('post', self.url('voters/%s/lift' % voter.uuid), {'csrf_token': 'tok'})]
    for method, url, data in pages:
      with self.subTest(url=url, who='anonymous'):
        self.assertEqual(getattr(self.client, method)(url, data).status_code, 403)
    self.login(self.other)
    for method, url, data in pages:
      with self.subTest(url=url, who='other user'):
        self.assertEqual(getattr(self.client, method)(url, data).status_code, 403)
    voter.refresh_from_db()
    self.assertEqual(voter.voter_email, 'voter0@example.net')
    self.assertEqual(EmailBatch.objects.count(), 1)
    self.login(self.admin)
    self.assertEqual(self.client.get(self.url()).status_code, 200)

  def test_site_suppression_list_only_for_site_administrators(self):
    self.login(self.admin)
    self.assertEqual(self.client.get('/helios/stats/suppressions').status_code, 403)
    self.assertEqual(self.client.post('/helios/stats/suppressions', {'csrf_token': 'tok', 'action': 'lift_all'})
                     .status_code, 403)

  def test_links(self):
    self.login(self.admin)
    link = 'href="%s"' % self.url()
    self.assertContains(self.client.get('/helios/elections/%s/view' % self.election.uuid), link)
    self.assertContains(self.client.get('/helios/elections/%s/voters/list' % self.election.uuid), link)
    self.login(self.other)
    self.assertNotContains(self.client.get('/helios/elections/%s/view' % self.election.uuid), link)


class StatusPageTests(EmailStatusTestCase):

  def test_counts(self):
    self.mailing()
    self.login(self.admin)
    response = self.client.get(self.url())
    (batch, counts, sendable, suppressed), = mail_status.batches(self.election)
    self.assertEqual({k: counts[k] for k in ('total', 'sent', 'waiting', 'retrying', 'failed', 'bounced', 'skipped')},
                     {'total': 6, 'sent': 2, 'waiting': 0, 'retrying': 1, 'failed': 1, 'bounced': 1, 'skipped': 1})
    self.assertEqual(counts['skipped_by'], [('address suppressed', 1)])
    self.assertEqual((sendable, suppressed), ([self.voters[3].id, self.voters[4].id], [self.voters[5].id]))
    overall = response.context['overall']
    self.assertEqual((overall['total'], overall['sent']), (7, 3))
    self.assertContains(response, 'Time to Vote: &quot;Board election&quot;')
    self.assertContains(response, 'send again to 2 voters')

  def test_voters_not_reached(self):
    self.mailing()
    self.login(self.admin)
    response = self.client.get(self.url())
    problems = response.context['problems']
    self.assertEqual([p.voter for p in problems], [self.voters[3], self.voters[4], self.voters[5]])
    self.assertEqual([p.reason for p in problems],
                     ['5.1.1 recipient unknown', '4.2.2 mailbox full', 'address suppressed after a permanent bounce'])
    self.assertEqual([bool(p.suppression) for p in problems], [False, False, True])
    self.assertContains(response, 'voter3@example.net')
    self.assertContains(response, 'status-badge status-failed')

  def test_csv(self):
    self.mailing()
    self.login(self.admin)
    response = self.client.get(self.url('csv'))
    self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
    rows = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
    self.assertEqual(rows[0][:3], ['Voter ID', 'Name', 'Email Address'])
    self.assertEqual(len(rows), 4)
    self.assertEqual(rows[1][0], 'voter3@example.net')
    self.assertEqual(rows[1][5:8], ['failed', '550 5.1.1', '5.1.1 recipient unknown'])
    self.assertEqual(rows[3][8], 'yes')

  def test_without_bounce_handling_says_so(self):
    self.login(self.admin)
    self.assertContains(self.client.get(self.url()), 'Bounces are not processed on this site')


class ResendTests(EmailStatusTestCase):

  def test_resend_only_to_voters_not_reached(self):
    self.mailing()
    self.login(self.admin)
    response = self.client.post(self.url('resend'), {'csrf_token': 'tok', 'batch_id': self.batch.id})
    self.assertTrue(response['Location'].endswith(self.url() + '?done=resent&n=2'), response['Location'])
    resend = EmailBatch.objects.get(resend_of=self.batch)
    self.assertEqual((resend.body_template, resend.triggered_by), (self.batch.body_template, self.admin))
    run_tasks()
    # not to the suppressed address, nor to those reached or still being retried
    self.assertEqual(sorted(resend.emails.values_list('voter_id', flat=True)), [self.voters[3].id, self.voters[4].id])
    self.assertEqual(sorted(m.to[0] for m in mail.outbox),
                     ['"Voter voter3" <voter3@example.net>', '"Voter voter4" <voter4@example.net>'])
    # they are reached now: nobody left to send it to
    problems = self.client.get(self.url()).context['problems']
    self.assertEqual([p.voter for p in problems], [self.voters[5]])
    self.client.post(self.url('resend'), {'csrf_token': 'tok', 'batch_id': self.batch.id})
    self.assertEqual(EmailBatch.objects.filter(resend_of=self.batch).count(), 1)
    # the resend is shown as such and has no resend of its own
    self.assertContains(self.client.get(self.url()), 'sent again to the voters not reached')

  def test_resend_keeps_the_choice_of_voters(self):
    reminder = helios_mail.create_batch(self.election, 'email/vote_subject.txt', 'email/vote_body.txt', {},
                                        language='en', voter_constraints=({'vote_hash': None}, None))
    self.add_email(self.voters[3], OutgoingEmail.FAILED, '550 5.1.1', batch=reminder)
    self.add_email(self.voters[4], OutgoingEmail.FAILED, '550 5.1.1', batch=reminder)
    Voter.objects.filter(id=self.voters[4].id).update(vote_hash='abc')   # has voted since
    new = mail_status.resend(reminder, self.admin)
    self.assertEqual(new.voter_constraints['include'], {'vote_hash': None, 'id__in': [self.voters[3].id, self.voters[4].id]})
    run_tasks()
    self.assertEqual(list(new.emails.values_list('voter_id', flat=True)), [self.voters[3].id])

  def test_no_resend_of_an_invitation_after_voting_closed(self):
    self.mailing()
    Election.objects.filter(id=self.election.id).update(voting_ended_at=datetime.datetime.utcnow())
    self.login(self.admin)
    self.assertContains(self.client.get(self.url()), 'Voting has closed')
    self.assertEqual(self.client.post(self.url('resend'), {'csrf_token': 'tok', 'batch_id': self.batch.id})
                     .status_code, 403)

  def test_other_elections_batch(self):
    other = Election.objects.create(admin=self.other, uuid=str(uuid.uuid4()), short_name='other', name='Other',
                                    election_type='election', frozen_at=datetime.datetime.utcnow())
    batch = helios_mail.create_batch(other, 'email/vote_subject.txt', 'email/vote_body.txt', {}, language='en')
    self.login(self.admin)
    self.assertEqual(self.client.post(self.url('resend'), {'csrf_token': 'tok', 'batch_id': batch.id}).status_code, 404)


class VoterAddressTests(EmailStatusTestCase):

  def test_correct_address_lifts_its_suppression(self):
    self.mailing()
    voter = self.voters[5]
    EmailSuppression.suppress('voter5-new@example.net', source=EmailSuppression.SOURCE_SOFT_BOUNCES)
    self.login(self.admin)
    page = self.client.get(self.url('voters/%s' % voter.uuid))
    self.assertContains(page, 'address suppressed after a permanent bounce')
    response = self.client.post(self.url('voters/%s' % voter.uuid), {'csrf_token': 'tok', 'email': 'voter5-new@example.net'})
    self.assertTrue(response['Location'].endswith(self.url('voters/%s?done=changed' % voter.uuid)))
    voter.refresh_from_db()
    self.assertEqual((voter.voter_email, voter.voter_login_id), ('voter5-new@example.net', 'voter5@example.net'))
    self.assertFalse(EmailSuppression.is_suppressed('voter5-new@example.net'))
    self.assertTrue(EmailSuppression.is_suppressed('voter5@example.net'))   # the old one stays
    # recorded with the suppression and in the election's log, the addresses masked
    lifted = EmailSuppression.objects.get(lifted_at__isnull=False)
    self.assertEqual((lifted.lifted_by, lifted.lifted_in), (self.admin, self.election))
    entry = self.election.get_log().first().log
    self.assertIn('v***@example.net', entry)
    self.assertIn('suppression of the new address lifted', entry)
    self.assertIn('admin@example.org', entry)
    self.assertNotIn('voter5', entry)
    # the mailing can now be sent again to the voter
    self.assertEqual(mail_status.resend_voters(self.batch), ([self.voters[3].id, self.voters[4].id, voter.id], []))

  def test_invalid_or_opted_out_address(self):
    voter = self.voters[3]
    EmailOptOut.add_opt_out('optedout@example.net')
    self.login(self.admin)
    for address, message in (('not an address', 'valid email address'), ('optedout@example.net', 'opted out')):
      response = self.client.post(self.url('voters/%s' % voter.uuid), {'csrf_token': 'tok', 'email': address})
      self.assertContains(response, message)
    voter.refresh_from_db()
    self.assertEqual(voter.voter_email, 'voter3@example.net')

  def test_not_after_tallying_started(self):
    Election.objects.filter(id=self.election.id).update(tallying_started_at=datetime.datetime.utcnow())
    voter = self.voters[3]
    self.login(self.admin)
    self.assertContains(self.client.get(self.url('voters/%s' % voter.uuid)), 'Voters can no longer be changed')
    self.assertEqual(self.client.post(self.url('voters/%s' % voter.uuid), {'csrf_token': 'tok', 'email': 'new@example.net'})
                     .status_code, 403)

  def test_not_for_voters_with_an_account(self):
    user = User.objects.create(user_type='google', user_id='sso@example.net', name='Sso User',
                               info={'email': 'sso@example.net'})
    voter = Voter.objects.create(uuid=str(uuid.uuid4()), election=self.election, user=user,
                                 voter_login_id='sso@example.net', voter_email='sso@example.net')
    self.login(self.admin)
    self.assertContains(self.client.get(self.url('voters/%s' % voter.uuid)), 'login system')
    self.assertEqual(self.client.post(self.url('voters/%s' % voter.uuid), {'csrf_token': 'tok', 'email': 'new@example.net'})
                     .status_code, 403)

  def test_lift(self):
    voter = self.voters[5]
    EmailSuppression.suppress(voter.voter_email, source=EmailSuppression.SOURCE_COMPLAINT)
    self.login(self.admin)
    self.client.post(self.url('voters/%s/lift' % voter.uuid), {'csrf_token': 'tok'})
    suppression = EmailSuppression.objects.get()
    self.assertEqual((suppression.lifted_by, suppression.lifted_in, bool(suppression.lifted_at)),
                     (self.admin, self.election, True))
    entry = self.election.get_log().first().log
    self.assertEqual(entry, 'Email suppression of v***@example.net (voter %s) lifted by admin@example.org (password)'
                     % voter.uuid)
    # lifted for all elections: the next email goes out
    batch = helios_mail.create_batch(self.election, 'email/simple_subject.txt', 'email/simple_body.txt', {},
                                     language='en')
    email = helios_mail.queue(OutgoingEmail.KIND_BULK, voter=voter, batch=batch)
    run_tasks()
    email.refresh_from_db()
    self.assertEqual(email.status, OutgoingEmail.SENT)


class SiteSuppressionTests(EmailStatusTestCase):

  def setUp(self):
    super().setUp()
    self.admin.admin_p = True
    self.admin.save()
    self.login(self.admin)

  def test_lookup_lift_suppress_and_lift_all(self):
    with self.assertLogs('helios.suppression', 'INFO') as logs:
      self._lookup_lift_suppress_and_lift_all()
    self.assertEqual(logs.output, [
      'INFO:helios.suppression:suppression of a***@example.net lifted by site administrator admin@example.org (password)',
      'INFO:helios.suppression:c***@example.net suppressed by site administrator admin@example.org (password)',
      'WARNING:helios.suppression:all 2 email suppressions lifted by site administrator admin@example.org (password)'])
    manual = EmailSuppression.objects.get(source=EmailSuppression.SOURCE_MANUAL)
    self.assertEqual((manual.created_by, manual.lifted_by, manual.lifted_in), (self.admin, self.admin, None))

  def _lookup_lift_suppress_and_lift_all(self):
    EmailSuppression.suppress('a@example.net', source=EmailSuppression.SOURCE_HARD_BOUNCE, status_code='5.1.1')
    EmailSuppression.suppress('b@example.net', source=EmailSuppression.SOURCE_COMPLAINT)
    page = self.client.get('/helios/stats/suppressions?email=A@example.net')
    self.assertContains(page, '2 suppressed addresses')
    self.assertContains(page, 'is suppressed since')
    self.assertContains(page, '(5.1.1)')
    self.client.post('/helios/stats/suppressions', {'csrf_token': 'tok', 'action': 'lift', 'email': 'a@example.net'})
    self.assertFalse(EmailSuppression.is_suppressed('a@example.net'))
    self.assertContains(self.client.get('/helios/stats/suppressions?email=a@example.net'),
                        'Its suppression was lifted on')
    self.client.post('/helios/stats/suppressions', {'csrf_token': 'tok', 'action': 'suppress', 'email': 'c@example.net'})
    self.assertEqual(EmailSuppression.for_address('c@example.net').source, EmailSuppression.SOURCE_MANUAL)
    self.client.post('/helios/stats/suppressions', {'csrf_token': 'tok', 'action': 'lift_all'})
    self.assertFalse(EmailSuppression.objects.filter(lifted_at__isnull=True).exists())
    self.assertContains(self.client.get('/helios/stats/'), 'href="/helios/stats/suppressions"')


class BatchSummaryTests(EmailStatusTestCase):

  def summary_tasks(self):
    return DBTaskResult.objects.filter(task_path='helios.tasks.email_batch_summary', status=TaskResultStatus.READY)

  def test_summary_once_the_mailing_is_sent(self):
    self.mailing()
    helios_mail.batch_summary(self.batch.id)
    # one email is still being retried: checked again later
    self.assertFalse(OutgoingEmail.objects.filter(kind=OutgoingEmail.KIND_BATCH_SUMMARY).exists())
    self.assertEqual(self.summary_tasks().count(), 1)
    OutgoingEmail.objects.filter(status=OutgoingEmail.DEFERRED).update(status=OutgoingEmail.SENT)
    helios_mail.batch_summary(self.batch.id)
    helios_mail.batch_summary(self.batch.id)   # only once
    DBTaskResult.objects.filter(task_path='helios.tasks.email_batch_summary').delete()
    run_tasks()
    summary = OutgoingEmail.objects.get(kind=OutgoingEmail.KIND_BATCH_SUMMARY)
    self.assertEqual((summary.status, summary.user, summary.batch), (OutgoingEmail.SENT, self.admin, None))
    message, = mail.outbox
    self.assertEqual(message.to, ['"Ada Admin" <admin@example.org>'])
    self.assertIn('Mailing sent: Board election', message.subject)
    self.assertIn('sent: 3', message.body)
    self.assertIn('failed: 1', message.body)
    self.assertIn('bounced so far: 1', message.body)
    self.assertIn(self.url(), message.body)
    # not counted among the emails to voters
    self.assertEqual(mail_status.overall(self.election)['total'], 7)

  def test_scheduled_by_the_email_voters_page(self):
    self.login(self.admin)
    self.client.post('/helios/elections/%s/voters/email?template=simple' % self.election.uuid,
                     {'csrf_token': 'tok', 'subject': 'Hello', 'body': 'Hi', 'send_to': 'all'})
    self.assertEqual(self.summary_tasks().count(), 1)
    with override_settings(HELIOS_EMAIL_BATCH_SUMMARY=False):
      self.client.post('/helios/elections/%s/voters/email?template=simple' % self.election.uuid,
                       {'csrf_token': 'tok', 'subject': 'Hello', 'body': 'Hi', 'send_to': 'all'})
    self.assertEqual(self.summary_tasks().count(), 1)

  def test_off(self):
    self.mailing()
    OutgoingEmail.objects.filter(status=OutgoingEmail.DEFERRED).update(status=OutgoingEmail.SENT)
    with override_settings(HELIOS_EMAIL_BATCH_SUMMARY=False):
      helios_mail.batch_summary(self.batch.id)
    self.assertFalse(OutgoingEmail.objects.filter(kind=OutgoingEmail.KIND_BATCH_SUMMARY).exists())
