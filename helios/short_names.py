"""
Election addresses: the short name in <URL_HOST>/helios/e/<short name>.

The short name is optional; without one an election uses its UUID. On a server
shared by several organizations, the name an administrator chooses is stored
with their organization's prefix ("<prefix>-<name>", see
ELECTION_SHORT_NAME_PREFIXES_BY_IDP and ELECTION_SHORT_NAME_PREFIXES in
settings.py), so that two organizations can both use e.g. "board-2026". The
prefix is a namespace only: nothing derives permissions from it.

Addresses are unique regardless of case ("Board" is taken if "board" exists),
but stored as entered.
"""

from django.conf import settings
from django.urls import reverse

from helios import url_names


def _domains(user):
  """email domains of a user: of the email address, then of a login ID such as jdoe@uni-a.example.org"""
  candidates = []
  info = getattr(user, 'info', None) or {}
  if isinstance(info, dict) and info.get('email'):
    candidates.append(info['email'])
  user_id = getattr(user, 'user_id', None) or ''
  if '@' in user_id:
    candidates.append(user_id)
  return [c.rsplit('@', 1)[1].strip().rstrip('.').lower() for c in candidates if '@' in c]


def _idps(user):
  """
  the identity provider a user logged in with, most specific first: the issuer
  (OIDC issuer or SAML IdP entity ID, stored at login since it was added), then
  the auth system name (user_type, e.g. "saml", "oidc", "ldap", "google")
  """
  keys = []
  info = getattr(user, 'info', None) or {}
  issuer = info.get('issuer') if isinstance(info, dict) else None
  if isinstance(issuer, str):
    # compared as in settings.idp_key
    keys.append(issuer.strip().rstrip('/'))
  user_type = getattr(user, 'user_type', None)
  if user_type:
    keys.append(user_type)
  return [key for key in keys if key]


def prefix_for_user(user):
  """
  the prefix for the addresses of elections created by user ('' for none): by
  identity provider, else by email domain, else the default prefix
  """
  if user:
    by_idp = settings.ELECTION_SHORT_NAME_PREFIXES_BY_IDP
    for idp in _idps(user):
      if idp in by_idp:
        return by_idp[idp]
    mapping = settings.ELECTION_SHORT_NAME_PREFIXES
    for domain in _domains(user):
      # the most specific entry wins, e.g. med.uni-a.example.org over uni-a.example.org
      matches = [key for key in mapping if domain == key or domain.endswith('.' + key)]
      if matches:
        return mapping[max(matches, key=len)]
  return settings.ELECTION_SHORT_NAME_PREFIX


def all_prefixes():
  return (set(settings.ELECTION_SHORT_NAME_PREFIXES_BY_IDP.values()) | set(settings.ELECTION_SHORT_NAME_PREFIXES.values())
          | ({settings.ELECTION_SHORT_NAME_PREFIX} - {''}))


def with_prefix(prefix, name):
  return '%s-%s' % (prefix, name) if prefix else name


def reserved_prefix(name):
  """the configured prefix that name starts with (as "<prefix>-", in any case), or None"""
  name = name.lower()
  for prefix in all_prefixes():
    if name.startswith(prefix + '-'):
      return prefix
  return None


def is_taken(short_name, election=None):
  """
  whether another election has this address, regardless of case: "Board" and
  "board" would look like the same link to people
  """
  # deleted elections keep their short name (the column is unique)
  from helios.models import Election
  others = Election.objects_with_deleted.filter(short_name__iexact=short_name)
  if election is not None and election.pk:
    others = others.exclude(pk=election.pk)
  return others.exists()


def base_url():
  """the address of an election without its short name, e.g. https://helios.example.org/helios/e/"""
  return settings.URL_HOST + reverse(url_names.ELECTION_SHORTCUT, args=['x'])[:-1]
