"""
The SCIM schemas Helios supports: the attributes of the core User and Group
schemas it stores (others, e.g. phone numbers or the enterprise extension, are
accepted and ignored: Helios keeps only what it needs), and the discovery
documents (ServiceProviderConfig, ResourceTypes, Schemas).
"""

USER_SCHEMA = 'urn:ietf:params:scim:schemas:core:2.0:User'
GROUP_SCHEMA = 'urn:ietf:params:scim:schemas:core:2.0:Group'
LIST_SCHEMA = 'urn:ietf:params:scim:api:messages:2.0:ListResponse'
PATCH_SCHEMA = 'urn:ietf:params:scim:api:messages:2.0:PatchOp'
SPC_SCHEMA = 'urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig'
RESOURCE_TYPE_SCHEMA = 'urn:ietf:params:scim:schemas:core:2.0:ResourceType'
SCHEMA_SCHEMA = 'urn:ietf:params:scim:schemas:core:2.0:Schema'

MAX_RESULTS = 200
DEFAULT_COUNT = 100


def _attr(name, type='string', multi=False, required=False, case_exact=False, mutability='readWrite',
          returned='default', uniqueness='none', sub=None, description='', reference_types=None):
  a = {'name': name, 'type': type, 'multiValued': multi, 'description': description, 'required': required,
       'caseExact': case_exact, 'mutability': mutability, 'returned': returned, 'uniqueness': uniqueness}
  if sub:
    a['subAttributes'] = sub
  if reference_types:
    a['referenceTypes'] = reference_types
  return a


EXTERNAL_ID = _attr('externalId', case_exact=True, description='Identifier of the resource at the client.')

USER_ATTRIBUTES = [
  _attr('userName', required=True, uniqueness='server',
        description='Unique identifier of the user; by default also the Helios user id (see SCIM_USER_ID_ATTRIBUTE).'),
  _attr('name', type='complex', sub=[
    _attr('formatted'), _attr('familyName'), _attr('givenName'), _attr('middleName')],
    description="The user's name."),
  _attr('displayName', description='Name shown in Helios.'),
  _attr('emails', type='complex', multi=True, sub=[
    _attr('value'), _attr('type'), _attr('primary', type='boolean')],
    description='Email addresses; the primary one is used for election mail.'),
  _attr('active', type='boolean', description='false: the user cannot log in and has no rights in Helios.'),
]

GROUP_ATTRIBUTES = [
  _attr('displayName', required=True, uniqueness='server',
        description='Group name: a role group (see SCIM_ADMIN_GROUP, SCIM_ELECTION_CREATOR_GROUP), an '
                    'administrator group of voter groups, or the name of a voter group.'),
  _attr('members', type='complex', multi=True, sub=[
    _attr('value', mutability='immutable', case_exact=True),
    _attr('$ref', type='reference', mutability='immutable', reference_types=['User']),
    _attr('display', mutability='readOnly'),
    _attr('type', mutability='immutable')],
    description='Members (users only, no nested groups).'),
]


def known_attributes(resource_type):
  """{lowercased name: attribute definition} of the attributes Helios stores."""
  attrs = (USER_ATTRIBUTES if resource_type == 'User' else GROUP_ATTRIBUTES) + [EXTERNAL_ID]
  return {a['name'].lower(): a for a in attrs}


def core_schema(resource_type):
  return USER_SCHEMA if resource_type == 'User' else GROUP_SCHEMA


def service_provider_config(base_url):
  return {
    'schemas': [SPC_SCHEMA],
    'patch': {'supported': True},
    'bulk': {'supported': False, 'maxOperations': 0, 'maxPayloadSize': 0},
    'filter': {'supported': True, 'maxResults': MAX_RESULTS},
    'changePassword': {'supported': False},
    'sort': {'supported': False},
    'etag': {'supported': False},
    'authenticationSchemes': [{
      'type': 'oauthbearertoken', 'name': 'Bearer token', 'primary': True,
      'description': 'A static bearer token per client, configured in SCIM_TOKENS.'}],
    'meta': {'resourceType': 'ServiceProviderConfig', 'location': base_url + 'ServiceProviderConfig'},
  }


def resource_types(base_url):
  return [
    {'schemas': [RESOURCE_TYPE_SCHEMA], 'id': 'User', 'name': 'User', 'endpoint': '/Users',
     'description': 'Helios user', 'schema': USER_SCHEMA, 'schemaExtensions': [],
     'meta': {'resourceType': 'ResourceType', 'location': base_url + 'ResourceTypes/User'}},
    {'schemas': [RESOURCE_TYPE_SCHEMA], 'id': 'Group', 'name': 'Group', 'endpoint': '/Groups',
     'description': 'Role group or voter group', 'schema': GROUP_SCHEMA, 'schemaExtensions': [],
     'meta': {'resourceType': 'ResourceType', 'location': base_url + 'ResourceTypes/Group'}},
  ]


def schemas(base_url):
  return [
    {'schemas': [SCHEMA_SCHEMA], 'id': USER_SCHEMA, 'name': 'User', 'description': 'User Account',
     'attributes': USER_ATTRIBUTES + [EXTERNAL_ID],
     'meta': {'resourceType': 'Schema', 'location': base_url + 'Schemas/' + USER_SCHEMA}},
    {'schemas': [SCHEMA_SCHEMA], 'id': GROUP_SCHEMA, 'name': 'Group', 'description': 'Group',
     'attributes': GROUP_ATTRIBUTES + [EXTERNAL_ID],
     'meta': {'resourceType': 'Schema', 'location': base_url + 'Schemas/' + GROUP_SCHEMA}},
  ]
