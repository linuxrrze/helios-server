"""
Organizations sharing this server and their corporate identity (CI): logos,
colors (stylesheet), site title, footer and legal links, help address.

Templates read the CI from the object `ci` in the template context, never from
settings directly. The default organization is the operator of the server,
described by the global settings (SITE_TITLE, SITE_CSS_URL, ORGANIZATION_*,
MAIN_LOGO_URL, FOOTER_LOGO_URL, IMPRINT_URL, PRIVACY_URL, ACCESSIBILITY_URL,
FOOTER_LINKS, HELP_EMAIL_ADDRESS, DARK_MODE). Further organizations come from
ORGANIZATIONS_FILE (settings.ORGANIZATIONS, see docs/organizations.md).

Each election belongs to an organization (Election.organization, a slug; ''
for the default). It is set when the election is created, from the identity of
its creator, and only site administrators can change it: otherwise anyone could
send out an election in another organization's CI. The pages of an election for
voters and trustees (view_utils.ELECTION_CI_TEMPLATES) show its organization's
CI; all other pages (start page, login, the lists of one's elections, the
election's administration pages, site administration) show the default CI.
The election's voting booth gets its organization's colors from a stylesheet
generated from validated values (helios/booth_theme.py), and its emails to
voters and trustees its sender name, Reply-To and signature (mail_context).

One mapping assigns people to organizations, and with that to the prefix of
their election addresses (see match_user): the rules of the organizations
("match") and the older ELECTION_SHORT_NAME_PREFIXES_BY_IDP,
ELECTION_SHORT_NAME_PREFIXES and ELECTION_SHORT_NAME_PREFIX, whose prefixes
belong to the organization with that short_name_prefix, if any.

CI is not a separation of tenants: data, site administrators, logins and the
email opt-out list stay shared.
"""

import contextlib
import contextvars
import copy
from collections import namedtuple
from email.utils import formataddr, parseaddr

from django.conf import settings
from django.utils import translation

from helios_auth.utils import localized_setting


def _language():
  return (translation.get_language() or settings.LANGUAGE_CODE).split('-')[0]


def localize(texts):
  """a text from {language or '': text} in the active language"""
  if not texts:
    return ''
  if isinstance(texts, str):
    return texts
  for language in (_language(), '', settings.LANGUAGE_CODE.split('-')[0], 'en'):
    if texts.get(language):
      return texts[language]
  return next(iter(texts.values()))


class Organization:
  """
  The CI of one organization. Texts are resolved in the active language when
  they are read, so one object can be used for a whole request.

  A configured organization takes the layout values it doesn't set (title,
  stylesheet, main logo, legal and footer links, help address) from the
  default organization; an empty value hides the item instead. Its name, home
  page and the logos in the header and footer identify it and are never taken
  from the default.
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

  def _layout(self, key, default_value):
    return self._config[key] if key in self._config else default_value()

  # the organization itself (shown in the header and footer)

  @property
  def name(self):
    if self.is_default:
      return localized_setting('ORGANIZATION_NAME') or ''
    return localize(self._config.get('name'))

  @property
  def url(self):
    return settings.ORGANIZATION_URL if self.is_default else self._config.get('url', '')

  @property
  def header_logo_url(self):
    """small logo in the header bar, instead of the name"""
    return settings.ORGANIZATION_LOGO_URL if self.is_default else self._config.get('header_logo_url', '')

  @property
  def footer_logo_url(self):
    """logo on the (dark) footer background"""
    return settings.FOOTER_LOGO_URL if self.is_default else self._config.get('footer_logo_url', '')

  @property
  def short_name_prefix(self):
    """prefix of the addresses of the organization's elections ('' for none)"""
    return self._config.get('short_name_prefix', '')

  # layout

  @property
  def site_title(self):
    if 'site_title' in self._config:
      return localize(self._config['site_title'])
    return localized_setting('SITE_TITLE') or ''

  @property
  def main_logo_url(self):
    """logo above the page content"""
    return self._layout('main_logo_url', lambda: settings.MAIN_LOGO_URL)

  @property
  def css_url(self):
    """stylesheet loaded after the default layout (usually sets CSS variables)"""
    return self._layout('css_url', lambda: settings.SITE_CSS_URL)

  @property
  def dark_mode(self):
    """
    'auto' or 'off'. Like DARK_MODE, an organization's own stylesheet turns it
    off unless dark_mode is given, as such a stylesheet usually sets light colors only.
    """
    if 'dark_mode' in self._config:
      return self._config['dark_mode']
    if self._config.get('css_url'):
      return 'off'
    return getattr(settings, 'DARK_MODE', 'off')

  @property
  def color_scheme_class(self):
    """class on <html> when the pages may follow the visitor's dark mode; layout.css keys its dark colors on it"""
    return 'color-scheme-auto' if self.dark_mode == 'auto' else ''

  # footer

  @property
  def imprint_url(self):
    return self._layout('imprint_url', lambda: settings.IMPRINT_URL)

  @property
  def privacy_url(self):
    return self._layout('privacy_url', lambda: settings.PRIVACY_URL)

  @property
  def accessibility_url(self):
    return self._layout('accessibility_url', lambda: settings.ACCESSIBILITY_URL)

  @property
  def footer_links(self):
    """[{'url': ..., 'text': ...}]"""
    if 'footer_links' in self._config:
      return [{'url': link['url'], 'text': localize(link['text'])} for link in self._config['footer_links']]
    return settings.FOOTER_LINKS

  @property
  def help_email(self):
    """the "Contact" address in the footer and on error messages"""
    return self._layout('help_email', lambda: settings.HELP_EMAIL_ADDRESS)

  # voting booth (helios/booth_theme.py)

  @property
  def booth_colors(self):
    """{'primary': '#rrggbb', ..., 'dark': {...}}, validated at start (settings.parse_booth_colors)"""
    return self._layout('colors', lambda: getattr(settings, 'BOOTH_COLORS', {}))

  @property
  def font_url(self):
    """a self-hosted web font for the booth's text (not for fingerprints and trackers)"""
    return self._layout('font_url', lambda: getattr(settings, 'BOOTH_FONT_URL', ''))

  @property
  def booth_logo_url(self):
    """the organization's own main logo, shown in the booth's header; never the operator's"""
    return '' if self.is_default else self._config.get('main_logo_url', '')

  # emails (see mail_context): the sender address stays DEFAULT_FROM_EMAIL

  def _mail(self, key):
    return self._config.get('mail', {}).get(key)

  @property
  def mail_from_name(self):
    """display name of the sender of the election's emails"""
    from_name = self._mail('from_name')
    return localize(from_name) if from_name else settings.DEFAULT_FROM_NAME

  @property
  def mail_reply_to(self):
    """Reply-To of the election's emails ('' for none)"""
    return self._mail('reply_to') or ''

  @property
  def mail_signature(self):
    """the signature after "-- " in the emails to voters and trustees"""
    signature = self._mail('signature')
    return localize(signature) if signature else (localized_setting('EMAIL_SIGNATURE') or 'Helios')

  @property
  def label(self):
    """how the organization is named in lists: its name, else its slug (or the site title for the default)"""
    return self.name or self.slug or self.site_title


def configured():
  """slug -> configuration of the organizations from ORGANIZATIONS_FILE (without the default)"""
  return getattr(settings, 'ORGANIZATIONS', None) or {}


def enabled():
  """whether organizations are configured at all; without them everything is the default organization"""
  return bool(configured())


def default():
  """the operator's organization, from the global settings"""
  return Organization()


def get(slug):
  """the organization with this slug; the default one for '' and for slugs no longer configured"""
  config = configured().get(slug or '')
  return Organization(slug, config) if config is not None else default()


def all_organizations():
  """the configured organizations, in the order of the file (without the default)"""
  return [Organization(slug, config) for slug, config in configured().items()]


def for_election(election):
  return get(getattr(election, 'organization', ''))


##
## emails in the context of an election
##

# The organization whose election an email is sent for. Set around sending (in the
# view or inside the background task, as tasks run without the request), and
# applied by helios.mail_backend.LoggingEmailBackend to every email sent with the
# default sender, so that the auth systems' send_message need not know about it.
_mail_organization = contextvars.ContextVar('helios_mail_organization', default=None)


@contextlib.contextmanager
def mail_context(election):
  """emails sent inside come from the election's organization (sender name, Reply-To)"""
  token = _mail_organization.set(for_election(election))
  try:
    yield
  finally:
    _mail_organization.reset(token)


def mail_organization():
  """the organization set by mail_context, else None"""
  return _mail_organization.get()


def apply_mail_identity(message, organization=None):
  """
  The email as the organization sends it: the display name of the sender
  ("<from_name>" <DEFAULT_FROM_EMAIL>) and its Reply-To. Only for emails sent with
  the default sender address, which stays the same for SPF and DKIM; others and
  emails of the default organization are returned unchanged. Names and addresses
  were validated at start (no line breaks); formataddr quotes and encodes the name.
  """
  organization = organization or mail_organization()
  if organization is None or organization.is_default:
    return message
  sender = parseaddr(str(message.from_email or ''))[1]
  if sender.lower() != settings.DEFAULT_FROM_EMAIL.lower():
    return message
  message = copy.copy(message)
  message.from_email = formataddr((organization.mail_from_name, settings.DEFAULT_FROM_EMAIL))
  if organization.mail_reply_to and not message.reply_to:
    message.reply_to = [organization.mail_reply_to]
  return message


def context_processor(request):
  """`ci` in every template rendered with a request; an election's pages for voters and trustees replace it (see view_utils)"""
  return {'ci': default()}


##
## people -> organization (and address prefix)
##

Match = namedtuple('Match', ['organization', 'prefix'])


def _domains(user):
  """email domains of a user: of the email address, then of a login ID such as jdoe@uni-a.example.org"""
  candidates = []
  info = getattr(user, 'info', None) or {}
  if isinstance(info, dict) and info.get('email'):
    candidates.append(info['email'])
  user_id = getattr(user, 'user_id', None) or ''
  if '@' in user_id:
    candidates.append(user_id)
  return [c.rsplit('@', 1)[1].strip().rstrip('.').lower() for c in candidates if '@' in c]


def _idps(user):
  """
  the identity provider a user logged in with, most specific first: the issuer
  (OIDC issuer or SAML IdP entity ID, stored at login since it was added), then
  the auth system name (user_type, e.g. "saml", "oidc", "ldap", "google")
  """
  keys = []
  info = getattr(user, 'info', None) or {}
  issuer = info.get('issuer') if isinstance(info, dict) else None
  if isinstance(issuer, str):
    # compared as in settings.idp_key
    keys.append(issuer.strip().rstrip('/'))
  user_type = getattr(user, 'user_type', None)
  if user_type:
    keys.append(user_type)
  return [key for key in keys if key]


def _domain_matches(domain, key):
  return domain == key or domain.endswith('.' + key)


def match_user(user):
  """
  The organization of a person and the prefix of their election addresses, as
  Match(organization slug or '', prefix or ''). In this order:

  1. their identity provider, first the issuer, then the auth system name:
     the "idps" of an organization, then ELECTION_SHORT_NAME_PREFIXES_BY_IDP;
  2. their email domain (subdomains included; the most specific entry wins,
     an organization's before an equally specific ELECTION_SHORT_NAME_PREFIXES entry):
     the "email_domains" of an organization and ELECTION_SHORT_NAME_PREFIXES;
  3. otherwise ELECTION_SHORT_NAME_PREFIX.

  An organization's rule gives the organization and its short_name_prefix. A
  rule of the ELECTION_SHORT_NAME_PREFIX* settings gives its prefix, and the
  organization whose short_name_prefix it is (else the default organization).
  """
  organizations = configured()
  by_prefix = {config['short_name_prefix']: slug for slug, config in organizations.items()
               if config.get('short_name_prefix')}

  def from_prefix(prefix):
    return Match(by_prefix.get(prefix, '') if prefix else '', prefix)

  def from_organization(slug):
    return Match(slug, organizations[slug].get('short_name_prefix', ''))

  if user:
    organization_idps = {idp: slug for slug, config in organizations.items() for idp in config['match']['idps']}
    prefix_idps = settings.ELECTION_SHORT_NAME_PREFIXES_BY_IDP
    for idp in _idps(user):
      if idp in organization_idps:
        return from_organization(organization_idps[idp])
      if idp in prefix_idps:
        return from_prefix(prefix_idps[idp])

    organization_domains = {domain: slug for slug, config in organizations.items()
                            for domain in config['match']['email_domains']}
    prefix_domains = settings.ELECTION_SHORT_NAME_PREFIXES
    for domain in _domains(user):
      candidates = ([(len(key), 1, key) for key in organization_domains if _domain_matches(domain, key)]
                    + [(len(key), 0, key) for key in prefix_domains if _domain_matches(domain, key)])
      if candidates:
        _length, from_organizations, key = max(candidates)
        if from_organizations:
          return from_organization(organization_domains[key])
        return from_prefix(prefix_domains[key])

  return from_prefix(settings.ELECTION_SHORT_NAME_PREFIX)


def organization_for_user(user):
  """the slug of a person's organization ('' for the default)"""
  return match_user(user).organization


def all_prefixes():
  """every configured address prefix"""
  return ({config['short_name_prefix'] for config in configured().values() if config.get('short_name_prefix')}
          | set(settings.ELECTION_SHORT_NAME_PREFIXES_BY_IDP.values()) | set(settings.ELECTION_SHORT_NAME_PREFIXES.values())
          | ({settings.ELECTION_SHORT_NAME_PREFIX} - {''}))


def featured_elections_for(user):
  """
  The featured elections on the start page. With organizations configured,
  people see those of their own organization only (the default organization
  also gets those of organizations no longer configured), and visitors who are
  not logged in none.
  """
  from helios.models import Election
  elections = Election.get_featured()
  if not enabled():
    return elections
  if not user:
    return elections.none()
  slug = organization_for_user(user)
  if slug:
    return elections.filter(organization=slug)
  return elections.exclude(organization__in=list(configured()))
