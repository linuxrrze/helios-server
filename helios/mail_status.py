"""
What became of an election's emails to its voters, for the "email delivery"
page of its administrators (helios.views.election_emails): counts per mailing
(EmailBatch) and overall, the voters not reached with a readable reason, and
sending a mailing again to them (a new EmailBatch, resend_of the first one).

A mailing and the mailings that resent it form a chain: a voter counts as not
reached by it if their latest email of the chain failed, bounced or was not
sent because their address is suppressed. Other emails to voters (vote-cast
confirmations, passwords resent one by one) are listed by kind.
"""
from collections import OrderedDict
from email.utils import parseaddr

from django.db import transaction
from django.db.models import Count
from django.utils.translation import gettext as _, gettext_lazy, pgettext_lazy

from . import mail
from .mail_errors import enhanced_code
from .models import EmailBatch, EmailSuppression, OutgoingEmail, Voter

# emails to voters (not to administrators, trustees, ...)
VOTER_KINDS = (OutgoingEmail.KIND_BULK, OutgoingEmail.KIND_VOTER_PASSWORD, OutgoingEmail.KIND_VOTE_CAST)
# not reached: shown in the list, sent again by a resend
PROBLEM_STATUSES = (OutgoingEmail.FAILED, OutgoingEmail.BOUNCED_SOFT, OutgoingEmail.BOUNCED_HARD,
                    OutgoingEmail.SKIPPED_SUPPRESSED)

# the columns of the counts
GROUPS = OrderedDict([
  ('sent', (OutgoingEmail.SENT,)),
  ('waiting', (OutgoingEmail.QUEUED, OutgoingEmail.SENDING)),
  ('retrying', (OutgoingEmail.DEFERRED,)),
  ('failed', (OutgoingEmail.FAILED,)),
  ('bounced', (OutgoingEmail.BOUNCED_SOFT, OutgoingEmail.BOUNCED_HARD)),
  ('skipped', (OutgoingEmail.SKIPPED_OPTOUT, OutgoingEmail.SKIPPED_SUPPRESSED, OutgoingEmail.SKIPPED_NOADDRESS,
               OutgoingEmail.SKIPPED_OBSOLETE)),
])

STATUS_LABELS = {
  OutgoingEmail.QUEUED: gettext_lazy("waiting"),
  OutgoingEmail.SENDING: gettext_lazy("being sent"),
  OutgoingEmail.SENT: pgettext_lazy("email", "sent"),
  OutgoingEmail.DEFERRED: gettext_lazy("retrying"),
  OutgoingEmail.FAILED: pgettext_lazy("email", "failed"),
  OutgoingEmail.BOUNCED_SOFT: gettext_lazy("bounced (temporary)"),
  OutgoingEmail.BOUNCED_HARD: gettext_lazy("bounced (permanent)"),
  OutgoingEmail.SKIPPED_OPTOUT: gettext_lazy("not sent: opted out"),
  OutgoingEmail.SKIPPED_SUPPRESSED: gettext_lazy("not sent: address suppressed"),
  OutgoingEmail.SKIPPED_NOADDRESS: gettext_lazy("not sent: no email address"),
  OutgoingEmail.SKIPPED_OBSOLETE: gettext_lazy("not sent: no longer applicable"),
}
# for the badge: status-ok, status-warn or status-failed (layout.css)
STATUS_TONES = {
  OutgoingEmail.SENT: 'ok', OutgoingEmail.DEFERRED: 'warn', OutgoingEmail.BOUNCED_SOFT: 'warn',
  OutgoingEmail.FAILED: 'failed', OutgoingEmail.BOUNCED_HARD: 'failed', OutgoingEmail.SKIPPED_SUPPRESSED: 'failed',
}

SKIPPED_LABELS = OrderedDict([
  (OutgoingEmail.SKIPPED_OPTOUT, gettext_lazy("opted out")),
  (OutgoingEmail.SKIPPED_SUPPRESSED, gettext_lazy("address suppressed")),
  (OutgoingEmail.SKIPPED_NOADDRESS, gettext_lazy("no email address")),
  (OutgoingEmail.SKIPPED_OBSOLETE, gettext_lazy("no longer applicable")),
])

KIND_LABELS = {
  OutgoingEmail.KIND_BULK: gettext_lazy("mailing"),
  OutgoingEmail.KIND_VOTER_PASSWORD: gettext_lazy("password resent"),
  OutgoingEmail.KIND_VOTE_CAST: gettext_lazy("vote-cast confirmation"),
}

# the templates of the "email voters" page (helios.views.voters_email)
TEMPLATE_LABELS = {
  'vote': gettext_lazy('Time to Vote'),
  'simple': gettext_lazy('Simple'),
  'info': gettext_lazy('Additional Info'),
  'result': gettext_lazy('Election Result'),
}

# RFC 3463 enhanced status codes, in words
CODE_REASONS = {
  '5.1.0': gettext_lazy("address rejected"),
  '5.1.1': gettext_lazy("recipient unknown"),
  '5.1.10': gettext_lazy("recipient unknown"),
  '5.1.2': gettext_lazy("domain does not exist or does not receive email"),
  '5.1.3': gettext_lazy("invalid address"),
  '5.1.6': gettext_lazy("mailbox has moved"),
  '5.2.1': gettext_lazy("mailbox disabled"),
  '5.2.2': gettext_lazy("mailbox full"),
  '4.2.2': gettext_lazy("mailbox full"),
  '5.2.3': gettext_lazy("message too large"),
  '5.3.0': gettext_lazy("recipient's mail server failed"),
  '5.4.4': gettext_lazy("recipient's mail server cannot be found"),
  '4.4.1': gettext_lazy("recipient's mail server not reachable"),
  '4.4.2': gettext_lazy("recipient's mail server not reachable"),
  '4.4.7': gettext_lazy("could not be delivered in time, given up"),
  '5.4.7': gettext_lazy("could not be delivered in time, given up"),
  '5.7.1': gettext_lazy("refused by the recipient's mail server (policy or spam filter)"),
  '4.7.1': gettext_lazy("refused for now by the recipient's mail server (greylisting or policy)"),
}

SUPPRESSION_REASONS = {
  EmailSuppression.SOURCE_HARD_BOUNCE: gettext_lazy("address suppressed after a permanent bounce"),
  EmailSuppression.SOURCE_SOFT_BOUNCES: gettext_lazy("address suppressed after repeated temporary bounces"),
  EmailSuppression.SOURCE_COMPLAINT: gettext_lazy("address suppressed: the recipient marked an email as spam"),
  EmailSuppression.SOURCE_MANUAL: gettext_lazy("address suppressed by a site administrator"),
}


def _enhanced_of(code):
  """'550 5.1.1' or '5.1.1' -> '5.1.1'; '550' -> ''"""
  for part in (code or '').split():
    enhanced = enhanced_code(part)
    if enhanced:
      return enhanced
  return ''


def code_reason(code):
  """'5.1.1' (or '550 5.1.1', '550') -> 'recipient unknown' etc., or ''"""
  code = (code or '').strip()
  enhanced = _enhanced_of(code)
  if enhanced in CODE_REASONS:
    return str(CODE_REASONS[enhanced])
  first = enhanced[:1] if enhanced else (code[:1] if code[:3].isdigit() else '')
  if first == '5':
    return _("permanent delivery failure")
  if first == '4':
    return _("temporary delivery problem")
  return ''


def reason(email):
  """why EMAIL (with status in PROBLEM_STATUSES or another) did not reach the voter, in words"""
  code = email.last_error_code or ''
  if email.status == OutgoingEmail.SKIPPED_SUPPRESSED:
    text = SUPPRESSION_REASONS.get(code)
    return str(text) if text else _("address suppressed")
  if email.status == OutgoingEmail.SKIPPED_OPTOUT:
    return _("the voter opted out of Helios emails")
  if code == 'max_age':
    return _("given up: could not be sent for too long")
  if code == 'render_error':
    return _("the email could not be created (an error in Helios)")
  words = code_reason(code)
  if words:
    return '%s %s' % (_enhanced_of(code) or code, words)
  if email.status in (OutgoingEmail.BOUNCED_SOFT, OutgoingEmail.BOUNCED_HARD):
    return _("bounced, reason unknown")
  if email.status == OutgoingEmail.FAILED:
    return _("the mail server did not accept the email") + (' (%s)' % code if code else '')
  return ''


def counts(emails):
  """{group: n} for the emails (a queryset), with 'total' and 'skipped_by' {status: n}"""
  by_status = dict(emails.values_list('status').annotate(n=Count('id')).values_list('status', 'n'))
  result = {group: sum(by_status.get(s, 0) for s in statuses) for group, statuses in GROUPS.items()}
  result['total'] = sum(by_status.values())
  result['skipped_by'] = [(str(label), by_status[s]) for s, label in SKIPPED_LABELS.items() if by_status.get(s)]
  return result


def root(batch):
  return batch.resend_of if batch.resend_of_id else batch


def batch_label(batch):
  """'Time to Vote: "Board election"'"""
  name = (batch.body_template or '').rsplit('/', 1)[-1].replace('_body.txt', '')
  label = str(TEMPLATE_LABELS.get(name, name))
  subject = (batch.extra_vars or {}).get('custom_subject')
  return '%s: "%s"' % (label, subject) if subject else label


def _latest_problems(election):
  """
  {voter_id: [email, ...]}: per voter, the emails not followed by another one of
  the same chain (mailing and its resends) or of the same kind, that did not reach them
  """
  emails = (OutgoingEmail.objects.filter(election=election, kind__in=VOTER_KINDS, voter__isnull=False)
            .select_related('batch').order_by('id'))
  latest = {}
  for email in emails:
    if email.batch_id:
      key = (email.voter_id, 'batch', email.batch.resend_of_id or email.batch_id)
    else:
      key = (email.voter_id, 'kind', email.kind)
    latest[key] = email
  problems = {}
  for (voter_id, _what, _which), email in latest.items():
    if email.status in PROBLEM_STATUSES:
      problems.setdefault(voter_id, []).append(email)
  return problems


def current_address(voter):
  """the bare address emails to VOTER go to now (their auth system's, or the voter list's)"""
  return parseaddr(mail.recipient_of(voter.get_user()))[1] or (voter.voter_email or '')


class Problem:
  """a voter not reached by an email (a row of the list)"""

  def __init__(self, voter, email, suppression):
    self.voter, self.email, self.suppression = voter, email, suppression
    self.reason = reason(email)
    self.status_label = str(STATUS_LABELS.get(email.status, email.status))
    self.tone = STATUS_TONES.get(email.status, 'warn')
    if email.batch_id:
      self.what = batch_label(root(email.batch))
    else:
      self.what = str(KIND_LABELS.get(email.kind, email.kind))
    self.at = email.sent_at or email.created_at


def problems(election):
  """the voters not reached, sorted by voter: a list of Problem"""
  latest = _latest_problems(election)
  voters = {v.id: v for v in Voter.objects.filter(id__in=latest).select_related('user').defer('vote')}
  rows = []
  for voter_id, emails in latest.items():
    voter = voters.get(voter_id)
    if voter is None:
      continue
    suppression = EmailSuppression.for_address(current_address(voter))
    for email in emails:
      rows.append(Problem(voter, email, suppression))
  rows.sort(key=lambda p: ((p.voter.voter_name or p.voter.voter_login_id or '').lower(), p.voter.id, -p.email.id))
  return rows


def _split_suppressed(voter_ids, election):
  """(voter ids whose address is not suppressed, those whose address is)"""
  voters = Voter.objects.filter(id__in=voter_ids, election=election).select_related('user').defer('vote')
  sendable, suppressed = [], []
  for voter in voters:
    (suppressed if EmailSuppression.is_suppressed(current_address(voter)) else sendable).append(voter.id)
  return sorted(sendable), sorted(suppressed)


def batches(election):
  """
  the mailings of the election, newest first: (batch, counts, voter ids to send
  it again to, voter ids not reached whose address is suppressed); the voters only
  for first mailings (a resend is sent again from its first mailing)
  """
  not_reached = {}
  for voter_id, emails in _latest_problems(election).items():
    for email in emails:
      if email.batch_id:
        not_reached.setdefault(email.batch.resend_of_id or email.batch_id, set()).add(voter_id)
  result = []
  for batch in EmailBatch.objects.filter(election=election).select_related('resend_of').order_by('-created_at', '-id'):
    voter_ids = not_reached.get(batch.id) if not batch.resend_of_id else None
    sendable, suppressed = _split_suppressed(voter_ids, election) if voter_ids else ([], [])
    result.append((batch, counts(batch.emails.all()), sendable, suppressed))
  return result


def overall(election):
  return counts(OutgoingEmail.objects.filter(election=election, kind__in=VOTER_KINDS))


def resend_voters(batch):
  """the voters of BATCH (a first mailing) not reached by it or its resends: (not suppressed, suppressed)"""
  voter_ids = [voter_id for voter_id, emails in _latest_problems(batch.election).items()
               if any(e.batch_id and (e.batch.resend_of_id or e.batch_id) == batch.id for e in emails)]
  return _split_suppressed(voter_ids, batch.election)


def resend(batch, user):
  """
  Send the mailing BATCH again to its voters not reached (see resend_voters): a
  new batch, resend_of BATCH, limited to them (and still to the voters the
  first mailing was for: a reminder skips those who have voted since). Returns
  it, or None if there is nobody to send it to.
  """
  from . import tasks
  voter_ids, _suppressed = resend_voters(batch)
  if not voter_ids:
    return None
  constraints = batch.voter_constraints or {}
  include = dict(constraints.get('include') or {})
  include['id__in'] = voter_ids
  exclude = constraints.get('exclude') or None
  with transaction.atomic():
    new = mail.create_batch(batch.election, batch.subject_template, batch.body_template, batch.extra_vars,
                            language=batch.language, triggered_by=user, voter_constraints=(include, exclude))
    new.resend_of = batch
    new.save(update_fields=['resend_of'])
    tasks.voters_email.enqueue(election_id=batch.election_id, batch_id=new.id, voter_constraints_include=include,
                               voter_constraints_exclude=exclude, language=batch.language)
    mail.schedule_batch_summary(new)
  return new
