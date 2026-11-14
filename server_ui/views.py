"""
server_ui specific views
"""

import copy

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.template import TemplateDoesNotExist
from django.template.loader import get_template

import helios_auth.views as auth_views
from helios import booth_theme, organizations
from helios.models import Election
from helios.security import can_create_election
from helios_auth.security import get_user
from . import glue
from .view_utils import render_template

glue.glue()  # actually apply glue helios.view <-> helios.signals


def get_election():
  return None


def home(request):
  """
  The start page in the operator's CI. Opening it ends a visit through an
  organization's entry link (the entry cookie is removed).
  """
  clear_cookie = organizations.ENTRY_COOKIE_NAME in request.COOKIES
  if clear_cookie:
    organizations.set_entry_organization(request, None)
  response = _home(request)
  if clear_cookie:
    organizations.delete_entry_cookie(response)
  return response


def organization_home(request, slug):
  """
  The entry link /<slug>/ of an organization: its start page with its featured
  elections, and its CI on the following pages (entry cookie, see
  helios.organizations.entry_organization)
  """
  organization = organizations.get(slug)
  organizations.set_entry_organization(request, organization)
  response = _home(request, organization)
  organizations.set_entry_cookie(request, response, organization)
  return response


def _home(request, entry=None):
  user = get_user(request)

  # the featured elections: on an entry link those of its organization; else, with
  # organizations configured, those of the visitor's organization only, and none
  # for visitors who are not logged in
  featured_elections = organizations.featured_elections_for(user, entry)

  create_p = can_create_election(request)

  if create_p:
    elections_administered = Election.get_by_user_as_admin(user, archived_p=False, limit=5)
  else:
    elections_administered = None

  if user:
    elections_voted = Election.get_by_user_as_voter(user, archived_p=False, limit=5)
  else:
    elections_voted = None

  auth_systems = copy.copy(settings.AUTH_ENABLED_SYSTEMS)
  try:
    auth_systems.remove('password')
  except: pass

  login_box = auth_views.login_box_raw(request, return_url=organizations.home_url(request), auth_systems=auth_systems)

  return render_template(request, "index", {'elections': featured_elections,
                                            'elections_administered' : elections_administered,
                                            'elections_voted' : elections_voted,
                                            'create_p':create_p,
                                            'featured_by_organization_p': organizations.enabled() and entry is None,
                                            'login_box' : login_box})

def about(request):
  return render_template(request, "about")

def docs(request):
  return render_template(request, "docs")

def faq(request):
  return render_template(request, "faq")

# The operator's legal pages: always in the operator's CI, also for visitors who
# came through an organization's entry link (whose own pages are below).

def privacy(request):
  # the generic privacy notes mention the entry cookie when organizations are configured
  slugs = list(organizations.configured())
  return render_template(request, "privacy", {
    'ci': organizations.default(),
    'entry_link_example': '%s/%s/' % (settings.URL_HOST, slugs[0]) if slugs else '',
    'entry_cookie_days': organizations.ENTRY_COOKIE_MAX_AGE // 86400,
  })


def _site_page(request, name):
  # imprint and accessibility statement are site-specific: the installation
  # provides them in SITE_TEMPLATES_DIR (or links external pages instead)
  try:
    get_template('server_ui/templates/%s.html' % name)
  except TemplateDoesNotExist:
    raise Http404
  return render_template(request, name, {'ci': organizations.default()})

def imprint(request):
  return _site_page(request, "imprint")

def accessibility(request):
  return _site_page(request, "accessibility")


# the legal pages an organization can have on this server, see organization_page
ORGANIZATION_PAGES = ('imprint', 'privacy', 'accessibility')


def organization_page(request, slug, page):
  """
  /<slug>/imprint, /<slug>/privacy, /<slug>/accessibility: an organization's own
  legal page from SITE_TEMPLATES_DIR/organizations/<slug>/<page>.html, in its CI;
  "page not found" if the organization has none
  """
  if page not in ORGANIZATION_PAGES:
    raise Http404
  # slug: a configured slug (organizations.EntrySlugConverter), page: one of the names above
  template_name = 'organizations/%s/%s.html' % (slug, page)
  try:
    get_template(template_name)
  except TemplateDoesNotExist:
    raise Http404
  return render(request, template_name, {
    'user': get_user(request), 'settings': settings, 'CURRENT_URL': request.path,
    'ci': organizations.get(slug), 'organization_page': page,
  })


def booth_dark_mode_css(request):
  """
  The dark colors of the voting booth (heliosbooth/css/booth-dark.css) with
  DARK_MODE=auto, an empty stylesheet otherwise: the booth is a static page
  (usually served by the web server), so it gets the setting this way. The booth
  opened for an election loads that election's stylesheet instead
  (/helios/elections/<uuid>/booth-theme.css), which contains the same colors.
  """
  css = booth_theme.dark_mode_css(getattr(settings, 'DARK_MODE', 'off'))
  response = HttpResponse(css, content_type='text/css; charset=utf-8')
  # the setting can change with a restart; the file is small
  response['Cache-Control'] = 'no-cache'
  return response
