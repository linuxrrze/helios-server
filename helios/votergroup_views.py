"""
Voter group views.

Every view checks that the logged-in user administers the group, and every
state-changing request is POST-only and CSRF-checked.
"""

import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponseForbidden, HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from helios_auth.security import check_csrf, get_user, login_required
from . import forms as election_forms
from . import short_names, url_names
from . import votergroup as vg
from .models import Election, Voter, VoterGroup, VoterGroupMember
from .security import can_create_election
from .view_utils import render_template


def _redirect(name, *args):
  return HttpResponseRedirect(settings.SECURE_URL_HOST + reverse(name, args=args))


def _get_group(request, votergroup_uuid):
  """Return the group if the current user administers it; raise 404 otherwise
  (no distinction between missing and foreign groups, to avoid leaking uuids)."""
  try:
    group = VoterGroup.objects.select_related('admin').get(uuid=votergroup_uuid)
  except VoterGroup.DoesNotExist:
    raise Http404
  if group.admin != get_user(request):
    raise Http404
  return group


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
        return _redirect(url_names.VOTERGROUP_VIEW, group.uuid)
      except IntegrityError:
        error = _("You already have a voter group named %(short_name)s") % {'short_name': form.cleaned_data['short_name']}

  return render_template(request, "votergroup_new", {'votergroup_form': form, 'error': error})


@login_required
def votergroup_view(request, votergroup_uuid):
  group = _get_group(request, votergroup_uuid)
  return render_template(request, "votergroup_view", {'group': group, 'members': group.get_members()})


@login_required
@require_http_methods(["GET", "POST"])
def votergroup_memberadd(request, votergroup_uuid):
  group = _get_group(request, votergroup_uuid)
  if request.method == "GET":
    form = vg.VoterGroupMemberForm()
  else:
    check_csrf(request)
    form = vg.VoterGroupMemberForm(request.POST)
    if form.is_valid():
      VoterGroupMember.objects.create(group=group, uuid=str(uuid.uuid4()), **form.cleaned_data)
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
  if member.group.admin != get_user(request):
    raise Http404
  group_uuid = member.group.uuid
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
        try:
          with transaction.atomic():
            election = Election.objects.create(**election_params)
            election.generate_trustee(ELGAMAL_PARAMS)
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
    'electionform': election_form, 'form': seat_formset, 'group': group, 'error': error})
