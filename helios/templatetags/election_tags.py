"""
{% load election_tags %}
{% election_status_badge election %} -> <span class="status-badge status-open">Voting open</span>
"""

from django import template
from django.utils.html import format_html

from helios.election_progress import election_status

register = template.Library()


@register.simple_tag
def election_status_badge(election):
  key, label = election_status(election)
  return format_html('<span class="status-badge status-{}">{}</span>', key, label)


@register.inclusion_tag('_setup_nav.html')
def setup_nav(election, current, part='steps'):
  """
  Where the administrator stands in setting up an election that is not frozen
  yet, with links to the previous and next setup page.
  {% setup_nav election "questions" %}  (questions, voters, trustees, freeze):
  the steps, for the top of the page;
  {% setup_nav election "questions" "pager" %}: the links to the previous and
  next step, for the bottom.
  """
  from django.urls import reverse
  from django.utils.translation import gettext as _
  from helios import url_names
  from helios.election_progress import admin_steps
  from helios.models import Trustee

  if election.frozen_at:
    return {'steps': None}
  progress = {s['key']: s for s in admin_steps(election, Trustee.get_by_election(election))}
  names = url_names.election
  steps = [
    {'key': 'settings', 'title': _('Settings'), 'url': reverse(names.ELECTION_EDIT, args=[election.uuid]), 'done': True},
    {'key': 'questions', 'title': _('Questions'), 'url': progress['questions']['url'], 'done': progress['questions']['done']},
    {'key': 'voters', 'title': _('Voters'), 'url': progress['voters']['url'], 'done': progress['voters']['done']},
    {'key': 'trustees', 'title': _('Trustees'), 'url': progress['trustees']['url'], 'done': progress['trustees']['done']},
    {'key': 'freeze', 'title': _('Check and open'), 'url': reverse(names.ELECTION_FREEZE, args=[election.uuid]), 'done': False},
  ]
  keys = [s['key'] for s in steps]
  index = keys.index(current)
  for i, step in enumerate(steps):
    step['number'] = i + 1
    step['current'] = i == index
  return {'steps': steps, 'number': index + 1, 'total': len(steps),
          'previous': steps[index - 1] if index > 0 else None,
          'next': steps[index + 1] if index + 1 < len(steps) else None,
          'election': election, 'part': part}
