"""
assign elections to organizations (their corporate identity), e.g. the
elections that existed before ORGANIZATIONS_FILE was set up

  manage.py assign_organizations --dry-run              by the organization of their creator
  manage.py assign_organizations --by prefix --dry-run  by the prefix of their address
  manage.py assign_organizations --set unia <uuid or address> ...
  manage.py assign_organizations --set "" <uuid or address> ...   back to the default organization

Without --overwrite, only elections of the default organization are changed.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from helios import organizations
from helios.models import Election


class Command(BaseCommand):
  help = 'assign elections to organizations (see docs/organizations.md)'

  def add_arguments(self, parser):
    parser.add_argument('elections', nargs='*',
                        help='UUIDs or addresses (short names) of elections; default: all elections')
    parser.add_argument('--by', choices=['creator', 'prefix'], default='creator',
                        help='creator: the organization of the election\'s creator (identity provider, email '
                             'domain), as for new elections (default); prefix: the organization whose '
                             'short_name_prefix the election\'s address starts with')
    parser.add_argument('--set', dest='set_slug', metavar='SLUG',
                        help='assign the given elections to this organization ("" for the default)')
    parser.add_argument('--only', metavar='SLUG', help='assign this organization only, leave the others')
    parser.add_argument('--overwrite', action='store_true',
                        help='also change elections already assigned to an organization')
    parser.add_argument('--include-deleted', action='store_true', help='include deleted elections')
    parser.add_argument('--dry-run', action='store_true', help='only show what would change')

  def handle(self, *args, **options):
    configured = organizations.configured()
    if not configured:
      raise CommandError('No organizations are configured (ORGANIZATIONS_FILE).')
    set_slug = options['set_slug']
    for slug in (set_slug, options['only']):
      if slug and slug not in configured:
        raise CommandError('Unknown organization %r; configured: %s.' % (slug, ', '.join(configured)))
    if set_slug is not None and not options['elections']:
      raise CommandError('--set needs the elections to assign.')

    manager = Election.objects_with_deleted if options['include_deleted'] else Election.objects
    elections = manager.select_related('admin').order_by('created_at', 'pk')
    if options['elections']:
      elections = elections.filter(Q(uuid__in=options['elections']) | Q(short_name__in=options['elections']))
      found = {value for election in elections for value in (election.uuid, election.short_name)}
      missing = [value for value in options['elections'] if value not in found]
      if missing:
        raise CommandError('No such election: %s' % ', '.join(missing))
    if not (options['overwrite'] or set_slug is not None):
      elections = elections.filter(organization='')

    by_prefix = {organization.short_name_prefix: organization.slug
                 for organization in organizations.all_organizations() if organization.short_name_prefix}

    def target(election):
      if set_slug is not None:
        return set_slug
      if options['by'] == 'prefix':
        address = election.short_name.lower()
        return next((slug for prefix, slug in by_prefix.items() if address.startswith(prefix + '-')), '')
      return organizations.organization_for_user(election.admin)

    changed = 0
    for election in elections:
      slug = target(election)
      if slug == election.organization or (options['only'] and slug != options['only']):
        continue
      # without a match, a run by creator or prefix leaves the election as it is
      if not slug and set_slug is None:
        continue
      self.stdout.write('%s %s (%s): %s -> %s' % ('would assign' if options['dry_run'] else 'assigned',
                                                  election.short_name, election.uuid,
                                                  election.organization or '(default)', slug or '(default)'))
      if not options['dry_run']:
        Election.objects_with_deleted.filter(pk=election.pk).update(organization=slug)
      changed += 1

    self.stdout.write('%s %d election(s)' % ('would change' if options['dry_run'] else 'changed', changed))
