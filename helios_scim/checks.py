"""System checks of the SCIM settings (run by manage.py check and at startup)."""

from django.conf import settings
from django.core import checks


@checks.register()
def check_scim_settings(app_configs, **kwargs):
  if not getattr(settings, 'SCIM_ENABLED', False):
    return []
  from helios.provisioning import OWNER_USER_TYPE
  from helios_auth.auth_systems import AUTH_SYSTEMS
  from .auth import configured_clients, TokenConfigError
  from .mapping import USER_ID_ATTRIBUTES, user_type

  errors = []
  try:
    if not configured_clients():
      errors.append(checks.Warning("SCIM_ENABLED is set, but SCIM_TOKENS has no client: every request is rejected.",
                                   id='helios_scim.W001'))
  except TokenConfigError as e:
    errors.append(checks.Error(str(e), id='helios_scim.E001'))
  if getattr(settings, 'SCIM_USER_ID_ATTRIBUTE', 'userName') not in USER_ID_ATTRIBUTES:
    errors.append(checks.Error("SCIM_USER_ID_ATTRIBUTE must be one of %s." % ", ".join(USER_ID_ATTRIBUTES),
                               id='helios_scim.E002'))
  if user_type() not in AUTH_SYSTEMS:
    errors.append(checks.Warning(
      "SCIM_USER_TYPE %r is not an auth system: provisioned users won't be able to log in." % user_type(),
      id='helios_scim.W002'))
  if OWNER_USER_TYPE in AUTH_SYSTEMS:
    errors.append(checks.Error("An auth system must not be named %r while SCIM is enabled." % OWNER_USER_TYPE,
                               id='helios_scim.E003'))
  return errors
