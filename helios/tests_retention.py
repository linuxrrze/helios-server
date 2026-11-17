"""
Deleting elections: election administrators can no longer delete an election
once a ballot has been cast (only archive it), site administrators still can
(logged); and `manage.py purge_elections`, which deletes elections for good
after HELIOS_ELECTION_RETENTION_DAYS (helios.retention).
"""

import datetime
import io
import os
import shutil
import tempfile
import uuid

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from helios import retention
from helios.models import (AuditedBallot, BounceEvent, CastVote, Election, ElectionLog, EmailBatch, EmailSuppression,
                           OutgoingEmail, Trustee, Voter, VoterFile)
from helios.tests_auto_close import ElectionSetup
from helios_auth.models import User

LOCK_MESSAGE = 'This election has ballots and can no longer be deleted; you can archive it.'


def days_ago(days):
  return datetime.datetime.utcnow() - datetime.timedelta(days=days)


class DeleteSetup(ElectionSetup):

  def login(self, user):
    session = self.client.session
    session['user'] = {'type': user.user_type, 'user_id': user.user_id}
    session.save()

  def site_admin(self):
    return User.objects.create(user_type='password', user_id='site@example.org', name='Sam Site', admin_p=True,
                               info={'password': 'x', 'email': 'site@example.org'})

  def archived(self, election):
    Election.objects.filter(pk=election.pk).update(archived_at=datetime.datetime.utcnow())
    return self.reload(election)

  def delete(self, election):
    return self.client.post('/helios/elections/%s/delete' % election.uuid, {'csrf_token': 'tok', 'delete_p': '1'})

  def page(self, election):
    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    self.assertEqual(response.status_code, 200)
    return response.content.decode()

  def deleted(self, election):
    return Election.objects_with_deleted.get(pk=election.pk).deleted_at is not None


class DeleteLockTests(DeleteSetup, TestCase):

  def test_delete_allowed_without_ballots(self):
    election = self.archived(self.make_election())
    self.assertFalse(election.has_cast_ballots)
    self.assertTrue(election.can_be_deleted_by(self.admin))
    self.assertIn('action="/helios/elections/%s/delete"' % election.uuid, self.page(election))

    response = self.delete(election)
    self.assertEqual(response.status_code, 302)
    self.assertTrue(self.deleted(election))
    self.assertTrue(ElectionLog.objects.filter(election=election, log=ElectionLog.DELETED).exists())

  def test_delete_refused_with_ballots(self):
    election = self.make_election()
    self.cast(self.voters(election)[0], run=False)
    election = self.archived(election)
    self.assertTrue(election.has_cast_ballots)
    self.assertFalse(election.can_be_deleted_by(self.admin))

    response = self.delete(election)
    self.assertEqual(response.status_code, 403)
    self.assertIn(LOCK_MESSAGE, response.content.decode())
    self.assertFalse(self.deleted(election))
    self.assertFalse(ElectionLog.objects.filter(election=election, log__startswith='deleted').exists())

  def test_button_hidden_with_ballots(self):
    election = self.make_election()
    self.cast(self.voters(election)[0], run=False)
    html = self.page(election)
    self.assertNotIn('/delete"', html)
    self.assertIn(LOCK_MESSAGE, html)
    self.assertNotIn('an archived election can be deleted', html)

    html = self.page(self.archived(election))
    self.assertNotIn('/delete"', html)
    self.assertIn('This election has ballots and can no longer be deleted.', html)
    # archiving stays possible
    self.assertIn('unarchive it', html)

  def test_unverified_ballot_counts(self):
    election = self.make_election()
    cast_vote = self.cast(self.voters(election)[0], run=False)
    self.assertIsNone(CastVote.objects.get(pk=cast_vote.pk).verified_at)
    self.assertTrue(election.has_cast_ballots)

  def test_other_users_cannot_delete(self):
    election = self.archived(self.make_election())
    other = User.objects.create(user_type='password', user_id='other@example.org', name='O',
                                info={'password': 'x', 'email': 'other@example.org'})
    self.assertFalse(election.can_be_deleted_by(other))
    self.assertFalse(election.can_be_deleted_by(None))

  def test_site_admin_can_delete_with_ballots(self):
    election = self.make_election()
    self.cast(self.voters(election)[0], run=False)
    election = self.archived(election)
    site_admin = self.site_admin()
    self.assertTrue(election.can_be_deleted_by(site_admin))

    self.login(site_admin)
    html = self.page(election)
    self.assertIn('action="/helios/elections/%s/delete"' % election.uuid, html)
    self.assertIn('Ballots have been cast in this election.', html)

    with self.assertLogs('helios.deletion', 'WARNING') as logs:
      response = self.delete(election)
    self.assertEqual(response.status_code, 302)
    self.assertTrue(self.deleted(election))
    self.assertTrue(ElectionLog.objects.filter(election=election, log=ElectionLog.DELETED_WITH_BALLOTS).exists())
    self.assertFalse(ElectionLog.objects.filter(election=election, log=ElectionLog.DELETED).exists())
    self.assertIn(election.uuid, logs.output[0])
    self.assertIn('site@example.org', logs.output[0])
    self.assertIn('1 ballots', logs.output[0])

    # translated in the log shown on the election page
    entry = ElectionLog.objects.get(election=election, log=ElectionLog.DELETED_WITH_BALLOTS)
    self.assertEqual(entry.message, 'Deleted by a site administrator although ballots had been cast')

    # and can restore it, as before
    response = self.client.post('/helios/stats/deleted-elections/%s/undelete' % election.uuid, {'csrf_token': 'tok'})
    self.assertEqual(response.status_code, 302)
    self.assertFalse(self.deleted(election))


@override_settings(HELIOS_ELECTION_RETENTION_DAYS=0)
class PurgeTests(DeleteSetup, TestCase):

  def setUp(self):
    super().setUp()
    self.media = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)

  def run_purge(self, *args):
    out = io.StringIO()
    with self.captureOnCommitCallbacks(execute=True):
      call_command('purge_elections', *args, stdout=out)
    return out.getvalue()

  def exists(self, election):
    return Election.objects_with_deleted.filter(pk=election.pk).exists()

  def set(self, election, **fields):
    Election.objects_with_deleted.filter(pk=election.pk).update(**fields)
    return Election.objects_with_deleted.get(pk=election.pk)

  def ended(self, days, **fields):
    """an election whose voting ended DAYS ago (frozen and created before that)"""
    election = self.make_election()
    return self.set(election, created_at=days_ago(days + 10), modified_at=days_ago(days + 10),
                    frozen_at=days_ago(days + 5), voting_ends_at=days_ago(days), **fields)

  def never_opened(self, days):
    election = self.make_election(freeze=False)
    # adding the trustee was logged
    ElectionLog.objects.filter(election=election).update(at=days_ago(days))
    return self.set(election, created_at=days_ago(days), modified_at=days_ago(days))

  # the setting

  def test_off_by_default(self):
    self.assertEqual(retention.retention_days(), 0)
    old = self.ended(1000)
    with self.assertRaisesMessage(CommandError, 'HELIOS_ELECTION_RETENTION_DAYS=0'):
      self.run_purge('--yes')
    self.assertTrue(self.exists(old))
    self.assertIsNone(retention.purge_date(old))

  def test_days_override_when_off(self):
    old, recent = self.ended(100), self.ended(10)
    out = self.run_purge('--days', '30', '--yes')
    self.assertFalse(self.exists(old))
    self.assertTrue(self.exists(recent))
    self.assertIn('1 election(s) deleted (retention period 30 days)', out)

  def test_days_must_be_positive(self):
    with self.assertRaises(CommandError):
      self.run_purge('--days', '0')

  @override_settings(HELIOS_ELECTION_RETENTION_DAYS=365)
  def test_days_override_setting(self):
    election = self.ended(100)
    self.run_purge('--yes')
    self.assertTrue(self.exists(election))
    self.run_purge('--days', '50', '--yes')
    self.assertFalse(self.exists(election))

  # dry run and --yes

  @override_settings(HELIOS_ELECTION_RETENTION_DAYS=30)
  def test_dry_run_deletes_nothing(self):
    old = self.ended(100)
    self.cast(self.voters(old)[0], run=False)
    out = self.run_purge()
    self.assertTrue(self.exists(old))
    self.assertIn(old.uuid, out)
    self.assertIn(old.short_name, out)
    self.assertIn('2 voters  1 ballots', out)
    self.assertIn('1 election(s) would be deleted', out)
    # no personal data
    self.assertNotIn('@example.org', out)
    self.assertNotIn('Voter a', out)

  @override_settings(HELIOS_ELECTION_RETENTION_DAYS=30)
  def test_yes_deletes_only_old_ones(self):
    old, recent = self.ended(31), self.ended(29)
    with self.assertLogs('helios.retention', 'INFO') as logs:
      out = self.run_purge('--yes')
    self.assertFalse(self.exists(old))
    self.assertTrue(self.exists(recent))
    self.assertIn(old.uuid, out)
    self.assertNotIn(recent.uuid, out)
    self.assertEqual(len(logs.output), 1)
    self.assertIn(old.uuid, logs.output[0])
    self.assertIn('2 voters', logs.output[0])
    self.assertNotIn('@example.org', logs.output[0])

  # reference date

  def test_reference_date_is_the_latest(self):
    election = self.ended(100)
    self.assertEqual(retention.reference_date(election), election.voting_ends_at)
    # closed early by hand: when it was closed
    election = self.set(election, voting_ended_at=days_ago(120))
    self.assertEqual(retention.reference_date(election), election.frozen_at)
    election = self.set(election, voting_ended_at=None, voting_extended_until=days_ago(90))
    self.assertEqual(retention.reference_date(election), election.voting_extended_until)
    election = self.set(election, result_released_at=days_ago(80))
    self.assertEqual(retention.reference_date(election), election.result_released_at)
    election = self.set(election, archived_at=days_ago(70))
    self.assertEqual(retention.reference_date(election), election.archived_at)
    election = self.set(election, deleted_at=days_ago(60))
    self.assertEqual(retention.reference_date(election), election.deleted_at)
    self.assertEqual(retention.purge_date(election, 30), election.deleted_at + datetime.timedelta(days=30))

  def test_open_voting_never_purged(self):
    # frozen without an end date, or with an end in the future
    no_end = self.set(self.make_election(), created_at=days_ago(1000), frozen_at=days_ago(1000))
    future_end = self.set(self.make_election(), created_at=days_ago(1000), frozen_at=days_ago(1000),
                          voting_ends_at=days_ago(-1), archived_at=days_ago(900))
    # extended past today
    extended = self.ended(500, voting_extended_until=days_ago(-1))
    for election in (no_end, future_end, extended):
      self.assertIsNone(retention.reference_date(election))
      self.assertEqual(retention.never_purged_reason(election), 'voting open')
    # also when deleted
    self.set(no_end, deleted_at=days_ago(900))
    self.run_purge('--days', '1', '--yes')
    for election in (no_end, future_end, extended):
      self.assertTrue(self.exists(election))

  def test_tally_in_progress_never_purged(self):
    election = self.ended(500, tallying_started_at=days_ago(499))
    self.assertEqual(retention.never_purged_reason(election), 'tally in progress')
    self.run_purge('--days', '1', '--yes')
    self.assertTrue(self.exists(election))
    # with a result it is
    self.set(election, result=[[1, 0]], tallying_finished_at=days_ago(498))
    self.run_purge('--days', '1', '--yes')
    self.assertFalse(self.exists(election))

  def test_soft_deleted_purged(self):
    deleted = self.ended(500, deleted_at=days_ago(100))
    recently_deleted = self.ended(500, deleted_at=days_ago(10))
    self.run_purge('--days', '30', '--yes')
    self.assertFalse(self.exists(deleted))
    self.assertTrue(self.exists(recently_deleted))

  def test_never_opened_by_last_change(self):
    old = self.never_opened(100)
    # created long ago, but changed recently
    edited = self.never_opened(100)
    edited.name = 'renamed'
    edited.save()
    logged = self.never_opened(100)
    logged.append_log('Administrator added')
    uploaded = self.never_opened(100)
    VoterFile.objects.create(election=uploaded, voter_file_content='password,x,x@example.org,X')
    # scheduled end in the past does not count before it was opened
    scheduled = self.never_opened(100)
    self.set(scheduled, voting_ends_at=days_ago(1))

    self.assertEqual(retention.reference_date(old), old.modified_at)
    self.run_purge('--days', '30', '--yes')
    self.assertFalse(self.exists(old))
    self.assertFalse(self.exists(scheduled))
    for election in (edited, logged, uploaded):
      self.assertTrue(self.exists(election))

  def test_modified_at_set_on_save(self):
    election = self.never_opened(100)
    self.assertLess(election.modified_at, days_ago(99))
    election.save()
    self.assertGreater(Election.objects.get(pk=election.pk).modified_at, days_ago(1))

  # what goes, what stays

  def test_related_rows_deleted_suppressions_kept(self):
    election = self.ended(100, result_released_at=days_ago(100))
    voter = self.voters(election)[0]
    self.cast(voter, run=False)
    AuditedBallot.objects.create(election=election, raw_vote='{}', vote_hash='h')
    VoterFile.objects.create(election=election, voter_file_content='password,x,x@example.org,X')
    batch = EmailBatch.objects.create(election=election, subject_template='s', body_template='b')
    email = OutgoingEmail.objects.create(batch=batch, election=election, voter=voter, kind=OutgoingEmail.KIND_BULK,
                                         recipient='a@example.org')
    trustee_email = OutgoingEmail.objects.create(election=election, trustee=election.trustee_set.first(),
                                                 kind=OutgoingEmail.KIND_TRUSTEE)
    BounceEvent.objects.create(outgoing_email=email, email_hash='e' * 64, bounce_type=BounceEvent.HARD)
    suppression = EmailSuppression.objects.create(email_hash='e' * 64, source=EmailSuppression.SOURCE_HARD_BOUNCE,
                                                  created_at=days_ago(100), lifted_at=days_ago(99),
                                                  lifted_by=self.admin, lifted_in=election)
    other = EmailSuppression.objects.create(email_hash='f' * 64, source=EmailSuppression.SOURCE_MANUAL,
                                            created_at=days_ago(100))
    keep = self.ended(10)
    election.admins.add(User.objects.create(user_type='password', user_id='co@example.org', name='Co',
                                            info={'password': 'x', 'email': 'co@example.org'}))

    self.run_purge('--days', '30', '--yes')

    self.assertFalse(self.exists(election))
    for model, field in ((Voter, 'election_id'), (CastVote, 'voter__election_id'), (AuditedBallot, 'election_id'),
                         (Trustee, 'election_id'), (ElectionLog, 'election_id'), (VoterFile, 'election_id'),
                         (EmailBatch, 'election_id'), (OutgoingEmail, 'election_id'),
                         (BounceEvent, 'outgoing_email__election_id')):
      self.assertFalse(model.objects.filter(**{field: election.pk}).exists(), model.__name__)
    self.assertFalse(OutgoingEmail.objects.filter(pk__in=[email.pk, trustee_email.pk]).exists())
    self.assertFalse(BounceEvent.objects.exists())
    self.assertFalse(Election.admins.through.objects.filter(election_id=election.pk).exists())
    # the suppression list stays, without the election
    suppression.refresh_from_db()
    self.assertIsNone(suppression.lifted_in)
    self.assertEqual(suppression.lifted_by, self.admin)
    self.assertTrue(EmailSuppression.objects.filter(pk=other.pk).exists())
    # the users and the other election stay
    self.assertTrue(User.objects.filter(user_id='co@example.org').exists())
    self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())
    self.assertTrue(self.exists(keep))
    self.assertEqual(keep.voter_set.count(), 2)

  def test_voter_file_in_storage_deleted(self):
    election = self.ended(100)
    with self.settings(MEDIA_ROOT=self.media):
      voter_file = VoterFile(election=election)
      voter_file.voter_file.save('voters.csv', ContentFile(b'password,x,x@example.org,X\n'))
      path = voter_file.voter_file.path
      self.assertTrue(os.path.exists(path))
      self.run_purge('--days', '30', '--yes')
    self.assertFalse(self.exists(election))
    self.assertFalse(os.path.exists(path))

  def test_purge_rechecks(self):
    election = self.ended(100)
    found = retention.candidates(30)
    self.assertEqual([e.pk for e, _ in found], [election.pk])
    # voting extended in the meantime
    self.set(election, voting_extended_until=days_ago(-1))
    self.assertIsNone(retention.purge(election, days=30))
    self.assertTrue(self.exists(election))

  # shown to site administrators

  @override_settings(HELIOS_ELECTION_RETENTION_DAYS=30)
  def test_purge_date_shown_to_site_admins(self):
    election = self.ended(10)
    html = self.page(election)
    self.assertNotIn('deleted permanently', html)

    self.login(self.site_admin())
    html = self.page(election)
    self.assertIn('This election will be deleted permanently after', html)
    self.assertIn('retention period of 30 days', html)

    election = self.set(self.make_election(), frozen_at=days_ago(1))
    html = self.page(election)
    self.assertIn('This election will be deleted permanently 30 days after voting has ended', html)

  @override_settings(HELIOS_ELECTION_RETENTION_DAYS=30)
  def test_purge_date_in_deleted_elections(self):
    election = self.ended(10, deleted_at=days_ago(5))
    self.login(self.site_admin())
    response = self.client.get('/helios/stats/deleted-elections')
    self.assertContains(response, 'Permanent deletion after:')
    expected = (election.deleted_at + datetime.timedelta(days=30)).strftime('%Y-%m-%dT%H:%MZ')
    self.assertContains(response, expected)


class StaleSessionTests(DeleteSetup, TestCase):
  """ids kept in the session whose rows are gone (a removed voter or trustee, an election deleted for good)"""

  def voter_session(self, voter_id):
    """a password voter's session (no user logged in)"""
    session = self.client.session
    session.pop('user', None)
    session['CURRENT_VOTER_ID'] = voter_id
    session.save()

  def test_removed_voter(self):
    election = self.make_election(freeze=False)
    voter = self.voters(election)[0]
    self.voter_session(voter.id)
    voter.delete()
    self.page(election)
    self.assertNotIn('CURRENT_VOTER_ID', self.client.session)

  def test_voter_of_purged_election(self):
    purged, other = self.make_election(freeze=False), self.make_election(freeze=False)
    self.voter_session(self.voters(purged)[0].id)
    Election.objects.filter(pk=purged.pk).delete()
    self.page(other)
    self.assertNotIn('CURRENT_VOTER_ID', self.client.session)

  def test_voter_of_other_election_kept(self):
    first, other = self.make_election(freeze=False), self.make_election(freeze=False)
    self.voter_session(self.voters(first)[0].id)
    self.page(other)
    self.assertIn('CURRENT_VOTER_ID', self.client.session)

  def test_removed_trustee(self):
    election = self.make_election(freeze=False)
    Election.objects.filter(pk=election.pk).update(private_p=True)
    trustee = election.trustee_set.first()
    session = self.client.session
    session['helios_trustee_uuid'] = trustee.uuid
    del session['user']
    session.save()
    trustee.delete()
    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    # not logged in: on to the voter login of the private election
    self.assertEqual(response.status_code, 302)
    self.assertNotIn('helios_trustee_uuid', self.client.session)

  def test_voter_upload_gone(self):
    from django_tasks_db.models import DBTaskResult
    election = self.make_election(freeze=False)
    voter_file = VoterFile.objects.create(election=election, voter_file_content='password,x,x@example.org,X')
    for path, data in (('upload-cancel', {}), ('upload', {'confirm_p': '1'})):
      session = self.client.session
      session['voter_file_id'] = voter_file.id + 1000
      session.save()
      response = self.client.post('/helios/elections/%s/voters/%s' % (election.uuid, path),
                                  dict(data, csrf_token='tok'))
      self.assertEqual(response.status_code, 302, path)
      self.assertNotIn('voter_file_id', self.client.session)
    self.assertFalse(DBTaskResult.objects.filter(task_path='helios.tasks.voter_file_process').exists())
    self.assertTrue(VoterFile.objects.filter(pk=voter_file.pk).exists())

  def test_voter_upload_of_other_election(self):
    election, other = self.make_election(freeze=False), self.make_election(freeze=False)
    voter_file = VoterFile.objects.create(election=other, voter_file_content='password,x,x@example.org,X')
    session = self.client.session
    session['voter_file_id'] = voter_file.id
    session.save()
    self.client.post('/helios/elections/%s/voters/upload-cancel' % election.uuid, {'csrf_token': 'tok'})
    self.assertTrue(VoterFile.objects.filter(pk=voter_file.pk).exists())
