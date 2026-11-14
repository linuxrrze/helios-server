"""
Tests for voter groups.
"""

from django.test import TestCase

from helios import votergroup as vg
from helios.models import Election, Voter, VoterGroup, VoterGroupMember
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


class VoterGroupAdminsTests(TestCase):
  """Additional administrators of a voter group."""

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

  def test_site_admin_can_open_any_group_like_any_election(self):
    self.stranger.admin_p = True
    self.stranger.save()
    self.login(self.stranger)
    self.assertContains(self.client.get('/helios/votergroup/view/g1'), 'anna@example.org')
    # but the list shows only the groups the site admin owns or co-administers
    self.assertNotContains(self.client.get('/helios/votergroup/administered'), 'votergroup/view/g1')

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
