from django import template

from helios_auth.utils import login_icon_url

register = template.Library()


@register.filter
def login_icon(auth_system_name):
  """{{ voter.voter_type|login_icon }}: icon URL, see helios_auth.utils.login_icon_url"""
  return login_icon_url(auth_system_name)
