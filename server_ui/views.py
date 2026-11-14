"""
server_ui specific views
"""

import copy

from django.conf import settings
from django.http import Http404, HttpResponse
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
  user = get_user(request)

  # the featured elections: with organizations configured, those of the
  # visitor's organization only, and none for visitors who are not logged in
  featured_elections = organizations.featured_elections_for(user)

  create_p = can_create_election(request)

  if create_p:
    elections_administered = Election.get_by_user_as_admin(user, archived_p=False, limit=5)
  else:
    elections_administered = None

  if user:
    elections_voted = Election.get_by_user_as_voter(user, limit=5)
  else:
    elections_voted = None
 
  auth_systems = copy.copy(settings.AUTH_ENABLED_SYSTEMS)
  try:
    auth_systems.remove('password')
  except: pass

  login_box = auth_views.login_box_raw(request, return_url="/", auth_systems=auth_systems)

  return render_template(request, "index", {'elections': featured_elections,
                                            'elections_administered' : elections_administered,
                                            'elections_voted' : elections_voted,
                                            'create_p':create_p,
                                            'featured_by_organization_p': organizations.enabled(),
                                            'login_box' : login_box})
  
def about(request):
  return render_template(request, "about")

def docs(request):
  return render_template(request, "docs")

def faq(request):
  return render_template(request, "faq")

def privacy(request):
  return render_template(request, "privacy")
    

def _site_page(request, name):
  # imprint and accessibility statement are site-specific: the installation
  # provides them in SITE_TEMPLATES_DIR (or links external pages instead)
  try:
    get_template('server_ui/templates/%s.html' % name)
  except TemplateDoesNotExist:
    raise Http404
  return render_template(request, name)

def imprint(request):
  return _site_page(request, "imprint")

def accessibility(request):
  return _site_page(request, "accessibility")


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
