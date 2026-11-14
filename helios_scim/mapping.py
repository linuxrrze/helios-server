"""
What SCIM resources mean in Helios (configured in settings, see docs/scim.md):

- a SCIM User is a Helios user of type SCIM_USER_TYPE whose user_id is the SCIM
  attribute SCIM_USER_ID_ATTRIBUTE (userName, email or externalId), i.e. the
  identity a later login creates;
- a SCIM Group is, by its displayName (case-insensitive) or externalId:
  * SCIM_ADMIN_GROUP: its members are site administrators,
  * SCIM_ELECTION_CREATOR_GROUP: its members may create (and so run) elections,
  * SCIM_VOTERGROUP_ADMINS_GROUP: its members administer all voter groups of the client,
  * "<voter group name><SCIM_VOTERGROUP_ADMINS_SUFFIX>": its members administer that voter group,
  * anything else: a voter group with these members (one vote each).

The effects are applied through helios.provisioning, with the source
"scim:<client>"; a client sees and changes only what it provisioned itself.
"""

import logging

from django.conf import settings

from helios import provisioning
from .errors import ScimError
from .models import ScimGroup, ScimUser

logger = logging.getLogger('helios.scim')

ROLE_KINDS = {'admin', 'election_creator'}
USER_ID_ATTRIBUTES = ('userName', 'email', 'externalId')


def source(client):
  return 'scim:%s' % client


def user_type():
  return getattr(settings, 'SCIM_USER_TYPE', '') or getattr(settings, 'OIDC_AUTH_SYSTEM_NAME', 'oidc')


def primary_email(attrs):
  emails = [e for e in attrs.get('emails') or [] if e.get('value')]
  for e in emails:
    if e.get('primary'):
      return e['value']
  for e in emails:
    if (e.get('type') or '').lower() == 'work':
      return e['value']
  return emails[0]['value'] if emails else ''


def user_record(attrs):
  """The provisioning record for the normalized SCIM user attributes ATTRS."""
  id_attribute = getattr(settings, 'SCIM_USER_ID_ATTRIBUTE', 'userName')
  if id_attribute == 'email':
    user_id = primary_email(attrs)
  elif id_attribute == 'externalId':
    user_id = attrs.get('externalId', '')
  else:
    user_id = attrs['userName']
  user_id = (user_id or '').strip()
  if not user_id:
    raise ScimError(400, 'invalidValue', 'the attribute %s is needed as the Helios user id' % id_attribute)
  if len(user_id) > 100:
    raise ScimError(400, 'invalidValue', 'the Helios user id (%s) is longer than 100 characters' % id_attribute)
  name = attrs.get('name') or {}
  return provisioning.UserRecord(
    user_type=user_type(), user_id=user_id,
    name=attrs.get('displayName') or name.get('formatted') or '',
    given_name=name.get('givenName') or '', family_name=name.get('familyName') or '',
    email=primary_email(attrs), external_id=attrs.get('externalId', ''), active=attrs.get('active', True))


##
## groups
##

def _setting(name):
  return (getattr(settings, name, '') or '').strip()


def _is(group_name, external_id, configured):
  return bool(configured) and (group_name.lower() == configured.lower() or external_id == configured)


def kind_of(display_name, external_id=''):
  """'admin', 'election_creator', 'votergroup_admins_all', 'votergroup_admins' or 'votergroup'."""
  if _is(display_name, external_id, _setting('SCIM_ADMIN_GROUP')):
    return 'admin'
  if _is(display_name, external_id, _setting('SCIM_ELECTION_CREATOR_GROUP')):
    return 'election_creator'
  if _is(display_name, external_id, _setting('SCIM_VOTERGROUP_ADMINS_GROUP')):
    return 'votergroup_admins_all'
  if admin_group_target(display_name):
    return 'votergroup_admins'
  return 'votergroup'


def admin_group_target(display_name):
  """For an administrator group "<name><suffix>": <name> (lowercased), else ''."""
  suffix = getattr(settings, 'SCIM_VOTERGROUP_ADMINS_SUFFIX', '') or ''
  if suffix and len(display_name) > len(suffix) and display_name.lower().endswith(suffix.lower()):
    return display_name[:-len(suffix)].strip().lower()
  return ''


def snapshot(group):
  """What matters about GROUP to derive its effects (before and after a change)."""
  return {'group': group, 'client': group.client, 'scim_id': group.scim_id, 'name': group.display_name,
          'kind': kind_of(group.display_name, group.external_id),
          'members': set(group.members.values_list('pk', flat=True))}


def _votergroup_admins(group):
  admins = []
  for other in ScimGroup.objects.filter(client=group.client).exclude(pk=group.pk):
    kind = kind_of(other.display_name, other.external_id)
    if kind == 'votergroup_admins_all' or \
        (kind == 'votergroup_admins' and admin_group_target(other.display_name) == group.display_name.lower()):
      admins.extend(su.user for su in other.members.select_related('user'))
  return admins


def sync_votergroup(group):
  members = [su.user for su in group.members.select_related('user')]
  try:
    provisioning.sync_votergroup(source(group.client), group.scim_id, group.display_name, members,
                                 _votergroup_admins(group), external_id=group.external_id)
  except provisioning.ProvisioningConflict as e:
    raise ScimError(409, 'uniqueness', str(e))


def _resync_votergroups(client, names=None):
  """Voter groups of CLIENT (only those whose lowercased name is in NAMES, if given)."""
  for group in ScimGroup.objects.filter(client=client):
    if (names is None or group.display_name.lower() in names) and \
        kind_of(group.display_name, group.external_id) == 'votergroup':
      sync_votergroup(group)


def sync_roles(scim_user):
  roles = {kind_of(g.display_name, g.external_id) for g in scim_user.groups.all()} & ROLE_KINDS
  provisioning.set_roles(source(scim_user.client), scim_user.user, roles)


def group_changed(client, before, after):
  """Apply the effects of a group change; BEFORE/AFTER are snapshots (None for
  a new or deleted group)."""
  snaps = [s for s in (before, after) if s]
  if any(s['kind'] in ROLE_KINDS for s in snaps):
    pks = set().union(*(s['members'] for s in snaps))
    for scim_user in ScimUser.objects.filter(pk__in=pks).select_related('user'):
      sync_roles(scim_user)

  if after and after['kind'] == 'votergroup':
    sync_votergroup(after['group'])
  elif before and before['kind'] == 'votergroup':
    provisioning.remove_votergroup(source(client), before['scim_id'])

  if any(s['kind'] == 'votergroup_admins_all' for s in snaps):
    _resync_votergroups(client)
  else:
    targets = {admin_group_target(s['name']) for s in snaps if s['kind'] == 'votergroup_admins'}
    if targets:
      _resync_votergroups(client, targets)


def user_changed(scim_user):
  """Apply a change of the user (active, name, email) to roles and voter groups."""
  sync_roles(scim_user)
  targets, everything = set(), False
  for group in scim_user.groups.all():
    kind = kind_of(group.display_name, group.external_id)
    if kind == 'votergroup':
      sync_votergroup(group)
    elif kind == 'votergroup_admins':
      targets.add(admin_group_target(group.display_name))
    elif kind == 'votergroup_admins_all':
      everything = True
  if everything:
    _resync_votergroups(scim_user.client)
  elif targets:
    _resync_votergroups(scim_user.client, targets)


def release_user(scim_user):
  """SCIM DELETE: leave all groups, forget the SCIM user; the Helios user is
  deleted or, if Helios holds records about them, deactivated."""
  client = scim_user.client
  groups = list(scim_user.groups.all())
  befores = [snapshot(g) for g in groups]
  scim_user.groups.clear()
  user = scim_user.user
  scim_user.delete()
  result = provisioning.release_user(source(client), user)
  for group, before in zip(groups, befores):
    group_changed(client, before, snapshot(group))
  return result


def resync(client=None):
  """Apply all groups and users again, e.g. after changing the group settings."""
  users = ScimUser.objects.select_related('user')
  groups = ScimGroup.objects.all()
  if client:
    users, groups = users.filter(client=client), groups.filter(client=client)
  for group in groups:
    snap = snapshot(group)
    if snap['kind'] != 'votergroup':
      provisioning.remove_votergroup(source(group.client), group.scim_id)
  for scim_user in users:
    sync_roles(scim_user)
  for group in groups:
    if kind_of(group.display_name, group.external_id) == 'votergroup':
      sync_votergroup(group)
