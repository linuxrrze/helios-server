"""
Provisioning: users, roles and voter groups managed by an external source.

This is the source-agnostic mapping layer. A source (SCIM, see helios_scim; later
e.g. a directory import) identifies itself as "<kind>:<name>", e.g. "scim:idm",
keeps its own bookkeeping (resource ids, raw attributes, group memberships) and
calls these functions with plain records and Helios users:

- provision_user / deactivate_user / release_user: a person, matched by
  (user_type, user_id) exactly as a later login creates it;
- set_roles: site administrator (admin_p) and election creator
  (election_creator_p);
- sync_votergroup / remove_votergroup: a voter group with its members (one vote
  each) and administrators.

What a source provisions is marked (provisioned_by, provisioned_at,
external_id) and read-only in Helios: the voter group views refuse to change a
provisioned group, and a login keeps a provisioned user's name and email
(helios_auth.models.User.update_or_create). Inactive (deactivated) users can't
log in, lose the roles granted by the source and are left out of provisioned
voter groups.

Every change is logged (logger "helios.provisioning", addresses masked);
changes to a voter group are also recorded in the group's log
(helios.models.VoterGroupLog, see log_votergroup).
"""

import dataclasses
import datetime
import logging
import uuid

from django.db import transaction
from django.db.models import Q

from helios_auth.models import User
from .mail_backend import mask_address

logger = logging.getLogger('helios.provisioning')

# user_type of the account that owns a source's voter groups (VoterGroup.admin
# can't be empty). No auth system has this name, so nobody can log in as it.
OWNER_USER_TYPE = 'provisioning'

# role name -> (flag on User, field recording the source that granted it)
ROLES = {
  'admin': ('admin_p', 'admin_provisioned_by'),
  'election_creator': ('election_creator_p', 'election_creator_provisioned_by'),
}


class ProvisioningConflict(Exception):
  """The identity or name is already taken (by another source or entity)."""


@dataclasses.dataclass
class UserRecord:
  """A person as a source describes them. user_type/user_id must be what the
  login creates (see docs/scim.md, "Matching the login")."""
  user_type: str
  user_id: str
  name: str = ''
  given_name: str = ''
  family_name: str = ''
  email: str = ''
  external_id: str = ''
  active: bool = True

  @property
  def display_name(self):
    return (self.name or " ".join(p for p in (self.given_name, self.family_name) if p) or self.user_id)[:200]


def _now():
  return datetime.datetime.utcnow()


def source_label(source):
  """'scim:idm' -> 'SCIM: idm' (shown in the badges)."""
  kind, _sep, name = (source or '').partition(':')
  return "%s: %s" % (kind.upper(), name) if name else kind


def who(user):
  """A user for the log: 'oidc:12 j***@example.org' (address masked)."""
  return '%s:%s %s' % (user.user_type, user.pk, mask_address(user.user_id))


def log_votergroup(source, group, message, *args, action=None, params=None):
  """Log a change to a provisioned voter group (the one place for such
  entries). With ACTION (a helios.models.VoterGroupLog constant), the change is
  also recorded in the group's log, shown to its administrators, with PARAMS
  (counts and names, no member data)."""
  logger.info('%s: votergroup %s: ' + message, source, group.uuid, *args)
  if action:
    group.append_log(action, source=source, **(params or {}))


def is_source_owner(user):
  return bool(user) and user.user_type == OWNER_USER_TYPE


def find_user(user_type, user_id):
  try:
    return User.get_by_type_and_id(user_type, user_id)
  except User.DoesNotExist:
    return None


##
## users
##

@transaction.atomic
def provision_user(source, record, user=None):
  """
  Create or update the Helios user for RECORD and return it.

  USER is the user the source linked to this person earlier (None for a new
  person); its identity follows RECORD, e.g. after a rename at the source.
  Without USER, an existing user with the same identity is taken over: someone
  who has logged in before, or a user this source released earlier. Raises
  ProvisioningConflict if the identity belongs to a user managed by another
  source, or (on a rename) to another user.
  """
  existing = find_user(record.user_type, record.user_id)
  if user is None:
    if existing and existing.provisioned_by and existing.provisioned_by != source:
      raise ProvisioningConflict("user %s is managed by %s" % (record.user_id, existing.provisioned_by))
    if existing:
      user, action = existing, ('linked' if existing.provisioned_by != source else 'relinked')
    else:
      user, action = User(user_type=record.user_type, user_id=record.user_id, info={}), 'created'
  else:
    if existing and existing.pk != user.pk:
      raise ProvisioningConflict("user id %s is taken by another user" % record.user_id)
    action = 'updated'
    if (user.user_type, user.user_id) != (record.user_type, record.user_id):
      logger.info('%s: user %s renamed to %s:%s', source, who(user), record.user_type, mask_address(record.user_id))
      user.user_type, user.user_id = record.user_type, record.user_id

  was_active = user.pk is not None and user.is_active
  info = dict(user.info or {})
  info['name'] = record.display_name
  for key, value in (('email', record.email), ('given_name', record.given_name), ('family_name', record.family_name)):
    if value:
      info[key] = value
    elif key != 'email':
      # without an address from the source, one from the login is kept
      info.pop(key, None)
  user.info = info
  user.name = record.display_name
  user.provisioned_by = source
  user.provisioned_at = user.provisioned_at or _now()
  user.external_id = (record.external_id or '')[:255]
  if record.active:
    user.deactivated_at = None
  elif user.deactivated_at is None:
    user.deactivated_at = _now()
  user.save()

  if action != 'updated' or was_active != user.is_active:
    logger.info('%s: user %s %s%s', source, who(user), action, '' if user.is_active else ' (deactivated)')
  return user


@transaction.atomic
def deactivate_user(source, user):
  """No login, no rights; the user and everything linked to them is kept."""
  if user.deactivated_at is None:
    user.deactivated_at = _now()
    user.save(update_fields=['deactivated_at'])
    logger.info('%s: user %s deactivated', source, who(user))
  set_roles(source, user, set())


def user_is_referenced(user):
  """Does Helios hold records about USER that must be kept (audit trail)?"""
  from .models import Election, Voter, VoterGroup
  return (Voter.objects.filter(user=user).exists()
          or Election.objects_with_deleted.filter(Q(admin=user) | Q(admins=user)).exists()
          or VoterGroup.objects.filter(Q(admin=user) | Q(admins=user), provisioned_by='').exists())


@transaction.atomic
def release_user(source, user):
  """
  The source no longer manages USER (e.g. SCIM DELETE). A user Helios holds no
  records about (no voter, no election or local voter group administered) is
  deleted; otherwise the user is deactivated and kept, still marked as
  provisioned, so elections and logs stay intact. Returns 'deleted' or
  'deactivated'.
  """
  if user_is_referenced(user):
    deactivate_user(source, user)
    return 'deactivated'
  logger.info('%s: user %s deleted', source, who(user))
  user.delete()
  return 'deleted'


@transaction.atomic
def set_roles(source, user, roles):
  """
  Grant USER exactly ROLES (names from ROLES) as far as SOURCE is concerned. A
  role granted by SOURCE is revoked when it is no longer in ROLES; a role
  granted otherwise (e.g. by hand) is left alone, but marked as granted by
  SOURCE when SOURCE grants it too, so that SOURCE revokes it later. Inactive
  users get no roles.
  """
  roles = set(roles) if user.is_active else set()
  changed = []
  for role, (flag, granted_by) in ROLES.items():
    has, by = getattr(user, flag), getattr(user, granted_by)
    if role in roles and (not has or not by):
      setattr(user, flag, True)
      setattr(user, granted_by, source)
      changed.append('+' + role)
    elif role not in roles and has and by == source:
      setattr(user, flag, False)
      setattr(user, granted_by, '')
      changed.append('-' + role)
  if changed:
    user.save()
    logger.info('%s: user %s roles %s', source, who(user), ' '.join(changed))
  return changed


##
## voter groups
##

def source_owner(source):
  """The account that owns SOURCE's voter groups (created when first needed)."""
  owner, _created = User.objects.get_or_create(
    user_type=OWNER_USER_TYPE, user_id=source,
    defaults={'name': source_label(source), 'info': {}, 'provisioned_by': source, 'provisioned_at': _now()})
  return owner


def member_fields(user):
  """First name, last name and email of USER as a voter group member."""
  info = user.info or {}
  first, last = info.get('given_name') or '', info.get('family_name') or ''
  if not first and not last:
    first, _sep, last = (user.name or user.user_id).rpartition(' ')
    if not first:
      first, last = last, ''
  email = info.get('email') or (user.user_id if '@' in user.user_id else '')
  return {'first_name': first[:100], 'last_name': last[:100], 'email': email[:100]}


@transaction.atomic
def sync_votergroup(source, ref, name, members, admins, external_id=''):
  """
  Create or update SOURCE's voter group with the stable reference REF: name
  NAME, one member with one vote per active user in MEMBERS, the active users in
  ADMINS as its administrators. Returns the group. Raises ProvisioningConflict
  if another group of SOURCE has this name.
  """
  from .models import VoterGroup, VoterGroupLog, VoterGroupMember
  owner = source_owner(source)
  name = name[:100]
  if VoterGroup.objects.filter(admin=owner, short_name=name).exclude(provisioned_ref=ref).exists():
    raise ProvisioningConflict("voter group %s exists" % name)

  group = VoterGroup.objects.filter(provisioned_by=source, provisioned_ref=ref).first()
  if group is None:
    group = VoterGroup.objects.create(admin=owner, uuid=str(uuid.uuid4()), short_name=name, provisioned_by=source,
                                      provisioned_ref=ref, provisioned_at=_now(), external_id=external_id[:255])
    log_votergroup(source, group, 'created as %s', name, action=VoterGroupLog.CREATED, params={'name': name})
  elif group.short_name != name or group.external_id != external_id:
    if group.short_name != name:
      log_votergroup(source, group, 'renamed to %s', name, action=VoterGroupLog.RENAMED, params={'name': name})
    group.short_name, group.external_id = name, external_id[:255]
    group.save(update_fields=['short_name', 'external_id'])

  wanted = {u.pk: u for u in members if u.is_active and not is_source_owner(u)}
  added = removed = 0
  for member in list(group.votergroupmember_set.all()):
    if member.user_id not in wanted:
      member.delete()
      removed += 1
      continue
    fields = member_fields(wanted.pop(member.user_id))
    if any(getattr(member, k) != v for k, v in fields.items()):
      for k, v in fields.items():
        setattr(member, k, v)
      member.save()
  for user in wanted.values():
    VoterGroupMember.objects.create(group=group, uuid=str(uuid.uuid4()), user=user, votes=1, **member_fields(user))
    added += 1

  new_admins = {u.pk for u in admins if u.is_active and not is_source_owner(u)}
  old_admins = set(group.admins.values_list('pk', flat=True))
  if new_admins != old_admins:
    group.admins.set(list(new_admins))
  if added or removed or new_admins != old_admins:
    log_votergroup(source, group, '%d member(s) added, %d removed, %d administrator(s)',
                   added, removed, len(new_admins), action=VoterGroupLog.PROVISIONED,
                   params={'added': added, 'removed': removed, 'admins': len(new_admins)})
  return group


@transaction.atomic
def remove_votergroup(source, ref):
  """Delete SOURCE's voter group REF, if any. Elections prepared from it keep
  their voters (they are copies)."""
  from .models import VoterGroup
  for group in VoterGroup.objects.filter(provisioned_by=source, provisioned_ref=ref):
    log_votergroup(source, group, 'deleted')
    group.delete()
