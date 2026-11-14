"""
Bearer token authentication of SCIM clients.

SCIM_TOKENS maps a client name to one token or a list of tokens (several during
a rotation), each given as "sha256:<hex digest of the token>" (recommended) or
as the token itself (at least 32 characters). Only digests are kept in memory
and compared, in constant time; the client name is recorded with everything the
client provisions ("scim:<client>").
"""

import functools
import hashlib
import hmac
import json
import re

from django.conf import settings

CLIENT_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,49}$')
DIGEST_RE = re.compile(r'^sha256:([0-9a-fA-F]{64})$')
MIN_TOKEN_LENGTH = 32


class TokenConfigError(ValueError):
  pass


def hash_token(token):
  return 'sha256:' + hashlib.sha256(token.encode('utf-8')).hexdigest()


@functools.lru_cache(maxsize=8)
def _parse(raw):
  try:
    data = json.loads(raw) if isinstance(raw, str) else raw
  except ValueError as e:
    raise TokenConfigError("SCIM_TOKENS is not valid JSON (%s)" % e)
  if not isinstance(data, dict):
    raise TokenConfigError('SCIM_TOKENS must be a JSON object such as {"idm": "sha256:<hex>"}')
  clients = {}
  for client, tokens in data.items():
    if not isinstance(client, str) or not CLIENT_NAME_RE.match(client):
      raise TokenConfigError("SCIM_TOKENS: invalid client name %r (letters, digits, '_', '.', '-'; at most 50)" % client)
    if isinstance(tokens, str):
      tokens = [tokens]
    if not isinstance(tokens, list) or not tokens:
      raise TokenConfigError("SCIM_TOKENS: client %s needs a token or a list of tokens" % client)
    digests = []
    for token in tokens:
      if not isinstance(token, str):
        raise TokenConfigError("SCIM_TOKENS: tokens of client %s must be strings" % client)
      m = DIGEST_RE.match(token.strip())
      if m:
        digests.append(bytes.fromhex(m.group(1)))
      elif len(token.strip()) >= MIN_TOKEN_LENGTH:
        digests.append(hashlib.sha256(token.strip().encode('utf-8')).digest())
      else:
        # never echo the token itself
        raise TokenConfigError("SCIM_TOKENS: a token of client %s is neither 'sha256:<64 hex digits>' nor "
                               "at least %d characters long" % (client, MIN_TOKEN_LENGTH))
    clients[client] = tuple(digests)
  return clients


def configured_clients():
  """{client: (sha256 digest, ...)}; raises TokenConfigError if misconfigured."""
  raw = getattr(settings, 'SCIM_TOKENS', '') or '{}'
  if isinstance(raw, dict):
    raw = json.dumps(raw, sort_keys=True)
  return _parse(raw)


def authenticate(request):
  """The name of the client whose token the request carries, or None."""
  scheme, _sep, token = request.META.get('HTTP_AUTHORIZATION', '').partition(' ')
  token = token.strip()
  if scheme.lower() != 'bearer' or not token:
    return None
  try:
    clients = configured_clients()
  except TokenConfigError:
    return None
  digest = hashlib.sha256(token.encode('utf-8')).digest()
  found = None
  # compare with every digest (no early exit)
  for client, digests in clients.items():
    for expected in digests:
      if hmac.compare_digest(expected, digest):
        found = client
  return found
