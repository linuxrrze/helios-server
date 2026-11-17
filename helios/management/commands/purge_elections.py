"""
delete elections for good once their retention period (HELIOS_ELECTION_RETENTION_DAYS)
has passed, see helios.retention; run it daily (deploy/systemd/helios-purge.timer,
or cron in a container). Without --yes, only lists what would be deleted.
"""

from django.core.management.base import BaseCommand, CommandError

from helios import retention


class Command(BaseCommand):
  help = ('delete elections (also soft-deleted ones) for good whose retention period '
          '(HELIOS_ELECTION_RETENTION_DAYS) has passed; a dry run unless --yes is given')

  def add_arguments(self, parser):
    parser.add_argument('--yes', action='store_true', help='really delete them (default: only list them)')
    parser.add_argument('--days', type=int, default=None,
                        help='retention period in days for this run, instead of HELIOS_ELECTION_RETENTION_DAYS')

  def handle(self, *args, **options):
    days = options['days']
    if days is None:
      days = retention.retention_days()
      if days <= 0:
        raise CommandError('election retention is off (HELIOS_ELECTION_RETENTION_DAYS=0): nothing is deleted. '
                           'Set HELIOS_ELECTION_RETENTION_DAYS, or give --days N for a one-off run.')
    elif days <= 0:
      raise CommandError('--days must be at least 1')

    found = retention.candidates(days)
    really = options['yes']
    deleted = 0
    for election, reference in found:
      numbers = retention.counts(election)
      line = '%s  %s  reference date %s  %d voters  %d ballots' % (
        election.uuid, election.short_name, reference.isoformat(sep=' ', timespec='seconds'),
        numbers['voters'], numbers['ballots'])
      if really:
        if retention.purge(election, days=days) is None:
          line += '  (changed in the meantime, kept)'
        else:
          deleted += 1
      self.stdout.write(line)

    if really:
      self.stdout.write('%d election(s) deleted (retention period %d days)' % (deleted, days))
    else:
      self.stdout.write('%d election(s) would be deleted (retention period %d days); dry run, '
                        'run with --yes to delete them' % (len(found), days))
