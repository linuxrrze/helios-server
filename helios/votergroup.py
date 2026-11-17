"""
Voter groups: forms and the pure logic that turns a group
into the list of password voters for a new election.

A member with N votes gets N voter rows ("seats"), a member with 0 votes (no
voting right, e.g. a deputy or an advisory member of a board) none. Before
the election is created, the administrator can delegate any seat to another
member of the group; that seat is then issued to the delegate instead.
"""

from django import forms
from django.forms import formset_factory
from django.utils.translation import gettext, gettext_lazy as _

from .models import VoterGroupMember

MAX_VOTES_PER_MEMBER = 100


class DateInput(forms.DateInput):
  """The browser's date picker; the value is sent as YYYY-MM-DD."""
  input_type = 'date'

  def __init__(self, attrs=None):
    super().__init__(attrs=attrs, format='%Y-%m-%d')


class VoterGroupForm(forms.Form):
  """A new group, or the settings of a group. Everything but the short name
  is optional (for a board: its long name and term of office)."""
  short_name = forms.SlugField(max_length=100, label=_('Short name'), help_text=_('no spaces, e.g. faculty-council'))
  name = forms.CharField(max_length=250, required=False, label=_('Name'),
                         help_text=_('The full name, shown as the title of the group, e.g. Faculty council of the Faculty of Sciences.'))
  name_suffix = forms.CharField(max_length=100, required=False, label=_('Name suffix'),
                                help_text=_('Shown after the name, e.g. the term of office in short form: 2025-2027.'))
  term_starts_on = forms.DateField(required=False, widget=DateInput, label=_('Term of office begins on'))
  term_ends_on = forms.DateField(required=False, widget=DateInput, label=_('Term of office ends on'),
                                 help_text=_('After this day, the group is marked as "term ended"; it can still be used.'))

  def clean(self):
    cleaned = super().clean()
    starts, ends = cleaned.get('term_starts_on'), cleaned.get('term_ends_on')
    if starts and ends and ends < starts:
      self.add_error('term_ends_on', _('The term of office cannot end before it begins.'))
    return cleaned


class VoterGroupMemberForm(forms.Form):
  first_name = forms.CharField(max_length=100, label=_('First name'))
  last_name = forms.CharField(max_length=100, label=_('Last name'))
  email = forms.EmailField(max_length=100, label=_('Email'))
  votes = forms.IntegerField(min_value=0, max_value=MAX_VOTES_PER_MEMBER, initial=1, label=_('Votes'),
                             help_text=_('0 for members without a voting right, e.g. deputy or advisory members: they get no vote in an election prepared from the group.'))
  initials = forms.CharField(max_length=20, required=False, label=_('Initials'),
                             help_text=_('A short identifier, e.g. the initials.'))
  person_group = forms.CharField(max_length=100, required=False, label=_('Person group'),
                                 help_text=_('e.g. Professors, Students; the member list is sorted by it.'))
  status = forms.ChoiceField(choices=VoterGroupMember.STATUS_CHOICES, initial=VoterGroupMember.STATUS_MEMBER,
                             label=_('Status'))

  def __init__(self, data=None, *args, **kwargs):
    if data is not None and not data.get('status'):
      # a client that doesn't know the field: a regular member
      data = data.copy()
      data['status'] = VoterGroupMember.STATUS_MEMBER
    super().__init__(data, *args, **kwargs)

  def clean(self):
    cleaned = super().clean()
    for field in ('first_name', 'last_name', 'initials', 'person_group'):
      if isinstance(cleaned.get(field), str):
        cleaned[field] = cleaned[field].strip()
    return cleaned


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


class TransferOwnershipByEmailForm(forms.Form):
  """For site administrators: the new owner may be any user who can create
  elections, found by email (see helios.view_utils.find_user_to_add_as_admin)."""
  email = forms.CharField(max_length=100, label=_('Email of the new owner'),
                          help_text=_('The person must have logged in to Helios at least once and be allowed to create elections.'))
  keep_previous_owner = forms.BooleanField(required=False, initial=True,
                                           label=_('Keep the previous owner as an administrator of the group'))


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
  """One seat per vote of every member (none for members with 0 votes)."""
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

