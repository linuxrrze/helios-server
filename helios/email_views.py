"""
The "email delivery" page of an election, for its administrators: what became
of the emails to its voters (per mailing and overall), the voters not reached
and why, correcting a voter's address, lifting a suppression and sending a
mailing again to the voters it did not reach (helios.mail_status). And, for
site administrators, the global suppression list (stats pages).
"""
import csv
import datetime
import logging
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext as _, gettext_lazy
from validate_email import validate_email

from helios_auth.security import check_csrf, get_user
from . import VOTERS_EMAIL, mail, mail_status, url_names
from .mail_backend import mask_address
from .models import EmailBatch, EmailOptOut, EmailSuppression, OutgoingEmail, Voter
from .security import election_admin
from .stats_views import require_admin
from .view_utils import render_template

logger = logging.getLogger(__name__)
# who suppressed or lifted which address (masked), also of site administrators
audit = logging.getLogger('helios.suppression')


def _actor(user):
  return '%s (%s)' % (user.user_id, user.user_type)


def _emails_url(election, **params):
  url = settings.SECURE_URL_HOST + reverse(url_names.election.ELECTION_EMAILS, args=[election.uuid])
  return url + ('?' + urlencode(params) if params else '')


def _voter_url(election, voter, **params):
  url = settings.SECURE_URL_HOST + reverse(url_names.election.ELECTION_EMAILS_VOTER, args=[election.uuid, voter.uuid])
  return url + ('?' + urlencode(params) if params else '')


def _can_resend(election, batch):
  """(True, None) or (False, why not) for sending BATCH again"""
  if not VOTERS_EMAIL:
    return False, _("Emails to voters are turned off on this site.")
  if not election.frozen_at:
    return False, _("The election is not frozen yet.")
  can_send, reason = election.can_send_voter_emails()
  if not can_send:
    return False, reason
  if batch.body_template in mail.VOTING_TEMPLATES:
    if election.voting_has_stopped():
      return False, _("Voting has closed: an invitation to vote is no longer sent.")
  return True, None


@election_admin()
def election_emails(request, election):
  batches = []
  for batch, counts, sendable, suppressed in mail_status.batches(election):
    can_resend, why_not = _can_resend(election, batch) if not batch.resend_of_id else (False, None)
    batches.append({'batch': batch, 'label': mail_status.batch_label(batch), 'counts': counts,
                    'sendable': len(sendable), 'suppressed': len(suppressed),
                    'can_resend': can_resend, 'why_not': why_not})
  can_modify, modify_reason = election.can_modify_voters()
  return render_template(request, 'election_emails', {
    'election': election, 'batches': batches, 'overall': mail_status.overall(election),
    'problems': mail_status.problems(election), 'can_modify_voters': can_modify,
    'modify_voters_reason': modify_reason, 'bounces_enabled': _bounces_enabled(),
    'done': request.GET.get('done', ''), 'count': _int(request.GET.get('n'))})


def _int(value):
  try:
    return max(int(value), 0)
  except (TypeError, ValueError):
    return 0


def _bounces_enabled():
  from . import verp
  return verp.enabled()


@election_admin()
def election_emails_csv(request, election):
  response = HttpResponse(content_type='text/csv; charset=utf-8')
  response['Content-Disposition'] = 'attachment; filename="emails-not-reached-%s.csv"' % election.short_name
  # Excel recognizes UTF-8 by the byte order mark
  response.write('﻿')
  writer = csv.writer(response)
  writer.writerow([_("Voter ID"), _("Name"), _("Email Address"), _("Email"), _("Date (UTC)"), _("Status"),
                   _("Code"), _("Reason"), _("Address suppressed")])
  for problem in mail_status.problems(election):
    voter, email = problem.voter, problem.email
    writer.writerow([voter.voter_login_id or '', voter.voter_name or '', voter.voter_email or '', problem.what,
                     problem.at.strftime('%Y-%m-%d %H:%M') if problem.at else '', problem.status_label,
                     email.last_error_code, problem.reason, _("yes") if problem.suppression else _("no")])
  return response


@election_admin()
def election_emails_resend(request, election):
  if request.method != 'POST':
    return HttpResponseRedirect(_emails_url(election))
  check_csrf(request)
  batch = EmailBatch.objects.filter(election=election, id=request.POST.get('batch_id') or 0,
                                    resend_of__isnull=True).first()
  if batch is None:
    raise Http404
  can_resend, _why_not = _can_resend(election, batch)
  if not can_resend:
    raise PermissionDenied()
  new = mail_status.resend(batch, get_user(request))
  count = len(new.voter_constraints['include']['id__in']) if new else 0
  return HttpResponseRedirect(_emails_url(election, done='resent', n=count))


class VoterAddressForm(forms.Form):
  email = forms.CharField(label=gettext_lazy("Email address"), max_length=250)

  def clean_email(self):
    address = self.cleaned_data['email'].strip()
    if not validate_email(address):
      raise forms.ValidationError(_("Please enter a valid email address."))
    if EmailOptOut.is_opted_out(address):
      raise forms.ValidationError(_("This address has opted out of Helios emails; only its owner can opt back in."))
    return address


def _voter(election, voter_uuid):
  voter = Voter.get_by_election_and_uuid(election, voter_uuid)
  if voter is None:
    raise Http404
  return voter


@election_admin()
def election_email_voter(request, election, voter_uuid):
  """a voter's emails, correcting their address (voters with a password from Helios), lifting a suppression"""
  voter = _voter(election, voter_uuid)
  can_modify, modify_reason = election.can_modify_voters()
  # the address of voters with an account comes from their login system, not from the voter list
  editable = voter.user_id is None
  address = mail_status.current_address(voter)
  form = None
  if editable and can_modify:
    if request.method == 'POST':
      check_csrf(request)
      form = VoterAddressForm(request.POST)
      if form.is_valid():
        new = form.cleaned_data['email']
        user = get_user(request)
        with transaction.atomic():
          old = voter.voter_email
          voter.voter_email = new
          voter.save(update_fields=['voter_email'])
          # the administrator vouches for the (corrected) address
          lifted = EmailSuppression.lift(new, user=user, election=election)
          election.append_log("Email address of voter %s changed from %s to %s by %s%s" % (
            voter.uuid, mask_address(old or ''), mask_address(new), _actor(user),
            '; suppression of the new address lifted' if lifted else ''))
        audit.info("election %s: address of voter %s changed from %s to %s by %s%s", election.uuid, voter.uuid,
                   mask_address(old or ''), mask_address(new), _actor(user), ' (suppression lifted)' if lifted else '')
        return HttpResponseRedirect(_voter_url(election, voter, done='changed'))
    else:
      form = VoterAddressForm(initial={'email': voter.voter_email})
  elif request.method == 'POST':
    raise PermissionDenied()

  emails = list(OutgoingEmail.objects.filter(election=election, voter=voter, kind__in=mail_status.VOTER_KINDS)
                .select_related('batch', 'batch__resend_of').order_by('-id')[:50])
  rows = []
  for email in emails:
    if email.batch_id:
      what = mail_status.batch_label(mail_status.root(email.batch))
    else:
      what = str(mail_status.KIND_LABELS.get(email.kind, email.kind))
    rows.append({'email': email, 'what': what, 'at': email.sent_at or email.created_at,
                 'status': str(mail_status.STATUS_LABELS.get(email.status, email.status)),
                 'tone': mail_status.STATUS_TONES.get(email.status, ''),
                 'reason': mail_status.reason(email) if email.status not in (OutgoingEmail.SENT,) else ''})
  suppression = EmailSuppression.for_address(address) if address else None
  return render_template(request, 'election_email_voter', {
    'election': election, 'voter': voter, 'address': address, 'form': form, 'editable': editable,
    'can_modify_voters': can_modify, 'modify_voters_reason': modify_reason, 'rows': rows,
    'suppression': suppression,
    'suppression_reason': str(mail_status.SUPPRESSION_REASONS[suppression.source]) if suppression else '',
    'done': request.GET.get('done', '')})


@election_admin()
def election_email_voter_lift(request, election, voter_uuid):
  """lift the suppression of the voter's address (for all elections)"""
  if request.method != 'POST':
    raise PermissionDenied()
  check_csrf(request)
  voter = _voter(election, voter_uuid)
  address = mail_status.current_address(voter)
  user = get_user(request)
  with transaction.atomic():
    lifted = bool(address) and EmailSuppression.lift(address, user=user, election=election)
    if lifted:
      election.append_log("Email suppression of %s (voter %s) lifted by %s" % (
        mask_address(address), voter.uuid, _actor(user)))
  if lifted:
    audit.info("election %s: suppression of %s (voter %s) lifted by %s", election.uuid, mask_address(address),
               voter.uuid, _actor(user))
  return HttpResponseRedirect(_voter_url(election, voter, done='lifted'))


##
## site administrators: the global suppression list
##

def suppressions(request):
  user = require_admin(request)
  address = (request.POST.get('email') or request.GET.get('email') or '').strip()
  done = request.GET.get('done', '')
  error = ''
  if request.method == 'POST':
    check_csrf(request)
    action = request.POST.get('action')
    if action == 'lift_all':
      count = EmailSuppression.objects.filter(lifted_at__isnull=True).update(
        lifted_at=datetime.datetime.utcnow(), lifted_by=user, lifted_in=None)
      audit.warning("all %d email suppressions lifted by site administrator %s", count, _actor(user))
      return HttpResponseRedirect(reverse(url_names.stats.STATS_SUPPRESSIONS) + '?' + urlencode({'done': 'lifted_all'}))
    if not validate_email(address):
      error = _("Please enter a valid email address.")
    elif action == 'lift':
      if EmailSuppression.lift(address, user=user):
        audit.info("suppression of %s lifted by site administrator %s", mask_address(address), _actor(user))
      return HttpResponseRedirect(reverse(url_names.stats.STATS_SUPPRESSIONS) + '?' +
                                  urlencode({'email': address, 'done': 'lifted'}))
    elif action == 'suppress':
      if not EmailSuppression.is_suppressed(address):
        EmailSuppression.suppress(address, source=EmailSuppression.SOURCE_MANUAL, reason='manual', user=user)
        audit.info("%s suppressed by site administrator %s", mask_address(address), _actor(user))
      return HttpResponseRedirect(reverse(url_names.stats.STATS_SUPPRESSIONS) + '?' +
                                  urlencode({'email': address, 'done': 'suppressed'}))
  from django.db.models import Count
  active = EmailSuppression.objects.filter(lifted_at__isnull=True)
  by_source = dict(active.values_list('source').annotate(n=Count('id')).values_list('source', 'n'))
  sources = [(str(mail_status.SUPPRESSION_REASONS[s]), by_source.get(s, 0)) for s, _label in EmailSuppression.SOURCES]
  found = None
  if address and validate_email(address):
    suppression = EmailSuppression.for_address(address)
    from . import utils
    lifted = EmailSuppression.objects.filter(email_hash=utils.hash_email(address), lifted_at__isnull=False).select_related(
      'lifted_by', 'lifted_in').first()
    found = {'address': address, 'suppression': suppression, 'lifted': lifted,
             'reason': str(mail_status.SUPPRESSION_REASONS[suppression.source]) if suppression else ''}
  return render_template(request, 'stats_suppressions', {
    'sources': sources, 'total': sum(by_source.values()), 'found': found, 'email': address, 'error': error,
    'done': done})
