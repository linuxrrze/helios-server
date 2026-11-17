"""
put background tasks back in the queue when their worker died mid-task,
leaving them RUNNING forever
"""

import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone
from django_tasks import TaskResultStatus
from django_tasks_db.models import DBTaskResult

STUCK_AFTER_MINUTES = 120

# re-running these would notify every voter again
NOT_REQUEUED = ['helios.tasks.voters_notify']


# re-running a bulk mailing adds and queues only its emails still missing (an
# OutgoingEmail per voter of its EmailBatch); not so tasks queued before batches
# existed (without batch_id), which would email every voter again
def _requeue_safe(task_result):
  if task_result.task_path in NOT_REQUEUED:
    return False
  if task_result.task_path == 'helios.tasks.voters_email':
    return bool((task_result.args_kwargs.get('kwargs') or {}).get('batch_id'))
  return True


class Command(BaseCommand):
  help = 'requeue background tasks that have been running for too long'

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
      return

    count = to_requeue.update(status=TaskResultStatus.READY, started_at=None)
    self.stdout.write('requeued %d tasks' % count)
