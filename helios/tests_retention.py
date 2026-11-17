"""
Deleting elections: election administrators can no longer delete an election
once a ballot has been cast (only archive it), site administrators still can
(logged).
"""

import datetime

from django.test import TestCase

from helios.models import CastVote, Election, ElectionLog
from helios.tests_auto_close import ElectionSetup
from helios_auth.models import User

LOCK_MESSAGE = 'This election has ballots and can no longer be deleted; you can archive it.'


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
