"""
The corporate identity (CI) of the page, see helios/organizations.py:
  {% current_ci as ci %}{{ ci.site_title }}
It is the `ci` of the context (the election's organization on an election's
pages, else the default organization), or the default organization when the
template is rendered without one (e.g. the server error page).
"""

from django import template

from helios import organizations

register = template.Library()


@register.simple_tag(takes_context=True)
def current_ci(context):
  return context.get('ci') or organizations.default()
