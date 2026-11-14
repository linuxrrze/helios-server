"""SCIM 2.0 endpoints, included under /scim/v2/ (see helios_scim.views)."""

from django.urls import path, re_path

from . import views

urlpatterns = [
  path('', views.root, name='scim@root'),
  path('ServiceProviderConfig', views.service_provider_config, name='scim@service-provider-config'),
  path('ResourceTypes', views.resource_types, name='scim@resource-types'),
  path('ResourceTypes/<str:resource_type>', views.resource_types, name='scim@resource-type'),
  path('Schemas', views.schema_list, name='scim@schemas'),
  re_path(r'^Schemas/(?P<schema_id>[A-Za-z0-9:._-]+)$', views.schema_list, name='scim@schema'),
  path('Users', views.users, name='scim@users'),
  path('Users/<str:scim_id>', views.user, name='scim@user'),
  path('Groups', views.groups, name='scim@groups'),
  path('Groups/<str:scim_id>', views.group, name='scim@group'),
]
