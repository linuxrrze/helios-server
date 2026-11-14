"""
Badges for entities managed by a provisioning source (see helios/provisioning.py):

  {% load provisioning_tags %}
  {% provisioned_badge group %}   "Provisioned (SCIM: idm)", for users and voter groups
  {% deactivated_badge user %}    "Deactivated", for users deactivated by provisioning
"""

from django import template
from django.utils.html import format_html
from django.utils.translation import gettext as _

from helios.provisioning import source_label

register = template.Library()


@register.simple_tag
def provisioned_badge(obj):
  source = getattr(obj, 'provisioned_by', '') if obj else ''
  if not source:
    return ''
  label = source_label(source)
  return format_html(
    '<span class="status-badge provisioned-badge" title="{}">{}</span>',
    _("Managed by the identity management (%(source)s) and cannot be changed here.") % {'source': label},
    _("Provisioned (%(source)s)") % {'source': label})


@register.simple_tag
def deactivated_badge(user):
  if not user or getattr(user, 'deactivated_at', None) is None:
    return ''
  return format_html('<span class="status-badge deactivated-badge" title="{}">{}</span>',
                     _("This account was deactivated by the identity management: no login, no rights."),
                     _("Deactivated"))


@register.filter(name='source_label')
def source_label_filter(source):
  """'scim:idm' -> 'SCIM: idm'"""
  return source_label(source)
