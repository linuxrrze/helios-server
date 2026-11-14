"""
Voter groups: forms and the pure logic that turns a group
into the list of password voters for a new election.

A member with N votes gets N voter rows ("seats"). Before the election is
created, the administrator can delegate any seat to another member of the
group; that seat is then issued to the delegate instead.
"""

from django import forms
from django.forms import formset_factory
from django.utils.translation import gettext, gettext_lazy as _

MAX_VOTES_PER_MEMBER = 100


class VoterGroupForm(forms.Form):
  short_name = forms.SlugField(max_length=100, label=_('Short name'), help_text=_('no spaces, e.g. faculty-council'))


class VoterGroupMemberForm(forms.Form):
  first_name = forms.CharField(max_length=100, label=_('First name'))
  last_name = forms.CharField(max_length=100, label=_('Last name'))
  email = forms.EmailField(max_length=100, label=_('Email'))
  votes = forms.IntegerField(min_value=1, max_value=MAX_VOTES_PER_MEMBER, initial=1, label=_('Votes'))


class VoterGroupAdminForm(forms.Form):
  email = forms.CharField(max_length=100, label=_('Email'),
                          help_text=_('The person must have logged in to Helios at least once before they can be added.'))


class TransferOwnershipForm(forms.Form):
  """The new owner, one of the group's additional administrators (CANDIDATES).
  The template renders the fields itself (a radio list of the candidates)."""
  new_owner = forms.ChoiceField(label=_('New owner'))
  keep_previous_owner = forms.BooleanField(required=False, initial=True)

  def __init__(self, *args, candidates=(), **kwargs):
    super().__init__(*args, **kwargs)
    self.fields['new_owner'].choices = [(str(u.pk), u.pretty_name) for u in candidates]


class PrepareElectionAdminsForm(forms.Form):
  add_group_admins = forms.BooleanField(
    required=False, initial=True,
    label=_('Add the other administrators of the group to the election administrators'),
    help_text=_('You become the creator of the election either way. Changes to the group later on do not change the election.'))


class SeatForm(forms.Form):
  """One voter row of the election being prepared."""
  first_name = forms.CharField(max_length=100)
  last_name = forms.CharField(max_length=100)
  email = forms.EmailField(max_length=100)
  login_id = forms.CharField(max_length=100)
  delegate_to = forms.ChoiceField(choices=(), required=False)

  def __init__(self, *args, **kwargs):
    delegate_choices = kwargs.pop('delegate_choices')
    super().__init__(*args, **kwargs)
    self.fields['delegate_to'].choices = delegate_choices


class BaseSeatFormSet(forms.BaseFormSet):
  def clean(self):
    if any(self.errors):
      return
    seen = set()
    for form in self.forms:
      login_id = form.cleaned_data.get('login_id')
      if login_id in seen:
        raise forms.ValidationError(gettext("Login IDs must be unique: %(login_id)s is used twice.") % {'login_id': login_id})
      seen.add(login_id)


SeatFormSet = formset_factory(SeatForm, formset=BaseSeatFormSet, extra=0)


def delegate_choices(members):
  return [('', gettext('No delegation'))] + [
    (m.uuid, "%s, %s <%s>" % (m.last_name, m.first_name, m.email)) for m in members]


def next_login_id(first_name, last_name, taken):
  """Initials plus the lowest free counter, e.g. JD1, JD2, ..."""
  prefix = (first_name[:1] + last_name[:1]).upper() or 'V'
  n = 1
  while "%s%d" % (prefix, n) in taken:
    n += 1
  return "%s%d" % (prefix, n)


def _seat(first_name, last_name, email, taken):
  login_id = next_login_id(first_name, last_name, taken)
  taken.add(login_id)
  return {'first_name': first_name, 'last_name': last_name, 'email': email,
          'login_id': login_id, 'delegate_to': ''}


def initial_seats(members):
  """One seat per vote of every member."""
  taken, seats = set(), []
  for m in members:
    for _ in range(m.votes):
      seats.append(_seat(m.first_name, m.last_name, m.email, taken))
  return seats


def apply_delegations(seats, members_by_uuid):
  """
  Replace every delegated seat with a seat for the delegate, keeping the
  order of non-delegated seats and appending delegated ones at the end.
  `seats` are cleaned SeatForm dicts.
  """
  kept = [dict(s, delegate_to='') for s in seats if not s.get('delegate_to')]
  taken = {s['login_id'] for s in kept}
  for s in seats:
    uuid = s.get('delegate_to')
    if uuid:
      m = members_by_uuid[uuid]
      kept.append(_seat(m.first_name, m.last_name, m.email, taken))
  return kept
