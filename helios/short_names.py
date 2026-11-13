"""
Election addresses: the short name in <URL_HOST>/helios/e/<short name>.

The short name is optional; without one an election uses its UUID. On a server
shared by several organizations, the name an administrator chooses is stored
with their organization's prefix ("<prefix>-<name>", see
ELECTION_SHORT_NAME_PREFIXES in settings.py), so that two organizations can both
use e.g. "board-2026". The prefix is a namespace only: nothing derives
permissions from it.
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


def prefix_for_user(user):
  """the prefix for the addresses of elections created by user ('' for none)"""
  mapping = settings.ELECTION_SHORT_NAME_PREFIXES
  if user and mapping:
    for domain in _domains(user):
      # the most specific entry wins, e.g. med.uni-a.example.org over uni-a.example.org
      matches = [key for key in mapping if domain == key or domain.endswith('.' + key)]
      if matches:
        return mapping[max(matches, key=len)]
  return settings.ELECTION_SHORT_NAME_PREFIX


def all_prefixes():
  return set(settings.ELECTION_SHORT_NAME_PREFIXES.values()) | ({settings.ELECTION_SHORT_NAME_PREFIX} - {''})


def with_prefix(prefix, name):
  return '%s-%s' % (prefix, name) if prefix else name


def reserved_prefix(name):
  """the configured prefix that name starts with (as "<prefix>-"), or None"""
  for prefix in all_prefixes():
    if name.startswith(prefix + '-'):
      return prefix
  return None


def is_taken(short_name, election=None):
  # deleted elections keep their short name (the column is unique)
  from helios.models import Election
  others = Election.objects_with_deleted.filter(short_name=short_name)
  if election is not None and election.pk:
    others = others.exclude(pk=election.pk)
  return others.exists()


def base_url():
  """the address of an election without its short name, e.g. https://helios.example.org/helios/e/"""
  return settings.URL_HOST + reverse(url_names.ELECTION_SHORTCUT, args=['x'])[:-1]
