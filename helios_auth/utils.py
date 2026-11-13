"""
Some basic utils 
(previously were in helios module, but making things less interdependent

2010-08-17
"""

import json
import os

from django.conf import settings
from django.utils import translation
from django.utils.http import url_has_allowed_host_and_scheme


class AuthenticationError(Exception):
  """
  Raised by an auth system when sign-in fails for a reason the person can fix
  themselves. The message is shown to them, so it must not contain secrets.
  """
  def __init__(self, message, help_url=None):
    super().__init__(message)
    self.message = message
    self.help_url = help_url


def same_site_path(url, default="/"):
  """
  url if it is a path on this site (a single leading '/'), otherwise default.
  """
  if url and url.startswith('/') and url_has_allowed_host_and_scheme(url, allowed_hosts=None):
    return url
  return default


## JSON
def to_json(d):
    return json.dumps(d, sort_keys=True)


def from_json(value):
    if value == "" or value is None:
        return None

    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception as e:
            # import ast
            # try:
            #     parsed_value = ast.literal_eval(parsed_value)
            # except Exception as e1:
            raise Exception("value is not JSON parseable, that's bad news") from e

    return value


def JSONFiletoDict(filename):
    with open(filename, 'r') as f:
        content = f.read()
    return from_json(content)


def format_recipient(name, email):
    """
    Format an email recipient as "name" <email>.
    Truncates name to 70 characters to avoid issues with Python3's email module.
    Quotes the name per RFC 5322 to handle special characters.
    """
    truncated_name = name[:70] if name else email
    return "\"%s\" <%s>" % (truncated_name, email)


def localized_setting(name, default=None):
  """
  A display text from settings in the active language: the <NAME>_<LANG>
  variant (e.g. WELCOME_MESSAGE_DE) if configured, else <NAME>, else default.
  """
  language = (translation.get_language() or settings.LANGUAGE_CODE).split('-')[0]
  value = getattr(settings, 'LOCALIZED_SETTINGS', {}).get(name, {}).get(language)
  return value or getattr(settings, name, None) or default


def login_icon_url(auth_system_name):
  """
  URL of the icon shown next to users of an auth system. An installation can add
  icons for its own auth system names (e.g. "campus") as
  SITE_STATIC_DIR/login-icons/<name>.png; otherwise the built-in icon is used.
  """
  name = os.path.basename(str(auth_system_name))
  site_dir = getattr(settings, 'SITE_STATIC_DIR', '')
  if site_dir and os.path.isfile(os.path.join(site_dir, 'login-icons', name + '.png')):
    return '/static/site/login-icons/%s.png' % name
  return '/static/auth/login-icons/%s.png' % name
