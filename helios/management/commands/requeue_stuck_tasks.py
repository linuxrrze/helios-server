"""
put background tasks back in the queue when their worker died mid-task,
leaving them RUNNING forever; and queue again emails whose task is gone
"""

import datetime

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from django_tasks import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from helios import mail
from helios.models import OutgoingEmail

STUCK_AFTER_MINUTES = 120

# re-running these would notify every voter again
NOT_REQUEUED = ['helios.tasks.voters_notify']

SEND_TASKS = ['helios.tasks.send_outgoing_email', 'helios.tasks.single_voter_email']


# re-running a bulk mailing adds and queues only its emails still missing (an
# OutgoingEmail per voter of its EmailBatch); not so tasks queued before batches
# existed (without batch_id), which would email every voter again
def _requeue_safe(task_result):
  if task_result.task_path in NOT_REQUEUED:
    return False
  if task_result.task_path == 'helios.tasks.voters_email':
    return bool((task_result.args_kwargs.get('kwargs') or {}).get('batch_id'))
  return True


def _email_id(task_result):
  """the OutgoingEmail a send task is for, or None"""
  args = task_result.args_kwargs.get('args') or []
  kwargs = task_result.args_kwargs.get('kwargs') or {}
  if task_result.task_path == 'helios.tasks.send_outgoing_email':
    return args[0] if args else kwargs.get('outgoing_email_id')
  if len(args) >= 6:
    return args[5]
  return kwargs.get('outgoing_email_id')


def lost_emails(cutoff):
  """
  Emails still to be sent whose task is gone (e.g. pruned, failed, or lost with
  a crash): queued, or due (deferred, or sending that died), since before CUTOFF,
  without a waiting or running task.
  """
  pending = DBTaskResult.objects.filter(task_path__in=SEND_TASKS,
                                        status__in=[TaskResultStatus.READY, TaskResultStatus.RUNNING])
  pending_ids = {_email_id(task_result) for task_result in pending}
  due = OutgoingEmail.objects.filter(
    Q(status=OutgoingEmail.QUEUED, created_at__lt=cutoff)
    | Q(status__in=[OutgoingEmail.SENDING, OutgoingEmail.DEFERRED], next_attempt_at__lt=cutoff)
    | Q(status__in=[OutgoingEmail.SENDING, OutgoingEmail.DEFERRED], next_attempt_at__isnull=True, created_at__lt=cutoff))
  return [(email_id, kind) for email_id, kind in due.values_list('id', 'kind') if email_id not in pending_ids]


class Command(BaseCommand):
  help = 'requeue background tasks that have been running for too long, and emails whose task is gone'

  def add_arguments(self, parser):
    parser.add_argument('--older-than', type=int, default=STUCK_AFTER_MINUTES,
                        help='minutes a task must have been running; longer than the longest tally (default %d)' % STUCK_AFTER_MINUTES)
    parser.add_argument('--dry-run', action='store_true')

  def handle(self, *args, **options):
    cutoff = timezone.now() - datetime.timedelta(minutes=options['older_than'])
    stuck = DBTaskResult.objects.running().filter(started_at__lt=cutoff)

    not_requeued = [task_result for task_result in stuck if not _requeue_safe(task_result)]
    for task_result in not_requeued:
      self.stdout.write('not requeued: %s %s' % (task_result.id, task_result.task_path))

    to_requeue = stuck.exclude(id__in=[task_result.id for task_result in not_requeued])
    if options['dry_run']:
      self.stdout.write('would requeue %d tasks' % to_requeue.count())
      self.stdout.write('would requeue %d emails' % len(lost_emails(cutoff)))
      return

    count = to_requeue.update(status=TaskResultStatus.READY, started_at=None)
    self.stdout.write('requeued %d tasks' % count)

    # after the tasks above: an email whose task is running (or now waiting again) is not lost
    lost = lost_emails(cutoff)
    for email_id, kind in lost:
      mail.enqueue(email_id, kind)
    self.stdout.write('requeued %d emails' % len(lost))
