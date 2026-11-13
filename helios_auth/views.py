"""
Views for authentication

Ben Adida
2009-07-05
"""

import logging
from urllib.parse import urlencode

from django.http import HttpResponseRedirect, HttpResponse
from django.urls import reverse
from django.utils.translation import gettext as _

import settings
from helios_auth import DEFAULT_AUTH_SYSTEM, ENABLED_AUTH_SYSTEMS
from helios_auth.security import get_user
from helios_auth.url_names import AUTH_INDEX, AUTH_START, AUTH_AFTER, AUTH_WHY, AUTH_AFTER_INTERVENTION
from .auth_systems import AUTH_SYSTEMS, password
from .models import User
from .security import FIELDS_TO_SAVE
from .utils import AuthenticationError, same_site_path
from .view_utils import render_template, render_template_raw

logger = logging.getLogger(__name__)


def auth_options(auth_systems):
  """(system name, button text) for each login option in login_box.html; password has its own form"""
  return [(s, getattr(AUTH_SYSTEMS[s], 'LOGIN_MESSAGE', None) or s) for s in auth_systems
          if s != 'password' and s in AUTH_SYSTEMS]

def index(request):
  """
  the page from which one chooses how to log in.
  """
  
  user = get_user(request)

  # single auth system?
  if len(ENABLED_AUTH_SYSTEMS) == 1 and not user:
    return HttpResponseRedirect(reverse(AUTH_START, args=[ENABLED_AUTH_SYSTEMS[0]])+ '?return_url=' + request.GET.get('return_url', ''))

  #if DEFAULT_AUTH_SYSTEM and not user:
  #  return HttpResponseRedirect(reverse(start, args=[DEFAULT_AUTH_SYSTEM])+ '?return_url=' + request.GET.get('return_url', ''))
  
  default_auth_system_obj = None
  if DEFAULT_AUTH_SYSTEM:
    default_auth_system_obj = AUTH_SYSTEMS[DEFAULT_AUTH_SYSTEM]

  #form = password.LoginForm()

  return render_template(request, 'index', {'return_url' : request.GET.get('return_url', '/'),
                                            'enabled_auth_systems' : ENABLED_AUTH_SYSTEMS,
                                            'auth_options': auth_options(ENABLED_AUTH_SYSTEMS),
                                            'default_auth_system': DEFAULT_AUTH_SYSTEM,
                                            'default_auth_system_obj': default_auth_system_obj})

def login_box_raw(request, return_url='/', auth_systems = None, use_default_auth_system=True):
  """
  a chunk of HTML that shows the various login options

  use_default_auth_system=False lists every system in auth_systems even when
  AUTH_DEFAULT_SYSTEM is set, e.g. for an election whose voters use other systems.
  """
  default_auth_system = DEFAULT_AUTH_SYSTEM if use_default_auth_system else None
  default_auth_system_obj = None
  if default_auth_system:
    default_auth_system_obj = AUTH_SYSTEMS[default_auth_system]

  # make sure that auth_systems includes only available and enabled auth systems
  if auth_systems is None:
    auth_systems = ENABLED_AUTH_SYSTEMS
  enabled_auth_systems = [s for s in auth_systems if s in ENABLED_AUTH_SYSTEMS and s in AUTH_SYSTEMS]

  form = password.LoginForm()

  return render_template_raw(request, 'login_box', {
      'enabled_auth_systems': enabled_auth_systems, 'auth_options': auth_options(enabled_auth_systems), 'return_url': return_url,
      'default_auth_system': default_auth_system, 'default_auth_system_obj': default_auth_system_obj,
      'form' : form})
  
def do_local_logout(request):
  """
  if there is a logged-in user, it is saved in the new session's "user_for_remote_logout"
  variable.
  """

  user = None

  if 'user' in request.session:
    user = request.session['user']
    
  # 2010-08-14 be much more aggressive here
  # we save a few fields across session renewals,
  # but we definitely kill the session and renew
  # the cookie
  field_names_to_save = request.session.get(FIELDS_TO_SAVE, [])

  # let's clean up the self-referential issue:
  field_names_to_save = set(field_names_to_save)
  field_names_to_save = field_names_to_save - {FIELDS_TO_SAVE}
  field_names_to_save = list(field_names_to_save)

  fields_to_save = dict([(name, request.session.get(name, None)) for name in field_names_to_save])

  # let's not forget to save the list of fields to save
  fields_to_save[FIELDS_TO_SAVE] = field_names_to_save

  request.session.flush()

  for name in field_names_to_save:
    request.session[name] = fields_to_save[name]

  # copy the list of fields to save
  request.session[FIELDS_TO_SAVE] = fields_to_save[FIELDS_TO_SAVE]

  request.session['user_for_remote_logout'] = user

def do_remote_logout(request, user, return_url="/"):
  # the session may belong to an auth system that is no longer configured,
  # e.g. after switching from SAML to OIDC under another name
  auth_system = AUTH_SYSTEMS.get(user['type'])

  # does the auth system have a special logout procedure?
  user_for_remote_logout = request.session.get('user_for_remote_logout', None)
  del request.session['user_for_remote_logout']
  if auth_system is None or not hasattr(auth_system, 'do_logout'):
    return None
  # systems that log out at the provider bring the user back to return_url
  if getattr(auth_system, 'LOGOUT_TAKES_RETURN_URL', False):
    return auth_system.do_logout(user_for_remote_logout, return_url=return_url)
  return auth_system.do_logout(user_for_remote_logout)

def do_complete_logout(request, return_url="/"):
  do_local_logout(request)
  user_for_remote_logout = request.session.get('user_for_remote_logout', None)
  if user_for_remote_logout:
    response = do_remote_logout(request, user_for_remote_logout, return_url)
    return response
  return None
  
def logout(request):
  """
  logout
  """

  return_url = same_site_path(request.GET.get('return_url'))
  response = do_complete_logout(request, return_url)
  if response:
    return response
  
  return HttpResponseRedirect(return_url)

def logout_done(request):
  """
  Where an identity provider sends the user back after logging out there
  (OIDC post_logout_redirect_uri). The page to return to comes back in state.
  """
  return HttpResponseRedirect(same_site_path(request.GET.get('state')))

def _do_auth(request):
  # the session has the system name
  system_name = request.session['auth_system_name']

  # get the system
  system = AUTH_SYSTEMS[system_name]
  
  # where to send the user to?
  redirect_url = settings.SECURE_URL_HOST + reverse(AUTH_AFTER)
  try:
    auth_url = system.get_auth_url(request, redirect_url=redirect_url)
  except Exception:
    # e.g. the identity provider's discovery document can't be fetched
    logger.warning("could not start login with auth system %s", system_name, exc_info=True)
    return _login_failed(request, error_message=_("The sign-in service cannot be reached right now. Please try again in a few minutes."))

  if auth_url:
    return HttpResponseRedirect(auth_url)
  else:
    return HttpResponse(_("an error occurred trying to contact %(system_name)s, try again later") % {'system_name': system_name})
  
def start(request, system_name):
  if not (system_name in ENABLED_AUTH_SYSTEMS):
    return HttpResponseRedirect(reverse(AUTH_INDEX))
  
  # why is this here? Let's try without it
  # request.session.save()
  
  # store in the session the name of the system used for auth
  request.session['auth_system_name'] = system_name
  
  # where to return to when done
  request.session['auth_return_url'] = same_site_path(request.GET.get('return_url'))

  return _do_auth(request)

def perms_why(request):
  if request.method == "GET":
    return render_template(request, "perms_why")

  return _do_auth(request)

def _login_failed(request, error_message=None, help_url=None):
  response = render_template(request, 'login_failed', {
    'system_name': request.session['auth_system_name'],
    'return_url': request.session.get('auth_return_url') or request.session.get('auth_last_return_url', '/'),
    'error_message': error_message,
    'help_url': help_url,
  })
  response.status_code = 400
  return response

def after(request):
  # which auth system were we using?
  if 'auth_system_name' not in request.session:
    do_local_logout(request)
    return HttpResponseRedirect("/")
    
  system = AUTH_SYSTEMS[request.session['auth_system_name']]
  
  # get the user info. This fails when the callback is replayed (back button,
  # refresh, double click), since the OAuth state was already used up, or when
  # the provider rejects the code. Show a "try again" page rather than a 500.
  try:
    user = system.get_user_info_after_auth(request)
  except AuthenticationError as e:
    logger.warning("login callback rejected for auth system %s: %s", request.session['auth_system_name'], e.message, exc_info=True)
    return _login_failed(request, error_message=e.message, help_url=e.help_url)
  except Exception:
    logger.warning("login callback failed for auth system %s", request.session['auth_system_name'], exc_info=True)
    return _login_failed(request)

  if user:
    # get the user and store any new data about him
    user_obj = User.update_or_create(user['type'], user['user_id'], user['name'], user['info'], user['token'])

    # new session key on login (session fixation); the session data, e.g. a
    # ballot waiting to be cast, is kept
    request.session.cycle_key()
    request.session['user'] = user
  else:
    return HttpResponseRedirect("%s?%s" % (reverse(AUTH_WHY), urlencode({'system_name' : request.session['auth_system_name']})))

  # does the auth system want to present an additional view?
  # this is, for example, to prompt the user to follow @heliosvoting
  # so they can hear about election results
  if hasattr(system, 'user_needs_intervention'):
    intervention_response = system.user_needs_intervention(user['user_id'], user['info'], user['token'])
    if intervention_response:
      return intervention_response

  # go to the after intervention page. This is for modularity
  return HttpResponseRedirect(reverse(AUTH_AFTER_INTERVENTION))

def after_intervention(request):
  return_url = "/"
  if 'auth_return_url' in request.session:
    return_url = same_site_path(request.session['auth_return_url'])
    del request.session['auth_return_url']
    # kept so a callback replayed after sign-in (back button) can still offer the way back
    request.session['auth_last_return_url'] = return_url
  return HttpResponseRedirect(settings.URL_HOST + return_url)

