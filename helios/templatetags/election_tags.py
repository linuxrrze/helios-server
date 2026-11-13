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
