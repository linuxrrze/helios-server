"""
read the bounce mailbox (HELIOS_BOUNCE_IMAP_*) and record the bounces and
complaints about Helios emails (helios.bounce_mailbox, helios.bounces); run it
every few minutes (deploy/systemd/helios-bounces.timer, or cron in a container)
"""

from django.core.management.base import BaseCommand, CommandError

from helios import bounce_mailbox


class Command(BaseCommand):
  help = 'record the bounces and complaints in the bounce mailbox (IMAP) and suppress addresses that bounce'

  def add_arguments(self, parser):
    parser.add_argument('--limit', type=int, default=1000, help='at most this many mails per run (default 1000)')

  def handle(self, *args, **options):
    if not bounce_mailbox.configured():
      raise CommandError('bounce handling is not configured: set HELIOS_BOUNCE_DOMAIN, HELIOS_BOUNCE_SECRET and '
                         'HELIOS_BOUNCE_IMAP_HOST (and HELIOS_BOUNCE_IMAP_USER, HELIOS_BOUNCE_IMAP_PASSWORD)')
    try:
      stats = bounce_mailbox.process(limit=options['limit'])
    except (OSError, bounce_mailbox.imaplib.IMAP4.error) as e:
      raise CommandError('bounce mailbox: %s: %s' % (type(e).__name__, e))
    self.stdout.write('%s; %d old mails deleted' % (
      ', '.join('%d %s' % (n, folder) for folder, n in stats.results.items()), stats.deleted))
