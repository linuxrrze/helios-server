"""
end a pause of sending emails (helios.mail) now, e.g. after fixing the mail
server settings, and send the deferred emails right away
"""

from django.core.management.base import BaseCommand

from helios import mail
from helios.models import EmailSendingPause, OutgoingEmail


class Command(BaseCommand):
  help = 'end a pause of sending emails and send the deferred emails now'

  def add_arguments(self, parser):
    parser.add_argument('--dry-run', action='store_true', help='only show the pause and the deferred emails')

  def handle(self, *args, **options):
    pause = EmailSendingPause.objects.filter(pk=1).first()
    if pause and pause.failures:
      self.stdout.write('sending paused until %s after %d failures since %s: %s' % (
        pause.paused_until, pause.failures, pause.since, pause.error_text))
    else:
      self.stdout.write('sending not paused')
    deferred = OutgoingEmail.objects.filter(status=OutgoingEmail.DEFERRED).count()
    if options['dry_run']:
      self.stdout.write('%d emails deferred' % deferred)
      return
    count = mail.resume_sending()
    self.stdout.write('pause ended, %d deferred emails queued' % count)
