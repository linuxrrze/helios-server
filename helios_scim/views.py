"""
SCIM 2.0 server endpoints (RFC 7644) under /scim/v2/.

Off unless SCIM_ENABLED=1 (404 otherwise). Every request needs a client's bearer
token (SCIM_TOKENS); there are no sessions and no CSRF tokens. A client sees
and changes only the resources it created. Responses are application/scim+json.
"""

import json
import logging
from functools import wraps

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from helios import provisioning
from . import filter as scim_filter
from . import mapping, resources, schemas
from .auth import authenticate
from .errors import ScimError, error_response, scim_response
from .models import ScimGroup, ScimUser
from .patch import apply_patch

logger = logging.getLogger('helios.scim')


def scim_endpoint(*methods):
  def decorator(func):
    @csrf_exempt
    @wraps(func)
    def wrapper(request, *args, **kwargs):
      if not getattr(settings, 'SCIM_ENABLED', False):
        raise Http404
      client = authenticate(request)
      if not client:
        logger.warning('SCIM request to %s rejected: missing or invalid bearer token', request.path)
        response = error_response(401, None, 'a valid bearer token is required')
        response['WWW-Authenticate'] = 'Bearer realm="scim"'
        return response
      if request.method not in methods:
        response = error_response(405, None, 'method %s not allowed' % request.method)
        response['Allow'] = ', '.join(methods)
        return response
      try:
        body = None
        if request.method in ('POST', 'PUT', 'PATCH'):
          try:
            body = json.loads(request.body.decode('utf-8') or 'null')
          except RequestDataTooBig:
            raise ScimError(413, None, 'request too large')
          except (ValueError, UnicodeDecodeError):
            raise ScimError(400, 'invalidSyntax', 'the request body is not valid JSON')
        if request.method == 'GET':
          return func(request, client, body, *args, **kwargs)
        try:
          with transaction.atomic():
            response = func(request, client, body, *args, **kwargs)
        except IntegrityError:
          # a concurrent request created the same userName/displayName
          raise ScimError(409, 'uniqueness', 'the resource conflicts with an existing one')
        logger.info('scim:%s %s %s -> %s', client, request.method, request.path, response.status_code)
        return response
      except ScimError as e:
        if request.method != 'GET':
          logger.info('scim:%s %s %s -> %s %s', client, request.method, request.path, e.status, e.scim_type or '')
        return e.response()
    return wrapper
  return decorator


##
## discovery
##

@scim_endpoint('GET')
def service_provider_config(request, client, body):
  return scim_response(schemas.service_provider_config(resources.base_url()))


def _list_response(items, total, start):
  return scim_response({'schemas': [schemas.LIST_SCHEMA], 'totalResults': total, 'startIndex': start,
                        'itemsPerPage': len(items), 'Resources': items})


@scim_endpoint('GET')
def resource_types(request, client, body, resource_type=None):
  types = schemas.resource_types(resources.base_url())
  if resource_type is None:
    return _list_response(types, len(types), 1)
  for t in types:
    if t['id'] == resource_type:
      return scim_response(t)
  raise ScimError(404, None, 'unknown resource type')


@scim_endpoint('GET')
def schema_list(request, client, body, schema_id=None):
  items = schemas.schemas(resources.base_url())
  if schema_id is None:
    return _list_response(items, len(items), 1)
  for s in items:
    if s['id'] == schema_id:
      return scim_response(s)
  raise ScimError(404, None, 'unknown schema')


##
## listing
##

# filter paths that are columns: path -> (field lookup, case-insensitive)
USER_COLUMNS = {('username',): 'user_name__iexact', ('externalid',): 'external_id', ('id',): 'scim_id'}
GROUP_COLUMNS = {('displayname',): 'display_name__iexact', ('externalid',): 'external_id', ('id',): 'scim_id'}


def _int_param(request, name, default):
  try:
    return int(request.GET.get(name, default))
  except (TypeError, ValueError):
    raise ScimError(400, 'invalidValue', '%s must be a number' % name)


def _list(request, queryset, columns, represent):
  start = max(1, _int_param(request, 'startIndex', 1))
  count = min(max(0, _int_param(request, 'count', schemas.DEFAULT_COUNT)), schemas.MAX_RESULTS)
  attributes, excluded = request.GET.get('attributes'), request.GET.get('excludedAttributes')
  queryset = queryset.order_by('pk')

  text = request.GET.get('filter')
  tree = scim_filter.parse_filter(text) if text is not None else None
  if tree is not None:
    # narrow by the conditions on columns, then match the whole filter
    for path, value in (scim_filter.simple_equality(tree) or {}).items():
      if path in columns:
        queryset = queryset.filter(**{columns[path]: value})
    exact = tree[0] == 'cmp' and tree[2] == 'eq' and tree[1] in columns and isinstance(tree[3], str)
    if not exact:
      matching = [rep for rep in (represent(obj, True) for obj in queryset) if scim_filter.matches(tree, rep)]
      page = matching[start - 1:start - 1 + count]
      items = [resources.select_attributes(rep, attributes, excluded) for rep in page]
      return _list_response(items, len(matching), start)

  include_members = 'members' not in (excluded or '').lower() and \
    (not attributes or 'members' in attributes.lower())
  total = queryset.count()
  page = queryset[start - 1:start - 1 + count]
  items = [resources.select_attributes(represent(obj, include_members), attributes, excluded) for obj in page]
  return _list_response(items, total, start)


def _user_rep(scim_user, _include_members=True):
  return resources.user_representation(scim_user)


def _get_scim_user(client, scim_id):
  try:
    return ScimUser.objects.select_related('user').get(client=client, scim_id=scim_id)
  except ScimUser.DoesNotExist:
    raise ScimError(404, None, 'user %s not found' % scim_id)


def _get_scim_group(client, scim_id):
  try:
    return ScimGroup.objects.get(client=client, scim_id=scim_id)
  except ScimGroup.DoesNotExist:
    raise ScimError(404, None, 'group %s not found' % scim_id)


def _selected(request, rep):
  return resources.select_attributes(rep, request.GET.get('attributes'), request.GET.get('excludedAttributes'))


##
## users
##

def _check_user_name(client, user_name, exclude_pk=None):
  others = ScimUser.objects.filter(client=client, user_name__iexact=user_name)
  if exclude_pk:
    others = others.exclude(pk=exclude_pk)
  if others.exists():
    raise ScimError(409, 'uniqueness', 'userName %s exists' % user_name)


def _provision(client, attrs, scim_user=None):
  record = mapping.user_record(attrs)
  existing = provisioning.find_user(record.user_type, record.user_id)
  if existing and ScimUser.objects.filter(user=existing).exclude(pk=getattr(scim_user, 'pk', None)).exists():
    raise ScimError(409, 'uniqueness', 'another SCIM user is linked to the Helios user %s' % record.user_id)
  try:
    return provisioning.provision_user(mapping.source(client), record, user=scim_user.user if scim_user else None)
  except provisioning.ProvisioningConflict as e:
    raise ScimError(409, 'uniqueness', str(e))


def _save_user(client, attrs, scim_user):
  user = _provision(client, attrs, scim_user)
  scim_user.user = user
  scim_user.user_name = attrs['userName']
  scim_user.external_id = attrs.get('externalId', '')
  scim_user.attributes = attrs
  scim_user.save()
  mapping.user_changed(scim_user)
  return scim_response(resources.user_representation(scim_user))


@scim_endpoint('GET', 'POST')
def users(request, client, body):
  if request.method == 'GET':
    return _list(request, ScimUser.objects.filter(client=client).select_related('user'), USER_COLUMNS, _user_rep)
  attrs = resources.normalize_user(body)
  _check_user_name(client, attrs['userName'])
  user = _provision(client, attrs)
  scim_user = ScimUser.objects.create(client=client, user=user, user_name=attrs['userName'],
                                      external_id=attrs.get('externalId', ''), attributes=attrs)
  mapping.user_changed(scim_user)
  rep = resources.user_representation(scim_user)
  return scim_response(rep, status=201, headers={'Location': rep['meta']['location']})


@scim_endpoint('GET', 'PUT', 'PATCH', 'DELETE')
def user(request, client, body, scim_id):
  scim_user = _get_scim_user(client, scim_id)
  if request.method == 'GET':
    return scim_response(_selected(request, resources.user_representation(scim_user)))
  if request.method == 'DELETE':
    mapping.release_user(scim_user)
    return HttpResponse(status=204)
  current = dict(scim_user.attributes, active=scim_user.user.is_active)
  if request.method == 'PATCH':
    body = apply_patch('User', current, body)
  attrs = resources.normalize_user(body, current)
  _check_user_name(client, attrs['userName'], exclude_pk=scim_user.pk)
  return _save_user(client, attrs, scim_user)


##
## groups
##

def _group_rep(group, include_members=True):
  return resources.group_representation(group, include_members)


def _save_group(client, group, attrs, member_ids):
  others = ScimGroup.objects.filter(client=client, display_name__iexact=attrs['displayName'])
  if group.pk:
    others = others.exclude(pk=group.pk)
  if others.exists():
    raise ScimError(409, 'uniqueness', 'displayName %s exists' % attrs['displayName'])
  before = mapping.snapshot(group) if group.pk else None
  group.display_name = attrs['displayName']
  group.external_id = attrs.get('externalId', '')
  group.save()
  members = list(ScimUser.objects.filter(client=client, scim_id__in=member_ids))
  if len(members) != len(member_ids):
    # e.g. users outside the client's provisioning scope: not an error, so that
    # the rest of the group is applied
    logger.warning('scim:%s group %s: %d unknown member(s) ignored', client, group.scim_id,
                   len(member_ids) - len(members))
  group.members.set(members)
  mapping.group_changed(client, before, mapping.snapshot(group))
  return group


@scim_endpoint('GET', 'POST')
def groups(request, client, body):
  if request.method == 'GET':
    return _list(request, ScimGroup.objects.filter(client=client), GROUP_COLUMNS, _group_rep)
  attrs, member_ids = resources.normalize_group(body)
  group = _save_group(client, ScimGroup(client=client), attrs, member_ids)
  rep = resources.group_representation(group)
  return scim_response(rep, status=201, headers={'Location': rep['meta']['location']})


@scim_endpoint('GET', 'PUT', 'PATCH', 'DELETE')
def group(request, client, body, scim_id):
  group = _get_scim_group(client, scim_id)
  if request.method == 'GET':
    excluded = (request.GET.get('excludedAttributes') or '').lower()
    rep = resources.group_representation(group, include_members='members' not in excluded)
    return scim_response(_selected(request, rep))
  if request.method == 'DELETE':
    before = mapping.snapshot(group)
    group.delete()
    mapping.group_changed(client, before, None)
    return HttpResponse(status=204)
  if request.method == 'PATCH':
    current = resources.group_representation(group)
    current = {k: v for k, v in current.items() if k in ('displayName', 'externalId', 'members')}
    body = apply_patch('Group', current, body)
  attrs, member_ids = resources.normalize_group(body)
  _save_group(client, group, attrs, member_ids)
  excluded = (request.GET.get('excludedAttributes') or '').lower()
  return scim_response(_selected(request, resources.group_representation(group, 'members' not in excluded)))


@scim_endpoint('GET')
def root(request, client, body):
  raise ScimError(404, None, 'see /ServiceProviderConfig, /ResourceTypes, /Schemas, /Users and /Groups')
