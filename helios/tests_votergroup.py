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
