"""
Deleting elections for good after a retention period (HELIOS_ELECTION_RETENTION_DAYS,
0: never), with `manage.py purge_elections`.

An election is purged RETENTION_DAYS after its reference date (reference_date):
the latest of when voting ended, the result was released, it was archived or
(soft-)deleted; for an election that was never opened (not frozen), its last
change. An election whose voting is still open, or whose tally has started but
has no result yet, has no reference date and is never purged.

Purging deletes the election with everything that belongs to it (purge); the
global list of suppressed addresses (EmailSuppression) and the user accounts
stay. Each purge is logged to "helios.retention" with the election's UUID and
counts only.
"""

import datetime
import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Max

from .models import AuditedBallot, BounceEvent, CastVote, Election, EmailBatch, OutgoingEmail, Voter


logger = logging.getLogger('helios.retention')


def retention_days():
  """the configured retention period in days, 0: off"""
  return max(int(getattr(settings, 'HELIOS_ELECTION_RETENTION_DAYS', 0) or 0), 0)


def never_purged_reason(election):
  """why ELECTION is not purged in its current state, or None"""
  if election.frozen_at and not election.voting_has_stopped():
    return 'voting open'
  if (election.tallying_started_at or election.encrypted_tally) and not election.has_complete_result:
    return 'tally in progress'
  return None


def reference_date(election):
  """
  The date from which ELECTION's retention period runs (naive UTC, like the
  model's fields), or None while it is never purged (never_purged_reason).
  """
  if never_purged_reason(election):
    return None

  dates = [election.created_at, election.archived_at, election.deleted_at, election.result_released_at,
           election.tallying_finished_at]
  if election.frozen_at:
    # the end of voting, as voting_has_stopped sees it (stopped, so in the past)
    dates += [election.frozen_at, election.voting_ended_at or election.voting_extended_until or election.voting_ends_at]
  else:
    # never opened: its last change (modified_at is set by each save; logs and
    # voter files are added without saving the election)
    dates += [election.modified_at,
              election.electionlog_set.aggregate(last=Max('at'))['last'],
              election.voterfile_set.aggregate(last=Max('uploaded_at'))['last']]
  return max(date for date in dates if date is not None)


def purge_date(election, days=None):
  """when ELECTION will be (or would have been) purged, or None (off, or never in its current state)"""
  days = retention_days() if days is None else days
  if days <= 0:
    return None
  reference = reference_date(election)
  return reference + datetime.timedelta(days=days) if reference else None


def candidates(days, now=None):
  """
  The elections (also soft-deleted ones) whose retention period of DAYS has
  passed, as (election, reference date), oldest reference date first.
  """
  now = now or datetime.datetime.utcnow()
  cutoff = now - datetime.timedelta(days=days)
  # the reference date is never before created_at
  found = []
  for election in Election.objects_with_deleted.filter(created_at__lt=cutoff).order_by('created_at'):
    reference = reference_date(election)
    if reference is not None and reference < cutoff:
      found.append((election, reference))
  found.sort(key=lambda item: item[1])
  return found


def counts(election):
  """how many rows of each kind belong to ELECTION (no personal data)"""
  return {
    'voters': Voter.objects.filter(election=election).count(),
    'ballots': CastVote.objects.filter(voter__election=election).count(),
    'audited_ballots': AuditedBallot.objects.filter(election=election).count(),
    'trustees': election.trustee_set.count(),
    'log_entries': election.electionlog_set.count(),
    'voter_files': election.voterfile_set.count(),
    'email_batches': EmailBatch.objects.filter(election=election).count(),
    'emails': OutgoingEmail.objects.filter(election=election).count(),
    'bounces': BounceEvent.objects.filter(outgoing_email__election=election).count(),
  }


def purge(election, days=None, now=None):
  """
  Delete ELECTION for good, with everything that belongs to it: voters and their
  ballots, audited ballots, trustees (with Helios' secret key), the log, voter
  files (also files of old uploads in the storage), mailings, emails and their
  bounces. Suppressed addresses stay (lifted_in becomes empty). Never while
  voting is open or a tally is in progress; with DAYS, only if its retention
  period of DAYS has (still) passed. Returns the counts (see counts), or None
  if it was not deleted.
  """
  with transaction.atomic():
    # lock it and look again: it may have changed since it was found
    election = Election.objects_with_deleted.select_for_update().filter(pk=election.pk).first()
    if election is None:
      return None
    reference = reference_date(election)
    # never while voting is open or a tally is in progress
    if reference is None:
      return None
    if days is not None and reference >= (now or datetime.datetime.utcnow()) - datetime.timedelta(days=days):
      return None
    numbers = counts(election)
    # old uploads were stored as files; newer ones only in the database
    files = [(voter_file.voter_file.storage, voter_file.voter_file.name)
             for voter_file in election.voterfile_set.all() if voter_file.voter_file]
    uuid = election.uuid
    election.delete()
    transaction.on_commit(lambda: _delete_files(uuid, files))

  logger.info("election %s purged (reference date %s): %s", uuid,
              reference.isoformat(timespec='seconds'),
              ', '.join('%d %s' % (n, kind) for kind, n in numbers.items()))
  return numbers


def _delete_files(uuid, files):
  for storage, name in files:
    try:
      storage.delete(name)
    except Exception as e:
      logger.warning("election %s purged, but a voter file could not be deleted from the storage: %s",
                     uuid, type(e).__name__)
