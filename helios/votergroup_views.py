"""
Voter group views.

Every view checks that the logged-in user administers the group (owner,
additional administrator or site administrator, see
helios.security.user_can_admin_votergroup), and every state-changing request
is POST-only and CSRF-checked. Changes to members and administrators are
logged (logger "helios.votergroup", addresses masked as in the mail log).
"""

import logging
import uuid

from django.conf import settings
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
from .models import Election, Voter, VoterGroup, VoterGroupMember
from .provisioning import is_source_owner, source_label
from .security import can_create_election, user_can_admin_votergroup, user_can_create_election
from .view_utils import find_user_to_add_as_admin, render_template

logger = logging.getLogger('helios.votergroup')


def _redirect(name, *args):
  return HttpResponseRedirect(settings.SECURE_URL_HOST + reverse(name, args=args))


def _who(user):
  """A user for the log: 'google:12 e***@example.org' (address masked)."""
  return '%s:%s %s' % (user.user_type, user.pk, mask_address(user.user_id))


def _get_group(request, votergroup_uuid):
  """Return the group if the current user administers it; raise 404 otherwise
  (no distinction between missing and foreign groups, to avoid leaking uuids)."""
  try:
    group = VoterGroup.objects.select_related('admin').get(uuid=votergroup_uuid)
  except VoterGroup.DoesNotExist:
    raise Http404
  if not user_can_admin_votergroup(get_user(request), group):
    raise Http404
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
    'group': group, 'source': source_label(group.provisioned_by)})
  response.status_code = 403
  return response


def _other_admins(group, user):
  """Owner and additional administrators of the group, without USER (and
  without the account owning provisioned groups, which can't log in)."""
  return [a for a in [group.admin] + list(group.get_admins()) if a != user and not is_source_owner(a)]


def _add_election_admins(group, election, user, admins):
  """Make ADMINS administrators of the election just prepared from GROUP."""
  election.admins.add(*admins)
  for a in admins:
    election.append_log("Administrator %s (%s) added" % (a.user_id, a.user_type))
  logger.info('votergroup %s: election %s prepared by %s, %d group administrator(s) added to it',
              group.uuid, election.uuid, _who(user), len(admins))


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
        logger.info('votergroup %s: created by %s', group.uuid, _who(group.admin))
        return _redirect(url_names.VOTERGROUP_VIEW, group.uuid)
      except IntegrityError:
        error = _("You already have a voter group named %(short_name)s") % {'short_name': form.cleaned_data['short_name']}

  return render_template(request, "votergroup_new", {'votergroup_form': form, 'error': error})


@login_required
def votergroup_view(request, votergroup_uuid):
  group = _get_group(request, votergroup_uuid)
  return render_template(request, "votergroup_view", {
    'group': group, 'members': group.get_members(), 'admins': group.get_admins()})


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
      member = VoterGroupMember.objects.create(group=group, uuid=str(uuid.uuid4()), **form.cleaned_data)
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
  if not user_can_admin_votergroup(user, member.group):
    raise Http404
  refused = _refuse_if_provisioned(request, member.group)
  if refused:
    return refused
  group_uuid = member.group.uuid
  logger.info('votergroup %s: member %s (%s) deleted by %s', group_uuid, member.uuid,
              mask_address(member.email), _who(user))
  member.delete()
  return _redirect(url_names.VOTERGROUP_VIEW, group_uuid)


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
            _add_election_admins(group, election, user, other_admins if add_admins else [])
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

@login_required
def votergroup_admins(request, votergroup_uuid):
  group = _get_group(request, votergroup_uuid)
  return render_template(request, "votergroup_admins", {'group': group, 'admins': group.get_admins()})


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
        group.admins.add(new_admin)
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
  group.admins.remove(admin_to_remove)
  logger.info('votergroup %s: administrator %s removed by %s', group.uuid, _who(admin_to_remove), _who(user))
  if leaving:
    return _redirect(url_names.VOTERGROUP_ADMINISTERED)
  return _redirect(url_names.VOTERGROUP_ADMINS, group.uuid)
