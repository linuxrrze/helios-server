"""
Bookkeeping of the SCIM server: the resources a client has provisioned, as the
client sees them (id, userName/displayName, the attributes Helios keeps, group
members). What they mean in Helios (users, roles, voter groups) is derived from
these by helios_scim.mapping, through the source-agnostic helios.provisioning.
"""

import uuid

from django.db import models
from django.db.models.functions import Lower

from helios_auth.models import User


def _new_id():
  return str(uuid.uuid4())


class ScimUser(models.Model):
  scim_id = models.CharField(max_length=36, unique=True, default=_new_id)
  client = models.CharField(max_length=50, db_index=True)
  user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='scim_user')
  user_name = models.CharField(max_length=255)
  external_id = models.CharField(max_length=255, blank=True, default='', db_index=True)
  # the attributes Helios keeps (see helios_scim.resources.normalize_user)
  attributes = models.JSONField(default=dict)
  created_at = models.DateTimeField(auto_now_add=True)
  updated_at = models.DateTimeField(auto_now=True)

  class Meta:
    app_label = 'helios_scim'
    constraints = [
      models.UniqueConstraint(Lower('user_name'), 'client', name='scim_user_unique_user_name'),
    ]


class ScimGroup(models.Model):
  scim_id = models.CharField(max_length=36, unique=True, default=_new_id)
  client = models.CharField(max_length=50, db_index=True)
  display_name = models.CharField(max_length=100)
  external_id = models.CharField(max_length=255, blank=True, default='', db_index=True)
  members = models.ManyToManyField(ScimUser, related_name='groups', blank=True)
  created_at = models.DateTimeField(auto_now_add=True)
  updated_at = models.DateTimeField(auto_now=True)

  class Meta:
    app_label = 'helios_scim'
    constraints = [
      models.UniqueConstraint(Lower('display_name'), 'client', name='scim_group_unique_display_name'),
    ]
