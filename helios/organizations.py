"""
Organizations and their corporate identity (CI): logos, colors (stylesheet),
site title, footer and legal links, help address.

Templates read the CI from the object `ci` in the template context, never from
settings directly. Without configuration there is one organization, the
default: the operator of the server, described by the global settings
(SITE_TITLE, SITE_CSS_URL, ORGANIZATION_*, MAIN_LOGO_URL, FOOTER_LOGO_URL,
IMPRINT_URL, PRIVACY_URL, ACCESSIBILITY_URL, FOOTER_LINKS, HELP_EMAIL_ADDRESS,
DARK_MODE).
"""

from django.conf import settings
from django.utils import translation

from helios_auth.utils import localized_setting


def _language():
  return (translation.get_language() or settings.LANGUAGE_CODE).split('-')[0]


class Organization:
  """
  The CI of one organization. Texts are resolved in the active language when
  they are read, so one object can be used for a whole request.
  """

  def __init__(self, slug='', config=None):
    self.slug = slug
    self._config = config or {}

  def __repr__(self):
    return '<Organization %r>' % (self.slug or '(default)')

  def __eq__(self, other):
    return isinstance(other, Organization) and other.slug == self.slug

  def __hash__(self):
    return hash(self.slug)

  @property
  def is_default(self):
    return not self.slug

  # the organization itself (shown in the header and footer)

  @property
  def name(self):
    return localized_setting('ORGANIZATION_NAME') or ''

  @property
  def url(self):
    return settings.ORGANIZATION_URL

  @property
  def header_logo_url(self):
    """small logo in the header bar, instead of the name"""
    return settings.ORGANIZATION_LOGO_URL

  @property
  def footer_logo_url(self):
    """logo on the (dark) footer background"""
    return settings.FOOTER_LOGO_URL

  # layout

  @property
  def site_title(self):
    return localized_setting('SITE_TITLE') or ''

  @property
  def main_logo_url(self):
    """logo above the page content"""
    return settings.MAIN_LOGO_URL

  @property
  def css_url(self):
    """stylesheet loaded after the default layout (usually sets CSS variables)"""
    return settings.SITE_CSS_URL

  @property
  def dark_mode(self):
    return getattr(settings, 'DARK_MODE', 'off')

  @property
  def color_scheme_class(self):
    """class on <html> when the pages may follow the visitor's dark mode; layout.css keys its dark colors on it"""
    return 'color-scheme-auto' if self.dark_mode == 'auto' else ''

  # footer

  @property
  def imprint_url(self):
    return settings.IMPRINT_URL

  @property
  def privacy_url(self):
    return settings.PRIVACY_URL

  @property
  def accessibility_url(self):
    return settings.ACCESSIBILITY_URL

  @property
  def footer_links(self):
    """[{'url': ..., 'text': ...}]"""
    return settings.FOOTER_LINKS

  @property
  def help_email(self):
    """the "Contact" address in the footer"""
    return settings.HELP_EMAIL_ADDRESS


def default():
  """the operator's organization, from the global settings"""
  return Organization()


def context_processor(request):
  """`ci` in every template rendered with a request; views of an election replace it (see view_utils)"""
  return {'ci': default()}
