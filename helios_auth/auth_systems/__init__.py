from django.conf import settings
from . import password, linkedin, cas, facebook, google, yahoo, github, ldapauth, gitlab

# Import devlogin only in debug mode
if settings.DEBUG:
    from . import devlogin

AUTH_SYSTEMS = {}

AUTH_SYSTEMS['password'] = password
AUTH_SYSTEMS['linkedin'] = linkedin
AUTH_SYSTEMS['cas'] = cas
AUTH_SYSTEMS['facebook'] = facebook
AUTH_SYSTEMS['google'] = google
AUTH_SYSTEMS['yahoo'] = yahoo
AUTH_SYSTEMS['github'] = github
AUTH_SYSTEMS['ldap'] = ldapauth
AUTH_SYSTEMS['gitlab'] = gitlab

# Generic single sign-on. Each can be registered under a custom name, e.g.
# "campus", so that switching protocol (SAML -> OIDC) keeps existing user
# identities: users are stored by (auth system name, user id).
if getattr(settings, 'OIDC_DISCOVERY_URL', None):
    from .oidc import OIDCAuthSystem
    _oidc_name = getattr(settings, 'OIDC_AUTH_SYSTEM_NAME', None) or 'oidc'
    AUTH_SYSTEMS[_oidc_name] = OIDCAuthSystem(_oidc_name)

if getattr(settings, 'SAML_IDP_METADATA_URL', None) or getattr(settings, 'SAML_IDP_METADATA_FILE', None):
    from .saml import SAMLAuthSystem
    _saml_name = getattr(settings, 'SAML_AUTH_SYSTEM_NAME', None) or 'saml'
    if _saml_name in AUTH_SYSTEMS:
        # e.g. both configured as "campus" while switching protocol: SAML would
        # silently replace OIDC. Configure only one of them under that name.
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured("SAML and OIDC are both configured as auth system %r; "
                                   "remove the SAML_IDP_* or the OIDC_DISCOVERY_URL setting" % _saml_name)
    AUTH_SYSTEMS[_saml_name] = SAMLAuthSystem(_saml_name)

# Add devlogin only in debug mode
if settings.DEBUG:
    AUTH_SYSTEMS['devlogin'] = devlogin

# not ready
#import live
#AUTH_SYSTEMS['live'] = live

def can_check_constraint(auth_system):
    return auth_system in AUTH_SYSTEMS and hasattr(AUTH_SYSTEMS[auth_system], 'check_constraint')

def can_list_categories(auth_system):
    return auth_system in AUTH_SYSTEMS and hasattr(AUTH_SYSTEMS[auth_system], 'list_categories')

def uses_case_insensitive_user_id(auth_system):
    """
    Check if an auth system uses case-insensitive user IDs.
    Auth systems can opt in by setting CASE_INSENSITIVE_USER_ID = True.
    """
    return auth_system in AUTH_SYSTEMS and getattr(AUTH_SYSTEMS[auth_system], 'CASE_INSENSITIVE_USER_ID', False)
