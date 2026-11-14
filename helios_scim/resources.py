"""
Validation of incoming SCIM resources (only the attributes Helios keeps) and
their JSON representation.
"""

import datetime

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .errors import ScimError
from .schemas import GROUP_SCHEMA, USER_SCHEMA, known_attributes


def base_url():
  return settings.SECURE_URL_HOST + reverse('scim@root')


def location(resource_type, scim_id):
  return '%s%s/%s' % (base_url(), 'Users' if resource_type == 'User' else 'Groups', scim_id)


def _timestamp(value):
  if timezone.is_naive(value):
    value = timezone.make_aware(value, timezone.get_default_timezone())
  return value.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_bool(value, name):
  """true/false, also as strings in any case ("False": Entra ID)."""
  if isinstance(value, bool):
    return value
  if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
    return value.strip().lower() == 'true'
  raise ScimError(400, 'invalidValue', '%s must be true or false' % name)


def _string(value, name, max_length, required=False):
  if value is None or value == '':
    if required:
      raise ScimError(400, 'invalidValue', '%s is required' % name)
    return ''
  if isinstance(value, bool) or not isinstance(value, (str, int)):
    raise ScimError(400, 'invalidValue', '%s must be a string' % name)
  value = str(value).strip()
  if required and not value:
    raise ScimError(400, 'invalidValue', '%s is required' % name)
  if len(value) > max_length:
    raise ScimError(400, 'invalidValue', '%s is longer than %d characters' % (name, max_length))
  return value


def _canonical(data, resource_type):
  """Top-level attributes of DATA with canonical names; schema-prefixed keys of
  the core schema are unwrapped, everything Helios doesn't keep is dropped."""
  if not isinstance(data, dict):
    raise ScimError(400, 'invalidSyntax', 'the resource must be a JSON object')
  known = known_attributes(resource_type)
  core = (USER_SCHEMA if resource_type == 'User' else GROUP_SCHEMA).lower()
  result = {}
  for key, value in data.items():
    if not isinstance(key, str):
      continue
    if key.lower() == core and isinstance(value, dict):
      result.update(_canonical(value, resource_type))
      continue
    if key.lower().startswith(core + ':'):
      key = key[len(core) + 1:]
    definition = known.get(key.lower())
    if definition:
      result[definition['name']] = value
  return result


def normalize_user(data, current=None):
  """The user attributes Helios keeps, validated. CURRENT: the stored attributes
  (a PUT without 'active' keeps the current state)."""
  attrs = _canonical(data, 'User')
  result = {'userName': _string(attrs.get('userName'), 'userName', 255, required=True)}
  external_id = _string(attrs.get('externalId'), 'externalId', 255)
  if external_id:
    result['externalId'] = external_id

  name = attrs.get('name')
  if name not in (None, ''):
    if not isinstance(name, dict):
      raise ScimError(400, 'invalidValue', 'name must be an object')
    clean = {}
    for sub in ('formatted', 'familyName', 'givenName', 'middleName'):
      value = next((v for k, v in name.items() if k.lower() == sub.lower()), None)
      value = _string(value, 'name.' + sub, 200)
      if value:
        clean[sub] = value
    if clean:
      result['name'] = clean

  display_name = _string(attrs.get('displayName'), 'displayName', 200)
  if display_name:
    result['displayName'] = display_name

  emails = attrs.get('emails')
  if emails not in (None, ''):
    if not isinstance(emails, list):
      raise ScimError(400, 'invalidValue', 'emails must be a list')
    clean = []
    for email in emails[:20]:
      if isinstance(email, str):
        email = {'value': email}
      if not isinstance(email, dict):
        raise ScimError(400, 'invalidValue', 'emails must contain objects')
      lower = {k.lower(): v for k, v in email.items()}
      value = _string(lower.get('value'), 'emails.value', 100)
      if not value:
        continue
      entry = {'value': value}
      if lower.get('type'):
        entry['type'] = _string(lower['type'], 'emails.type', 50)
      if lower.get('primary') not in (None, ''):
        entry['primary'] = parse_bool(lower['primary'], 'emails.primary')
      clean.append(entry)
    if clean:
      result['emails'] = clean

  if attrs.get('active') is not None:
    result['active'] = parse_bool(attrs['active'], 'active')
  else:
    result['active'] = (current or {}).get('active', True)
  return result


def normalize_group(data):
  """(attributes Helios keeps, list of member ids), validated."""
  attrs = _canonical(data, 'Group')
  result = {'displayName': _string(attrs.get('displayName'), 'displayName', 100, required=True)}
  external_id = _string(attrs.get('externalId'), 'externalId', 255)
  if external_id:
    result['externalId'] = external_id
  members = attrs.get('members') or []
  if not isinstance(members, list):
    raise ScimError(400, 'invalidValue', 'members must be a list')
  ids = []
  for member in members:
    if isinstance(member, str):
      member = {'value': member}
    if not isinstance(member, dict):
      raise ScimError(400, 'invalidValue', 'members must contain objects')
    lower = {k.lower(): v for k, v in member.items()}
    if str(lower.get('type') or 'User').lower() != 'user':
      continue  # nested groups are not supported
    value = _string(lower.get('value'), 'members.value', 100)
    if value and value not in ids:
      ids.append(value)
  return result, ids


def user_representation(scim_user):
  rep = {'schemas': [USER_SCHEMA], 'id': scim_user.scim_id}
  rep.update({k: v for k, v in scim_user.attributes.items() if k != 'active'})
  rep['active'] = scim_user.user.is_active
  rep['meta'] = {'resourceType': 'User', 'created': _timestamp(scim_user.created_at),
                 'lastModified': _timestamp(scim_user.updated_at), 'location': location('User', scim_user.scim_id)}
  return rep


def group_representation(group, include_members=True):
  rep = {'schemas': [GROUP_SCHEMA], 'id': group.scim_id, 'displayName': group.display_name}
  if group.external_id:
    rep['externalId'] = group.external_id
  if include_members:
    rep['members'] = [
      {'value': su.scim_id, 'display': su.attributes.get('displayName') or su.user_name,
       '$ref': location('User', su.scim_id), 'type': 'User'}
      for su in group.members.order_by('pk')]
  rep['meta'] = {'resourceType': 'Group', 'created': _timestamp(group.created_at),
                 'lastModified': _timestamp(group.updated_at), 'location': location('Group', group.scim_id)}
  return rep


def select_attributes(rep, attributes=None, excluded=None):
  """Apply the attributes / excludedAttributes query parameters (top-level
  attributes; id, schemas and meta are always returned)."""
  def names(value):
    out = set()
    for item in (value or '').split(','):
      item = item.strip()
      if item.lower().startswith('urn:'):
        item = item.rpartition(':')[2]
      if item:
        out.add(item.split('.')[0].lower())
    return out
  keep, drop = names(attributes), names(excluded)
  always = {'id', 'schemas', 'meta'}
  if keep:
    rep = {k: v for k, v in rep.items() if k.lower() in keep | always}
  if drop:
    rep = {k: v for k, v in rep.items() if k.lower() not in drop - always}
  return rep
