"""
Tests for SCIM provisioning: the protocol (helios_scim), the mapping to Helios
(helios.provisioning) and the effects in Helios (login, rights, read-only voter
groups, badges). Tokens are dummies.
"""

import io
import json
from unittest import mock

from django.core import checks
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from helios import provisioning
from helios.models import Election, VoterGroup, VoterGroupLog, VoterGroupMember
from helios.security import user_can_create_election
from helios_auth.models import User
from . import filter as scim_filter
from .auth import hash_token
from .errors import ScimError
from .models import ScimGroup, ScimUser
from .patch import apply_patch

TOKEN = 'dummy-token-for-tests-0123456789abcdef'
OTHER_TOKEN = 'another-dummy-token-for-tests-0123456789'
BASE = '/scim/v2/'

SCIM_SETTINGS = dict(
  SCIM_ENABLED=True,
  SCIM_TOKENS=json.dumps({'idm': hash_token(TOKEN), 'other': OTHER_TOKEN}),
  SCIM_USER_TYPE='google',
  SCIM_USER_ID_ATTRIBUTE='userName',
  SCIM_ADMIN_GROUP='helios-admins',
  SCIM_ELECTION_CREATOR_GROUP='helios-election-creators',
  SCIM_VOTERGROUP_ADMINS_GROUP='helios-votergroup-admins',
  SCIM_VOTERGROUP_ADMINS_SUFFIX=' (admins)',
)


def user_body(user_name, **extra):
  body = {'schemas': ['urn:ietf:params:scim:schemas:core:2.0:User'], 'userName': user_name,
          'name': {'givenName': 'Jane', 'familyName': 'Doe'}, 'displayName': 'Jane Doe',
          'emails': [{'value': user_name, 'type': 'work', 'primary': True}], 'active': True}
  body.update(extra)
  return body


def patch_body(*operations):
  return {'schemas': ['urn:ietf:params:scim:api:messages:2.0:PatchOp'], 'Operations': list(operations)}


@override_settings(**SCIM_SETTINGS)
class ScimTestCase(TestCase):

  def call(self, method, path, body=None, token=TOKEN, **params):
    headers = {'HTTP_AUTHORIZATION': 'Bearer %s' % token} if token else {}
    url = BASE + path
    if method == 'GET':
      return self.client.get(url, params, **headers)
    return self.client.generic(method, url, json.dumps(body) if body is not None else '',
                               content_type='application/scim+json', **headers)

  def json(self, response):
    self.assertEqual(response['Content-Type'], 'application/scim+json')
    return json.loads(response.content)

  def create_user(self, user_name='jane@example.org', **extra):
    response = self.call('POST', 'Users', user_body(user_name, **extra))
    self.assertEqual(response.status_code, 201, response.content)
    return self.json(response)

  def create_group(self, name, members=(), token=TOKEN, **extra):
    body = {'schemas': ['urn:ietf:params:scim:schemas:core:2.0:Group'], 'displayName': name,
            'members': [{'value': m} for m in members]}
    body.update(extra)
    response = self.call('POST', 'Groups', body, token=token)
    self.assertEqual(response.status_code, 201, response.content)
    return self.json(response)

  def helios_user(self, user_id):
    return User.objects.get(user_type='google', user_id=user_id)

  def login(self, user):
    self.client.get('/')
    session = self.client.session
    session['user'] = {'type': user.user_type, 'user_id': user.user_id}
    session['csrf_token'] = 'tok'
    session.save()


class AuthenticationTests(ScimTestCase):

  @override_settings(SCIM_ENABLED=False)
  def test_disabled_is_404(self):
    self.assertEqual(self.call('GET', 'Users').status_code, 404)
    self.assertEqual(self.call('POST', 'Users', user_body('a@example.org')).status_code, 404)

  def test_missing_token_is_401(self):
    response = self.call('GET', 'Users', token=None)
    self.assertEqual(response.status_code, 401)
    self.assertIn('Bearer', response['WWW-Authenticate'])
    self.assertEqual(self.json(response)['schemas'], ['urn:ietf:params:scim:api:messages:2.0:Error'])

  def test_wrong_token_and_scheme_are_401(self):
    self.assertEqual(self.call('GET', 'Users', token='wrong-token-wrong-token-wrong-token').status_code, 401)
    response = self.client.get(BASE + 'Users', HTTP_AUTHORIZATION='Basic %s' % TOKEN)
    self.assertEqual(response.status_code, 401)
    self.assertFalse(ScimUser.objects.exists())

  def test_hashed_and_plain_tokens_identify_their_client(self):
    self.create_user('a@example.org')
    self.create_user_for_other = self.call('POST', 'Users', user_body('b@example.org'), token=OTHER_TOKEN)
    self.assertEqual(self.create_user_for_other.status_code, 201)
    self.assertEqual(self.helios_user('a@example.org').provisioned_by, 'scim:idm')
    self.assertEqual(self.helios_user('b@example.org').provisioned_by, 'scim:other')

  def test_several_tokens_per_client_for_rotation(self):
    with override_settings(SCIM_TOKENS=json.dumps({'idm': [hash_token('old-' + TOKEN), hash_token(TOKEN)]})):
      self.assertEqual(self.call('GET', 'Users').status_code, 200)
      self.assertEqual(self.call('GET', 'Users', token='old-' + TOKEN).status_code, 200)

  def test_invalid_token_configuration_rejects_everything_and_fails_check(self):
    with override_settings(SCIM_TOKENS='{"idm": "short"}'):
      self.assertEqual(self.call('GET', 'Users', token='short').status_code, 401)
      errors = [e for e in checks.run_checks() if e.id.startswith('helios_scim')]
      self.assertIn('helios_scim.E001', [e.id for e in errors])
      self.assertNotIn('short', ' '.join(e.msg for e in errors))

  def test_checks_pass_with_valid_configuration(self):
    self.assertEqual([e.id for e in checks.run_checks() if e.id.startswith('helios_scim')], [])

  def test_no_session_is_created(self):
    self.call('GET', 'Users')
    self.assertNotIn('sessionid', self.client.cookies)


class DiscoveryTests(ScimTestCase):

  def test_service_provider_config(self):
    data = self.json(self.call('GET', 'ServiceProviderConfig'))
    self.assertTrue(data['patch']['supported'])
    self.assertTrue(data['filter']['supported'])
    self.assertFalse(data['bulk']['supported'])
    self.assertEqual(data['authenticationSchemes'][0]['type'], 'oauthbearertoken')

  def test_resource_types_and_schemas(self):
    types = self.json(self.call('GET', 'ResourceTypes'))
    self.assertEqual({t['id'] for t in types['Resources']}, {'User', 'Group'})
    self.assertEqual(self.json(self.call('GET', 'ResourceTypes/User'))['endpoint'], '/Users')
    schemas = self.json(self.call('GET', 'Schemas'))
    self.assertEqual(schemas['totalResults'], 2)
    user_schema = self.json(self.call('GET', 'Schemas/urn:ietf:params:scim:schemas:core:2.0:User'))
    self.assertIn('userName', [a['name'] for a in user_schema['attributes']])
    self.assertEqual(self.call('GET', 'Schemas/urn:nope').status_code, 404)


class UserTests(ScimTestCase):

  def test_create_user_matches_login_identity(self):
    data = self.create_user('jane@example.org', externalId='ext-1')
    user = self.helios_user('jane@example.org')
    self.assertEqual(user.name, 'Jane Doe')
    self.assertEqual(user.info['email'], 'jane@example.org')
    self.assertEqual(user.external_id, 'ext-1')
    self.assertIsNotNone(user.provisioned_at)
    self.assertTrue(data['active'])
    self.assertEqual(data['meta']['resourceType'], 'User')
    self.assertTrue(data['meta']['location'].endswith('/scim/v2/Users/%s' % data['id']))

  def test_duplicate_user_name_is_409(self):
    self.create_user('jane@example.org')
    response = self.call('POST', 'Users', user_body('JANE@example.org'))
    self.assertEqual(response.status_code, 409)
    self.assertEqual(self.json(response)['scimType'], 'uniqueness')

  def test_user_of_another_client_is_409_and_invisible(self):
    data = self.create_user('jane@example.org')
    response = self.call('POST', 'Users', user_body('jane@example.org'), token=OTHER_TOKEN)
    self.assertEqual(response.status_code, 409)
    self.assertEqual(self.call('GET', 'Users/%s' % data['id'], token=OTHER_TOKEN).status_code, 404)
    self.assertEqual(self.json(self.call('GET', 'Users', token=OTHER_TOKEN))['totalResults'], 0)

  def test_missing_user_name_is_400(self):
    response = self.call('POST', 'Users', {'schemas': [], 'displayName': 'x'})
    self.assertEqual(response.status_code, 400)
    self.assertEqual(self.json(response)['scimType'], 'invalidValue')
    self.assertEqual(self.call('POST', 'Users', None).status_code, 400)
    response = self.client.generic('POST', BASE + 'Users', '{not json', content_type='application/scim+json',
                                   HTTP_AUTHORIZATION='Bearer %s' % TOKEN)
    self.assertEqual(self.json(response)['scimType'], 'invalidSyntax')

  def test_existing_user_who_logged_in_before_is_taken_over(self):
    existing = User.objects.create(user_type='google', user_id='jane@example.org', name='J', info={'email': 'x@example.org'})
    self.create_user('jane@example.org')
    self.assertEqual(User.objects.filter(user_type='google').count(), 1)
    existing.refresh_from_db()
    self.assertEqual(existing.provisioned_by, 'scim:idm')
    self.assertEqual(existing.info['email'], 'jane@example.org')

  def test_list_filter_and_pagination(self):
    for i in range(5):
      self.create_user('user%d@example.org' % i, externalId='e%d' % i)
    data = self.json(self.call('GET', 'Users', startIndex=2, count=2))
    self.assertEqual((data['totalResults'], data['startIndex'], data['itemsPerPage']), (5, 2, 2))
    self.assertEqual([r['userName'] for r in data['Resources']], ['user1@example.org', 'user2@example.org'])

    data = self.json(self.call('GET', 'Users', filter='userName eq "USER3@example.org"'))
    self.assertEqual([r['userName'] for r in data['Resources']], ['user3@example.org'])
    data = self.json(self.call('GET', 'Users', filter='externalId eq "e4"'))
    self.assertEqual(data['totalResults'], 1)
    data = self.json(self.call('GET', 'Users', filter='externalId eq "E4"'))  # caseExact
    self.assertEqual(data['totalResults'], 0)
    data = self.json(self.call('GET', 'Users', filter='emails[type eq "work" and value sw "user1"] or userName eq "user2@example.org"'))
    self.assertEqual(sorted(r['userName'] for r in data['Resources']), ['user1@example.org', 'user2@example.org'])
    data = self.json(self.call('GET', 'Users', filter='userName eq "user0@example.org"', attributes='userName'))
    self.assertEqual(set(data['Resources'][0]), {'schemas', 'id', 'meta', 'userName'})

  def test_invalid_filter_is_400(self):
    response = self.call('GET', 'Users', filter='userName xx "a"')
    self.assertEqual(response.status_code, 400)
    self.assertEqual(self.json(response)['scimType'], 'invalidFilter')

  def test_put_replaces_profile(self):
    data = self.create_user('jane@example.org')
    body = user_body('jane@example.org', displayName='Jane Roe', name={'givenName': 'Jane', 'familyName': 'Roe'})
    response = self.call('PUT', 'Users/%s' % data['id'], body)
    self.assertEqual(response.status_code, 200)
    self.assertEqual(self.helios_user('jane@example.org').name, 'Jane Roe')

  def test_rename_changes_helios_user_id(self):
    data = self.create_user('jane@example.org')
    pk = self.helios_user('jane@example.org').pk
    self.call('PATCH', 'Users/%s' % data['id'], patch_body({'op': 'replace', 'path': 'userName', 'value': 'jroe@example.org'}))
    self.assertEqual(User.objects.get(pk=pk).user_id, 'jroe@example.org')

  def test_patch_entra_style_deactivates(self):
    data = self.create_user('jane@example.org')
    response = self.call('PATCH', 'Users/%s' % data['id'], patch_body({'op': 'Replace', 'path': 'active', 'value': 'False'}))
    self.assertEqual(response.status_code, 200, response.content)
    self.assertFalse(self.json(response)['active'])
    self.assertFalse(self.helios_user('jane@example.org').is_active)
    self.call('PATCH', 'Users/%s' % data['id'], patch_body({'op': 'Replace', 'path': 'active', 'value': 'True'}))
    self.assertTrue(self.helios_user('jane@example.org').is_active)

  def test_patch_okta_style_without_path(self):
    data = self.create_user('jane@example.org')
    self.call('PATCH', 'Users/%s' % data['id'], patch_body({'op': 'replace', 'value': {'active': False, 'name.familyName': 'Roe'}}))
    user = self.helios_user('jane@example.org')
    self.assertFalse(user.is_active)
    self.assertEqual(user.info['family_name'], 'Roe')

  def test_patch_entra_email_value_path_and_ignored_extension(self):
    data = self.create_user('jane@example.org', emails=[])
    response = self.call('PATCH', 'Users/%s' % data['id'], patch_body(
      {'op': 'Add', 'path': 'emails[type eq "work"].value', 'value': 'jane.doe@example.org'},
      {'op': 'Add', 'path': 'urn:ietf:params:scim:schemas:extension:enterprise:2.0:User:department', 'value': 'X'},
      {'op': 'Replace', 'path': 'urn:ietf:params:scim:schemas:core:2.0:User:displayName', 'value': 'J. Doe'},
      {'op': 'Add', 'path': 'phoneNumbers[type eq "work"].value', 'value': '123'}))
    self.assertEqual(response.status_code, 200, response.content)
    rep = self.json(response)
    self.assertEqual(rep['emails'], [{'type': 'work', 'value': 'jane.doe@example.org'}])
    self.assertNotIn('phoneNumbers', rep)
    user = self.helios_user('jane@example.org')
    self.assertEqual((user.info['email'], user.name), ('jane.doe@example.org', 'J. Doe'))

  def test_patch_errors(self):
    data = self.create_user('jane@example.org')
    url = 'Users/%s' % data['id']
    self.assertEqual(self.json(self.call('PATCH', url, patch_body({'op': 'move', 'path': 'x'})))['scimType'], 'invalidSyntax')
    self.assertEqual(self.json(self.call('PATCH', url, patch_body({'op': 'remove'})))['scimType'], 'noTarget')
    self.assertEqual(self.json(self.call('PATCH', url, patch_body({'op': 'replace', 'path': 'active', 'value': 'maybe'})))['scimType'], 'invalidValue')
    self.assertEqual(self.json(self.call('PATCH', url, {'Operations': []}))['scimType'], 'invalidSyntax')

  @override_settings(SCIM_USER_ID_ATTRIBUTE='email')
  def test_user_id_from_primary_email(self):
    self.create_user('jdoe', emails=[{'value': 'other@example.org'}, {'value': 'jane@example.org', 'primary': True}])
    self.assertTrue(User.objects.filter(user_type='google', user_id='jane@example.org').exists())
    response = self.call('POST', 'Users', user_body('nomail', emails=[]))
    self.assertEqual(response.status_code, 400)

  @override_settings(SCIM_USER_ID_ATTRIBUTE='externalId')
  def test_user_id_from_external_id(self):
    self.create_user('jdoe', externalId='0b5c4f1e-subject')
    self.assertTrue(User.objects.filter(user_type='google', user_id='0b5c4f1e-subject').exists())

  def test_delete_unreferenced_user_deletes_it(self):
    data = self.create_user('jane@example.org')
    self.assertEqual(self.call('DELETE', 'Users/%s' % data['id']).status_code, 204)
    self.assertFalse(User.objects.filter(user_id='jane@example.org').exists())
    self.assertEqual(self.call('GET', 'Users/%s' % data['id']).status_code, 404)

  def test_delete_referenced_user_deactivates_it(self):
    data = self.create_user('jane@example.org')
    user = self.helios_user('jane@example.org')
    Election.objects.create(admin=user, uuid='e1', short_name='e1', name='E', description='', election_type='election')
    self.assertEqual(self.call('DELETE', 'Users/%s' % data['id']).status_code, 204)
    user.refresh_from_db()
    self.assertFalse(user.is_active)
    self.assertEqual(user.provisioned_by, 'scim:idm')
    self.assertEqual(self.call('GET', 'Users/%s' % data['id']).status_code, 404)
    # the client creates the person again: same Helios user, active again
    self.create_user('jane@example.org')
    user.refresh_from_db()
    self.assertTrue(user.is_active)

  def test_method_not_allowed(self):
    self.assertEqual(self.call('DELETE', 'Users').status_code, 405)


class GroupTests(ScimTestCase):

  def setUp(self):
    self.jane = self.create_user('jane@example.org')
    self.john = self.create_user('john@example.org', name={'givenName': 'John', 'familyName': 'Roe'}, displayName='John Roe')

  def votergroup(self, scim_id):
    return VoterGroup.objects.get(provisioned_by='scim:idm', provisioned_ref=scim_id)

  def test_group_becomes_voter_group(self):
    data = self.create_group('Council', [self.jane['id'], self.john['id']], externalId='g-1')
    group = self.votergroup(data['id'])
    self.assertEqual((group.short_name, group.external_id, group.admin.user_type), ('Council', 'g-1', 'provisioning'))
    members = sorted((m.first_name, m.last_name, m.email, m.votes) for m in group.get_members())
    self.assertEqual(members, [('Jane', 'Doe', 'jane@example.org', 1), ('John', 'Roe', 'john@example.org', 1)])
    self.assertEqual(len(data['members']), 2)

  def test_duplicate_display_name_is_409(self):
    self.create_group('Council')
    response = self.call('POST', 'Groups', {'displayName': 'council'})
    self.assertEqual(response.status_code, 409)
    self.assertEqual(self.json(response)['scimType'], 'uniqueness')
    # another client may use the name
    self.create_group('Council', token=OTHER_TOKEN)

  def test_filter_and_excluded_members(self):
    self.create_group('Council', [self.jane['id']])
    self.create_group('Senate')
    data = self.json(self.call('GET', 'Groups', filter='displayName eq "council"', excludedAttributes='members'))
    self.assertEqual(data['totalResults'], 1)
    self.assertNotIn('members', data['Resources'][0])
    data = self.json(self.call('GET', 'Groups', filter='members[value eq "%s"]' % self.jane['id']))
    self.assertEqual([r['displayName'] for r in data['Resources']], ['Council'])
    group_id = data['Resources'][0]['id']
    data = self.json(self.call('GET', 'Groups', filter='id eq "%s" and members[value eq "%s"]' % (group_id, self.john['id'])))
    self.assertEqual(data['totalResults'], 0)

  def test_patch_members_entra_style(self):
    data = self.create_group('Council')
    url = 'Groups/%s' % data['id']
    response = self.call('PATCH', url, patch_body({'op': 'Add', 'path': 'members', 'value': [{'value': self.jane['id']}, {'value': self.john['id']}]}))
    self.assertEqual(response.status_code, 200, response.content)
    self.assertEqual(self.votergroup(data['id']).get_members().count(), 2)
    self.call('PATCH', url, patch_body({'op': 'Remove', 'path': 'members', 'value': [{'value': self.jane['id']}]}))
    self.assertEqual([m.email for m in self.votergroup(data['id']).get_members()], ['john@example.org'])
    self.call('PATCH', url, patch_body({'op': 'remove', 'path': 'members[value eq "%s"]' % self.john['id']}))
    self.assertEqual(self.votergroup(data['id']).get_members().count(), 0)

  def test_patch_okta_style_replace_members_and_rename(self):
    data = self.create_group('Council', [self.jane['id']])
    self.call('PATCH', 'Groups/%s' % data['id'], patch_body(
      {'op': 'replace', 'path': 'members', 'value': [{'value': self.john['id'], 'display': 'John'}]},
      {'op': 'replace', 'value': {'id': data['id'], 'displayName': 'Council 2027'}}))
    group = self.votergroup(data['id'])
    self.assertEqual(group.short_name, 'Council 2027')
    self.assertEqual([m.email for m in group.get_members()], ['john@example.org'])

  def test_unknown_members_are_ignored(self):
    data = self.create_group('Council', [self.jane['id'], 'no-such-user'])
    self.assertEqual(len(data['members']), 1)

  def test_put_and_delete_group(self):
    data = self.create_group('Council', [self.jane['id']])
    response = self.call('PUT', 'Groups/%s' % data['id'], {'displayName': 'Council', 'members': []})
    self.assertEqual(response.status_code, 200)
    self.assertEqual(self.votergroup(data['id']).get_members().count(), 0)
    self.assertEqual(self.call('DELETE', 'Groups/%s' % data['id']).status_code, 204)
    self.assertFalse(VoterGroup.objects.filter(provisioned_ref=data['id']).exists())
    self.assertFalse(ScimGroup.objects.exists())

  def test_admin_role_group_grants_and_revokes(self):
    data = self.create_group('helios-admins', [self.jane['id']])
    jane = self.helios_user('jane@example.org')
    self.assertTrue(jane.admin_p)
    self.assertEqual(jane.admin_provisioned_by, 'scim:idm')
    self.assertFalse(VoterGroup.objects.filter(provisioned_ref=data['id']).exists())
    self.call('PATCH', 'Groups/%s' % data['id'], patch_body({'op': 'remove', 'path': 'members[value eq "%s"]' % self.jane['id']}))
    self.assertFalse(self.helios_user('jane@example.org').admin_p)

  def test_role_group_by_external_id_and_deleting_it_revokes(self):
    data = self.create_group('Some name', [self.jane['id']], externalId='helios-admins')
    self.assertTrue(self.helios_user('jane@example.org').admin_p)
    self.call('DELETE', 'Groups/%s' % data['id'])
    self.assertFalse(self.helios_user('jane@example.org').admin_p)

  def test_locally_granted_admin_is_kept_until_the_source_grants_it(self):
    john = self.helios_user('john@example.org')
    john.admin_p = True
    john.save()
    data = self.create_group('helios-admins', [self.jane['id']])
    self.assertTrue(self.helios_user('john@example.org').admin_p)  # not in the group, but not granted by SCIM
    self.call('DELETE', 'Groups/%s' % data['id'])
    self.assertTrue(self.helios_user('john@example.org').admin_p)

  def test_deactivated_user_loses_roles_and_memberships(self):
    self.create_group('helios-admins', [self.jane['id']])
    council = self.create_group('Council', [self.jane['id'], self.john['id']])
    self.call('PATCH', 'Users/%s' % self.jane['id'], patch_body({'op': 'replace', 'path': 'active', 'value': False}))
    jane = self.helios_user('jane@example.org')
    self.assertFalse(jane.admin_p)
    self.assertEqual([m.email for m in self.votergroup(council['id']).get_members()], ['john@example.org'])
    self.call('PATCH', 'Users/%s' % self.jane['id'], patch_body({'op': 'replace', 'path': 'active', 'value': True}))
    self.assertTrue(self.helios_user('jane@example.org').admin_p)
    self.assertEqual(self.votergroup(council['id']).get_members().count(), 2)

  def test_deleting_member_user_updates_voter_group(self):
    council = self.create_group('Council', [self.jane['id'], self.john['id']])
    self.call('DELETE', 'Users/%s' % self.jane['id'])
    self.assertEqual([m.email for m in self.votergroup(council['id']).get_members()], ['john@example.org'])
    self.assertEqual(len(self.json(self.call('GET', 'Groups/%s' % council['id']))['members']), 1)

  @override_settings(ELECTION_CREATORS_ONLY=True)
  def test_election_creator_group(self):
    jane = self.helios_user('jane@example.org')
    self.assertFalse(user_can_create_election(jane))
    data = self.create_group('helios-election-creators', [self.jane['id']])
    jane = self.helios_user('jane@example.org')
    self.assertTrue(jane.election_creator_p)
    self.assertTrue(user_can_create_election(jane))
    self.call('PUT', 'Groups/%s' % data['id'], {'displayName': 'helios-election-creators', 'members': []})
    self.assertFalse(user_can_create_election(self.helios_user('jane@example.org')))

  def test_voter_group_admins_by_suffix_and_global_group(self):
    council = self.create_group('Council', [self.john['id']])
    self.create_group('Council (admins)', [self.jane['id']])
    group = self.votergroup(council['id'])
    self.assertEqual([a.user_id for a in group.admins.all()], ['jane@example.org'])
    self.assertFalse(VoterGroup.objects.filter(short_name='Council (admins)').exists())
    # the administrator group can exist before the voter group, too
    self.create_group('Senate (admins)', [self.john['id']])
    senate = self.create_group('Senate')
    self.assertEqual([a.user_id for a in self.votergroup(senate['id']).admins.all()], ['john@example.org'])
    # everyone in the global group administers all voter groups
    third = self.create_user('third@example.org')
    self.create_group('helios-votergroup-admins', [third['id']])
    self.assertIn('third@example.org', [a.user_id for a in self.votergroup(council['id']).admins.all()])
    self.assertIn('third@example.org', [a.user_id for a in self.votergroup(senate['id']).admins.all()])

  def test_resync_after_changing_settings(self):
    data = self.create_group('Wahlamt', [self.jane['id']])
    self.assertTrue(VoterGroup.objects.filter(provisioned_ref=data['id']).exists())
    with override_settings(SCIM_ADMIN_GROUP='Wahlamt'):
      call_command('scim_sync', stdout=io.StringIO())
      self.assertTrue(self.helios_user('jane@example.org').admin_p)
      self.assertFalse(VoterGroup.objects.filter(provisioned_ref=data['id']).exists())

  def test_changes_are_recorded_in_the_group_log(self):
    data = self.create_group('Council', [self.jane['id']])
    self.call('PATCH', 'Groups/%s' % data['id'], patch_body(
      {'op': 'replace', 'path': 'members', 'value': [{'value': self.john['id']}]},
      {'op': 'replace', 'value': {'id': data['id'], 'displayName': 'Council 2027'}}))
    log = list(self.votergroup(data['id']).get_log())
    self.assertTrue(all(e.source == 'scim:idm' and e.actor is None for e in log))
    self.assertEqual([e.message for e in reversed(log)], [
      'Voter group Council created',
      'Members updated: 1 added, 0 removed; 0 administrator(s)',
      'Renamed to Council 2027',
      'Members updated: 1 added, 1 removed; 0 administrator(s)',
    ])
    # counts only, no member data
    self.assertNotIn('example.org', str([e.params for e in log]))


class LoginTests(ScimTestCase):

  def _login(self, user_info):
    from helios_auth.auth_systems import google
    self.client.get('/')
    session = self.client.session
    session['auth_system_name'] = 'google'
    session['auth_return_url'] = '/'
    session.save()
    with mock.patch.object(google, 'get_user_info_after_auth', return_value=user_info):
      return self.client.get(reverse('auth@after'), {'code': 'c', 'state': 's'})

  def test_first_login_picks_up_provisioned_user_and_keeps_profile(self):
    self.create_user('jane@example.org')
    response = self._login({'type': 'google', 'user_id': 'jane@example.org', 'name': 'jane (login)',
                            'info': {'name': 'jane (login)', 'email': 'login@example.org', 'picture': 'p'}, 'token': {}})
    self.assertEqual(response.status_code, 302)
    self.assertEqual(self.client.session['user']['user_id'], 'jane@example.org')
    self.assertEqual(User.objects.filter(user_type='google').count(), 1)
    user = self.helios_user('jane@example.org')
    self.assertEqual((user.name, user.info['email'], user.info['picture']), ('Jane Doe', 'jane@example.org', 'p'))

  def test_login_of_local_user_still_updates_profile(self):
    User.objects.create(user_type='google', user_id='local@example.org', name='Old', info={})
    self._login({'type': 'google', 'user_id': 'local@example.org', 'name': 'New',
                 'info': {'email': 'local@example.org'}, 'token': {}})
    self.assertEqual(self.helios_user('local@example.org').name, 'New')

  def test_deactivated_user_cannot_log_in_and_is_logged_out(self):
    data = self.create_user('jane@example.org')
    user = self.helios_user('jane@example.org')
    self.login(user)
    self.assertEqual(self.client.get('/helios/votergroup/administered').status_code, 200)
    self.call('PATCH', 'Users/%s' % data['id'], patch_body({'op': 'replace', 'path': 'active', 'value': False}))
    # the running session no longer counts
    self.assertEqual(self.client.get('/helios/votergroup/administered').status_code, 302)
    response = self._login({'type': 'google', 'user_id': 'jane@example.org', 'name': 'J', 'info': {}, 'token': {}})
    self.assertEqual(response.status_code, 400)
    self.assertContains(response, 'deactivated', status_code=400)
    self.assertNotIn('user', self.client.session)


class ReadOnlyVoterGroupTests(ScimTestCase):

  def setUp(self):
    self.jane = self.create_user('jane@example.org')
    self.john = self.create_user('john@example.org')
    council = self.create_group('Council', [self.john['id']])
    self.create_group('Council (admins)', [self.jane['id']])
    self.group = VoterGroup.objects.get(provisioned_ref=council['id'])
    self.member = self.group.get_members().get()
    self.login(self.helios_user('jane@example.org'))

  def test_member_and_admin_changes_are_refused(self):
    vg = self.group.uuid
    for response in (
        self.client.get('/helios/votergroup/memberadd/%s' % vg),
        self.client.post('/helios/votergroup/memberadd/%s' % vg, {'csrf_token': 'tok', 'first_name': 'A', 'last_name': 'B',
                                                                  'email': 'a@example.org', 'votes': '1'}),
        self.client.post('/helios/votergroup/memberdelete/%s' % self.member.uuid, {'csrf_token': 'tok'}),
        self.client.post('/helios/votergroup/admins/%s/add' % vg, {'csrf_token': 'tok', 'email': 'john@example.org'}),
        self.client.post('/helios/votergroup/admins/%s/remove' % vg, {'csrf_token': 'tok', 'user_id': 'jane@example.org',
                                                                      'user_type': 'google'})):
      self.assertEqual(response.status_code, 403)
      self.assertContains(response, 'cannot be changed in Helios', status_code=403)
    self.assertEqual(self.group.get_members().count(), 1)
    self.assertEqual([a.user_id for a in self.group.admins.all()], ['jane@example.org'])

  def test_group_log_shows_the_source(self):
    response = self.client.get('/helios/votergroup/view/%s' % self.group.uuid)
    self.assertContains(response, '<td>SCIM: idm</td>', html=False)
    self.assertContains(response, 'Voter group Council created')

  def test_no_transfer_of_provisioned_groups(self):
    vg = self.group.uuid
    # its owner is the source's account; a site administrator is refused, too
    admin = User.objects.create(user_type='password', user_id='admin', name='Admin', info={'password': 'x'}, admin_p=True)
    self.login(admin)
    response = self.client.get('/helios/votergroup/view/%s' % vg)
    self.assertContains(response, 'only an overview is shown')
    self.assertNotContains(response, '/helios/votergroup/transfer/')
    self.assertNotContains(response, 'john@example.org')
    jane = self.helios_user('jane@example.org')
    for response in (self.client.get('/helios/votergroup/transfer/%s' % vg),
                     self.client.post('/helios/votergroup/transfer/%s' % vg, {
                       'csrf_token': 'tok', 'new_owner': str(jane.pk), 'confirmed': '1'})):
      self.assertContains(response, 'cannot be changed in Helios', status_code=403)
      self.assertNotContains(response, 'You can still prepare elections', status_code=403)
    self.assertTrue(provisioning.is_source_owner(VoterGroup.objects.get(uuid=vg).admin))
    # the group's (provisioned) administrators can't transfer it either
    self.login(jane)
    self.assertEqual(self.client.get('/helios/votergroup/transfer/%s' % vg).status_code, 403)
    self.assertNotContains(self.client.get('/helios/votergroup/view/%s' % vg), '/helios/votergroup/transfer/')

  def test_pages_show_badge_and_hide_edit_actions(self):
    response = self.client.get('/helios/votergroup/administered')
    self.assertContains(response, 'Provisioned (SCIM: idm)')
    response = self.client.get('/helios/votergroup/view/%s' % self.group.uuid)
    self.assertContains(response, 'Provisioned (SCIM: idm)')
    self.assertNotContains(response, '/helios/votergroup/memberadd/')
    self.assertNotContains(response, '/helios/votergroup/memberdelete/')
    response = self.client.get('/helios/votergroup/admins/%s' % self.group.uuid)
    self.assertNotContains(response, '/admins/%s/add' % self.group.uuid)
    self.assertNotContains(response, '/admins/%s/remove' % self.group.uuid)

  def test_badge_in_german(self):
    response = self.client.get('/helios/votergroup/view/%s' % self.group.uuid, HTTP_ACCEPT_LANGUAGE='de')
    self.assertContains(response, 'Provisioniert (SCIM: idm)')
    response = self.client.get('/helios/votergroup/memberadd/%s' % self.group.uuid, HTTP_ACCEPT_LANGUAGE='de')
    self.assertContains(response, 'in Helios nicht geändert werden', status_code=403)

  def test_prepare_election_still_works(self):
    response = self.client.get('/helios/votergroup/prepareelection/%s' % self.group.uuid)
    self.assertEqual(response.status_code, 200)
    # the account owning the group is not offered as election administrator
    self.assertNotIn(self.group.admin, response.context['other_admins'])

  def test_local_groups_are_unaffected(self):
    jane = self.helios_user('jane@example.org')
    local = VoterGroup.objects.create(admin=jane, uuid='local', short_name='local')
    response = self.client.post('/helios/votergroup/memberadd/local', {'csrf_token': 'tok', 'first_name': 'A',
                                                                       'last_name': 'B', 'email': 'a@example.org', 'votes': '1'})
    self.assertEqual(response.status_code, 302)
    self.assertEqual(local.get_members().count(), 1)
    self.assertNotContains(self.client.get('/helios/votergroup/view/local'), 'Provisioned')

  def test_site_admin_user_search_shows_provenance(self):
    admin = User.objects.create(user_type='password', user_id='admin', name='Admin', info={'password': 'x'}, admin_p=True)
    self.login(admin)
    response = self.client.get('/helios/stats/user-search', {'q': 'jane'})
    self.assertContains(response, 'Provisioned (SCIM: idm)')

  def test_election_admin_page_shows_badge(self):
    jane = self.helios_user('jane@example.org')
    election = Election.objects.create(admin=jane, uuid='e1', short_name='e1', name='E', description='',
                                       election_type='election')
    response = self.client.get('/helios/elections/%s/admins/' % election.uuid)
    self.assertContains(response, 'Provisioned (SCIM: idm)')

  def test_deactivated_users_cannot_be_added_as_admins(self):
    from helios.view_utils import find_user_to_add_as_admin
    john = self.helios_user('john@example.org')
    self.assertEqual(find_user_to_add_as_admin('john@example.org')[0], john)
    provisioning.deactivate_user('scim:idm', john)
    self.assertIsNone(find_user_to_add_as_admin('john@example.org')[0])


class FilterAndPatchUnitTests(TestCase):

  def test_parse_and_match(self):
    tree = scim_filter.parse_filter('not (userName eq "a") and (emails.value co "@example" or title pr)')
    self.assertTrue(scim_filter.matches(tree, {'userName': 'b', 'emails': [{'value': 'b@example.org'}]}))
    self.assertFalse(scim_filter.matches(tree, {'userName': 'A', 'emails': [{'value': 'a@example.org'}]}))
    self.assertTrue(scim_filter.matches(scim_filter.parse_filter('active eq true'), {'active': True}))
    self.assertTrue(scim_filter.matches(scim_filter.parse_filter(
      'urn:ietf:params:scim:schemas:core:2.0:User:name.familyName sw "do"'), {'name': {'familyName': 'Doe'}}))
    for bad in ('', 'userName eq', 'userName eq "a" and', '(userName eq "a"', 'userName ~ "a"'):
      with self.assertRaises(ScimError):
        scim_filter.parse_filter(bad)

  def test_parse_path(self):
    self.assertEqual(scim_filter.parse_path('name.givenName')[1:], ('name', None, 'givenname'))
    schema, attr, flt, sub = scim_filter.parse_path('urn:ietf:params:scim:schemas:core:2.0:User:emails[type eq "work"].value')
    self.assertEqual((attr, sub), ('emails', 'value'))
    self.assertTrue(schema.endswith(':User'))
    self.assertEqual(scim_filter.parse_path('members[value eq "urn:x:1"]')[1], 'members')

  def test_patch_add_does_not_duplicate_members(self):
    group = {'displayName': 'x', 'members': [{'value': '1'}]}
    result = apply_patch('Group', group, patch_body({'op': 'add', 'path': 'members', 'value': [{'value': '1'}, {'value': '2'}]}))
    self.assertEqual([m['value'] for m in result['members']], ['1', '2'])
    self.assertEqual([m['value'] for m in group['members']], ['1'])  # unchanged

  def test_patch_complex_attribute(self):
    user = {'userName': 'a', 'name': {'givenName': 'A', 'familyName': 'B'}}
    result = apply_patch('User', user, patch_body({'op': 'replace', 'path': 'name', 'value': {'familyName': 'C'}},
                                                  {'op': 'remove', 'path': 'name.givenName'}))
    self.assertEqual(result['name'], {'familyName': 'C'})


class TokenCommandTests(TestCase):

  def test_scim_token_prints_digest_entry(self):
    out = io.StringIO()
    call_command('scim_token', 'idm', stdout=out)
    lines = [l.strip() for l in out.getvalue().splitlines()]
    token = lines[1]
    self.assertEqual(json.loads(lines[3]), {'idm': hash_token(token)})
