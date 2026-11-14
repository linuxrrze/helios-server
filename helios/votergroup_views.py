"""
Voter group views.

Every view checks that the logged-in user administers the group (owner or
additional administrator, see helios.security.user_can_admin_votergroup), and
every state-changing request is POST-only and CSRF-checked. A site
administrator who doesn't administer the group sees only its overview (name,
owner, number of members) and may transfer its ownership; every other page of
the group is refused (403), since the member list is personal data.

Changes are logged (logger "helios.votergroup", addresses masked as in the mail
log) and recorded in the group's log (VoterGroupLog), shown on the group's page
to its administrators.
"""

import logging
import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponseForbidden, HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext as _, pgettext
from django.views.decorators.http import require_http_methods, require_POST

from helios_auth.models import User
from helios_auth.security import check_csrf, get_user, login_required
from . import forms as election_forms
from . import organizations, short_names, url_names
from . import votergroup as vg
from .mail_backend import mask_address
from .models import Election, Voter, VoterGroup, VoterGroupLog, VoterGroupMember
from .provisioning import is_source_owner, source_label
from .security import (can_create_election, user_can_admin_votergroup, user_can_create_election,
                       user_can_see_votergroup_overview)
from .view_utils import find_user_to_add_as_admin, render_template

logger = logging.getLogger('helios.votergroup')


def _redirect(name, *args):
  return HttpResponseRedirect(settings.SECURE_URL_HOST + reverse(name, args=args))


def _who(user):
  """A user for the log: 'google:12 e***@example.org' (address masked)."""
  return '%s:%s %s' % (user.user_type, user.pk, mask_address(user.user_id))


def _admin_label(user):
  """An administrator in the group's log: name and account, as on the
  administrators page."""
  return '%s (%s)' % (user.pretty_name, user.user_id)


def _check_access(request, group, overview=False):
  """
  Raise unless the current user administers GROUP. With OVERVIEW, a site
  administrator is let through, too (the overview and the transfer of
  ownership); otherwise a site administrator gets 403, everybody else 404 (no
  distinction between missing and foreign groups, to avoid leaking uuids).
  """
  user = get_user(request)
  if user_can_admin_votergroup(user, group):
    return
  if user_can_see_votergroup_overview(user, group):
    if overview:
      return
    logger.info('votergroup %s: access refused, %s is a site administrator but not an administrator of the group',
                group.uuid, _who(user))
    raise PermissionDenied
  raise Http404


def _get_group(request, votergroup_uuid, overview=False):
  """Return the group if the current user may see this page of it (see _check_access)."""
  try:
    group = VoterGroup.objects.select_related('admin').get(uuid=votergroup_uuid)
  except VoterGroup.DoesNotExist:
    raise Http404
  _check_access(request, group, overview=overview)
  return group


def _refuse_if_provisioned(request, group):
  """
  A provisioned group (e.g. by SCIM) is managed by its source: its name,
  members and administrators can't be changed here. Returns the 403 response
  to send for such a group, None for a group that may be changed. Every view
  that changes a group calls this.
  """
  if not group.is_provisioned:
    return None
  logger.info('votergroup %s: change refused, provisioned by %s (%s)', group.uuid, group.provisioned_by,
              _who(get_user(request)))
  response = render_template(request, "votergroup_provisioned", {
    'group': group, 'source': source_label(group.provisioned_by),
    'can_prepare': user_can_admin_votergroup(get_user(request), group)})
  response.status_code = 403
  return response


def _other_admins(group, user):
  """Owner and additional administrators of the group, without USER (and
  without the account owning provisioned groups, which can't log in)."""
  return [a for a in [group.admin] + list(group.get_admins()) if a != user and not is_source_owner(a)]


def _add_election_admins(group, election, user, admins, num_voters):
  """Make ADMINS administrators of the election just prepared from GROUP, and log it."""
  election.admins.add(*admins)
  for a in admins:
    election.append_log("Administrator %s (%s) added" % (a.user_id, a.user_type))
  logger.info('votergroup %s: election %s prepared by %s, %d group administrator(s) added to it',
              group.uuid, election.uuid, _who(user), len(admins))
  group.append_log(VoterGroupLog.ELECTION_PREPARED, actor=user, name=election.name, election_uuid=election.uuid,
                   voters=num_voters, admins_added=bool(admins))


@login_required
def votergroup_administered(request):
  if not can_create_election(request):
    return HttpResponseForbidden(_('only an administrator has voter groups to administer'))
  groups = VoterGroup.get_by_user_as_admin(get_user(request))
  return render_template(request, "votergroup_administered", {'votergroups': groups})


@login_required
@require_http_methods(["GET", "POST"])
def votergroup_new(request):
  if not can_create_election(request):
    return HttpResponseForbidden(_('only an administrator can create a voter group'))

  error = None
  if request.method == "GET":
    form = vg.VoterGroupForm()
  else:
    check_csrf(request)
    form = vg.VoterGroupForm(request.POST)
    if form.is_valid():
      try:
        with transaction.atomic():
          group = VoterGroup.objects.create(
            admin=get_user(request), uuid=str(uuid.uuid4()),
            short_name=form.cleaned_data['short_name'])
          group.append_log(VoterGroupLog.CREATED, actor=group.admin, name=group.short_name)
        logger.info('votergroup %s: created by %s', group.uuid, _who(group.admin))
        return _redirect(url_names.VOTERGROUP_VIEW, group.uuid)
      except IntegrityError:
        error = _("You already have a voter group named %(short_name)s") % {'short_name': form.cleaned_data['short_name']}

  return render_template(request, "votergroup_new", {'votergroup_form': form, 'error': error})


LOG_ENTRIES_PER_PAGE = 20


@login_required
def votergroup_view(request, votergroup_uuid):
  group = _get_group(request, votergroup_uuid, overview=True)
  user = get_user(request)
  if not user_can_admin_votergroup(user, group):
    # a site administrator who doesn't administer the group: no member data
    return render_template(request, "votergroup_overview", {
      'group': group, 'num_members': group.votergroupmember_set.count(), 'num_admins': group.admins.count(),
      'can_transfer': _can_transfer(user, group), 'transfer_form': vg.TransferOwnershipByEmailForm()})
  log_page = Paginator(group.get_log(), LOG_ENTRIES_PER_PAGE).get_page(request.GET.get('log_page'))
  return render_template(request, "votergroup_view", {
    'group': group, 'members': group.get_members(), 'admins': group.get_admins(), 'log_page': log_page,
    'can_transfer': _can_transfer(user, group)})


@login_required
@require_http_methods(["GET", "POST"])
def votergroup_memberadd(request, votergroup_uuid):
  group = _get_group(request, votergroup_uuid)
  refused = _refuse_if_provisioned(request, group)
  if refused:
    return refused
  if request.method == "GET":
    form = vg.VoterGroupMemberForm()
  else:
    check_csrf(request)
    form = vg.VoterGroupMemberForm(request.POST)
    if form.is_valid():
      with transaction.atomic():
        member = VoterGroupMember.objects.create(group=group, uuid=str(uuid.uuid4()), **form.cleaned_data)
        group.append_log(VoterGroupLog.MEMBER_ADDED, actor=get_user(request), email=mask_address(member.email),
                         votes=member.votes)
      logger.info('votergroup %s: member %s (%s, %d vote(s)) added by %s', group.uuid, member.uuid,
                  mask_address(member.email), member.votes, _who(get_user(request)))
      return _redirect(url_names.VOTERGROUP_VIEW, group.uuid)

  return render_template(request, "votergroup_memberadd", {'votergroupmember_form': form, 'group': group})


@login_required
@require_POST
def votergroup_memberdelete(request, member_uuid):
  check_csrf(request)
  try:
    member = VoterGroupMember.objects.select_related('group__admin').get(uuid=member_uuid)
  except VoterGroupMember.DoesNotExist:
    raise Http404
  user = get_user(request)
  group = member.group
  _check_access(request, group)
  refused = _refuse_if_provisioned(request, group)
  if refused:
    return refused
  logger.info('votergroup %s: member %s (%s) deleted by %s', group.uuid, member.uuid,
              mask_address(member.email), _who(user))
  with transaction.atomic():
    group.append_log(VoterGroupLog.MEMBER_REMOVED, actor=user, email=mask_address(member.email))
    member.delete()
  return _redirect(url_names.VOTERGROUP_VIEW, group.uuid)


@login_required
@require_http_methods(["GET", "POST"])
def votergroup_prepareelection(request, votergroup_uuid):
  # imported here to avoid a circular import (helios.views imports url modules)
  from .views import ELGAMAL_PARAMS, one_election_cast

  if not can_create_election(request):
    return HttpResponseForbidden(_('only an administrator can create an election'))

  group = _get_group(request, votergroup_uuid)
  user = get_user(request)
  # offered as administrators of the new election (the preparing user becomes its creator)
  other_admins = _other_admins(group, user)
  admins_form = vg.PrepareElectionAdminsForm(request.POST if request.method == "POST" else None)
  members = list(group.get_members())
  choices = vg.delegate_choices(members)
  error = None
  prefix = short_names.prefix_for_user(user)

  if request.method == "GET":
    seat_formset = vg.SeatFormSet(initial=vg.initial_seats(members), form_kwargs={'delegate_choices': choices})
    election_form = election_forms.ElectionForm(initial={
      'description': _("Election for voter group %(short_name)s") % {'short_name': group.short_name},
      'private_p': True,
      'help_email': user.info.get("email", '')}, short_name_prefix=prefix)
  else:
    check_csrf(request)
    seat_formset = vg.SeatFormSet(request.POST, form_kwargs={'delegate_choices': choices})
    election_form = election_forms.ElectionForm(request.POST, short_name_prefix=prefix)

    if seat_formset.is_valid() and election_form.is_valid():
      seats = vg.apply_delegations(seat_formset.cleaned_data, {m.uuid: m for m in members})
      election_params = dict(election_form.cleaned_data)

      if not seats:
        error = _("The voter group has no members.")
      else:
        election_params['uuid'] = str(uuid.uuid4())
        # without a chosen address, the election is reached by its UUID
        election_params['short_name'] = election_params.get('short_name') or election_params['uuid']
        election_params['cast_url'] = settings.SECURE_URL_HOST + reverse(one_election_cast, args=[election_params['uuid']])
        election_params['openreg'] = False
        election_params['admin'] = user
        election_params['organization'] = organizations.organization_for_user(user)
        try:
          with transaction.atomic():
            election = Election.objects.create(**election_params)
            election.generate_trustee(ELGAMAL_PARAMS)
            add_admins = admins_form.is_valid() and admins_form.cleaned_data['add_group_admins']
            _add_election_admins(group, election, user, other_admins if add_admins else [], len(seats))
            for num, seat in enumerate(seats, start=1):
              voter = Voter(uuid=str(uuid.uuid4()), user=None, election=election,
                            voter_login_id=seat['login_id'],
                            voter_name="%s %s" % (seat['first_name'], seat['last_name']),
                            voter_email=seat['email'])
              # same scheme as upstream's voter upload (new election, so start at V1);
              # without an alias the voter's name is published with the ballots
              if election.use_voter_aliases:
                voter.alias = "V%s" % num
              voter.generate_password()
              voter.save()
          return _redirect(url_names.election.ELECTION_VIEW, election.uuid)
        except IntegrityError:
          # taken in the meantime (the form checks it, too)
          error = _("This address is already taken. Please choose a different one.")

  return render_template(request, "votergroup_prepareelection", {
    'electionform': election_form, 'form': seat_formset, 'group': group, 'error': error,
    'admins_form': admins_form, 'other_admins': other_admins})


##
## administrators of a group
##
## The owner (group.admin) created the group and cannot be removed. Additional
## administrators have the same rights as the owner: they edit the members,
## prepare elections and add or remove administrators. Unlike on elections,
## an additional administrator may remove themself ("leave the group"): the
## owner always remains, so a group can't end up without an administrator.
##
## The owner can transfer the ownership to one of the additional
## administrators; a site administrator (e.g. when the owner has left) to any
## user who can create elections, found by email. The previous owner then
## stays an additional administrator unless unchecked. A provisioned group's owner is its source's account, and its
## administrators come from the source only: no transfer.
##

@login_required
def votergroup_admins(request, votergroup_uuid):
  group = _get_group(request, votergroup_uuid)
  return render_template(request, "votergroup_admins", {
    'group': group, 'admins': group.get_admins(), 'can_transfer': _can_transfer(get_user(request), group)})


def _new_admin_error(group, new_admin):
  if new_admin == group.admin:
    return _("This user is the owner of the voter group.")
  if group.admins.filter(pk=new_admin.pk).exists():
    return pgettext('voter group', "This user is already an administrator.")
  # without this right, voter groups are not available to the user at all
  if not user_can_create_election(new_admin):
    return _("This user cannot create elections and therefore cannot administer voter groups.")
  return None


@login_required
@require_http_methods(["GET", "POST"])
def votergroup_adminadd(request, votergroup_uuid):
  group = _get_group(request, votergroup_uuid)
  refused = _refuse_if_provisioned(request, group)
  if refused:
    return refused
  matching_users = None

  if request.method == "GET":
    form = vg.VoterGroupAdminForm()
  else:
    check_csrf(request)
    form = vg.VoterGroupAdminForm(request.POST)
    if form.is_valid():
      # by email; with several auth systems, the user chooses (auth_type)
      new_admin, matching_users, error = find_user_to_add_as_admin(
        form.cleaned_data['email'], request.POST.get('auth_type', ''),
        not_found=pgettext('voter group', 'No user found with that email. They must log in to Helios at least once before being added as an administrator.'))
      if new_admin:
        error = _new_admin_error(group, new_admin)
      if error:
        form.add_error('email', error)
      elif new_admin:
        with transaction.atomic():
          group.admins.add(new_admin)
          group.append_log(VoterGroupLog.ADMIN_ADDED, actor=get_user(request), admin=_admin_label(new_admin))
        logger.info('votergroup %s: administrator %s added by %s', group.uuid, _who(new_admin), _who(get_user(request)))
        return _redirect(url_names.VOTERGROUP_ADMINS, group.uuid)

  return render_template(request, "votergroup_adminadd", {
    'group': group, 'form': form, 'matching_users': matching_users})


@login_required
@require_http_methods(["GET", "POST"])
def votergroup_adminremove(request, votergroup_uuid):
  group = _get_group(request, votergroup_uuid)
  refused = _refuse_if_provisioned(request, group)
  if refused:
    return refused
  data = request.POST if request.method == "POST" else request.GET
  # only additional administrators can be removed, the owner is not among them
  try:
    admin_to_remove = group.admins.get(user_id=data.get('user_id', ''), user_type=data.get('user_type', ''))
  except User.DoesNotExist:
    raise Http404
  user = get_user(request)
  leaving = admin_to_remove == user

  if request.method == "GET":
    return render_template(request, "votergroup_adminremove", {
      'group': group, 'admin_to_remove': admin_to_remove, 'leaving': leaving})

  check_csrf(request)
  with transaction.atomic():
    group.admins.remove(admin_to_remove)
    group.append_log(VoterGroupLog.ADMIN_LEFT if leaving else VoterGroupLog.ADMIN_REMOVED, actor=user,
                     admin=_admin_label(admin_to_remove))
  logger.info('votergroup %s: administrator %s removed by %s', group.uuid, _who(admin_to_remove), _who(user))
  if leaving:
    return _redirect(url_names.VOTERGROUP_ADMINISTERED)
  return _redirect(url_names.VOTERGROUP_ADMINS, group.uuid)


def _can_transfer(user, group):
  """The owner and site administrators may transfer the ownership of a local group."""
  return bool(user) and not group.is_provisioned and (group.admin_id == user.pk or user.admin_p)


def _transfer_error(group, new_owner):
  if new_owner == group.admin:
    return _("This user is the owner of the voter group.")
  # an owner needs the same right as an administrator (see _new_admin_error)
  if not user_can_create_election(new_owner):
    return _("This user cannot create elections and therefore cannot administer voter groups.")
  # group names are unique per owner
  if VoterGroup.objects.filter(admin=new_owner, short_name=group.short_name).exists():
    return _("%(name)s already owns a voter group named %(short_name)s, so this group cannot be transferred to them.") % {
      'name': new_owner.pretty_name, 'short_name': group.short_name}
  return None


@login_required
@require_http_methods(["GET", "POST"])
def votergroup_transfer(request, votergroup_uuid):
  """
  Transfer the ownership: choose the new owner, confirm (two POSTs, the
  second with confirmed=1), done.

  The owner chooses one of the group's additional administrators. A site
  administrator may choose any user who can create elections, found by email
  as when adding an administrator (e.g. when the owner has left and nobody
  else administers the group); the log marks such a transfer.
  """
  group = _get_group(request, votergroup_uuid, overview=True)
  user = get_user(request)
  if group.admin_id != user.pk and not user.admin_p:
    # an additional administrator: only the owner hands the group over
    raise PermissionDenied
  refused = _refuse_if_provisioned(request, group)
  if refused:
    return refused

  by_email = user.admin_p
  candidates = list(group.get_admins())
  data = request.POST if request.method == "POST" else None
  if by_email:
    form = vg.TransferOwnershipByEmailForm(data)
    field = 'email'
  else:
    form = vg.TransferOwnershipForm(data, candidates=candidates)
    field = 'new_owner'
  new_owner = matching_users = None
  confirm = False

  if request.method == "POST":
    check_csrf(request)
    if form.is_valid():
      error = None
      if by_email:
        new_owner, matching_users, error = find_user_to_add_as_admin(
          form.cleaned_data['email'], request.POST.get('auth_type', ''),
          not_found=_('No user found with that email. They must log in to Helios at least once before they can become the owner.'))
      else:
        new_owner = next(u for u in candidates if str(u.pk) == form.cleaned_data['new_owner'])
      keep = form.cleaned_data['keep_previous_owner']
      if new_owner:
        error = _transfer_error(group, new_owner)
      if error:
        form.add_error(field, error)
        new_owner = None
      elif matching_users:
        pass  # the site administrator chooses one of several users with this email
      elif request.POST.get('confirmed') != '1':
        confirm = True
      else:
        old_owner = group.admin
        # a site administrator acting on a group they don't own
        by_site_admin = old_owner != user
        try:
          with transaction.atomic():
            group.admin = new_owner
            group.save(update_fields=['admin'])
            group.admins.remove(new_owner)
            if keep:
              group.admins.add(old_owner)
            group.append_log(VoterGroupLog.OWNER_CHANGED, actor=user, old_owner=_admin_label(old_owner),
                             new_owner=_admin_label(new_owner), previous_owner_left=not keep,
                             by_site_admin=by_site_admin)
        except IntegrityError:
          # a group of the same name, created by the new owner in the meantime
          group.admin = old_owner
          form.add_error(field, _transfer_error(group, new_owner) or _("The ownership could not be transferred."))
          new_owner = None
        else:
          logger.info('votergroup %s: ownership transferred from %s to %s by %s%s%s', group.uuid, _who(old_owner),
                      _who(new_owner), 'site administrator ' if by_site_admin else '', _who(user),
                      '' if keep else ', previous owner no longer an administrator')
          if user_can_see_votergroup_overview(user, group):
            return _redirect(url_names.VOTERGROUP_VIEW, group.uuid)
          return _redirect(url_names.VOTERGROUP_ADMINISTERED)

  return render_template(request, "votergroup_transfer", {
    'group': group, 'form': form, 'candidates': candidates, 'confirm': confirm, 'new_owner': new_owner,
    'matching_users': matching_users, 'by_email': by_email, 'is_owner': group.admin_id == user.pk})
