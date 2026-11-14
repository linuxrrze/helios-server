
# a massive hack to see if we're testing, in which case we use different settings
import sys

import json
import os

import ldap
from django_auth_ldap.config import LDAPSearch

TESTING = 'test' in sys.argv

# go through environment variables and override them
def get_from_env(var, default):
    if not TESTING and var in os.environ:
        return os.environ[var]
    else:
        return default

DEBUG = (get_from_env('DEBUG', '1') == '1')

# add admins of the form: 
#    ('Ben Adida', 'ben@adida.net'),
# if you want to be emailed about errors.
admin_email = get_from_env('ADMIN_EMAIL', None)
if admin_email:
    ADMINS = [(get_from_env('ADMIN_NAME', ''), admin_email)]
else:
    ADMINS = []

MANAGERS = ADMINS

# Subject prefix of the error mails to ADMINS (Django's default is "[Django] "). A space
# is added after a non-empty prefix if missing, because .env files often lose trailing spaces.
EMAIL_SUBJECT_PREFIX = get_from_env('EMAIL_SUBJECT_PREFIX', '[Helios]')
if EMAIL_SUBJECT_PREFIX and not EMAIL_SUBJECT_PREFIX.endswith(' '):
    EMAIL_SUBJECT_PREFIX += ' '

# is this the master Helios web site?
MASTER_HELIOS = (get_from_env('MASTER_HELIOS', '0') == '1')

# show ability to log in? (for example, if the site is mostly used by voters)
# if turned off, the admin will need to know to go to /auth/login manually
SHOW_LOGIN_OPTIONS = (get_from_env('SHOW_LOGIN_OPTIONS', '1') == '1')

# sometimes, when the site is not that social, it's not helpful
# to display who created the election
SHOW_USER_INFO = (get_from_env('SHOW_USER_INFO', '1') == '1')

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'helios',
        'CONN_MAX_AGE': 600,
    },
}

# override if we have an env variable
if get_from_env('DATABASE_URL', None):
    import dj_database_url
    _db_ssl = get_from_env('DATABASE_SSL_REQUIRE', '1') == '1'
    DATABASES['default'] = dj_database_url.config(conn_max_age=600, ssl_require=_db_ssl)
    DATABASES['default']['ENGINE'] = 'django.db.backends.postgresql'

# explicitly set the default auto-created primary field to silence warning models.W042
DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'

# Local time zone for this installation. Choices can be found here:
# http://en.wikipedia.org/wiki/List_of_tz_zones_by_name
# although not all choices may be available on all operating systems.
# If running in a Windows environment this must be set to the same as your
# system time zone.
#
# USE_TZ is False. Helios itself works in UTC (election start/end, cast times
# and so on use utcnow()); this only affects Django's automatic timestamps
# such as Election.created_at and the server process' local time.
# Default UTC (Helios upstream used America/Los_Angeles): set TIME_ZONE, or ELECTION_TIME_ZONE,
# to the zone your administrators think in, e.g. Europe/Berlin.
TIME_ZONE = get_from_env('TIME_ZONE', 'UTC')

# Time zone in which administrators enter election dates (voting start, end,
# extension) and in which the pages show them, always with the zone's name.
# Helios still stores and compares these times in UTC; set it to UTC to enter
# and show UTC as before. An IANA name such as Europe/Berlin.
ELECTION_TIME_ZONE = get_from_env('ELECTION_TIME_ZONE', TIME_ZONE)

USE_TZ = False

# Interface languages. Each request uses the language the visitor chose with
# the language switcher (cookie), else the browser's preferred language, else
# LANGUAGE_CODE. Emails use the language of the person whose action sends them.
LANGUAGES = [
    ('en', 'English'),
    ('de', 'Deutsch'),
]
LANGUAGE_CODE = get_from_env('LANGUAGE_CODE', 'en')
LOCALE_PATHS = [os.path.join(os.path.dirname(__file__), 'locale')]

SITE_ID = 1

# If you set this to False, Django will make some optimizations so as not
# to load the internationalization machinery.
USE_I18N = True

# Absolute path to the directory that holds media.
# Example: "/home/media/media.lawrence.com/"
MEDIA_ROOT = ''

# URL that handles the media served from MEDIA_ROOT. Make sure to use a
# trailing slash if there is a path component (optional in other cases).
# Examples: "http://media.lawrence.com", "http://example.com/media/"
MEDIA_URL = ''

# URL prefix for admin media -- CSS, JavaScript and images. Make sure to use a
# trailing slash.
# Examples: "http://foo.com/media/", "/media/".
STATIC_URL = '/media/'

# Make this unique, and don't share it with anybody.
SECRET_KEY = get_from_env('SECRET_KEY', 'replaceme')

# Secret key for HMAC confirmation codes (separate from Django SECRET_KEY)
EMAIL_OPTOUT_SECRET = get_from_env('EMAIL_OPTOUT_SECRET', 'replace-with-secure-random-key')

# Refuse to run in production with placeholder secrets: Django's signing
# (and anything added later that relies on it) assumes SECRET_KEY is secret,
# and a known EMAIL_OPTOUT_SECRET lets anyone opt voters out of election mail.
# The messages say what is wrong (never the value itself) and how to fix it.
# CHANGE-ME is the placeholder of the example configurations (.env.example,
# helios.env.example).
def _secret_problem(var, value, min_length):
  if var not in os.environ or not value.strip():
    return "%s is not set (or empty)." % var
  if value in ('replaceme', 'replace-with-secure-random-key') or 'change-me' in value.lower():
    return "%s is still the placeholder from the example configuration." % var
  if len(value) < min_length:
    return ("%s is only %d characters long; it needs at least %d. With Docker Compose, a $ in .env "
            "must be written as $$, otherwise Compose replaces it and what follows." % (var, len(value), min_length))
  return None

if not DEBUG and not TESTING:
  from django.core.exceptions import ImproperlyConfigured
  for _var, _value, _min_length in (('SECRET_KEY', SECRET_KEY, 32), ('EMAIL_OPTOUT_SECRET', EMAIL_OPTOUT_SECRET, 1)):
    _problem = _secret_problem(_var, _value, _min_length)
    if _problem:
      raise ImproperlyConfigured(
        "%s Set %s in the environment (Docker Compose: .env; systemd: the EnvironmentFile) to a long "
        "random value, e.g. the output of: python3 -c \"import secrets; print(secrets.token_urlsafe(50))\"" % (_problem, _var))

# If debug is set to false and ALLOWED_HOSTS is not declared, django raises  "CommandError: You must set settings.ALLOWED_HOSTS if DEBUG is False."
# If in production, you got a bad request (400) error
#More info: https://docs.djangoproject.com/en/1.7/ref/settings/#allowed-hosts (same for 1.6)

ALLOWED_HOSTS = get_from_env('ALLOWED_HOSTS', 'localhost').split(",")

# Secure Stuff
if get_from_env('SSL', '0') == '1':
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True

    # tuned for Heroku
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SESSION_COOKIE_HTTPONLY = True

# Set this explicitly rather than relying on the Django default: it is a load-bearing
# part of our CSRF defense, since Helios does its own CSRF checking (check_csrf) rather
# than using Django's CsrfViewMiddleware. 'Lax' keeps the session cookie off every
# cross-site POST and off cross-site GET subresources (images, iframes, fetch), while
# still allowing ordinary inbound links to Helios to stay logged in.
SESSION_COOKIE_SAMESITE = get_from_env('SESSION_COOKIE_SAMESITE', 'Lax')

# let's go with one year because that's the way to do it now
STS = False
if get_from_env('HSTS', '0') == '1':
    STS = True
    # we're using our own custom middleware now
    # SECURE_HSTS_SECONDS = 31536000
    # not doing subdomains for now cause that is not likely to be necessary and can screw things up.
    # SECURE_HSTS_INCLUDE_SUBDOMAINS = True

SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True

# Content Security Policy Configuration (django-csp)
# https://django-csp.readthedocs.io/
#
# 'unsafe-inline' and 'unsafe-eval' are required due to legacy inline scripts,
# onclick handlers, and jQuery JSON eval usage. Future improvement: use nonces.

CSP_DEFAULT_SRC = ("'self'",)
CSP_SCRIPT_SRC = ("'self'", "'unsafe-inline'", "'unsafe-eval'")
CSP_STYLE_SRC = ("'self'", "'unsafe-inline'")
CSP_IMG_SRC = ("'self'", "data:")
CSP_FONT_SRC = ("'self'",)
CSP_CONNECT_SRC = ("'self'",)
CSP_WORKER_SRC = ("'self'", "blob:")
CSP_FORM_ACTION = ("'self'",)
CSP_FRAME_ANCESTORS = ("'self'",)
CSP_BASE_URI = ("'self'",)
CSP_OBJECT_SRC = ("'none'",)

# Set CSP_REPORT_ONLY=1 to test without enforcing
if get_from_env('CSP_REPORT_ONLY', '0') == '1':
    CSP_REPORT_ONLY = True
else:
    CSP_REPORT_ONLY = False

# Optional: URI to receive CSP violation reports
_csp_report_uri = get_from_env('CSP_REPORT_URI', None)
if _csp_report_uri:
    CSP_REPORT_URI = _csp_report_uri

SILENCED_SYSTEM_CHECKS = ['urls.W002']

MIDDLEWARE = [
    # secure a bunch of things
    'django.middleware.security.SecurityMiddleware',
    'helios.security.HSTSMiddleware',
    'csp.middleware.CSPMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # 'django.middleware.csrf.CsrfViewMiddleware',

    'django.middleware.common.CommonMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
]

ROOT_URLCONF = 'urls'

ROOT_PATH = os.path.dirname(__file__)

# Site-specific overrides, searched before the built-in files:
# - SITE_TEMPLATES_DIR mirrors the repository layout, e.g.
#   <dir>/server_ui/templates/imprint.html replaces or adds that template;
# - SITE_STATIC_DIR is served under /static/site/ (logos, stylesheets, images).
SITE_TEMPLATES_DIR = get_from_env('SITE_TEMPLATES_DIR', '')
SITE_STATIC_DIR = get_from_env('SITE_STATIC_DIR', '')

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'APP_DIRS': True,
        'DIRS': ([SITE_TEMPLATES_DIR] if SITE_TEMPLATES_DIR else []) + [
            ROOT_PATH,
            os.path.join(ROOT_PATH, 'templates'),
            # os.path.join(ROOT_PATH, 'helios/templates'),  # covered by APP_DIRS:True
            # os.path.join(ROOT_PATH, 'helios_auth/templates'),  # covered by APP_DIRS:True
            # os.path.join(ROOT_PATH, 'server_ui/templates'),  # covered by APP_DIRS:True
        ],
        'OPTIONS': {
            'debug': DEBUG
        }
    },
]

INSTALLED_APPS = (
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.sites',
    'anymail',
    'django_tasks',
    'django_tasks_db',
    ## HELIOS stuff
    'helios_auth',
    'helios',
    'server_ui',
)

# Email backend configuration
# In development mode, set EMAIL_USE_CONSOLE=1 to print emails to stdout
if DEBUG and get_from_env('EMAIL_USE_CONSOLE', '0') == '1':
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

ANYMAIL = {
    "MAILGUN_API_KEY": get_from_env('MAILGUN_API_KEY', None),
}

# Mailgun overrides console backend if configured
if ANYMAIL["MAILGUN_API_KEY"]:
    EMAIL_BACKEND = "anymail.backends.mailgun.EmailBackend"

##
## HELIOS
##


MEDIA_ROOT = ROOT_PATH + "media/"

# a relative path where voter upload files are stored
VOTER_UPLOAD_REL_PATH = "voters/%Y/%m/%d"


# Change your email settings
DEFAULT_FROM_EMAIL = get_from_env('DEFAULT_FROM_EMAIL', 'ben@adida.net')
DEFAULT_FROM_NAME = get_from_env('DEFAULT_FROM_NAME', 'Ben for Helios')
SERVER_EMAIL = '%s <%s>' % (DEFAULT_FROM_NAME, DEFAULT_FROM_EMAIL)

LOGIN_URL = '/auth/'
LOGOUT_ON_CONFIRMATION = True

# The two hosts are here so the main site can be over plain HTTP
# while the voting URLs are served over SSL.
URL_HOST = get_from_env("URL_HOST", "http://localhost:8000").rstrip("/")

# IMPORTANT: you should not change this setting once you've created
# elections, as your elections' cast_url will then be incorrect.
# SECURE_URL_HOST = "https://localhost:8443"
SECURE_URL_HOST = get_from_env("SECURE_URL_HOST", URL_HOST).rstrip("/")

# election stuff
SITE_TITLE = get_from_env('SITE_TITLE', 'Helios Voting')
MAIN_LOGO_URL = get_from_env('MAIN_LOGO_URL', '/static/logo.png')
ALLOW_ELECTION_INFO_URL = (get_from_env('ALLOW_ELECTION_INFO_URL', '0') == '1')

# Election addresses (<URL_HOST>/helios/e/<short name>). On a server shared by several
# organizations, each can get its own prefix, so that their chosen addresses don't
# collide. The prefix of the person creating the election comes from
# ELECTION_SHORT_NAME_PREFIXES_BY_IDP (their identity provider: issuer / entity ID, then
# auth system name), else from ELECTION_SHORT_NAME_PREFIXES (their email domain,
# subdomains included), else ELECTION_SHORT_NAME_PREFIX. The prefix only separates the
# names; it grants no permissions.
SHORT_NAME_PREFIX_RE = r'^[a-z0-9]{1,20}$'
SHORT_NAME_PREFIX_RULE = "1 to 20 lowercase letters or digits"

def parse_short_name_prefixes(mapping_json, default_prefix):
  """(domain -> prefix, default prefix) from the two variables; ImproperlyConfigured if invalid"""
  import re
  from django.core.exceptions import ImproperlyConfigured
  prefix_re = re.compile(SHORT_NAME_PREFIX_RE)
  rule = SHORT_NAME_PREFIX_RULE

  default_prefix = (default_prefix or '').strip()
  if default_prefix and not prefix_re.match(default_prefix):
    raise ImproperlyConfigured("ELECTION_SHORT_NAME_PREFIX %r is not a valid prefix: use %s." % (default_prefix, rule))
  try:
    raw = json.loads(mapping_json or '{}')
  except ValueError as e:
    raise ImproperlyConfigured("ELECTION_SHORT_NAME_PREFIXES is not valid JSON (%s). Expected an object such as "
                               "{\"uni-a.example.org\": \"unia\"}." % e)
  if not isinstance(raw, dict):
    raise ImproperlyConfigured("ELECTION_SHORT_NAME_PREFIXES must be a JSON object mapping email domains to "
                               "prefixes, such as {\"uni-a.example.org\": \"unia\"}.")
  mapping = {}
  for domain, prefix in raw.items():
    key = domain.strip().lstrip('@').rstrip('.').lower()
    if not key or '@' in key or ' ' in key:
      raise ImproperlyConfigured("ELECTION_SHORT_NAME_PREFIXES: %r is not an email domain." % domain)
    if not isinstance(prefix, str) or not prefix_re.match(prefix):
      raise ImproperlyConfigured("ELECTION_SHORT_NAME_PREFIXES: the prefix %r for %r is not valid: use %s."
                                 % (prefix, domain, rule))
    mapping[key] = prefix
  return mapping, default_prefix

ELECTION_SHORT_NAME_PREFIXES, ELECTION_SHORT_NAME_PREFIX = parse_short_name_prefixes(
  get_from_env('ELECTION_SHORT_NAME_PREFIXES', ''), get_from_env('ELECTION_SHORT_NAME_PREFIX', ''))

def idp_key(value):
  """an auth system name or issuer as compared: without surrounding spaces and trailing slashes"""
  return value.strip().rstrip('/')

def parse_short_name_prefixes_by_idp(mapping_json):
  """identity provider (auth system name or issuer) -> prefix; ImproperlyConfigured if invalid"""
  import re
  from django.core.exceptions import ImproperlyConfigured
  prefix_re = re.compile(SHORT_NAME_PREFIX_RE)
  example = "{\"saml\": \"unia\", \"https://idp.uni-b.example.org/realms/staff\": \"unib\"}"
  try:
    raw = json.loads(mapping_json or '{}')
  except ValueError as e:
    raise ImproperlyConfigured("ELECTION_SHORT_NAME_PREFIXES_BY_IDP is not valid JSON (%s). Expected an object "
                               "such as %s." % (e, example))
  if not isinstance(raw, dict):
    raise ImproperlyConfigured("ELECTION_SHORT_NAME_PREFIXES_BY_IDP must be a JSON object mapping auth system "
                               "names or issuers to prefixes, such as %s." % example)
  mapping = {}
  for idp, prefix in raw.items():
    key = idp_key(idp)
    if not key or any(c.isspace() for c in key):
      raise ImproperlyConfigured("ELECTION_SHORT_NAME_PREFIXES_BY_IDP: %r is not an auth system name or issuer." % idp)
    if not isinstance(prefix, str) or not prefix_re.match(prefix):
      raise ImproperlyConfigured("ELECTION_SHORT_NAME_PREFIXES_BY_IDP: the prefix %r for %r is not valid: use %s."
                                 % (prefix, idp, SHORT_NAME_PREFIX_RULE))
    mapping[key] = prefix
  return mapping

ELECTION_SHORT_NAME_PREFIXES_BY_IDP = parse_short_name_prefixes_by_idp(
  get_from_env('ELECTION_SHORT_NAME_PREFIXES_BY_IDP', ''))

# Layout and branding of the operating organization. Everything below is optional;
# without it the site shows only the Helios logo and the built-in pages.
# Organization shown in the header and footer (logo links to ORGANIZATION_URL).
ORGANIZATION_NAME = get_from_env('ORGANIZATION_NAME', '')
ORGANIZATION_URL = get_from_env('ORGANIZATION_URL', '')
# small logo in the header bar, and a larger one (for a dark background) in the footer
ORGANIZATION_LOGO_URL = get_from_env('ORGANIZATION_LOGO_URL', '')
FOOTER_LOGO_URL = get_from_env('FOOTER_LOGO_URL', '')
# extra stylesheet loaded after the default layout, e.g. to set colors and a login
# background image (CSS variables, see server_ui/media/layout.css)
SITE_CSS_URL = get_from_env('SITE_CSS_URL', '')
# Dark mode: 'auto' follows the visitor's system setting (prefers-color-scheme),
# 'off' always shows the light colors. The default is 'auto' without a site
# stylesheet and 'off' with one, because a site stylesheet usually sets light
# colors only; set 'auto' after giving it dark colors too (see layout.css).
DARK_MODE = get_from_env('DARK_MODE', 'off' if SITE_CSS_URL else 'auto')
if DARK_MODE not in ('auto', 'off'):
    DARK_MODE = 'off'
# Legal pages linked in the footer; a link is shown only when its URL is set. They can
# point to external pages, or to /imprint, /privacy and /accessibility, which render
# server_ui/templates/<page>.html (provide imprint.html and accessibility.html in
# SITE_TEMPLATES_DIR; privacy.html has a generic default).
IMPRINT_URL = get_from_env('IMPRINT_URL', '')
PRIVACY_URL = get_from_env('PRIVACY_URL', '/privacy')
ACCESSIBILITY_URL = get_from_env('ACCESSIBILITY_URL', '')
# further footer links, JSON: [{"url": "https://...", "text": "..."}]
FOOTER_LINKS = json.loads(get_from_env('FOOTER_LINKS', '[]'))

WELCOME_MESSAGE = get_from_env('WELCOME_MESSAGE', "This is the default message")

HELP_EMAIL_ADDRESS = get_from_env('HELP_EMAIL_ADDRESS', 'help@heliosvoting.org')

AUTH_TEMPLATE_BASE = "server_ui/templates/base.html"
HELIOS_TEMPLATE_BASE = "server_ui/templates/base.html"
HELIOS_ADMIN_ONLY = False
HELIOS_VOTERS_UPLOAD = True
HELIOS_VOTERS_EMAIL = True

# Number of weeks after tallying when voter emails should be disabled
HELIOS_VOTER_EMAIL_CUTOFF_WEEKS = int(get_from_env('HELIOS_VOTER_EMAIL_CUTOFF_WEEKS', '3'))

# are elections private by default?
HELIOS_PRIVATE_DEFAULT = False

# authentication systems enabled
# AUTH_ENABLED_SYSTEMS = ['password','facebook', 'google', 'yahoo']
AUTH_ENABLED_SYSTEMS = get_from_env('AUTH_ENABLED_SYSTEMS',
                                    get_from_env('AUTH_ENABLED_AUTH_SYSTEMS', 'ldap,password,google,facebook')
                                    ).split(",")

# Add development login in debug mode
if DEBUG:
    AUTH_ENABLED_SYSTEMS = ['devlogin'] + AUTH_ENABLED_SYSTEMS

AUTH_DEFAULT_SYSTEM = get_from_env('AUTH_DEFAULT_SYSTEM', get_from_env('AUTH_DEFAULT_AUTH_SYSTEM', None))

# google
GOOGLE_CLIENT_ID = get_from_env('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = get_from_env('GOOGLE_CLIENT_SECRET', '')

# facebook
FACEBOOK_APP_ID = get_from_env('FACEBOOK_APP_ID','')
FACEBOOK_API_KEY = get_from_env('FACEBOOK_API_KEY','')
FACEBOOK_API_SECRET = get_from_env('FACEBOOK_API_SECRET','')

# LinkedIn
LINKEDIN_CLIENT_ID = get_from_env('LINKEDIN_CLIENT_ID', '')
LINKEDIN_CLIENT_SECRET = get_from_env('LINKEDIN_CLIENT_SECRET', '')

# CAS (for universities)
CAS_USERNAME = get_from_env('CAS_USERNAME', "")
CAS_PASSWORD = get_from_env('CAS_PASSWORD', "")
CAS_ELIGIBILITY_URL = get_from_env('CAS_ELIGIBILITY_URL', "")
CAS_ELIGIBILITY_REALM = get_from_env('CAS_ELIGIBILITY_REALM', "")

# GitHub
GH_CLIENT_ID = get_from_env('GH_CLIENT_ID', '')
GH_CLIENT_SECRET = get_from_env('GH_CLIENT_SECRET', '')

# Gitlab
GITLAB_CLIENT_ID = get_from_env('GITLAB_CLIENT_ID', "")
GITLAB_CLIENT_SECRET = get_from_env('GITLAB_CLIENT_SECRET', "")

# Generic OpenID Connect (see helios_auth/auth_systems/oidc.py).
# Enabled when OIDC_DISCOVERY_URL is set; add its name to AUTH_ENABLED_SYSTEMS.
OIDC_AUTH_SYSTEM_NAME = get_from_env('OIDC_AUTH_SYSTEM_NAME', 'oidc')
OIDC_DISCOVERY_URL = get_from_env('OIDC_DISCOVERY_URL', '')
OIDC_CLIENT_ID = get_from_env('OIDC_CLIENT_ID', '')
OIDC_CLIENT_SECRET = get_from_env('OIDC_CLIENT_SECRET', '')
OIDC_SCOPES = get_from_env('OIDC_SCOPES', 'openid email profile')
OIDC_USER_ID_CLAIM = get_from_env('OIDC_USER_ID_CLAIM', 'sub')
OIDC_TRUST_UNVERIFIED_EMAIL = (get_from_env('OIDC_TRUST_UNVERIFIED_EMAIL', '0') == '1')
# also end the session at the identity provider on logout (register <URL_HOST>/auth/logout/done there)
OIDC_LOGOUT_AT_PROVIDER = (get_from_env('OIDC_LOGOUT_AT_PROVIDER', '0') == '1')
OIDC_LOGIN_MESSAGE = get_from_env('OIDC_LOGIN_MESSAGE', '')  # empty: translated default

# Generic SAML 2.0 (see helios_auth/auth_systems/saml.py; needs `uv sync --extra saml`).
# Enabled when SAML_IDP_METADATA_URL or SAML_IDP_METADATA_FILE is set.
SAML_AUTH_SYSTEM_NAME = get_from_env('SAML_AUTH_SYSTEM_NAME', 'saml')
SAML_IDP_METADATA_URL = get_from_env('SAML_IDP_METADATA_URL', '')
SAML_IDP_METADATA_FILE = get_from_env('SAML_IDP_METADATA_FILE', '')
SAML_IDP_ENTITY_ID = get_from_env('SAML_IDP_ENTITY_ID', '')
SAML_IDP_X509_CERT = get_from_env('SAML_IDP_X509_CERT', '')
SAML_SP_ENTITY_ID = get_from_env('SAML_SP_ENTITY_ID', '')
SAML_SP_ACS_URL = get_from_env('SAML_SP_ACS_URL', '')
SAML_SP_X509_CERT = get_from_env('SAML_SP_X509_CERT', '')
SAML_SP_PRIVATE_KEY = get_from_env('SAML_SP_PRIVATE_KEY', '')
SAML_ATTRIBUTE_USER_ID = get_from_env('SAML_ATTRIBUTE_USER_ID', 'nameid')
SAML_ATTRIBUTE_EMAIL = get_from_env('SAML_ATTRIBUTE_EMAIL', '')
SAML_ATTRIBUTE_NAME = get_from_env('SAML_ATTRIBUTE_NAME', '')
SAML_ATTRIBUTE_GIVEN_NAME = get_from_env('SAML_ATTRIBUTE_GIVEN_NAME', '')
SAML_ATTRIBUTE_SURNAME = get_from_env('SAML_ATTRIBUTE_SURNAME', '')
SAML_LOGIN_MESSAGE = get_from_env('SAML_LOGIN_MESSAGE', '')  # empty: translated default

# email server
EMAIL_HOST = get_from_env('EMAIL_HOST', 'localhost')
EMAIL_PORT = int(get_from_env('EMAIL_PORT', "2525"))
EMAIL_HOST_USER = get_from_env('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = get_from_env('EMAIL_HOST_PASSWORD', '')
# STARTTLS (usually port 587) or implicit TLS (usually port 465), not both
EMAIL_USE_TLS = (get_from_env('EMAIL_USE_TLS', '0') == '1')
EMAIL_USE_SSL = (get_from_env('EMAIL_USE_SSL', '0') == '1')
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("Set only one of EMAIL_USE_TLS (STARTTLS) and EMAIL_USE_SSL (implicit TLS).")
# seconds; without a timeout a hanging mail server blocks the task worker
EMAIL_TIMEOUT = int(get_from_env('EMAIL_TIMEOUT', '60'))

# to use AWS Simple Email Service
# in which case environment should contain
# AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
if get_from_env('EMAIL_USE_AWS', '0') == '1':
    EMAIL_BACKEND = 'django_ses.SESBackend'

# Every email goes through helios.mail_backend, which logs one line per email
# (recipient masked) and passes it on to the backend selected above.
HELIOS_EMAIL_BACKEND = globals().get('EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_BACKEND = 'helios.mail_backend.LoggingEmailBackend'

# set up logging
import logging

logging.basicConfig(
    level=logging.DEBUG if TESTING else logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)

# background tasks are stored in the database and run by `manage.py db_worker`
TASKS = {'default': {'BACKEND': 'django_tasks_db.DatabaseBackend', 'QUEUES': ['default']}}

# Rollbar Error Logging
ROLLBAR_ACCESS_TOKEN = get_from_env('ROLLBAR_ACCESS_TOKEN', None)
if ROLLBAR_ACCESS_TOKEN:
  print("setting up rollbar")
  MIDDLEWARE += ['rollbar.contrib.django.middleware.RollbarNotifierMiddleware',]
  ROLLBAR = {
    'access_token': ROLLBAR_ACCESS_TOKEN,
    'environment': 'development' if DEBUG else 'production',  
  }


# ldap
# see configuration example at https://pythonhosted.org/django-auth-ldap/example.html
AUTH_LDAP_SERVER_URI = "ldap://ldap.forumsys.com" # replace by your Ldap URI
AUTH_LDAP_BIND_DN = "cn=read-only-admin,dc=example,dc=com"
AUTH_LDAP_BIND_PASSWORD = "password"
AUTH_LDAP_USER_SEARCH = LDAPSearch("dc=example,dc=com",
    ldap.SCOPE_SUBTREE, "(uid=%(user)s)"
)

AUTH_LDAP_USER_ATTR_MAP = {
    "first_name": "givenName",
    "last_name": "sn",
    "email": "mail",
}

AUTH_LDAP_BIND_AS_AUTHENTICATING_USER = True

AUTH_LDAP_ALWAYS_UPDATE_USER = False


# Per-language variants of display texts set above: e.g. WELCOME_MESSAGE_DE is
# shown instead of WELCOME_MESSAGE when the page is in German. Read with
# helios_auth.utils.localized_setting().
LOCALIZED_SETTING_NAMES = ('SITE_TITLE', 'WELCOME_MESSAGE', 'ORGANIZATION_NAME', 'OIDC_LOGIN_MESSAGE', 'SAML_LOGIN_MESSAGE')
LOCALIZED_SETTINGS = {
    name: {code: get_from_env('%s_%s' % (name, code.upper().replace('-', '_')), None) for code, _name in LANGUAGES}
    for name in LOCALIZED_SETTING_NAMES
}
