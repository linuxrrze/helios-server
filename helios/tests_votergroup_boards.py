"""
Tests for voter groups as boards: name and term of office of a group,
initials, person group and status of its members, members without a vote.
"""

import datetime

from django.test import TestCase

from helios import votergroup as vg
from helios.models import Election, Voter, VoterGroup, VoterGroupLog, VoterGroupMember
from helios_auth.models import User


class _BoardTestCase(TestCase):
  """The group "council" owned by Olga with the additional administrator Carl
  and the members Anna (2 votes) and Bea (1 vote)."""

  def setUp(self):
    def user(user_id, name, **kw):
      return User.objects.create(user_type='password', user_id=user_id, name=name,
                                 info={'password': 'x', 'email': '%s@example.org' % user_id}, **kw)
    self.owner = user('owner', 'Olga Owner')
    self.coadmin = user('coadmin', 'Carl Coadmin')
    self.stranger = user('stranger', 'Sam Stranger')
    self.siteadmin = user('siteadmin', 'Sita Admin', admin_p=True)
    self.group = VoterGroup.objects.create(admin=self.owner, uuid='g1', short_name='council')
    self.group.admins.add(self.coadmin)
    self.anna = self.member('m1', 'Anna', 'Arp', 'anna@example.org', votes=2)
    self.bea = self.member('m2', 'Bea', 'Bach', 'bea@example.org')

  def member(self, uuid, first_name, last_name, email, group=None, **kw):
    return VoterGroupMember.objects.create(group=group or self.group, uuid=uuid, first_name=first_name,
                                           last_name=last_name, email=email, **kw)

  def login(self, user):
    self.client.get("/")
    session = self.client.session
    session['user'] = {'type': user.user_type, 'user_id': user.user_id}
    session['csrf_token'] = 'tok'
    session.save()

  def provisioned_group(self):
    group = VoterGroup.objects.create(admin=self.owner, uuid='p1', short_name='idm-board', provisioned_by='scim:idm',
                                      provisioned_ref='ref1')
    group.admins.add(self.coadmin)
    self.member('pm1', 'Paul', 'Prov', 'paul@example.org', group=group)
    return group

  def assertRedirectsTo(self, response, path):
    self.assertEqual(response.status_code, 302)
    self.assertTrue(response['Location'].endswith(path), response['Location'])

  def reload(self):
    self.group = VoterGroup.objects.get(pk=self.group.pk)
    return self.group

  def actions(self):
    return [e.action for e in self.reload().get_log()]


class BoardModelTests(_BoardTestCase):

  def test_defaults(self):
    self.assertEqual((self.group.name, self.group.name_suffix, self.group.term_starts_on, self.group.term_ends_on),
                     ('', '', None, None))
    self.assertEqual(self.group.display_name, 'council')
    self.assertFalse(self.group.term_ended)
    self.assertEqual((self.anna.initials, self.anna.person_group, self.anna.status), ('', '', 'member'))

  def test_display_name_and_summary(self):
    self.group.name, self.group.name_suffix = 'Faculty council', '2025-2027'
    self.group.term_starts_on, self.group.term_ends_on = datetime.date(2025, 10, 1), datetime.date(2027, 9, 30)
    self.assertEqual(self.group.display_name, 'Faculty council')
    self.assertEqual(self.group.details_summary(), 'Faculty council · 2025-2027 · 2025-10-01 – 2027-09-30')
    self.assertEqual(VoterGroup(short_name='x').details_summary(), '—')

  def test_term_ended_from_the_day_after(self):
    today = datetime.date.today()
    self.group.term_ends_on = today
    self.assertFalse(self.group.term_ended)
    self.group.term_ends_on = today - datetime.timedelta(days=1)
    self.assertTrue(self.group.term_ended)

  def test_member_order_by_person_group_then_name(self):
    self.member('m3', 'Carl', 'Arp', 'carl@example.org', person_group='Students')
    self.member('m4', 'Dora', 'Zorn', 'dora@example.org', person_group='Professors')
    self.member('m5', 'Emil', 'Berg', 'emil@example.org', person_group='Professors')
    self.member('m6', 'Abe', 'Arp', 'abe@example.org', person_group='Students')
    self.assertEqual([m.uuid for m in self.group.get_members()], ['m5', 'm4', 'm6', 'm3', 'm1', 'm2'])

  def test_list_counts_members(self):
    group = VoterGroup.get_by_user_as_admin(self.coadmin).get()
    self.assertEqual(group.num_members, 2)


class NoVoteTests(_BoardTestCase):
  """Members with 0 votes have no voting right."""

  def _election_post(self):
    seats = vg.initial_seats(self.group.get_members())
    data = {'csrf_token': 'tok', 'form-TOTAL_FORMS': str(len(seats)), 'form-INITIAL_FORMS': str(len(seats)),
            'short_name': 'council-2026', 'name': 'Council 2026', 'description': 'd',
            'election_type': 'election', 'help_email': 'owner@example.org'}
    for i, s in enumerate(seats):
      for k, v in s.items():
        data['form-%d-%s' % (i, k)] = v
    return data

  def test_no_seats_for_zero_votes(self):
    self.member('m3', 'Dan', 'Deputy', 'dan@example.org', votes=0, status='deputy')
    seats = vg.initial_seats(self.group.get_members())
    self.assertEqual([s['email'] for s in seats], ['anna@example.org', 'anna@example.org', 'bea@example.org'])

  def test_add_member_with_zero_votes(self):
    self.login(self.owner)
    response = self.client.post('/helios/votergroup/memberadd/g1', {
      'csrf_token': 'tok', 'first_name': 'Dan', 'last_name': 'Deputy', 'email': 'dan@example.org', 'votes': '0',
      'initials': 'DD', 'person_group': 'Staff', 'status': 'deputy'})
    self.assertEqual(response.status_code, 302)
    dan = VoterGroupMember.objects.get(email='dan@example.org')
    self.assertEqual((dan.votes, dan.initials, dan.person_group, dan.status), (0, 'DD', 'Staff', 'deputy'))
    response = self.client.post('/helios/votergroup/memberadd/g1', {
      'csrf_token': 'tok', 'first_name': 'X', 'last_name': 'Y', 'email': 'x@example.org', 'votes': '-1'})
    self.assertEqual(response.status_code, 200)
    self.assertFalse(VoterGroupMember.objects.filter(email='x@example.org').exists())

  def test_prepare_shows_members_without_vote_and_issues_no_voter(self):
    self.member('m3', 'Dan', 'Deputy', 'dan@example.org', votes=0, status='deputy')
    self.login(self.owner)
    response = self.client.get('/helios/votergroup/prepareelection/g1')
    self.assertContains(response, '1 member has no voting right (0 votes)')
    self.assertContains(response, 'Deputy, Dan')
    self.assertContains(response, 'no vote')
    self.assertEqual(response.context['form'].total_form_count(), 3)
    response = self.client.post('/helios/votergroup/prepareelection/g1', self._election_post())
    self.assertEqual(response.status_code, 302)
    voters = Voter.objects.filter(election__short_name='council-2026')
    self.assertEqual(voters.count(), 3)
    self.assertFalse(voters.filter(voter_email='dan@example.org').exists())

  def test_prepare_refused_when_nobody_has_a_vote(self):
    VoterGroupMember.objects.update(votes=0)
    self.login(self.owner)
    response = self.client.get('/helios/votergroup/prepareelection/g1')
    self.assertContains(response, 'None of the members of the voter group has a vote')
    self.assertNotContains(response, 'id="create_election_form"')
    # also when rows are posted anyway
    data = self._election_post()
    data.update({'form-TOTAL_FORMS': '1', 'form-INITIAL_FORMS': '1', 'form-0-first_name': 'Anna',
                 'form-0-last_name': 'Arp', 'form-0-email': 'anna@example.org', 'form-0-login_id': 'AA1',
                 'form-0-delegate_to': ''})
    response = self.client.post('/helios/votergroup/prepareelection/g1', data)
    self.assertEqual(response.status_code, 200)
    self.assertContains(response, 'None of the members of the voter group has a vote')
    self.assertFalse(Election.objects.exists())

  def test_member_table_marks_zero_votes(self):
    self.member('m3', 'Dan', 'Deputy', 'dan@example.org', votes=0)
    self.login(self.owner)
    self.assertContains(self.client.get('/helios/votergroup/view/g1'), 'no voting right')


class GroupSettingsTests(_BoardTestCase):

  def post(self, **data):
    return self.client.post('/helios/votergroup/edit/g1', dict({'csrf_token': 'tok', 'short_name': 'council'}, **data))

  def test_edit_and_log(self):
    self.login(self.coadmin)
    response = self.client.get('/helios/votergroup/edit/g1')
    self.assertContains(response, 'value="council"')
    self.assertContains(response, 'type="date"')
    response = self.post(name='Faculty council of the Faculty of Sciences', name_suffix='2025-2027',
                         term_starts_on='2025-10-01', term_ends_on='2027-09-30')
    self.assertEqual(response.status_code, 302)
    group = self.reload()
    self.assertEqual(group.name, 'Faculty council of the Faculty of Sciences')
    self.assertEqual(group.term_ends_on, datetime.date(2027, 9, 30))
    entry = group.get_log().first()
    self.assertEqual((entry.action, entry.actor), (VoterGroupLog.DETAILS_CHANGED, self.coadmin))
    self.assertEqual(entry.message, 'Name and term of office changed: Faculty council of the Faculty of Sciences'
                                    ' · 2025-2027 · 2025-10-01 – 2027-09-30')
    # nothing changed: no entry; a new short name: renamed
    self.post(name='Faculty council of the Faculty of Sciences', name_suffix='2025-2027',
              term_starts_on='2025-10-01', term_ends_on='2027-09-30')
    self.assertEqual(self.actions(), [VoterGroupLog.DETAILS_CHANGED])
    self.post(short_name='faculty-council', name='Faculty council of the Faculty of Sciences', name_suffix='2025-2027',
              term_starts_on='2025-10-01', term_ends_on='2027-09-30')
    self.assertEqual(self.actions(), [VoterGroupLog.RENAMED, VoterGroupLog.DETAILS_CHANGED])
    self.assertEqual(self.group.short_name, 'faculty-council')

  def test_log_in_german(self):
    self.group.append_log(VoterGroupLog.DETAILS_CHANGED, actor=self.owner, details='Senate')
    self.login(self.owner)
    self.assertContains(self.client.get('/helios/votergroup/view/g1', HTTP_ACCEPT_LANGUAGE='de'),
                        'Name und Amtszeit geändert: Senate')

  def test_end_before_start_is_refused(self):
    self.login(self.owner)
    response = self.post(term_starts_on='2027-10-01', term_ends_on='2025-09-30')
    self.assertEqual(response.status_code, 200)
    self.assertContains(response, 'The term of office cannot end before it begins.')
    self.assertIsNone(self.reload().term_starts_on)
    self.assertEqual(self.actions(), [])

  def test_short_name_taken(self):
    VoterGroup.objects.create(admin=self.owner, uuid='g2', short_name='senate')
    self.login(self.coadmin)
    response = self.post(short_name='senate', name='Senate')
    self.assertContains(response, 'The owner of the group already has a voter group named senate')
    self.assertEqual((self.reload().short_name, self.group.name), ('council', ''))

  def test_create_with_details(self):
    self.login(self.owner)
    self.client.post('/helios/votergroup/new', {'csrf_token': 'tok', 'short_name': 'senate', 'name': 'Senate',
                                                'term_starts_on': '2025-04-01'})
    group = VoterGroup.objects.get(short_name='senate')
    self.assertEqual((group.name, group.term_starts_on), ('Senate', datetime.date(2025, 4, 1)))
    self.assertEqual([e.action for e in group.get_log()], [VoterGroupLog.DETAILS_CHANGED, VoterGroupLog.CREATED])

  def test_access(self):
    self.login(self.owner)
    self.assertEqual(self.post(csrf_token='wrong', name='X').status_code, 400)
    self.login(self.stranger)
    self.assertEqual(self.client.get('/helios/votergroup/edit/g1').status_code, 404)
    self.assertEqual(self.post(name='X').status_code, 404)
    self.login(self.siteadmin)
    self.assertEqual(self.client.get('/helios/votergroup/edit/g1').status_code, 403)
    self.assertEqual(self.post(name='X').status_code, 403)
    self.assertEqual(self.reload().name, '')
    self.provisioned_group()
    self.login(self.coadmin)
    self.assertEqual(self.client.get('/helios/votergroup/edit/p1').status_code, 403)
    response = self.client.post('/helios/votergroup/edit/p1', {'csrf_token': 'tok', 'short_name': 'x', 'name': 'X'})
    self.assertEqual(response.status_code, 403)
    self.assertEqual(VoterGroup.objects.get(uuid='p1').name, '')

  def test_shown_on_page_and_in_list(self):
    self.group.name, self.group.name_suffix = 'Faculty council of the Faculty of Sciences', '2025-2027'
    self.group.term_starts_on, self.group.term_ends_on = datetime.date(2025, 10, 1), datetime.date(2099, 9, 30)
    self.group.save()
    self.login(self.owner)
    content = self.client.get('/helios/votergroup/view/g1').content.decode()
    self.assertIn('<h1 class="title">Faculty council of the Faculty of Sciences <span class="muted">2025-2027</span>', content)
    self.assertIn('<span class="short-name">council</span>', content)
    self.assertIn('Oct. 1, 2025 – Sept. 30, 2099', content)
    self.assertNotIn('Term ended', content)
    content = self.client.get('/helios/votergroup/administered').content.decode()
    self.assertIn('>Faculty council of the Faculty of Sciences</a> 2025-2027', content)
    self.assertIn('<span class="short-name muted">council</span>', content)
    self.assertIn('<td class="num">2</td>', content)

  def test_term_ended_badge(self):
    self.group.term_ends_on = datetime.date.today() - datetime.timedelta(days=1)
    self.group.save()
    self.login(self.owner)
    self.assertContains(self.client.get('/helios/votergroup/view/g1'), 'Term ended')
    self.assertContains(self.client.get('/helios/votergroup/administered'), 'Term ended')
    self.assertContains(self.client.get('/helios/votergroup/view/g1', HTTP_ACCEPT_LANGUAGE='de'), 'Amtszeit beendet')
    # everything keeps working
    self.assertEqual(self.client.get('/helios/votergroup/prepareelection/g1').status_code, 200)
    self.login(self.siteadmin)
    self.assertContains(self.client.get('/helios/votergroup/view/g1'), 'Term ended')


class MemberEditTests(_BoardTestCase):

  def post(self, uuid='m1', **data):
    fields = {'csrf_token': 'tok', 'first_name': 'Anna', 'last_name': 'Arp', 'email': 'anna@example.org', 'votes': '2',
              'initials': '', 'person_group': '', 'status': 'member'}
    return self.client.post('/helios/votergroup/memberedit/%s' % uuid, dict(fields, **data))

  def test_edit_and_log(self):
    self.login(self.coadmin)
    response = self.client.get('/helios/votergroup/memberedit/m1')
    self.assertContains(response, 'value="anna@example.org"')
    self.assertContains(response, 'Deputy member')
    with self.assertLogs('helios.votergroup', 'INFO') as logs:
      response = self.post(votes='0', initials='AA', person_group='Professors', status='advisory')
    self.assertEqual(response.status_code, 302)
    anna = VoterGroupMember.objects.get(uuid='m1')
    self.assertEqual((anna.votes, anna.initials, anna.person_group, anna.status), (0, 'AA', 'Professors', 'advisory'))
    entry = self.reload().get_log().get()
    self.assertEqual((entry.action, entry.actor, entry.message),
                     (VoterGroupLog.MEMBER_CHANGED, self.coadmin, 'Member a***@example.org changed, now with 0 vote(s)'))
    self.assertNotIn('anna@example.org', logs.output[0])
    # without changes: no entry
    self.post(votes='0', initials='AA', person_group='Professors', status='advisory')
    self.assertEqual(self.actions(), [VoterGroupLog.MEMBER_CHANGED])

  def test_table_shows_new_columns_only_when_used(self):
    self.login(self.owner)
    content = self.client.get('/helios/votergroup/view/g1').content.decode()
    self.assertNotIn('>Person group<', content)
    self.assertIn('/helios/votergroup/memberedit/m1', content)
    VoterGroupMember.objects.filter(uuid='m2').update(person_group='Students', status='guest', initials='BB')
    content = self.client.get('/helios/votergroup/view/g1').content.decode()
    for text in ['>Person group<', '>Status<', '>Initials<', '>Students<', '>Guest<', '>BB<']:
      self.assertIn(text, content)
    # Bea (Students) before Anna (no person group)
    self.assertLess(content.index('Bach, Bea'), content.index('Arp, Anna'))

  def test_access(self):
    self.login(self.owner)
    self.assertEqual(self.post(csrf_token='wrong', votes='5').status_code, 400)
    self.login(self.stranger)
    self.assertEqual(self.client.get('/helios/votergroup/memberedit/m1').status_code, 404)
    self.assertEqual(self.post(votes='5').status_code, 404)
    self.assertEqual(self.client.get('/helios/votergroup/memberedit/nope').status_code, 404)
    self.login(self.siteadmin)
    self.assertEqual(self.client.get('/helios/votergroup/memberedit/m1').status_code, 403)
    self.assertEqual(self.post(votes='5').status_code, 403)
    self.assertEqual(VoterGroupMember.objects.get(uuid='m1').votes, 2)
    self.provisioned_group()
    self.login(self.coadmin)
    self.assertEqual(self.client.get('/helios/votergroup/memberedit/pm1').status_code, 403)
    self.assertEqual(self.post(uuid='pm1', first_name='Paul', last_name='Prov', email='paul@example.org',
                               votes='5').status_code, 403)
    self.assertEqual(VoterGroupMember.objects.get(uuid='pm1').votes, 1)
    self.assertNotContains(self.client.get('/helios/votergroup/view/p1'), 'memberedit')

