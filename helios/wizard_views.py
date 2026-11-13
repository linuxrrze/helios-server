"""
Guided creation of an election: the settings of the single election form
(ElectionForm), asked a few at a time, then a summary to check before the
election is created. The answers are kept in the session until then.

After creation, the administrator continues with the questions; the setup
pages show where they are (templates/_setup_nav.html).
"""

from django.conf import settings
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext as _, gettext_lazy

from helios import url_names
from helios_auth.security import check_csrf, get_user, login_required
from . import forms, short_names
from .security import can_create_election
from .view_utils import render_template
from .views import create_election

SESSION_KEY = 'election_wizard'

# (key, title, fields of ElectionForm)
STEPS = [
  ('basics', gettext_lazy('Name and description'), ['name', 'short_name', 'description']),
  ('schedule', gettext_lazy('Voting period'), ['voting_starts_at', 'voting_ends_at']),
  ('options', gettext_lazy('Options'), ['election_type', 'private_p', 'use_voter_aliases', 'randomize_answer_order',
                                        'help_email', 'election_info_url']),
  ('review', gettext_lazy('Check and create'), []),
]
STEP_KEYS = [key for key, _title, _fields in STEPS]
STEPS_BY_KEY = {key: (key, title, fields) for key, title, fields in STEPS}


def _wizard_url(step=None):
  url = reverse(url_names.ELECTIONS_NEW_GUIDED)
  return url + ('?step=%s' % step if step else '')


def step_form(step, data, prefix):
  """ElectionForm restricted to the fields of one step; prefix: of the election's address"""
  fields = STEPS_BY_KEY[step][2]
  form = forms.ElectionForm(data, short_name_prefix=prefix)
  # in the order of the step
  form.fields = {name: form.fields[name] for name in fields if name in form.fields}
  return form


def _initial_draft(user):
  return {'private_p': 'on' if settings.HELIOS_PRIVATE_DEFAULT else '',
          'help_email': user.info.get('email', '') if user.info else '',
          'election_type': 'election'}


def summary(form):
  """(step key, label, value) for the review page, from a validated ElectionForm"""
  from .templatetags.timezone_tags import utc_time
  from .short_names import base_url
  data = form.cleaned_data
  rows = []
  for key, _title, fields in STEPS:
    for name in fields:
      if name not in form.fields:
        continue
      field = form.fields[name]
      value = data.get(name)
      if name in ('voting_starts_at', 'voting_ends_at'):
        if value:
          shown = utc_time(value)
        elif name == 'voting_starts_at':
          shown = _('When you freeze the ballot')
        else:
          shown = _('When you compute the tally')
      elif isinstance(value, bool):
        shown = _('Yes') if value else _('No')
      elif name == 'election_type':
        shown = dict(field.choices).get(value, value)
      elif name == 'short_name':
        shown = base_url() + value if value else _('Created automatically')
      else:
        shown = value or _('(not set)')
      rows.append({'step': key, 'name': name, 'label': field.label, 'value': shown})
  return rows


@login_required
def election_new_guided(request):
  if not can_create_election(request):
    return HttpResponseForbidden(_('only an administrator can create an election'))
  user = get_user(request)
  prefix = short_names.prefix_for_user(user)

  if request.GET.get('restart'):
    request.session.pop(SESSION_KEY, None)
    return HttpResponseRedirect(_wizard_url())

  draft = request.session.get(SESSION_KEY) or _initial_draft(user)
  step = request.POST.get('step') or request.GET.get('step') or 'basics'
  if step not in STEP_KEYS:
    step = 'basics'
  index = STEP_KEYS.index(step)
  error = None
  form = None
  rows = None

  if step == 'review':
    full_form = forms.ElectionForm(draft, short_name_prefix=prefix)
    if not draft.get('name') or not full_form.is_valid():
      # something is missing (e.g. a step skipped by URL) or the address has been
      # taken in the meantime: go to the first step with a problem
      for key, _title, fields in STEPS[:-1]:
        if not step_form(key, draft, prefix).is_valid():
          return HttpResponseRedirect(_wizard_url(key))
    rows = summary(full_form) if full_form.is_valid() else []
    if request.method == 'POST' and full_form.is_valid():
      check_csrf(request)
      election, error = create_election(user, full_form.cleaned_data)
      if election:
        request.session.pop(SESSION_KEY, None)
        return HttpResponseRedirect(settings.SECURE_URL_HOST + reverse(url_names.election.ELECTION_QUESTIONS, args=[election.uuid]))
  elif request.method == 'POST':
    check_csrf(request)
    data = dict(draft)
    for name in STEPS_BY_KEY[step][2]:
      # unchecked checkboxes are not submitted
      data[name] = request.POST.get(name, '')
    form = step_form(step, data, prefix)
    if form.is_valid():
      # the address as entered (without the prefix); empty: the UUID, assigned on creation
      request.session[SESSION_KEY] = data
      return HttpResponseRedirect(_wizard_url(STEP_KEYS[index + 1]))
  elif any(name in draft and name not in _initial_draft(user) for name in STEPS_BY_KEY[step][2]):
    # a step filled in before: checked again, e.g. an address that has been taken since
    form = step_form(step, draft, prefix)
    form.is_valid()
  else:
    # unbound, so that a step not filled in yet shows no errors
    form = step_form(step, None, prefix)
    form.initial = {name: draft.get(name) for name in form.fields}

  steps = [{'key': key, 'title': title, 'number': i + 1,
            'state': 'current' if i == index else ('done' if i < index else 'todo'),
            'url': _wizard_url(key) if i < index else None}
           for i, (key, title, _fields) in enumerate(STEPS)]

  return render_template(request, 'election_new_guided', {
    'form': form, 'step': step, 'steps': steps, 'step_number': index + 1, 'num_steps': len(STEPS),
    'step_title': STEPS_BY_KEY[step][1], 'rows': rows, 'error': error,
    'previous_url': _wizard_url(STEP_KEYS[index - 1]) if index > 0 else None,
    'last_step': step == 'review',
  })
