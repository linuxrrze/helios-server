"""
Display texts from settings in the active language, e.g.
  {% localized_setting "SITE_TITLE" %}
  {% localized_setting "WELCOME_MESSAGE" as welcome %}{{ welcome|safe }}
WELCOME_MESSAGE_DE etc. override the plain setting for that language.
"""

from django import template

from helios_auth.utils import localized_setting as _localized_setting

register = template.Library()


@register.simple_tag
def localized_setting(name):
  return _localized_setting(name) or ''


@register.simple_tag(takes_context=True)
def color_scheme_class(context):
  """
  "color-scheme-auto" on <html> when the pages may follow the visitor's dark
  mode (DARK_MODE == 'auto', or the dark_mode of the page's organization);
  layout.css keys its dark colors on it.
  """
  from helios import organizations
  return (context.get('ci') or organizations.default()).color_scheme_class
