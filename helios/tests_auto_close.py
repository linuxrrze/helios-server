"""
Tests for closing voting automatically once every voter has voted
(Election.close_when_all_voted) and for the one-step tally when Helios is the
election's only trustee.
"""

import datetime
import uuid
from unittest import mock

from django.core import mail
from django.test import TestCase

from helios import datatypes, forms, tasks, views
from helios import votergroup as vg
from helios.models import (CastVote, Election, ElectionLog, OutgoingEmail, TallyDecryptionError, Trustee, Voter,
                           VoterGroup, VoterGroupMember)
from helios.tests import run_queued_tasks
from helios.wizard_views import STEPS_BY_KEY
from helios.workflows import homomorphic
from helios_auth.models import User

QUESTIONS = [{"answer_urls": [None, None], "answers": ["Yes", "No"], "choice_type": "approval", "max": 1, "min": 0,
              "question": "Agreed?", "result_type": "absolute", "short_name": "Agreed?", "tally_type": "homomorphic"}]


class ElectionSetup:
  """an administrator, who is logged in, and helpers for elections with voters and ballots"""

  def setUp(self):
    self.admin = User.objects.create(user_type='password', user_id='admin@example.org', name='Ada Admin',
                                     info={'password': 'x', 'email': 'admin@example.org'})
    self.client.get('/')
    session = self.client.session
    session['user'] = {'type': self.admin.user_type, 'user_id': self.admin.user_id}
    session['csrf_token'] = 'tok'
    session.save()

  def make_election(self, close_when_all_voted=True, voters=('a', 'b'), openreg=False, freeze=True,
                    extra_trustee=False):
    election = Election.objects.create(
      admin=self.admin, uuid=str(uuid.uuid4()), short_name='board-%s' % uuid.uuid4().hex[:8], name='Board vote',
      election_type='election', questions=QUESTIONS, openreg=openreg, close_when_all_voted=close_when_all_voted)
    election.generate_trustee(views.ELGAMAL_PARAMS)
    if extra_trustee:
      # a trustee with its own key, of which Helios doesn't know the secret
      keypair = views.ELGAMAL_PARAMS.generate_keypair()
      self.extra_secret_key = keypair.sk
      Trustee.objects.create(election=election, uuid=str(uuid.uuid4()), name='Tess Trustee',
                             email='tess@example.org', public_key=keypair.pk, public_key_hash='x')
    for login_id in voters:
      self.add_voter(election, login_id)
    if freeze:
      election.freeze()
    return election

  def add_voter(self, election, login_id):
    voter = Voter(uuid=str(uuid.uuid4()), election=election, voter_login_id=login_id,
                  voter_name='Voter %s' % login_id, voter_email='%s@example.org' % login_id.rstrip('0123456789'))
    voter.generate_password()
    voter.save()
    return voter

  def cast(self, voter, answer=0, run=True):
    """a ballot of VOTER, verified and stored by its task (as the booth's cast does)"""
    election = Election.objects.get(pk=voter.election_id)
    ballot = homomorphic.EncryptedVote.fromElectionAndAnswers(election, [[answer]]).ld_object.toJSONDict()
    vote = datatypes.LDObject.fromDict(ballot, type_hint='legacy/EncryptedVote').wrapped_obj
    cast_vote = CastVote(vote=vote, vote_hash=str(uuid.uuid4()), voter=voter, cast_at=datetime.datetime.utcnow())
    cast_vote.save()
    tasks.cast_vote_verify_and_store.enqueue(cast_vote_id=cast_vote.id)
    if run:
      run_queued_tasks()
    return cast_vote

  def voters(self, election):
    return list(election.voter_set.order_by('voter_login_id'))

  def reload(self, election):
    return Election.objects.get(pk=election.pk)

  def auto_close_logs(self, election):
    return ElectionLog.objects.filter(election=election, log=ElectionLog.VOTING_AUTO_CLOSED)

  def admin_mails(self, election, subject_part):
    """the emails sent to administrators of ELECTION with SUBJECT_PART in their subject"""
    run_queued_tasks()
    sent = {e.message_id for e in OutgoingEmail.objects.filter(election=election, kind=OutgoingEmail.KIND_ADMIN_INFO)}
    return [m for m in mail.outbox if m.extra_headers.get('Message-ID') in sent and subject_part in m.subject]


class SettingTests(ElectionSetup, TestCase):

  def test_off_by_default(self):
    self.assertFalse(Election._meta.get_field('close_when_all_voted').default)
    self.assertFalse(forms.ElectionForm().fields['close_when_all_voted'].initial)
    response = self.client.post('/helios/elections/new', {
      'csrf_token': 'tok', 'name': 'Plain', 'short_name': 'plain', 'description': '', 'election_type': 'election',
      'help_email': 'admin@example.org'})
    self.assertEqual(response.status_code, 302)
    self.assertFalse(Election.objects.get(short_name='plain').close_when_all_voted)

  def test_offered_on_new_and_edit_page_and_wizard_options(self):
    self.assertContains(self.client.get('/helios/elections/new'), 'name="close_when_all_voted"')
    self.assertIn('close_when_all_voted', STEPS_BY_KEY['options'][2])
    self.assertContains(self.client.get('/helios/elections/new/guided?step=options'), 'name="close_when_all_voted"')

    election = self.make_election(close_when_all_voted=False, freeze=False)
    response = self.client.get('/helios/elections/%s/edit' % election.uuid)
    self.assertContains(response, 'name="close_when_all_voted"')
    response = self.client.post('/helios/elections/%s/edit' % election.uuid, {
      'csrf_token': 'tok', 'name': 'Board vote', 'short_name': election.short_name, 'description': '',
      'election_type': 'election', 'help_email': '', 'close_when_all_voted': 'on'})
    self.assertEqual(response.status_code, 302)
    self.assertTrue(self.reload(election).close_when_all_voted)

  def test_wizard_keeps_the_setting(self):
    self.client.post('/helios/elections/new/guided', {'csrf_token': 'tok', 'step': 'basics', 'name': 'Wizard vote',
                                                      'short_name': 'wizard-vote', 'description': ''})
    self.client.post('/helios/elections/new/guided', {'csrf_token': 'tok', 'step': 'schedule'})
    self.client.post('/helios/elections/new/guided', {'csrf_token': 'tok', 'step': 'options',
                                                      'election_type': 'election', 'close_when_all_voted': 'on'})
    response = self.client.post('/helios/elections/new/guided', {'csrf_token': 'tok', 'step': 'review'})
    self.assertEqual(response.status_code, 302)
    self.assertTrue(Election.objects.get(short_name='wizard-vote').close_when_all_voted)

  def test_hidden_on_edit_page_with_open_registration(self):
    election = self.make_election(close_when_all_voted=False, freeze=False, openreg=True)
    response = self.client.get('/helios/elections/%s/edit' % election.uuid)
    self.assertNotContains(response, 'name="close_when_all_voted"')
    response = self.client.post('/helios/elections/%s/edit' % election.uuid, {
      'csrf_token': 'tok', 'name': 'Renamed', 'short_name': election.short_name, 'description': '',
      'election_type': 'election', 'help_email': '', 'close_when_all_voted': 'on'})
    self.assertEqual(response.status_code, 302)
    election = self.reload(election)
    self.assertEqual(election.name, 'Renamed')
    self.assertFalse(election.close_when_all_voted)

  def test_copied_with_the_election(self):
    election = self.make_election(freeze=False)
    self.client.post('/helios/elections/%s/copy' % election.uuid, {'csrf_token': 'tok'})
    self.assertTrue(Election.objects.get(name='Copy of Board vote').close_when_all_voted)


class VoterGroupPreselectionTests(ElectionSetup, TestCase):

  def setUp(self):
    super().setUp()
    self.group = VoterGroup.objects.create(admin=self.admin, uuid='g1', short_name='council')
    VoterGroupMember.objects.create(group=self.group, uuid='m1', first_name='Anna', last_name='Arp',
                                    email='anna@example.org', votes=2)

  def post_data(self, **extra):
    seats = vg.initial_seats(self.group.get_members())
    data = {'csrf_token': 'tok', 'form-TOTAL_FORMS': str(len(seats)), 'form-INITIAL_FORMS': str(len(seats)),
            'short_name': 'council-2026', 'name': 'Council 2026', 'description': 'd',
            'election_type': 'election', 'help_email': 'admin@example.org', **extra}
    for i, seat in enumerate(seats):
      for key, value in seat.items():
        data['form-%d-%s' % (i, key)] = value
    return data

  def test_preselected_on(self):
    response = self.client.get('/helios/votergroup/prepareelection/g1')
    self.assertTrue(response.context['electionform']['close_when_all_voted'].value())
    self.assertContains(response, 'name="close_when_all_voted"')
    self.client.post('/helios/votergroup/prepareelection/g1', self.post_data(close_when_all_voted='on'))
    self.assertTrue(Election.objects.get(short_name='council-2026').close_when_all_voted)

  def test_can_be_turned_off(self):
    # an unchecked box is not submitted
    self.client.post('/helios/votergroup/prepareelection/g1', self.post_data())
    self.assertFalse(Election.objects.get(short_name='council-2026').close_when_all_voted)


class AutoCloseTests(ElectionSetup, TestCase):

  def test_closes_after_the_last_ballot_not_before(self):
    election = self.make_election()
    a, b = self.voters(election)
    self.cast(a)
    election = self.reload(election)
    self.assertIsNone(election.voting_ended_at)
    self.assertFalse(election.voting_has_stopped())

    self.cast(b)
    election = self.reload(election)
    self.assertIsNotNone(election.voting_ended_at)
    self.assertTrue(election.voting_has_stopped())
    # voting ended, nothing more: the tally is the administrator's step
    self.assertIsNone(election.tallying_started_at)
    self.assertIsNone(election.encrypted_tally)
    self.assertEqual(self.auto_close_logs(election).count(), 1)
    self.assertEqual(len(self.admin_mails(election, 'voting closed')), 1)

  def test_voter_with_two_votes_must_cast_both(self):
    # a voter group member with two votes has two seats (Voter rows)
    election = self.make_election(voters=('anna1', 'anna2', 'bea1'))
    anna1, anna2, bea1 = self.voters(election)
    self.cast(anna1)
    self.cast(bea1)
    self.assertIsNone(self.reload(election).voting_ended_at)
    self.cast(anna2)
    self.assertIsNotNone(self.reload(election).voting_ended_at)

  def test_recast_does_not_count_twice(self):
    election = self.make_election()
    a, b = self.voters(election)
    self.cast(a)
    self.cast(a, answer=1)
    self.assertIsNone(self.reload(election).voting_ended_at)

  def test_not_when_off(self):
    election = self.make_election(close_when_all_voted=False)
    for voter in self.voters(election):
      self.cast(voter)
    election = self.reload(election)
    self.assertIsNone(election.voting_ended_at)
    self.assertFalse(self.auto_close_logs(election).exists())

  def test_not_with_open_registration(self):
    election = self.make_election(openreg=True)
    for voter in self.voters(election):
      self.cast(voter)
    election = self.reload(election)
    self.assertIsNone(election.voting_ended_at)
    self.assertFalse(election.close_voting_if_all_voted())
    self.assertFalse(self.auto_close_logs(election).exists())

  def test_idempotent(self):
    election = self.make_election()
    for voter in self.voters(election):
      self.cast(voter)
    election = self.reload(election)
    ended_at = election.voting_ended_at

    # a second trigger (e.g. a ballot still being processed, or a concurrent one) does nothing
    self.assertFalse(election.close_voting_if_all_voted())
    self.assertFalse(tasks.close_voting_if_all_voted(self.reload(election)))
    run_queued_tasks()
    election = self.reload(election)
    self.assertEqual(election.voting_ended_at, ended_at)
    self.assertEqual(self.auto_close_logs(election).count(), 1)
    self.assertEqual(len(self.admin_mails(election, 'voting closed')), 1)

  def test_last_ballots_processed_together_close_once(self):
    election = self.make_election()
    a, b = self.voters(election)
    # both ballots are cast, then processed by the worker one after the other
    self.cast(a, run=False)
    self.cast(b, run=False)
    run_queued_tasks()
    election = self.reload(election)
    self.assertIsNotNone(election.voting_ended_at)
    self.assertEqual(self.auto_close_logs(election).count(), 1)
    self.assertEqual(len(self.admin_mails(election, 'voting closed')), 1)

  def test_not_after_voting_was_ended_by_the_tally(self):
    election = self.make_election()
    a, b = self.voters(election)
    self.cast(a)
    election.voting_ended_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
    election.tallying_started_at = election.voting_ended_at
    election.save()
    self.assertFalse(self.reload(election).close_voting_if_all_voted())
    self.assertFalse(self.auto_close_logs(election).exists())

  def test_admin_mail_goes_to_the_administrator(self):
    election = self.make_election()
    for voter in self.voters(election):
      self.cast(voter)
    [email] = self.admin_mails(election, 'voting closed')
    self.assertEqual(email.to, ['"Ada Admin" <admin@example.org>'])
    self.assertIn('Board vote', email.subject)
    self.assertIn('compute the tally', email.body)

  def test_admin_page_shows_setting_and_closing(self):
    election = self.make_election()
    a, b = self.voters(election)
    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    self.assertContains(response, 'id="close-when-all-voted"')
    self.assertNotContains(response, 'id="voting-auto-closed"')

    self.cast(a)
    self.cast(b)
    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    self.assertNotContains(response, 'id="close-when-all-voted"')
    self.assertContains(response, 'id="voting-auto-closed"')
    self.assertContains(response, 'Voting closed automatically: all voters have voted')
    self.assertContains(response, 'Voting closed')
    # voting has ended for good: no extending it
    self.assertNotContains(response, '/extend"')

  def test_admin_page_without_setting(self):
    election = self.make_election(close_when_all_voted=False)
    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    self.assertNotContains(response, 'id="close-when-all-voted"')

  def test_extend_refused_after_closing(self):
    election = self.make_election()
    election.voting_ends_at = datetime.datetime.utcnow() + datetime.timedelta(days=1)
    election.save()
    for voter in self.voters(election):
      self.cast(voter)
    response = self.client.post('/helios/elections/%s/extend' % election.uuid, {
      'csrf_token': 'tok', 'voting_extended_until': '2099-01-01 10:00'})
    self.assertEqual(response.status_code, 302)
    election = self.reload(election)
    self.assertIsNone(election.voting_extended_until)
    self.assertTrue(election.voting_has_stopped())

  def test_booth_refuses_ballots_after_closing(self):
    election = self.make_election()
    for voter in self.voters(election):
      self.cast(voter)
    response = self.client.get('/helios/e/%s/vote' % election.short_name)
    self.assertTemplateUsed(response, 'helios/templates/election_tallied.html')

  def test_tally_after_closing_keeps_the_closing_time(self):
    election = self.make_election()
    for voter in self.voters(election):
      self.cast(voter)
    ended_at = self.reload(election).voting_ended_at
    self.client.post('/helios/elections/%s/compute_tally' % election.uuid, {'csrf_token': 'tok'})
    run_queued_tasks()
    election = self.reload(election)
    self.assertEqual(election.voting_ended_at, ended_at)
    self.assertTrue(election.has_complete_result)


class VoterRemovalTests(ElectionSetup, TestCase):
  """removing the last voter who hasn't voted ends voting, too (views.voter_delete)"""

  def remove(self, election, voter):
    response = self.client.post('/helios/elections/%s/voters/%s/delete' % (election.uuid, voter.uuid),
                                {'csrf_token': 'tok'})
    self.assertEqual(response.status_code, 302)

  def test_removing_the_last_voter_who_has_not_voted_closes(self):
    election = self.make_election(voters=('a', 'b', 'c'))
    a, b, c = self.voters(election)
    self.cast(a)
    self.cast(b)
    self.assertIsNone(self.reload(election).voting_ended_at)
    self.remove(election, c)
    election = self.reload(election)
    self.assertIsNotNone(election.voting_ended_at)
    self.assertTrue(election.voting_has_stopped())
    self.assertEqual(self.auto_close_logs(election).count(), 1)
    self.assertEqual(len(self.admin_mails(election, 'voting closed')), 1)

  def test_removing_a_voter_while_others_have_not_voted(self):
    election = self.make_election(voters=('a', 'b', 'c'))
    a, b, c = self.voters(election)
    self.cast(a)
    # a voter who has voted, and one who hasn't: c still hasn't voted
    self.remove(election, a)
    self.remove(election, b)
    election = self.reload(election)
    self.assertIsNone(election.voting_ended_at)
    self.assertFalse(self.auto_close_logs(election).exists())

  def test_removing_all_voters_does_not_close(self):
    election = self.make_election()
    for voter in self.voters(election):
      self.remove(election, voter)
    election = self.reload(election)
    self.assertEqual(election.num_voters, 0)
    self.assertIsNone(election.voting_ended_at)
    self.assertFalse(self.auto_close_logs(election).exists())
    self.assertEqual(self.admin_mails(election, 'voting closed'), [])

  def test_not_when_off(self):
    election = self.make_election(close_when_all_voted=False)
    a, b = self.voters(election)
    self.cast(a)
    self.remove(election, b)
    self.assertIsNone(self.reload(election).voting_ended_at)

  def test_not_before_the_freeze(self):
    election = self.make_election(freeze=False)
    a, b = self.voters(election)
    # not possible through the booth before the freeze; set directly to check the guard
    Voter.objects.filter(pk=a.pk).update(vote_hash='x', cast_at=datetime.datetime.utcnow())
    self.remove(election, b)
    election = self.reload(election)
    self.assertIsNone(election.voting_ended_at)
    self.assertFalse(self.auto_close_logs(election).exists())

  def test_not_after_voting_has_ended(self):
    election = self.make_election()
    a, b = self.voters(election)
    self.cast(a)
    ended_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
    election.voting_ends_at = ended_at
    election.save()
    self.remove(election, b)
    election = self.reload(election)
    self.assertEqual(election.voting_ends_at, ended_at)
    self.assertIsNone(election.voting_ended_at)
    self.assertFalse(self.auto_close_logs(election).exists())
    self.assertEqual(self.admin_mails(election, 'voting closed'), [])


class OneStepTallyTests(ElectionSetup, TestCase):

  def tally(self, election):
    response = self.client.post('/helios/elections/%s/compute_tally' % election.uuid, {'csrf_token': 'tok'})
    self.assertEqual(response.status_code, 302)

  def test_helios_only_trustee_runs_through_to_the_result(self):
    election = self.make_election(close_when_all_voted=False, voters=('a', 'b', 'c'))
    a, b, c = self.voters(election)
    self.cast(a, 0)
    self.cast(b, 0)
    self.cast(c, 1)
    self.assertTrue(election.helios_is_only_trustee())

    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    self.assertContains(response, '>tally</a>')

    self.tally(election)
    # until the worker has run: in progress
    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    self.assertContains(response, 'id="tally-progress"')

    run_queued_tasks()
    election = self.reload(election)
    self.assertEqual(election.result, [[2, 1]])
    self.assertTrue(ElectionLog.objects.filter(election=election, log=ElectionLog.DECRYPTIONS_COMBINED).exists())
    # releasing stays a step of its own
    self.assertIsNone(election.result_released_at)
    self.assertEqual(len(self.admin_mails(election, 'result computed')), 1)
    self.assertEqual(self.admin_mails(election, 'Helios Decrypt'), [])

    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    self.assertNotContains(response, 'id="tally-progress"')
    self.assertContains(response, '/release_result"')

  def test_with_another_trustee_the_old_flow_is_unchanged(self):
    election = self.make_election(close_when_all_voted=False, extra_trustee=True)
    a, b = self.voters(election)
    self.cast(a, 0)
    self.cast(b, 1)
    self.assertFalse(election.helios_is_only_trustee())
    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    self.assertContains(response, 'compute encrypted tally')

    self.tally(election)
    run_queued_tasks()
    election = self.reload(election)
    helios_trustee = election.get_helios_trustee()
    other = election.trustee_set.exclude(pk=helios_trustee.pk).get()
    # Helios decrypted its share; the result waits for the other trustee and the administrator
    self.assertIsNotNone(helios_trustee.decryption_factors)
    self.assertIsNone(other.decryption_factors)
    self.assertIsNone(election.result)
    self.assertEqual(len(self.admin_mails(election, 'Helios Decrypt')), 1)
    self.assertEqual(self.admin_mails(election, 'result computed'), [])
    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    self.assertContains(response, 'Waiting for the trustees')
    self.assertNotContains(response, 'id="tally-progress"')

    # the other trustee decrypts, the administrator combines by hand
    tally = election.encrypted_tally
    tally.init_election(election)
    other.decryption_factors, other.decryption_proofs = tally.decryption_factors_and_proofs(self.extra_secret_key)
    other.save()
    election = self.reload(election)
    self.assertIsNone(election.result)
    response = self.client.post('/helios/elections/%s/combine_decryptions' % election.uuid, {'csrf_token': 'tok'})
    self.assertEqual(response.status_code, 302)
    election = self.reload(election)
    self.assertEqual(election.result, [[1, 1]])
    self.assertIsNone(election.result_released_at)

  def test_failed_combination_leaves_the_manual_step(self):
    election = self.make_election(close_when_all_voted=False)
    a, b = self.voters(election)
    self.cast(a, 0)
    self.cast(b, 0)
    self.tally(election)
    with mock.patch('helios.models.Election.combine_decryptions', side_effect=TallyDecryptionError('no')), \
         self.assertLogs('helios.tasks', 'ERROR'):
      run_queued_tasks()
    election = self.reload(election)
    self.assertIsNone(election.result)
    self.assertTrue(election.ready_for_decryption_combination())
    self.assertTrue(election.automatic_tally_failed)
    self.assertEqual(len(self.admin_mails(election, 'could not be computed')), 1)

    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    self.assertContains(response, 'id="automatic-tally-failed"')
    self.assertContains(response, '/combine_decryptions"')
    self.assertNotContains(response, 'id="tally-progress"')

    # the existing manual step still works
    self.client.post('/helios/elections/%s/combine_decryptions' % election.uuid, {'csrf_token': 'tok'})
    election = self.reload(election)
    self.assertEqual(election.result, [[2, 0]])
    self.assertFalse(election.automatic_tally_failed)

  def test_combination_runs_only_once(self):
    election = self.make_election(close_when_all_voted=False)
    for voter in self.voters(election):
      self.cast(voter)
    self.tally(election)
    run_queued_tasks()
    election = self.reload(election)
    self.assertTrue(election.has_complete_result)
    # a second run (e.g. a requeued task) finds the result and does nothing
    self.assertFalse(election.combine_decryptions_automatically())
    self.assertEqual(ElectionLog.objects.filter(election=election, log=ElectionLog.DECRYPTIONS_COMBINED).count(), 1)

  def test_auto_close_then_one_click_tally(self):
    election = self.make_election()
    for voter in self.voters(election):
      self.cast(voter, 1)
    election = self.reload(election)
    self.assertTrue(election.voting_has_stopped())
    self.tally(election)
    run_queued_tasks()
    election = self.reload(election)
    self.assertEqual(election.result, [[0, 2]])
    self.assertIsNone(election.result_released_at)
