"""
Apply all SCIM groups and users again: roles, voter groups and their
administrators. Needed after changing SCIM_ADMIN_GROUP,
SCIM_ELECTION_CREATOR_GROUP, SCIM_VOTERGROUP_ADMINS_GROUP or
SCIM_VOTERGROUP_ADMINS_SUFFIX, because groups are otherwise applied only when
a client changes them.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from helios_scim import mapping


class Command(BaseCommand):
  help = "Apply the SCIM groups and users again (after changing the SCIM group settings)"

  def add_arguments(self, parser):
    parser.add_argument('--client', default=None, help="only this client")

  def handle(self, client=None, **options):
    with transaction.atomic():
      mapping.resync(client)
    self.stdout.write("done")
