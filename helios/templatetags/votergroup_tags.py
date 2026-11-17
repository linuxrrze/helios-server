"""
Voter groups as boards:

  {% load votergroup_tags %}
  {% term_ended_badge group %}   "Term ended" once the group's term of office is over
  {% term_of_office group %}     "1 Oct 2025 – 30 Sep 2027", "from ...", "until ...", or ''
"""

from django import template
from django.template.defaultfilters import date as date_filter
from django.utils.html import format_html
from django.utils.translation import gettext as _

register = template.Library()


@register.simple_tag
def term_ended_badge(group):
  if not getattr(group, 'term_ended', False):
    return ''
  return format_html('<span class="status-badge term-ended-badge" title="{}">{}</span>',
                     _("The term of office ended on %(date)s. The group can still be used.") % {
                       'date': date_filter(group.term_ends_on, 'DATE_FORMAT')},
                     _("Term ended"))


@register.simple_tag
def term_of_office(group):
  starts, ends = getattr(group, 'term_starts_on', None), getattr(group, 'term_ends_on', None)
  if starts and ends:
    return '%s – %s' % (date_filter(starts, 'DATE_FORMAT'), date_filter(ends, 'DATE_FORMAT'))
  if starts:
    return _('from %(date)s') % {'date': date_filter(starts, 'DATE_FORMAT')}
  if ends:
    return _('until %(date)s') % {'date': date_filter(ends, 'DATE_FORMAT')}
  return ''
