"""
Generic Security -- for the auth system

Ben Adida (ben@adida.net)
"""

import uuid
from django.conf import settings
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.http import HttpResponseRedirect
# nicely update the wrapper function
from functools import update_wrapper

from helios_auth.models import User

FIELDS_TO_SAVE = 'FIELDS_TO_SAVE'

# decorator for login required
def login_required(func):
  def login_required_wrapper(request, *args, **kw):
    if not get_user(request):
      return HttpResponseRedirect(settings.LOGIN_URL + "?return_url=" + request.get_full_path())
  
    return func(request, *args, **kw)

  return update_wrapper(login_required_wrapper, func)
  
# decorator for admin required
def admin_required(func):
  def admin_required_wrapper(request, *args, **kw):
    user = get_user(request)
    if not user or not user.is_staff:
      raise PermissionDenied()
      
    return func(request, *args, **kw)

  return update_wrapper(admin_required_wrapper, func)

# get the user
def get_user(request):
  # push the expiration of the session back
  # request.session.set_expiry(settings.SESSION_COOKIE_AGE)
  
  # set up CSRF protection if needed
  if 'csrf_token' not in request.session or not isinstance(request.session['csrf_token'], str):
    request.session['csrf_token'] = str(uuid.uuid4())

  if 'user' in request.session:
    user = request.session['user']

    # find the user
    user_obj = User.get_by_type_and_id(user['type'], user['user_id'])
    return user_obj
  else:
    return None  

def check_csrf(request):
  """
  Raises SuspiciousOperation (which Django turns into a 400) when the request is
  not a CSRF-protected POST. Both checks must raise rather than return: callers
  invoke this as a bare statement, so a returned response would be discarded and
  the view would keep going as if the check had passed.
  """
  if request.method != "POST":
    raise SuspiciousOperation("only a POST for this URL")

  if ('csrf_token' not in request.POST) or (request.POST['csrf_token'] != request.session.get('csrf_token')):
    raise SuspiciousOperation("A CSRF problem was detected")

def save_in_session_across_logouts(request, field_name, field_value):
  fields_to_save = request.session.get(FIELDS_TO_SAVE, [])
  if field_name not in fields_to_save:
    fields_to_save.append(field_name)
    request.session[FIELDS_TO_SAVE] = fields_to_save

  request.session[field_name] = field_value
