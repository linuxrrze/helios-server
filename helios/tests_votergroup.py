"""
Tests for voter groups.
"""

import datetime

from django.test import TestCase

from helios import votergroup as vg
from helios.models import Election, Voter, VoterGroup, VoterGroupLog, VoterGroupMember
from helios_auth.models import User


class _Member:
  def __init__(self, uuid, first_name, last_name, email, votes):
    self.uuid, self.first_name, self.last_name, self.email, self.votes = uuid, first_name, last_name, email, votes


class SeatLogicTests(TestCase):

  def test_one_seat_per_vote_with_unique_login_ids(self):
    seats = vg.initial_seats([_Member('a', 'Jane', 'Doe', 'j@x', 2), _Member('b', 'John', 'Dee', 'd@x', 1)])
    self.assertEqual([s['login_id'] for s in seats], ['JD1', 'JD2', 'JD3'])
    self.assertEqual([s['email'] for s in seats], ['j@x', 'j@x', 'd@x'])

  def test_login_id_counter_beyond_nine(self):
    taken = {"AB%d" % i for i in range(1, 11)}
    self.assertEqual(vg.next_login_id('Anna', 'Berg', taken), 'AB11')

  def test_delegated_seat_is_issued_to_delegate(self):
    a, b = _Member('a', 'Anna', 'Arp', 'a@x', 2), _Member('b', 'Bea', 'Bach', 'b@x', 1)
    seats = vg.initial_seats([a, b])
    seats[1]['delegate_to'] = 'b'
    result = vg.apply_delegations(seats, {'a': a, 'b': b})
    self.assertEqual([s['email'] for s in result], ['a@x', 'b@x', 'b@x'])
    self.assertEqual(len({s['login_id'] for s in result}), 3)
    self.assertTrue(all(s['delegate_to'] == '' for s in result))

  def test_formset_rejects_duplicate_login_ids(self):
    data = {'form-TOTAL_FORMS': '2', 'form-INITIAL_FORMS': '2'}
    for i in range(2):
      data.update({'form-%d-first_name' % i: 'A', 'form-%d-last_name' % i: 'B',
                   'form-%d-email' % i: 'a@example.org', 'form-%d-login_id' % i: 'AB1', 'form-%d-delegate_to' % i: ''})
    formset = vg.SeatFormSet(data, form_kwargs={'delegate_choices': [('', '-')]})
    self.assertFalse(formset.is_valid())


class VoterGroupViewTests(TestCase):

  def setUp(self):
    self.admin = User.objects.create(user_type='password', user_id='admin', name='Admin', info={'password': 'x', 'email': 'admin@example.org'})
    self.other = User.objects.create(user_type='password', user_id='other', name='Other', info={'password': 'x', 'email': 'other@example.org'})
    self.group = VoterGroup.objects.create(admin=self.admin, uuid='g1', short_name='council')
    self.anna = VoterGroupMember.objects.create(group=self.group, uuid='m1', first_name='Anna', last_name='Arp', email='anna@example.org', votes=2)
    self.bea = VoterGroupMember.objects.create(group=self.group, uuid='m2', first_name='Bea', last_name='Bach', email='bea@example.org', votes=1)

  def login(self, user):
    self.client.get("/")
    session = self.client.session
    session['user'] = {'type': user.user_type, 'user_id': user.user_id}
    session['csrf_token'] = 'tok'
    session.save()

  def test_requires_login(self):
    response = self.client.get('/helios/votergroup/view/g1')
    self.assertEqual(response.status_code, 302)

  def test_admin_sees_group(self):
    self.login(self.admin)
    response = self.client.get('/helios/votergroup/view/g1')
    self.assertContains(response, 'anna@example.org')

  def test_other_user_and_unknown_uuid_get_404(self):
    self.login(self.other)
    self.assertEqual(self.client.get('/helios/votergroup/view/g1').status_code, 404)
    self.assertEqual(self.client.get('/helios/votergroup/view/nope').status_code, 404)
    self.assertEqual(self.client.get('/helios/votergroup/prepareelection/g1').status_code, 404)

  def test_create_group_and_member(self):
    self.login(self.admin)
    response = self.client.post('/helios/votergroup/new', {'csrf_token': 'tok', 'short_name': 'senate'})
    self.assertEqual(response.status_code, 302)
    group = VoterGroup.objects.get(admin=self.admin, short_name='senate')
    response = self.client.post('/helios/votergroup/memberadd/%s' % group.uuid, {
      'csrf_token': 'tok', 'first_name': 'C', 'last_name': 'D', 'email': 'c@example.org', 'votes': '1'})
    self.assertEqual(response.status_code, 302)
    self.assertEqual(group.get_members().count(), 1)

  def test_same_group_name_allowed_for_different_admins(self):
    self.login(self.other)
    self.client.post('/helios/votergroup/new', {'csrf_token': 'tok', 'short_name': 'council'})
    self.assertTrue(VoterGroup.objects.filter(admin=self.other, short_name='council').exists())

  def test_member_delete_requires_post_and_csrf(self):
    self.login(self.admin)
    self.assertEqual(self.client.get('/helios/votergroup/memberdelete/m1').status_code, 405)
    self.assertEqual(self.client.post('/helios/votergroup/memberdelete/m1', {'csrf_token': 'wrong'}).status_code, 400)
    self.assertTrue(VoterGroupMember.objects.filter(uuid='m1').exists())
    self.assertEqual(self.client.post('/helios/votergroup/memberdelete/m1', {'csrf_token': 'tok'}).status_code, 302)
    self.assertFalse(VoterGroupMember.objects.filter(uuid='m1').exists())

  def test_member_delete_by_other_user_is_404(self):
    self.login(self.other)
    self.assertEqual(self.client.post('/helios/votergroup/memberdelete/m1', {'csrf_token': 'tok'}).status_code, 404)
    self.assertTrue(VoterGroupMember.objects.filter(uuid='m1').exists())

  def _election_post(self, seats):
    data = {'csrf_token': 'tok', 'form-TOTAL_FORMS': str(len(seats)), 'form-INITIAL_FORMS': str(len(seats)),
            'short_name': 'council-2026', 'name': 'Council 2026', 'description': 'd',
            'election_type': 'election', 'help_email': 'admin@example.org'}
    for i, s in enumerate(seats):
      for k, v in s.items():
        data['form-%d-%s' % (i, k)] = v
    return data

  def test_prepare_election_creates_password_voters_with_delegation(self):
    self.login(self.admin)
    response = self.client.get('/helios/votergroup/prepareelection/g1')
    self.assertEqual(response.status_code, 200)

    seats = vg.initial_seats(self.group.get_members())
    self.assertEqual(len(seats), 3)
    anna_seat = next(i for i, s in enumerate(seats) if s['email'] == 'anna@example.org')
    seats[anna_seat]['delegate_to'] = 'm2'  # Anna delegates one of her two votes to Bea

    response = self.client.post('/helios/votergroup/prepareelection/g1', self._election_post(seats))
    self.assertEqual(response.status_code, 302, getattr(response, 'context', None) and response.context.get('error'))

    election = Election.objects.get(short_name='council-2026')
    voters = Voter.objects.filter(election=election)
    self.assertEqual(sorted(v.voter_email for v in voters), ['anna@example.org', 'bea@example.org', 'bea@example.org'])
    self.assertTrue(all(v.voter_password for v in voters))
    self.assertIn('Anna Arp', [v.voter_name for v in voters])
    self.assertEqual(election.trustee_set.count(), 1)

  def test_seat_table_inputs_have_names(self):
    """Every input of the seat table is labelled by its column and vote (row)"""
    import re
    self.login(self.admin)
    content = self.client.get('/helios/votergroup/prepareelection/g1').content.decode()
    ids = re.findall(r'<(?:input|select)[^>]* id="(id_form-\d+-[a-z_]+)"', content)
    self.assertEqual(len(ids), 3 * 5)
    for field_id in ids:
      self.assertRegex(content, r'<label class="screen-reader-text" for="%s">[^<]+, vote \d+</label>' % field_id)
    self.assertIn('<label class="screen-reader-text" for="id_form-2-email">Email, vote 3</label>', content)
    self.assertIn('<th scope="row" class="num">3</th>', content)
    content = self.client.get('/helios/votergroup/prepareelection/g1', HTTP_ACCEPT_LANGUAGE='de').content.decode()
    self.assertIn('for="id_form-0-delegate_to">Übertragen an, Stimme 1</label>', content)

  def test_prepare_election_with_voter_aliases(self):
    self.login(self.admin)
    seats = vg.initial_seats(self.group.get_members())
    data = self._election_post(seats)
    data['use_voter_aliases'] = 'on'
    response = self.client.post('/helios/votergroup/prepareelection/g1', data)
    self.assertEqual(response.status_code, 302)
    election = Election.objects.get(short_name='council-2026')
    self.assertTrue(election.use_voter_aliases)
    self.assertEqual(sorted(v.alias for v in election.voter_set.all()), ['V1', 'V2', 'V3'])
    self.assertEqual(election.last_alias_num, 3)

  def test_prepare_election_is_atomic(self):
    self.login(self.admin)
    seats = vg.initial_seats(self.group.get_members())
    # an existing election with the same short name makes creation fail
    Election.objects.create(admin=self.admin, uuid='e0', short_name='council-2026', name='x', election_type='election')
    response = self.client.post('/helios/votergroup/prepareelection/g1', self._election_post(seats))
    self.assertEqual(response.status_code, 200)
    self.assertEqual(Election.objects.filter(short_name='council-2026').count(), 1)
    self.assertFalse(Voter.objects.exists())


class _GroupWithAdminsTestCase(TestCase):
  """A group owned by Olga with the additional administrator Carl and the member Anna."""

  def setUp(self):
    def user(user_id, name, user_type='password', **kw):
      return User.objects.create(user_type=user_type, user_id=user_id, name=name,
                                 info={'password': 'x', 'email': '%s@example.org' % user_id}, **kw)
    self.owner = user('owner', 'Olga Owner')
    self.coadmin = user('coadmin', 'Carl Coadmin')
    self.third = user('third', 'Tina Third')
    self.stranger = user('stranger', 'Sam Stranger')
    self.group = VoterGroup.objects.create(admin=self.owner, uuid='g1', short_name='council')
    self.group.admins.add(self.coadmin)
    self.anna = VoterGroupMember.objects.create(group=self.group, uuid='m1', first_name='Anna', last_name='Arp', email='anna@example.org', votes=2)

  def login(self, user):
    self.client.get("/")
    session = self.client.session
    session['user'] = {'type': user.user_type, 'user_id': user.user_id}
    session['csrf_token'] = 'tok'
    session.save()

  def _election_post(self, **extra):
    seats = vg.initial_seats(self.group.get_members())
    data = {'csrf_token': 'tok', 'form-TOTAL_FORMS': str(len(seats)), 'form-INITIAL_FORMS': str(len(seats)),
            'short_name': 'council-2026', 'name': 'Council 2026', 'description': 'd',
            'election_type': 'election', 'help_email': 'coadmin@example.org'}
    for i, s in enumerate(seats):
      for k, v in s.items():
        data['form-%d-%s' % (i, k)] = v
    data.update(extra)
    return data


class VoterGroupAdminsTests(_GroupWithAdminsTestCase):
  """Additional administrators of a voter group."""

  # access

  def test_coadmin_sees_and_edits_group(self):
    self.login(self.coadmin)
    response = self.client.get('/helios/votergroup/view/g1')
    self.assertContains(response, 'anna@example.org')
    self.assertContains(response, '/helios/votergroup/admins/g1')
    response = self.client.post('/helios/votergroup/memberadd/g1', {
      'csrf_token': 'tok', 'first_name': 'Bea', 'last_name': 'Bach', 'email': 'bea@example.org', 'votes': '1'})
    self.assertEqual(response.status_code, 302)
    self.assertEqual(self.group.get_members().count(), 2)
    self.assertEqual(self.client.post('/helios/votergroup/memberdelete/m1', {'csrf_token': 'tok'}).status_code, 302)
    self.assertFalse(VoterGroupMember.objects.filter(uuid='m1').exists())

  def test_stranger_gets_404_everywhere(self):
    self.login(self.stranger)
    for url in ['/helios/votergroup/view/g1', '/helios/votergroup/memberadd/g1',
                '/helios/votergroup/prepareelection/g1', '/helios/votergroup/admins/g1',
                '/helios/votergroup/admins/g1/add',
                '/helios/votergroup/admins/g1/remove?user_id=coadmin&user_type=password']:
      self.assertEqual(self.client.get(url).status_code, 404, url)
    response = self.client.post('/helios/votergroup/admins/g1/add', {'csrf_token': 'tok', 'email': 'stranger'})
    self.assertEqual(response.status_code, 404)
    response = self.client.post('/helios/votergroup/admins/g1/remove', {'csrf_token': 'tok', 'user_id': 'coadmin', 'user_type': 'password'})
    self.assertEqual(response.status_code, 404)
    self.assertEqual(self.client.post('/helios/votergroup/memberdelete/m1', {'csrf_token': 'tok'}).status_code, 404)
    self.assertFalse(self.group.admins.filter(pk=self.stranger.pk).exists())
    self.assertTrue(self.group.admins.filter(pk=self.coadmin.pk).exists())
    self.assertTrue(VoterGroupMember.objects.filter(uuid='m1').exists())

  def test_list_shows_own_and_coadministered_groups_with_owner(self):
    VoterGroup.objects.create(admin=self.coadmin, uuid='g2', short_name='council')
    self.login(self.coadmin)
    response = self.client.get('/helios/votergroup/administered')
    self.assertContains(response, 'votergroup/view/g1')
    self.assertContains(response, 'votergroup/view/g2')
    # the owner's name tells the two groups named "council" apart
    self.assertContains(response, 'Olga Owner', count=1)
    self.login(self.third)
    self.assertNotContains(self.client.get('/helios/votergroup/administered'), 'votergroup/view/g1')

  def test_coadmin_with_own_group_of_same_name_can_still_create_it(self):
    self.login(self.coadmin)
    self.client.post('/helios/votergroup/new', {'csrf_token': 'tok', 'short_name': 'council'})
    self.assertTrue(VoterGroup.objects.filter(admin=self.coadmin, short_name='council').exists())

  # adding administrators

  def test_admins_page_lists_owner_and_admins(self):
    self.login(self.coadmin)
    response = self.client.get('/helios/votergroup/admins/g1')
    self.assertContains(response, 'Olga Owner')
    self.assertContains(response, 'Carl Coadmin')
    # the owner cannot be removed: no remove link for them
    self.assertNotContains(response, 'user_id=owner')
    self.assertContains(response, 'user_id=coadmin')

  def test_add_admin(self):
    self.login(self.coadmin)
    self.assertEqual(self.client.get('/helios/votergroup/admins/g1/add').status_code, 200)
    with self.assertLogs('helios.votergroup', 'INFO') as logs:
      response = self.client.post('/helios/votergroup/admins/g1/add', {'csrf_token': 'tok', 'email': 'third'})
    self.assertEqual(response.status_code, 302)
    self.assertTrue(self.group.admins.filter(pk=self.third.pk).exists())
    self.assertIn('administrator password:%d *** added by password:%d ***' % (self.third.pk, self.coadmin.pk), logs.output[0])
    # the new administrator sees the group
    self.login(self.third)
    self.assertEqual(self.client.get('/helios/votergroup/view/g1').status_code, 200)

  def test_add_admin_requires_csrf(self):
    self.login(self.owner)
    response = self.client.post('/helios/votergroup/admins/g1/add', {'csrf_token': 'wrong', 'email': 'third'})
    self.assertEqual(response.status_code, 400)
    self.assertFalse(self.group.admins.filter(pk=self.third.pk).exists())

  def test_add_admin_errors(self):
    self.login(self.owner)
    for email, message in [('nobody', 'must log in to Helios at least once'),
                           ('owner', 'is the owner of the voter group'),
                           ('coadmin', 'already an administrator'),
                           ('', 'This field is required')]:
      response = self.client.post('/helios/votergroup/admins/g1/add', {'csrf_token': 'tok', 'email': email})
      self.assertContains(response, message, msg_prefix=email)
    self.assertEqual(self.group.admins.count(), 1)

  def test_add_admin_who_cannot_create_elections(self):
    from unittest import mock
    self.login(self.owner)
    with mock.patch('helios.ADMIN_ONLY', True):
      self.owner.admin_p = True
      self.owner.save()
      response = self.client.post('/helios/votergroup/admins/g1/add', {'csrf_token': 'tok', 'email': 'third'})
    self.assertContains(response, 'cannot create elections')
    self.assertFalse(self.group.admins.filter(pk=self.third.pk).exists())

  def test_add_admin_with_several_auth_systems(self):
    google = User.objects.create(user_type='google', user_id='third', name='Tina G', info={'email': 'third@example.org'})
    self.login(self.owner)
    response = self.client.post('/helios/votergroup/admins/g1/add', {'csrf_token': 'tok', 'email': 'third'})
    self.assertContains(response, 'name="auth_type" id="auth_type_2" value="')
    self.assertFalse(self.group.admins.filter(pk=google.pk).exists())
    response = self.client.post('/helios/votergroup/admins/g1/add', {'csrf_token': 'tok', 'email': 'third', 'auth_type': 'google'})
    self.assertEqual(response.status_code, 302)
    self.assertEqual(list(self.group.admins.exclude(pk=self.coadmin.pk)), [google])
    response = self.client.post('/helios/votergroup/admins/g1/add', {'csrf_token': 'tok', 'email': 'third', 'auth_type': 'github'})
    self.assertEqual(response.status_code, 404)

  # removing administrators

  def test_remove_admin(self):
    self.group.admins.add(self.third)
    self.login(self.coadmin)
    url = '/helios/votergroup/admins/g1/remove'
    self.assertContains(self.client.get(url, {'user_id': 'third', 'user_type': 'password'}), 'Tina Third')
    self.assertEqual(self.client.post(url, {'csrf_token': 'wrong', 'user_id': 'third', 'user_type': 'password'}).status_code, 400)
    response = self.client.post(url, {'csrf_token': 'tok', 'user_id': 'third', 'user_type': 'password'})
    self.assertTrue(response['Location'].endswith('/helios/votergroup/admins/g1'))
    self.assertFalse(self.group.admins.filter(pk=self.third.pk).exists())

  def test_owner_and_non_admins_cannot_be_removed(self):
    self.login(self.coadmin)
    for user_id in ['owner', 'third', 'nobody']:
      response = self.client.post('/helios/votergroup/admins/g1/remove', {'csrf_token': 'tok', 'user_id': user_id, 'user_type': 'password'})
      self.assertEqual(response.status_code, 404, user_id)
    self.assertEqual(VoterGroup.objects.get(uuid='g1').admin, self.owner)

  def test_coadmin_can_leave_group(self):
    self.login(self.coadmin)
    response = self.client.get('/helios/votergroup/admins/g1/remove', {'user_id': 'coadmin', 'user_type': 'password'})
    self.assertContains(response, 'Leave')
    response = self.client.post('/helios/votergroup/admins/g1/remove', {'csrf_token': 'tok', 'user_id': 'coadmin', 'user_type': 'password'})
    self.assertTrue(response['Location'].endswith('/helios/votergroup/administered'))
    self.assertEqual(self.client.get('/helios/votergroup/view/g1').status_code, 404)

  # breadcrumbs

  def test_breadcrumbs_of_admin_pages(self):
    self.login(self.owner)
    response = self.client.get('/helios/votergroup/admins/g1/add')
    self.assertContains(response, 'href="/helios/votergroup/admins/g1"')
    self.assertContains(response, 'href="/helios/votergroup/view/g1"')

  # preparing an election

  def test_prepare_shows_option_only_with_other_admins(self):
    self.login(self.coadmin)
    self.assertContains(self.client.get('/helios/votergroup/prepareelection/g1'), 'name="add_group_admins"')
    self.group.admins.remove(self.coadmin)
    self.login(self.owner)
    self.assertNotContains(self.client.get('/helios/votergroup/prepareelection/g1'), 'name="add_group_admins"')

  def test_coadmin_prepares_election_with_group_admins(self):
    self.group.admins.add(self.third)
    self.login(self.coadmin)
    response = self.client.post('/helios/votergroup/prepareelection/g1', self._election_post(add_group_admins='on'))
    self.assertEqual(response.status_code, 302)
    election = Election.objects.get(short_name='council-2026')
    self.assertEqual(election.admin, self.coadmin)
    self.assertEqual(set(election.admins.all()), {self.owner, self.third})
    self.assertEqual(election.electionlog_set.filter(log__startswith='Administrator').count(), 2)

  def test_coadmin_prepares_election_without_group_admins(self):
    self.login(self.coadmin)
    # an unchecked checkbox is not sent
    response = self.client.post('/helios/votergroup/prepareelection/g1', self._election_post())
    self.assertEqual(response.status_code, 302)
    election = Election.objects.get(short_name='council-2026')
    self.assertEqual(election.admin, self.coadmin)
    self.assertFalse(election.admins.exists())
    # the election does not follow the group afterwards
    self.group.admins.add(self.third)
    self.assertFalse(election.admins.exists())


class FindUserToAddAsAdminTests(TestCase):

  def test_lookup(self):
    from django.http import Http404
    from helios.view_utils import find_user_to_add_as_admin
    a = User.objects.create(user_type='password', user_id='a@example.org', name='A', info={})
    self.assertEqual(find_user_to_add_as_admin(' a@example.org '), (a, None, None))
    self.assertIsNotNone(find_user_to_add_as_admin('')[2])
    self.assertIsNotNone(find_user_to_add_as_admin('b@example.org')[2])
    b = User.objects.create(user_type='google', user_id='a@example.org', name='A', info={})
    self.assertEqual(set(find_user_to_add_as_admin('a@example.org')[1]), {a, b})
    self.assertEqual(find_user_to_add_as_admin('a@example.org', 'google')[0], b)
    with self.assertRaises(Http404):
      find_user_to_add_as_admin('a@example.org', 'github')


class VoterGroupSiteAdminPrivacyTests(_GroupWithAdminsTestCase):
  """A site administrator who doesn't administer a group sees only its overview."""

  def setUp(self):
    super().setUp()
    self.siteadmin = User.objects.create(user_type='password', user_id='siteadmin', name='Sita Admin',
                                         info={'password': 'x', 'email': 'siteadmin@example.org'}, admin_p=True)

  def test_overview_without_member_data(self):
    self.login(self.siteadmin)
    response = self.client.get('/helios/votergroup/view/g1')
    self.assertEqual(response.status_code, 200)
    self.assertContains(response, 'only an overview is shown')
    self.assertContains(response, 'Olga Owner')
    self.assertContains(response, '<dt>Members</dt><dd>1</dd>', html=False)
    self.assertContains(response, '/helios/votergroup/transfer/g1')
    for hidden in ['anna@example.org', 'Arp', '/helios/votergroup/memberadd/', '/helios/votergroup/memberdelete/',
                   '/helios/votergroup/prepareelection/', '/helios/votergroup/admins/g1', 'votergroup-log']:
      self.assertNotContains(response, hidden, msg_prefix=hidden)
    # the list shows only the groups the site admin owns or co-administers
    self.assertNotContains(self.client.get('/helios/votergroup/administered'), 'votergroup/view/g1')

  def test_every_other_page_is_refused(self):
    self.login(self.siteadmin)
    for url in ['/helios/votergroup/memberadd/g1', '/helios/votergroup/prepareelection/g1',
                '/helios/votergroup/admins/g1', '/helios/votergroup/admins/g1/add',
                '/helios/votergroup/admins/g1/remove?user_id=coadmin&user_type=password']:
      self.assertEqual(self.client.get(url).status_code, 403, url)
    with self.assertLogs('helios.votergroup', 'INFO') as logs:
      posts = [
        ('/helios/votergroup/memberadd/g1', {'csrf_token': 'tok', 'first_name': 'B', 'last_name': 'B',
                                             'email': 'b@example.org', 'votes': '1'}),
        ('/helios/votergroup/memberdelete/m1', {'csrf_token': 'tok'}),
        ('/helios/votergroup/prepareelection/g1', self._election_post()),
        ('/helios/votergroup/admins/g1/add', {'csrf_token': 'tok', 'email': 'siteadmin'}),
        ('/helios/votergroup/admins/g1/remove', {'csrf_token': 'tok', 'user_id': 'coadmin', 'user_type': 'password'}),
      ]
      for url, data in posts:
        self.assertEqual(self.client.post(url, data).status_code, 403, url)
    self.assertIn('access refused, password:%d *** is a site administrator' % self.siteadmin.pk, logs.output[0])
    self.assertEqual([m.uuid for m in self.group.get_members()], ['m1'])
    self.assertEqual(list(self.group.admins.all()), [self.coadmin])
    self.assertFalse(Election.objects.exists())
    self.assertFalse(self.group.votergrouplog_set.exists())

  def test_site_admin_who_administers_the_group_has_full_access(self):
    self.group.admins.add(self.siteadmin)
    self.login(self.siteadmin)
    response = self.client.get('/helios/votergroup/view/g1')
    self.assertContains(response, 'anna@example.org')
    self.assertNotContains(response, 'only an overview is shown')
    self.assertEqual(self.client.get('/helios/votergroup/prepareelection/g1').status_code, 200)

  def test_strangers_still_get_404(self):
    self.login(self.stranger)
    self.assertEqual(self.client.get('/helios/votergroup/view/g1').status_code, 404)
    self.assertEqual(self.client.get('/helios/votergroup/transfer/g1').status_code, 404)

  def test_user_search_lists_groups_with_member_count(self):
    self.login(self.siteadmin)
    response = self.client.get('/helios/stats/user-search', {'q': 'Olga'})
    self.assertContains(response, 'Voter groups as owner or administrator (1)')
    self.assertContains(response, 'href="/helios/votergroup/view/g1"')
    self.assertContains(response, '1 member)')
    self.assertNotContains(response, 'anna@example.org')


class VoterGroupTransferTests(_GroupWithAdminsTestCase):
  """Transferring the ownership of a group."""

  URL = '/helios/votergroup/transfer/g1'

  def post(self, confirmed=True, keep=True, new_owner=None, **extra):
    data = {'csrf_token': 'tok', 'new_owner': str((new_owner or self.coadmin).pk)}
    if keep:
      data['keep_previous_owner'] = 'on'
    if confirmed:
      data['confirmed'] = '1'
    data.update(extra)
    return self.client.post(self.URL, data)

  def reload(self):
    return VoterGroup.objects.get(uuid='g1')

  def test_owner_transfers_after_confirmation_and_stays_admin(self):
    self.login(self.owner)
    response = self.client.get(self.URL)
    self.assertContains(response, 'Carl Coadmin')
    self.assertContains(response, 'Remain an administrator of the group')
    self.assertContains(response, 'name="keep_previous_owner" id="id_keep_previous_owner" checked')
    # first POST: the confirmation step, nothing changes yet
    response = self.post(confirmed=False)
    self.assertContains(response, 'the owner of voter group council?')
    self.assertContains(response, 'You remain an administrator of the group.')
    self.assertContains(response, 'name="confirmed" value="1"')
    self.assertEqual(self.reload().admin, self.owner)
    with self.assertLogs('helios.votergroup', 'INFO') as logs:
      response = self.post()
    self.assertTrue(response['Location'].endswith('/helios/votergroup/view/g1'))
    group = self.reload()
    self.assertEqual(group.admin, self.coadmin)
    self.assertEqual(list(group.admins.all()), [self.owner])
    self.assertIn('ownership transferred from password:%d *** to password:%d *** by password:%d ***'
                  % (self.owner.pk, self.coadmin.pk, self.owner.pk), logs.output[0])
    entry = group.get_log().first()
    self.assertEqual((entry.action, entry.actor), (VoterGroupLog.OWNER_CHANGED, self.owner))
    self.assertEqual(entry.message, 'Ownership transferred from Olga Owner (owner) to Carl Coadmin (coadmin)')
    # the previous owner still sees the group, the new owner can transfer it on
    self.assertContains(self.client.get('/helios/votergroup/view/g1'), 'anna@example.org')
    self.login(self.coadmin)
    self.assertEqual(self.client.get(self.URL).status_code, 200)

  def test_owner_can_leave_with_the_transfer(self):
    self.login(self.owner)
    response = self.post(confirmed=False, keep=False)
    self.assertContains(response, 'You will no longer be an administrator of this voter group')
    self.assertNotContains(response, 'name="keep_previous_owner"')
    response = self.post(keep=False)
    self.assertTrue(response['Location'].endswith('/helios/votergroup/administered'))
    group = self.reload()
    self.assertEqual(group.admin, self.coadmin)
    self.assertFalse(group.admins.exists())
    self.assertEqual(self.client.get('/helios/votergroup/view/g1').status_code, 404)
    self.assertIn('The previous owner is no longer an administrator.', group.get_log().first().message)

  def site_admin(self):
    return User.objects.create(user_type='password', user_id='siteadmin', name='Sita Admin',
                               info={'password': 'x'}, admin_p=True)

  def post_email(self, email, confirmed=True, keep=True, **extra):
    data = {'csrf_token': 'tok', 'email': email}
    if keep:
      data['keep_previous_owner'] = 'on'
    if confirmed:
      data['confirmed'] = '1'
    data.update(extra)
    return self.client.post(self.URL, data)

  def test_site_admin_transfers_to_any_eligible_user_by_email(self):
    siteadmin = self.site_admin()
    self.login(siteadmin)
    # the overview offers the email field
    response = self.client.get('/helios/votergroup/view/g1')
    self.assertContains(response, 'action="/helios/votergroup/transfer/g1"')
    self.assertContains(response, 'name="email"')
    response = self.client.get(self.URL)
    self.assertContains(response, 'name="email"')
    self.assertContains(response, 'Keep the previous owner as an administrator of the group')
    self.assertNotContains(response, 'name="new_owner"')
    self.assertNotContains(response, 'anna@example.org')
    # Tina is not an administrator of the group: first the confirmation step
    response = self.post_email('third', confirmed=False)
    self.assertContains(response, 'Tina Third')
    self.assertContains(response, 'The previous owner remains an administrator of the group.')
    self.assertContains(response, 'name="auth_type" value="password"')
    self.assertEqual(self.reload().admin, self.owner)
    with self.assertLogs('helios.votergroup', 'INFO') as logs:
      response = self.post_email('third', keep=False, auth_type='password')
    self.assertTrue(response['Location'].endswith('/helios/votergroup/view/g1'))
    group = self.reload()
    self.assertEqual(group.admin, self.third)
    self.assertEqual(list(group.admins.all()), [self.coadmin])
    self.assertIn('to password:%d *** by site administrator password:%d ***' % (self.third.pk, siteadmin.pk),
                  logs.output[0])
    entry = group.get_log().first()
    self.assertEqual(entry.actor, siteadmin)
    self.assertTrue(entry.params['by_site_admin'])
    self.assertEqual(entry.message, 'Ownership transferred from Olga Owner (owner) to Tina Third (third). '
                                    'Transferred by a site administrator. The previous owner is no longer an administrator.')
    # the new owner administers the group, the site administrator still sees the overview only
    self.assertContains(self.client.get('/helios/votergroup/view/g1'), 'only an overview is shown')
    self.login(self.third)
    self.assertContains(self.client.get('/helios/votergroup/view/g1'), 'anna@example.org')

  def test_site_admin_transfer_to_a_coadmin_removes_them_from_the_admins(self):
    self.login(self.site_admin())
    self.assertEqual(self.post_email('coadmin').status_code, 302)
    group = self.reload()
    self.assertEqual(group.admin, self.coadmin)
    self.assertEqual(list(group.admins.all()), [self.owner])

  def test_site_admin_transfer_refuses_unknown_and_ineligible_users(self):
    from unittest import mock
    self.login(self.site_admin())
    for email, message in [('nobody@example.org', 'before they can become the owner'),
                           ('owner', 'is the owner of the voter group'),
                           ('', 'This field is required')]:
      for confirmed in (False, True):
        self.assertContains(self.post_email(email, confirmed=confirmed), message, msg_prefix=email)
    with mock.patch('helios.ADMIN_ONLY', True):
      self.assertContains(self.post_email('third'), 'cannot create elections')
    # deactivated users are not found
    self.third.deactivated_at = datetime.datetime.utcnow()
    self.third.save()
    self.assertContains(self.post_email('third'), 'before they can become the owner')
    self.assertEqual(self.reload().admin, self.owner)
    self.assertFalse(self.group.votergrouplog_set.exists())

  def test_site_admin_transfer_name_clash(self):
    VoterGroup.objects.create(admin=self.third, uuid='g2', short_name='council')
    self.login(self.site_admin())
    for confirmed in (False, True):
      self.assertContains(self.post_email('third', confirmed=confirmed),
                          'Tina Third already owns a voter group named council')
    self.assertEqual(self.reload().admin, self.owner)

  def test_site_admin_transfer_with_several_auth_systems(self):
    google = User.objects.create(user_type='google', user_id='third', name='Tina G', info={})
    self.login(self.site_admin())
    response = self.post_email('third', confirmed=False)
    self.assertContains(response, 'Which one should become the owner?')
    self.assertContains(response, 'name="auth_type" id="auth_type_2"')
    # even with confirmed=1, nothing happens before a user is chosen
    self.post_email('third')
    self.assertEqual(self.reload().admin, self.owner)
    self.assertContains(self.post_email('third', confirmed=False, auth_type='google'), 'Tina G')
    self.post_email('third', auth_type='google')
    self.assertEqual(self.reload().admin, google)
    self.assertEqual(self.post_email('third', auth_type='github').status_code, 404)

  def test_owner_cannot_pick_a_user_by_email(self):
    self.login(self.owner)
    self.assertNotContains(self.client.get(self.URL), 'name="email"')
    # the owner's form ignores an email; a non-administrator can't be chosen
    response = self.post_email('third')
    self.assertContains(response, 'This field is required')
    self.assertContains(self.post(new_owner=self.third), 'Select a valid choice')
    self.assertEqual(self.reload().admin, self.owner)

  def test_only_owner_and_site_admins_may_transfer(self):
    self.login(self.coadmin)
    self.assertEqual(self.client.get(self.URL).status_code, 403)
    self.assertEqual(self.post(new_owner=self.coadmin).status_code, 403)
    self.login(self.stranger)
    self.assertEqual(self.client.get(self.URL).status_code, 404)
    self.assertEqual(self.post().status_code, 404)
    self.assertEqual(self.reload().admin, self.owner)
    # the link is only shown to who may use it
    self.login(self.coadmin)
    self.assertNotContains(self.client.get('/helios/votergroup/view/g1'), self.URL)
    self.assertNotContains(self.client.get('/helios/votergroup/admins/g1'), self.URL)
    self.login(self.owner)
    self.assertContains(self.client.get('/helios/votergroup/view/g1'), self.URL)
    self.assertContains(self.client.get('/helios/votergroup/admins/g1'), self.URL)

  def test_requires_csrf(self):
    self.login(self.owner)
    self.assertEqual(self.post(csrf_token='wrong').status_code, 400)
    self.assertEqual(self.reload().admin, self.owner)

  def test_new_owner_must_be_an_additional_administrator(self):
    self.login(self.owner)
    for user in (self.stranger, self.owner):
      response = self.post(new_owner=user)
      self.assertContains(response, 'Select a valid choice')
    self.assertEqual(self.reload().admin, self.owner)

  def test_new_owner_with_group_of_same_name(self):
    VoterGroup.objects.create(admin=self.coadmin, uuid='g2', short_name='council')
    self.login(self.owner)
    for confirmed in (False, True):
      response = self.post(confirmed=confirmed)
      self.assertContains(response, 'Carl Coadmin already owns a voter group named council')
    self.assertEqual(self.reload().admin, self.owner)
    self.assertFalse(self.group.votergrouplog_set.exists())

  def test_new_owner_who_cannot_create_elections(self):
    from unittest import mock
    self.login(self.owner)
    with mock.patch('helios.ADMIN_ONLY', True):
      response = self.post()
    self.assertContains(response, 'cannot create elections')
    self.assertEqual(self.reload().admin, self.owner)

  def test_group_without_additional_administrators(self):
    self.group.admins.clear()
    self.login(self.owner)
    response = self.client.get(self.URL)
    self.assertContains(response, 'this group has none besides its owner')
    self.assertNotContains(response, 'name="new_owner"')


class VoterGroupLogTests(_GroupWithAdminsTestCase):
  """The group's log in the database, shown to its administrators."""

  def actions(self):
    return [e.action for e in VoterGroup.objects.get(uuid='g1').get_log()]

  def test_changes_are_recorded(self):
    self.login(self.owner)
    self.client.post('/helios/votergroup/memberadd/g1', {'csrf_token': 'tok', 'first_name': 'Bea', 'last_name': 'Bach',
                                                         'email': 'bea@example.org', 'votes': '3'})
    self.client.post('/helios/votergroup/memberdelete/m1', {'csrf_token': 'tok'})
    self.client.post('/helios/votergroup/admins/g1/add', {'csrf_token': 'tok', 'email': 'third'})
    self.client.post('/helios/votergroup/admins/g1/remove', {'csrf_token': 'tok', 'user_id': 'third', 'user_type': 'password'})
    self.login(self.coadmin)
    self.client.post('/helios/votergroup/prepareelection/g1', self._election_post(add_group_admins='on'))
    self.client.post('/helios/votergroup/admins/g1/remove', {'csrf_token': 'tok', 'user_id': 'coadmin', 'user_type': 'password'})
    self.assertEqual(self.actions(), [VoterGroupLog.ADMIN_LEFT, VoterGroupLog.ELECTION_PREPARED, VoterGroupLog.ADMIN_REMOVED,
                                      VoterGroupLog.ADMIN_ADDED, VoterGroupLog.MEMBER_REMOVED, VoterGroupLog.MEMBER_ADDED])
    log = list(VoterGroup.objects.get(uuid='g1').get_log())
    election = Election.objects.get(short_name='council-2026')
    self.assertEqual([e.message for e in log], [
      'Administrator Carl Coadmin (coadmin) left the group',
      'Election Council 2026 prepared with 3 voter(s). The other administrators of the group were added to the election administrators.',
      'Administrator Tina Third (third) removed',
      'Administrator Tina Third (third) added',
      'Member a***@example.org removed',
      'Member b***@example.org added with 3 vote(s)',
    ])
    self.assertEqual(log[1].params['election_uuid'], election.uuid)
    self.assertEqual([e.actor for e in log], [self.coadmin, self.coadmin] + [self.owner] * 4)
    # no full addresses or member names in the log
    for entry in log:
      self.assertNotIn('bea@example.org', str(entry.params))
      self.assertNotIn('Arp', str(entry.params))

  def test_create_is_recorded(self):
    self.login(self.owner)
    self.client.post('/helios/votergroup/new', {'csrf_token': 'tok', 'short_name': 'senate'})
    entry = VoterGroup.objects.get(short_name='senate').get_log().get()
    self.assertEqual((entry.action, entry.actor, entry.message), (VoterGroupLog.CREATED, self.owner, 'Voter group senate created'))

  def test_shown_to_group_admins_newest_first_and_paginated(self):
    for i in range(25):
      self.group.append_log(VoterGroupLog.MEMBER_ADDED, actor=self.owner, email='x%02d***@example.org' % i, votes=1)
    self.login(self.coadmin)
    content = self.client.get('/helios/votergroup/view/g1').content.decode()
    self.assertIn('Member x24***@example.org added with 1 vote(s)', content)
    self.assertLess(content.index('x24***'), content.index('x23***'))
    self.assertNotIn('x04***', content)
    self.assertIn('Entries 1 - 20 (of 25)', content)
    content = self.client.get('/helios/votergroup/view/g1', {'log_page': '2'}).content.decode()
    self.assertIn('x04***', content)
    self.assertNotIn('x05***', content)
    # site administrators who don't administer the group don't see it
    siteadmin = User.objects.create(user_type='password', user_id='siteadmin', name='S', info={}, admin_p=True)
    self.login(siteadmin)
    self.assertNotContains(self.client.get('/helios/votergroup/view/g1'), 'x24***')

  def test_in_german(self):
    self.group.append_log(VoterGroupLog.MEMBER_REMOVED, actor=self.owner, email='a***@example.org')
    self.login(self.owner)
    response = self.client.get('/helios/votergroup/view/g1', HTTP_ACCEPT_LANGUAGE='de')
    self.assertContains(response, 'Mitglied a***@example.org entfernt')

  def test_unknown_action_or_missing_params(self):
    self.assertEqual(self.group.append_log('something new').message, 'something new')
    self.assertEqual(self.group.append_log(VoterGroupLog.MEMBER_ADDED).message, VoterGroupLog.MEMBER_ADDED)

  def test_actor_deletion_keeps_entry(self):
    self.group.append_log(VoterGroupLog.ADMIN_ADDED, actor=self.third, admin='Tina Third (third)')
    self.third.delete()
    entry = self.group.get_log().get()
    self.assertIsNone(entry.actor)


class PrepareElectionWordingTests(_GroupWithAdminsTestCase):

  def test_checkbox_label(self):
    self.login(self.owner)
    self.assertContains(self.client.get('/helios/votergroup/prepareelection/g1'),
                        'Add the other administrators of the group to the election administrators')
    self.assertContains(self.client.get('/helios/votergroup/prepareelection/g1', HTTP_ACCEPT_LANGUAGE='de'),
                        'Die anderen Administrator*innen der Gruppe zur Wahlleitung hinzufügen')
