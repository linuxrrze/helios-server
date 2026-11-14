"""
Election addresses: the short name in <URL_HOST>/helios/e/<short name>.

The short name is optional; without one an election uses its UUID. On a server
shared by several organizations, the name an administrator chooses is stored
with their organization's prefix ("<prefix>-<name>"), so that two
organizations can both use e.g. "board-2026". The prefix comes from the
short_name_prefix of the organizations in ORGANIZATIONS_FILE and from
ELECTION_SHORT_NAME_PREFIXES_BY_IDP, ELECTION_SHORT_NAME_PREFIXES and
ELECTION_SHORT_NAME_PREFIX (helios/organizations.py). It is a namespace only:
nothing derives permissions from it.

Addresses are unique regardless of case ("Board" is taken if "board" exists),
but stored as entered.
"""

from django.conf import settings
from django.urls import reverse

from helios import organizations, url_names


def prefix_for_user(user):
  """
  the prefix for the addresses of elections created by user ('' for none): by
  identity provider, else by email domain, else the default prefix; from the
  same mapping as the user's organization (organizations.match_user)
  """
  return organizations.match_user(user).prefix


def all_prefixes():
  return organizations.all_prefixes()


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
