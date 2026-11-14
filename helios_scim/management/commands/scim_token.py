"""
Generate a token for a SCIM client and print the SCIM_TOKENS entry with its
SHA-256 digest. Give the token to the client (e.g. Keycloak or Entra ID) and put
only the digest into the configuration.
"""

import json
import secrets

from django.core.management.base import BaseCommand, CommandError

from helios_scim.auth import CLIENT_NAME_RE, hash_token


class Command(BaseCommand):
  help = "Generate a SCIM bearer token and the SCIM_TOKENS entry (digest) for it"

  def add_arguments(self, parser):
    parser.add_argument('client', help="client name, e.g. idm (letters, digits, '_', '.', '-')")

  def handle(self, client, **options):
    if not CLIENT_NAME_RE.match(client):
      raise CommandError("invalid client name %r" % client)
    token = secrets.token_urlsafe(32)
    self.stdout.write("Token for the client (shown only now, store it in the client's configuration):")
    self.stdout.write("  %s" % token)
    self.stdout.write("SCIM_TOKENS entry (add it to the JSON object; several tokens per client: a list):")
    self.stdout.write("  %s" % json.dumps({client: hash_token(token)}))
