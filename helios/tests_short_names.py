"""
Tests for election addresses: optional short names (default: the election's UUID)
and per-organization prefixes (ELECTION_SHORT_NAME_PREFIXES_BY_IDP,
ELECTION_SHORT_NAME_PREFIXES, helios/short_names.py).
"""

import datetime
import os
import runpy
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

import settings as project_settings
from helios import short_names, views
from helios import votergroup as vg
from helios.tests import run_queued_tasks
from helios.models import Election, Trustee, Voter, VoterGroup, VoterGroupMember
from helios_auth.models import User

UNIS = {'uni-a.example.org': 'unia', 'uni-b.example.org': 'unib', 'med.uni-a.example.org': 'unimed'}
IDP_B = 'https://idp.uni-b.example.org/realms/staff'
IDPS = {'saml': 'unia', IDP_B: 'unib', 'ldap': 'unic'}
BASE = 'http://localhost:8000/helios/e/'


class PrefixSettingTests(TestCase):

  def test_empty(self):
    self.assertEqual(project_settings.parse_short_name_prefixes('', ''), ({}, ''))

  def test_mapping_is_normalized(self):
    mapping, default = project_settings.parse_short_name_prefixes('{"@Uni-A.example.org.": "unia"}', 'site')
    self.assertEqual(mapping, {'uni-a.example.org': 'unia'})
    self.assertEqual(default, 'site')

  def test_invalid_values(self):
    for mapping_json, default, message in [
        ('{"uni-a.example.org": "unia"', '', 'not valid JSON'),
        ('["unia"]', '', 'must be a JSON object'),
        ('{"uni-a.example.org": "Uni-A"}', '', 'is not valid'),
        ('{"uni-a.example.org": 1}', '', 'is not valid'),
        ('{"jane@uni-a.example.org": "unia"}', '', 'is not an email domain'),
        ('', 'with space', 'ELECTION_SHORT_NAME_PREFIX '),
    ]:
      with self.subTest(mapping_json=mapping_json, default=default):
        with self.assertRaisesMessage(ImproperlyConfigured, message):
          project_settings.parse_short_name_prefixes(mapping_json, default)

  def test_invalid_json_stops_start(self):
    env = dict(os.environ, DEBUG='1', ELECTION_SHORT_NAME_PREFIXES='{"uni-a.example.org": unia}')
    with mock.patch.dict(os.environ, env, clear=True), mock.patch('sys.argv', ['manage.py', 'runserver']):
      with self.assertRaisesMessage(ImproperlyConfigured, 'ELECTION_SHORT_NAME_PREFIXES is not valid JSON'):
        runpy.run_path(os.path.join(settings.ROOT_PATH, 'settings.py'))

  def test_read_from_environment(self):
    env = dict(os.environ, DEBUG='1', ELECTION_SHORT_NAME_PREFIXES='{"uni-a.example.org": "unia"}',
               ELECTION_SHORT_NAME_PREFIX='site', ELECTION_SHORT_NAME_PREFIXES_BY_IDP='{"saml": "unia"}')
    with mock.patch.dict(os.environ, env, clear=True), mock.patch('sys.argv', ['manage.py', 'runserver']):
      values = runpy.run_path(os.path.join(settings.ROOT_PATH, 'settings.py'))
    self.assertEqual(values['ELECTION_SHORT_NAME_PREFIXES'], {'uni-a.example.org': 'unia'})
    self.assertEqual(values['ELECTION_SHORT_NAME_PREFIX'], 'site')
    self.assertEqual(values['ELECTION_SHORT_NAME_PREFIXES_BY_IDP'], {'saml': 'unia'})


class PrefixByIdPSettingTests(TestCase):

  def test_empty(self):
    self.assertEqual(project_settings.parse_short_name_prefixes_by_idp(''), {})
    self.assertEqual(settings.ELECTION_SHORT_NAME_PREFIXES_BY_IDP, {})

  def test_mapping_is_normalized(self):
    mapping = project_settings.parse_short_name_prefixes_by_idp(
      '{" saml ": "unia", "https://idp.uni-b.example.org/realms/staff/": "unib", "urn:example:idp": "unic"}')
    # auth system names and issuers are kept as they are, apart from spaces and trailing slashes
    self.assertEqual(mapping, {'saml': 'unia', IDP_B: 'unib', 'urn:example:idp': 'unic'})

  def test_invalid_values(self):
    for mapping_json, message in [
        ('{"saml": "unia"', 'ELECTION_SHORT_NAME_PREFIXES_BY_IDP is not valid JSON'),
        ('["unia"]', 'must be a JSON object'),
        ('{"saml": "Uni-A"}', 'is not valid'),
        ('{"saml": ""}', 'is not valid'),
        ('{"saml": null}', 'is not valid'),
        ('{"": "unia"}', 'is not an auth system name or issuer'),
        ('{"/": "unia"}', 'is not an auth system name or issuer'),
        ('{"my saml": "unia"}', 'is not an auth system name or issuer'),
    ]:
      with self.subTest(mapping_json=mapping_json):
        with self.assertRaisesMessage(ImproperlyConfigured, message):
          project_settings.parse_short_name_prefixes_by_idp(mapping_json)

  def test_invalid_json_stops_start(self):
    env = dict(os.environ, DEBUG='1', ELECTION_SHORT_NAME_PREFIXES_BY_IDP="{'saml': 'unia'}")
    with mock.patch.dict(os.environ, env, clear=True), mock.patch('sys.argv', ['manage.py', 'runserver']):
      with self.assertRaisesMessage(ImproperlyConfigured, 'ELECTION_SHORT_NAME_PREFIXES_BY_IDP is not valid JSON'):
        runpy.run_path(os.path.join(settings.ROOT_PATH, 'settings.py'))


def _user(email=None, user_id='jdoe', user_type='password', issuer=None):
  info = {'email': email} if email else {}
  if issuer is not None:
    info['issuer'] = issuer
  return SimpleNamespace(info=info, user_id=user_id, user_type=user_type)


class PrefixForUserTests(TestCase):

  def test_without_configuration(self):
    self.assertEqual(short_names.prefix_for_user(_user('jdoe@uni-a.example.org')), '')

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS)
  def test_mapping(self):
    for email, prefix in [
        ('jdoe@uni-a.example.org', 'unia'),
        ('jdoe@UNI-B.example.org', 'unib'),
        # subdomains match, the most specific entry wins
        ('jdoe@cs.uni-a.example.org', 'unia'),
        ('jdoe@lab.med.uni-a.example.org', 'unimed'),
        # only whole domain labels
        ('jdoe@xuni-a.example.org', ''),
        ('jdoe@example.org', ''),
    ]:
      with self.subTest(email=email):
        self.assertEqual(short_names.prefix_for_user(_user(email)), prefix)

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS)
  def test_scoped_login_id(self):
    # e.g. an eduPersonPrincipalName as user ID, without an email address
    self.assertEqual(short_names.prefix_for_user(_user(None, 'jdoe@uni-b.example.org')), 'unib')
    self.assertEqual(short_names.prefix_for_user(_user(None, 'jdoe')), '')

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS, ELECTION_SHORT_NAME_PREFIX='site')
  def test_default_prefix(self):
    self.assertEqual(short_names.prefix_for_user(_user('jdoe@uni-a.example.org')), 'unia')
    self.assertEqual(short_names.prefix_for_user(_user('jdoe@elsewhere.example.net')), 'site')
    self.assertEqual(short_names.prefix_for_user(_user(None)), 'site')


@override_settings(ELECTION_SHORT_NAME_PREFIXES_BY_IDP=IDPS)
class PrefixByIdPTests(TestCase):

  def prefix(self, *args, **kwargs):
    return short_names.prefix_for_user(_user(*args, **kwargs))

  def test_auth_system(self):
    self.assertEqual(self.prefix(None, user_type='saml'), 'unia')
    self.assertEqual(self.prefix(None, user_type='ldap'), 'unic')
    # names are compared exactly, like Helios' auth system names
    self.assertEqual(self.prefix(None, user_type='SAML'), '')
    self.assertEqual(self.prefix(None, user_type='password'), '')

  def test_issuer_before_auth_system(self):
    # e.g. the auth system "saml" with the IdP of another organization
    self.assertEqual(self.prefix(None, user_type='saml', issuer=IDP_B), 'unib')
    self.assertEqual(self.prefix(None, user_type='saml', issuer=IDP_B + '/'), 'unib')
    # an issuer without an entry: the auth system decides
    self.assertEqual(self.prefix(None, user_type='saml', issuer='https://idp.example.net'), 'unia')
    # logged in before the issuer was stored
    self.assertEqual(self.prefix(None, user_type='oidc'), '')
    self.assertEqual(self.prefix(None, user_type='oidc', issuer=IDP_B), 'unib')

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS, ELECTION_SHORT_NAME_PREFIX='site')
  def test_identity_provider_before_email_domain(self):
    self.assertEqual(self.prefix('jdoe@uni-b.example.org', user_type='saml'), 'unia')
    self.assertEqual(self.prefix('jdoe@uni-a.example.org', user_type='oidc', issuer=IDP_B), 'unib')
    # nothing configured for the identity provider: email domain, then the default prefix
    self.assertEqual(self.prefix('jdoe@uni-b.example.org', user_type='oidc', issuer='https://idp.example.net'), 'unib')
    self.assertEqual(self.prefix(None, 'jdoe@med.uni-a.example.org', user_type='google'), 'unimed')
    self.assertEqual(self.prefix('jdoe@elsewhere.example.net', user_type='google'), 'site')

  def test_odd_info(self):
    for info in (None, [], {'issuer': None}, {'issuer': 42}, {'issuer': ' '}):
      with self.subTest(info=info):
        user = SimpleNamespace(info=info, user_id='jdoe', user_type='saml')
        self.assertEqual(short_names.prefix_for_user(user), 'unia')
    self.assertEqual(short_names.prefix_for_user(None), '')

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS, ELECTION_SHORT_NAME_PREFIX='site')
  def test_all_prefixes_are_reserved(self):
    self.assertEqual(short_names.all_prefixes(), {'unia', 'unib', 'unic', 'unimed', 'site'})
    self.assertEqual(short_names.reserved_prefix('unic-board'), 'unic')
    self.assertIsNone(short_names.reserved_prefix('unicorn-board'))


class CaseTests(TestCase):
  """addresses that differ only in case collide; they are stored as entered"""

  def setUp(self):
    self.admin = User.objects.create(user_type='password', user_id='a', name='a', info={})

  def test_reserved_prefix_in_any_case(self):
    with override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS):
      for name in ('unia-board', 'UNIA-board', 'UniA-Board'):
        with self.subTest(name=name):
          self.assertEqual(short_names.reserved_prefix(name), 'unia')
      self.assertIsNone(short_names.reserved_prefix('unia_board'))

  def test_is_taken_in_any_case(self):
    election = Election.objects.create(admin=self.admin, uuid='e1', short_name='Board-2026', name='x')
    for name in ('Board-2026', 'board-2026', 'BOARD-2026'):
      with self.subTest(name=name):
        self.assertTrue(short_names.is_taken(name))
    self.assertFalse(short_names.is_taken('board-2027'))
    # the election itself: changing the case of its own address is fine
    self.assertFalse(short_names.is_taken('board-2026', election))

  def test_deleted_election_in_any_case(self):
    election = Election.objects.create(admin=self.admin, uuid='e1', short_name='board', name='x')
    election.soft_delete()
    self.assertTrue(short_names.is_taken('BOARD'))


class AddressTestCase(TestCase):

  def setUp(self):
    self.unia = self.make_user('unia-admin', 'admin@uni-a.example.org')
    self.unib = self.make_user('unib-admin', 'admin@uni-b.example.org')
    self.other = self.make_user('other-admin', 'admin@elsewhere.example.net')

  def make_user(self, user_id, email):
    return User.objects.create(user_type='password', user_id=user_id, name=user_id, info={'password': 'x', 'email': email})

  def login(self, user):
    self.client.get('/')
    session = self.client.session
    session['user'] = {'type': user.user_type, 'user_id': user.user_id}
    session['csrf_token'] = 'tok'
    session.save()

  def election_data(self, **kwargs):
    return dict({'csrf_token': 'tok', 'name': 'Board election', 'description': 'd',
                 'election_type': 'election', 'help_email': 'help@example.org'}, **kwargs)

  def create(self, user, short_name):
    self.login(user)
    return self.client.post('/helios/elections/new', self.election_data(short_name=short_name))


class SingleFormTests(AddressTestCase):

  def test_form_explains_the_address(self):
    self.login(self.other)
    response = self.client.get('/helios/elections/new')
    self.assertContains(response, 'Address of the election')
    self.assertContains(response, '<span class="optional-marker">(optional)</span>')
    self.assertContains(response, '<span class="address-input-base" id="id_short_name_base">%s</span>' % BASE, html=False)
    self.assertContains(response, 'aria-describedby="id_short_name_base id_short_name_helptext"')
    self.assertContains(response, 'Leave empty to have one created automatically')
    self.assertContains(response, '/static/helios/election-address.js')

  def test_empty_gives_uuid(self):
    response = self.create(self.other, '')
    election = Election.objects.get(name='Board election')
    self.assertEqual(election.short_name, election.uuid)
    self.assertTrue(response['location'].endswith('/helios/elections/%s/view' % election.uuid))

  def test_chosen_name_without_configuration(self):
    self.create(self.unia, 'board-2026')
    self.assertTrue(Election.objects.filter(short_name='board-2026').exists())

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS)
  def test_prefix(self):
    self.login(self.unia)
    response = self.client.get('/helios/elections/new')
    self.assertContains(response, '%s<strong>unia-</strong>' % BASE)
    self.create(self.unia, 'board-2026')
    self.create(self.unib, 'board-2026')
    self.assertEqual(sorted(Election.objects.values_list('short_name', flat=True)), ['unia-board-2026', 'unib-board-2026'])

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS)
  def test_no_prefix_on_uuid(self):
    self.create(self.unia, '')
    election = Election.objects.get()
    self.assertEqual(election.short_name, election.uuid)

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS)
  def test_prefixes_are_reserved(self):
    # someone without a prefix can't take an address in an organization's namespace
    response = self.create(self.other, 'unia-board')
    self.assertContains(response, 'Addresses starting with &quot;unia-&quot; are reserved for another organization')
    self.assertFalse(Election.objects.exists())
    self.create(self.other, 'board')
    self.assertTrue(Election.objects.filter(short_name='board').exists())

  def test_taken(self):
    Election.objects.create(admin=self.unib, uuid='e0', short_name='board-2026', name='Secret ballot of B')
    response = self.create(self.unia, 'board-2026')
    self.assertContains(response, 'This address is already taken. Please choose a different one.')
    self.assertNotContains(response, 'Secret ballot of B')
    self.assertEqual(Election.objects.count(), 1)

  def test_taken_in_another_case(self):
    Election.objects.create(admin=self.unib, uuid='e0', short_name='board-2026', name='x')
    response = self.create(self.unia, 'Board-2026')
    self.assertContains(response, 'This address is already taken.')
    self.assertEqual(Election.objects.count(), 1)

  def test_stored_as_entered(self):
    self.create(self.unia, 'Board-2026')
    self.assertEqual(Election.objects.get().short_name, 'Board-2026')

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS)
  def test_prefixes_are_reserved_in_any_case(self):
    response = self.create(self.other, 'UNIA-board')
    self.assertContains(response, 'Addresses starting with &quot;unia-&quot; are reserved for another organization')
    self.assertFalse(Election.objects.exists())

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS, ELECTION_SHORT_NAME_PREFIXES_BY_IDP={'password': 'local'})
  def test_prefix_by_identity_provider(self):
    # the auth system decides before the email domain
    self.create(self.unia, 'board')
    self.assertEqual(Election.objects.get().short_name, 'local-board')

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS)
  def test_site_administrators_follow_the_same_rules(self):
    # no way around the prefixes for site administrators (admin_p) either
    for user in (self.unia, self.other):
      user.admin_p = True
      user.save()
    response = self.create(self.other, 'unia-board')
    self.assertContains(response, 'are reserved for another organization')
    self.assertFalse(Election.objects.exists())
    self.create(self.unia, 'board')
    self.assertEqual(Election.objects.get().short_name, 'unia-board')

  def test_taken_by_deleted_election(self):
    election = Election.objects.create(admin=self.unib, uuid='e0', short_name='board-2026', name='x')
    election.soft_delete()
    response = self.create(self.unia, 'board-2026')
    self.assertContains(response, 'This address is already taken.')

  def test_taken_in_the_meantime(self):
    # the form's check passes, the database refuses the duplicate
    Election.objects.create(admin=self.unib, uuid='e0', short_name='board-2026', name='x')
    with mock.patch('helios.short_names.is_taken', return_value=False):
      response = self.create(self.unia, 'board-2026')
    self.assertContains(response, 'This address is already taken.')

  def test_invalid_characters(self):
    response = self.create(self.unia, 'board 2026/ä')
    self.assertContains(response, 'error-summary')
    self.assertFalse(Election.objects.exists())

  def test_german(self):
    self.login(self.unia)
    response = self.client.get('/helios/elections/new', HTTP_ACCEPT_LANGUAGE='de')
    self.assertContains(response, 'Adresse der Wahl')
    response = self.client.post('/helios/elections/new', self.election_data(short_name='x y'), HTTP_ACCEPT_LANGUAGE='de')
    Election.objects.create(admin=self.unib, uuid='e0', short_name='board', name='x')
    response = self.client.post('/helios/elections/new', self.election_data(short_name='board'), HTTP_ACCEPT_LANGUAGE='de')
    self.assertContains(response, 'Diese Adresse ist bereits vergeben.')


class EditTests(AddressTestCase):

  def make_election(self, admin, short_name, uuid='e1'):
    return Election.objects.create(admin=admin, uuid=uuid, short_name=short_name, name='Board election',
                                   description='d', election_type='election')

  def edit(self, election, short_name):
    return self.client.post('/helios/elections/%s/edit' % election.uuid, self.election_data(short_name=short_name))

  def reload(self, election):
    return Election.objects.get(pk=election.pk)

  def test_emptying_gives_uuid(self):
    election = self.make_election(self.unia, 'board')
    self.login(self.unia)
    response = self.edit(election, '')
    self.assertEqual(response.status_code, 302)
    self.assertEqual(self.reload(election).short_name, 'e1')

  def test_uuid_shown_as_empty(self):
    election = self.make_election(self.unia, 'e1')
    self.login(self.unia)
    response = self.client.get('/helios/elections/e1/edit')
    self.assertContains(response, 'name="short_name"')
    self.assertNotContains(response, 'value="e1"')
    self.assertContains(response, 'Current address: <span class="address-url">%se1</span>' % BASE)
    # without a chosen address, no links can break
    self.assertNotContains(response, 'stop working if you change it')
    self.edit(election, '')
    self.assertEqual(self.reload(election).short_name, 'e1')

  def test_change(self):
    election = self.make_election(self.unia, 'board')
    self.login(self.unia)
    response = self.client.get('/helios/elections/e1/edit')
    self.assertContains(response, 'value="board"')
    self.assertContains(response, 'stop working if you change it')
    self.edit(election, 'board-2026')
    self.assertEqual(self.reload(election).short_name, 'board-2026')

  def test_taken(self):
    self.make_election(self.unib, 'senate', uuid='e0')
    election = self.make_election(self.unia, 'board')
    self.login(self.unia)
    response = self.edit(election, 'senate')
    self.assertContains(response, 'This address is already taken.')
    self.assertEqual(self.reload(election).short_name, 'board')

  def test_change_of_case(self):
    election = self.make_election(self.unia, 'board')
    self.login(self.unia)
    self.edit(election, 'Board')
    self.assertEqual(self.reload(election).short_name, 'Board')

  def test_taken_in_another_case(self):
    self.make_election(self.unib, 'senate', uuid='e0')
    election = self.make_election(self.unia, 'board')
    self.login(self.unia)
    response = self.edit(election, 'Senate')
    self.assertContains(response, 'This address is already taken.')
    self.assertEqual(self.reload(election).short_name, 'board')

  def test_existing_names_differing_in_case_are_kept(self):
    # from before the check: both stay, and both can still be saved unchanged
    self.make_election(self.unib, 'Board', uuid='e0')
    election = self.make_election(self.unia, 'board')
    self.login(self.unia)
    self.assertEqual(self.edit(election, 'board').status_code, 302)
    self.assertEqual(self.reload(election).short_name, 'board')

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS)
  def test_prefixed_name_shown_without_prefix(self):
    election = self.make_election(self.unia, 'unia-board')
    self.login(self.unia)
    response = self.client.get('/helios/elections/e1/edit')
    self.assertContains(response, 'value="board"')
    self.assertContains(response, '<strong>unia-</strong>')
    self.edit(election, 'board')
    self.assertEqual(self.reload(election).short_name, 'unia-board')
    self.edit(election, 'senate')
    self.assertEqual(self.reload(election).short_name, 'unia-senate')

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS)
  def test_existing_name_without_prefix_is_kept(self):
    # chosen before the prefixes were configured: links keep working
    election = self.make_election(self.unia, 'board')
    self.login(self.unia)
    response = self.client.get('/helios/elections/e1/edit')
    self.assertContains(response, 'value="board"')
    self.edit(election, 'board')
    self.assertEqual(self.reload(election).short_name, 'board')
    # a new one gets the prefix
    self.edit(election, 'board-2')
    self.assertEqual(self.reload(election).short_name, 'unia-board-2')

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS)
  def test_prefix_of_the_creator(self):
    election = self.make_election(self.unia, 'unia-board')
    election.admins.add(self.unib)
    self.login(self.unib)
    self.edit(election, 'senate')
    self.assertEqual(self.reload(election).short_name, 'unia-senate')

  def test_not_after_freezing(self):
    election = self.make_election(self.unia, 'board')
    election.frozen_at = datetime.datetime.utcnow()
    election.save()
    self.login(self.unia)
    self.assertEqual(self.edit(election, 'other').status_code, 403)
    self.assertEqual(self.reload(election).short_name, 'board')


class OtherWaysTests(AddressTestCase):
  """the guided setup, voter groups and copies"""

  WIZARD = '/helios/elections/new/guided'

  def wizard(self, data):
    return self.client.post(self.WIZARD, dict(data, csrf_token='tok'))

  def run_wizard(self, user, short_name):
    self.login(user)
    self.wizard({'step': 'basics', 'name': 'Board', 'short_name': short_name})
    self.wizard({'step': 'schedule', 'voting_starts_at': '', 'voting_ends_at': ''})
    self.wizard({'step': 'options', 'election_type': 'election'})
    review = self.client.get(self.WIZARD + '?step=review')
    self.wizard({'step': 'review'})
    return review, Election.objects.get(name='Board')

  def test_wizard_empty_gives_uuid(self):
    review, election = self.run_wizard(self.unia, '')
    self.assertEqual(election.short_name, election.uuid)
    self.assertContains(review, 'Created automatically')

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS)
  def test_wizard_prefix(self):
    self.login(self.unia)
    self.assertContains(self.client.get(self.WIZARD), '<strong>unia-</strong>')
    review, election = self.run_wizard(self.unia, 'board')
    self.assertEqual(election.short_name, 'unia-board')
    self.assertContains(review, BASE + 'unia-board')

  def voter_group_post(self, user, short_name):
    group = VoterGroup.objects.create(admin=user, uuid='g1', short_name='council')
    VoterGroupMember.objects.create(group=group, uuid='m1', first_name='Anna', last_name='Arp', email='anna@example.org', votes=1)
    self.login(user)
    data = self.election_data(short_name=short_name, **{'form-TOTAL_FORMS': '1', 'form-INITIAL_FORMS': '1'})
    for key, value in vg.initial_seats(group.get_members())[0].items():
      data['form-0-%s' % key] = value
    return self.client.post('/helios/votergroup/prepareelection/g1', data)

  def test_voter_group_empty_gives_uuid(self):
    response = self.voter_group_post(self.unia, '')
    self.assertEqual(response.status_code, 302)
    election = Election.objects.get()
    self.assertEqual(election.short_name, election.uuid)

  @override_settings(ELECTION_SHORT_NAME_PREFIXES=UNIS)
  def test_voter_group_prefix(self):
    self.voter_group_post(self.unib, 'council')
    self.assertEqual(Election.objects.get().short_name, 'unib-council')

  def test_voter_group_taken(self):
    Election.objects.create(admin=self.unib, uuid='e0', short_name='council', name='x')
    response = self.voter_group_post(self.unia, 'council')
    self.assertContains(response, 'This address is already taken.')

  def test_copy_gets_uuid(self):
    election = Election.objects.create(admin=self.unia, uuid='e1', short_name='board', name='Board', description='d')
    self.login(self.unia)
    self.client.post('/helios/elections/e1/copy', {'csrf_token': 'tok'})
    copy = Election.objects.exclude(pk=election.pk).get()
    self.assertEqual(copy.short_name, copy.uuid)


class LookupTests(AddressTestCase):
  """links typed in another case still find the election, unless ambiguous"""

  def make_election(self, short_name, uuid):
    return Election.objects.create(admin=self.unia, uuid=uuid, short_name=short_name, name='x')

  def view_url(self, uuid):
    return '/helios/elections/%s/view' % uuid

  def test_case_insensitive_hit(self):
    self.make_election('board-2026', 'e1')
    self.assertEqual(Election.get_by_short_name('Board-2026').uuid, 'e1')
    response = self.client.get('/helios/e/BOARD-2026')
    self.assertTrue(response['location'].endswith(self.view_url('e1')))

  def test_exact_wins_over_case_variant(self):
    # both from before the check
    self.make_election('Board', 'e1')
    self.make_election('board', 'e2')
    self.assertEqual(Election.get_by_short_name('Board').uuid, 'e1')
    self.assertEqual(Election.get_by_short_name('board').uuid, 'e2')
    self.assertTrue(self.client.get('/helios/e/board')['location'].endswith(self.view_url('e2')))

  def test_ambiguous_is_not_found(self):
    self.make_election('Board', 'e1')
    self.make_election('board', 'e2')
    self.assertIsNone(Election.get_by_short_name('BOARD'))
    self.assertEqual(self.client.get('/helios/e/BOARD').status_code, 404)
    self.assertEqual(self.client.get('/helios/e/BOARD/vote').status_code, 404)

  def test_not_found(self):
    self.make_election('board', 'e1')
    self.assertIsNone(Election.get_by_short_name('boards'))
    self.assertEqual(self.client.get('/helios/e/boards').status_code, 404)

  def test_deleted_elections(self):
    deleted = self.make_election('Board', 'e1')
    deleted.soft_delete()
    self.assertIsNone(Election.get_by_short_name('board'))
    self.assertEqual(Election.get_by_short_name('board', include_deleted=True).uuid, 'e1')
    # a deleted case variant doesn't make a live election ambiguous
    self.make_election('BOARD', 'e2')
    self.assertEqual(Election.get_by_short_name('board').uuid, 'e2')

  def test_vote_link(self):
    self.make_election('board', 'e1')
    response = self.client.get('/helios/e/Board/vote')
    # not started yet: the election was found
    self.assertEqual(response.status_code, 200)
    self.assertEqual(self.client.get('/helios/e/Boards/vote').status_code, 404)

  def test_trustee_link(self):
    election = self.make_election('board', 'e1')
    trustee = Trustee.objects.create(election=election, uuid='t1', name='T', email='t@example.org', secret='s3cret')
    from helios.security import HELIOS_TRUSTEE_UUID
    response = self.client.get('/helios/t/Board/t@example.org/s3cret')
    self.assertEqual(response.status_code, 302)
    self.assertTrue(response['location'].endswith('/helios/elections/e1/trustees/t1/home'))
    self.assertEqual(self.client.session[HELIOS_TRUSTEE_UUID], trustee.uuid)
    self.assertEqual(self.client.get('/helios/t/Board/t@example.org/wrong').status_code, 404)


class LinkTests(AddressTestCase):
  """links with the UUID as address: on the pages, in emails, for trustees"""

  def setUp(self):
    super().setUp()
    self.create(self.unia, '')
    self.election = Election.objects.get()
    self.uuid = self.election.uuid

  def test_urls(self):
    self.assertEqual(self.election.url, BASE + self.uuid)
    self.assertEqual(views.get_election_govote_url(self.election), BASE + self.uuid + '/vote')
    response = self.client.get('/helios/e/%s' % self.uuid)
    self.assertTrue(response['location'].endswith('/helios/elections/%s/view' % self.uuid))

  def test_voter_email(self):
    voter = Voter.objects.create(uuid='v1', election=self.election, voter_login_id='v1', voter_password='pw',
                                 voter_name='Voter', voter_email='voter@example.org')
    self.election.frozen_at = datetime.datetime.utcnow()
    self.election.save()
    response = self.client.post('/helios/elections/%s/voters/email' % self.uuid,
                                {'csrf_token': 'tok', 'subject': 'Vote', 'body': 'Please vote.', 'send_to': 'all'})
    self.assertEqual(response.status_code, 302)
    run_queued_tasks()
    self.assertIn('voter@example.org', mail.outbox[-1].to[0])
    self.assertIn(BASE + self.uuid + '/vote', mail.outbox[-1].body)
    self.assertEqual(voter.election.short_name, self.uuid)

  def test_trustee_link(self):
    trustee = Trustee.objects.create(election=self.election, uuid='t1', name='T', email='t@example.org', secret='s3cret')
    self.client.post('/helios/elections/%s/trustees/t1/sendurl' % self.uuid, {'csrf_token': 'tok'})
    run_queued_tasks()
    self.assertIn('/helios/t/%s/t@example.org/s3cret' % self.uuid, mail.outbox[-1].body)
    from helios.security import HELIOS_TRUSTEE_UUID
    response = self.client.get('/helios/t/%s/t@example.org/s3cret' % self.uuid)
    self.assertEqual(response.status_code, 302)
    self.assertEqual(self.client.session[HELIOS_TRUSTEE_UUID], trustee.uuid)
