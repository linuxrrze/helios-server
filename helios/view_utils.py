"""
Utilities for all views

Ben Adida (12-30-2008)
"""

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.template import Context, loader
from django.utils.translation import gettext as _
# nicely update the wrapper function
from functools import update_wrapper

import helios
from . import organizations, utils
from helios_auth.models import User
from helios_auth.security import get_user

##
## BASICS
##

# A fresh response per request: a shared module-level HttpResponse would carry
# cookies and headers set by middleware for one request into the next one.
# FAILURE stays HTTP 200 because the admin and trustee pages check the body text.
def success():
  return HttpResponse("SUCCESS")

def failure():
  return HttpResponse("FAILURE")

##
## template abstraction
##
def prepare_vars(request, values):
  vars_with_user = values.copy() if values is not None else {}
  vars_with_user['user'] = get_user(request)

  # csrf protection
  if 'csrf_token' in request.session:
    vars_with_user['csrf_token'] = request.session['csrf_token']

  vars_with_user['utils'] = utils
  vars_with_user['settings'] = settings
  vars_with_user['HELIOS_STATIC'] = '/static/helios/helios'
  vars_with_user['TEMPLATE_BASE'] = helios.TEMPLATE_BASE
  vars_with_user['CURRENT_URL'] = request.path
  vars_with_user['SECURE_URL_HOST'] = settings.SECURE_URL_HOST

  return vars_with_user


# The pages of an election for voters and trustees, and its public pages, show the
# corporate identity (CI) of the election's organization (helios/organizations.py).
# The election's administration pages (edit, voters upload and emails, trustees and
# administrators management, freeze, tally) and all other pages keep the default CI.
# Opt-in per page: add a template here only if voters or trustees are meant to see it.
ELECTION_CI_TEMPLATES = frozenset([
  # public pages of the election (its administrators see them in the same CI)
  'election_view', 'election_questions', 'voters_list', 'list_trustees',
  'election_not_started', 'election_tallied', 'election_bboard', 'election_audited_ballots', 'castvote',
  # voters
  'password_voter_login', 'password_voter_resend', 'election_cast_confirm', 'cast_done',
  # trustees
  'trustee_home', 'trustee_check_sk', 'election_keygenerator', 'trustee_decrypt_and_prove',
])


def render_template(request, template_name, values = None, include_user=True):
  vars_with_user = prepare_vars(request, values)

  if template_name in ELECTION_CI_TEMPLATES and 'ci' not in vars_with_user:
    vars_with_user['ci'] = organizations.for_election(vars_with_user.get('election'))
    # the logo and "home" lead to the start page of the organization whose CI is shown
    vars_with_user.setdefault('home_url', organizations.organization_home_url(vars_with_user['ci']))

  if not include_user:
    del vars_with_user['user']

  return render(request, 'helios/templates/%s.html' % template_name, vars_with_user)


def render_template_raw(request, template_name, values=None):
  t = loader.get_template(template_name)

  # if there's a request, prep the vars, otherwise can't do it.
  if request:
    full_vars = prepare_vars(request, values)
  else:
    full_vars = values or {}

  return t.render(context=full_vars, request=request)


def render_text_template(template_name, values=None):
  """
  Render a plain-text template (email, notification) without HTML autoescaping,
  so names and URLs with &, ' or < come out as typed rather than as entities.

  `ci` is the organization of the election in VALUES (else of the surrounding
  organizations.mail_context, else the default): emails end with its signature.
  """
  values = dict(values or {})
  if 'ci' not in values:
    if values.get('election') is not None:
      values['ci'] = organizations.for_election(values['election'])
    else:
      values['ci'] = organizations.mail_organization() or organizations.default()
  t = loader.get_template(template_name)
  return t.template.render(Context(values, autoescape=False))


def render_json(json_txt):
  return HttpResponse(utils.to_json(json_txt), content_type="application/json")


# decorator
def return_json(func):
    """
    A decorator that serializes the output to JSON before returning to the
    web client.
    """
    def convert_to_json(self, *args, **kwargs):
      return_val = func(self, *args, **kwargs)
      # let a view return an error response (e.g. a 400) as-is
      if isinstance(return_val, HttpResponse):
        return return_val
      try:
        return render_json(return_val)
      except Exception as e:
        import logging
        logging.error("problem with serialization: " + str(return_val) + " / " + str(e))
        raise e

    return update_wrapper(convert_to_json,func)


##
## adding an administrator (elections, voter groups)
##

def find_user_to_add_as_admin(email, auth_type='', not_found=None):
  """
  Look up the user an administrator wants to add by the email they entered.
  Users are found by user_id, so they must have logged in to Helios at least
  once (or have been provisioned, e.g. by SCIM), and the same email can exist in several auth systems (google, ldap,
  ...); then the form asks which one is meant and sends it back as auth_type.

  NOT_FOUND replaces the error message for an unknown email.

  Returns (user, matching_users, error), exactly one of them set:
  the user to add, the users to choose from, or an error message for the form.
  Raises Http404 if the chosen auth_type has no such user.
  """
  email = (email or '').strip()
  if not email:
    return None, None, _('Please enter an email address.')

  # users deactivated by provisioning can't be added
  candidates = User.objects.filter(deactivated_at__isnull=True)
  auth_type = (auth_type or '').strip()
  if auth_type:
    try:
      return candidates.get(user_id=email, user_type=auth_type), None, None
    except User.DoesNotExist:
      raise Http404("User not found")

  matching_users = list(candidates.filter(user_id=email))
  if not matching_users:
    return None, None, not_found or _('No user found with that email. They must log in to Helios at least once before being added as an administrator.')
  if len(matching_users) > 1:
    return None, matching_users, None
  return matching_users[0], None, None
