"""
Generic SAML 2.0 authentication (SP-initiated, HTTP-Redirect / HTTP-POST),
built on python3-saml. Install with the "saml" extra: uv sync --extra saml

Settings:

  SAML_IDP_METADATA_URL   IdP metadata URL (fetched over HTTPS, cached), or
  SAML_IDP_METADATA_FILE  path to a local copy of the IdP metadata
  SAML_IDP_ENTITY_ID      pick one IdP if the metadata lists several
  SAML_IDP_X509_CERT      optional: pin the IdP signing certificate instead
                          of trusting the one in the metadata
  SAML_SP_ENTITY_ID       default <SECURE_URL_HOST>/auth/saml/metadata
  SAML_SP_ACS_URL         default <SECURE_URL_HOST>/auth/saml/acs
  SAML_SP_X509_CERT, SAML_SP_PRIVATE_KEY
                          optional SP key pair (PEM) to sign requests and
                          decrypt encrypted assertions
  SAML_ATTRIBUTE_USER_ID  attribute used as the Helios user id; "nameid"
                          means the NameID. Default "nameid".
  SAML_ATTRIBUTE_EMAIL, SAML_ATTRIBUTE_NAME,
  SAML_ATTRIBUTE_GIVEN_NAME, SAML_ATTRIBUTE_SURNAME
  SAML_LOGIN_MESSAGE      text on the login button (SAML_LOGIN_MESSAGE_DE etc. per language)

The IdP POSTs its response cross-site to the ACS URL, where the Lax session
cookie is not sent. The ACS view therefore only bounces the response, with a
same-site auto-submitting form, to Helios' normal /auth/after/ callback. There
the session is available and the response is fully validated (signature,
audience, destination, validity window and InResponseTo against the request id
stored in the session, which also makes each response single-use).
"""

import logging
import threading
import time
from urllib.parse import urlparse

from django.conf import settings
from django.core.mail import send_mail
from django.http import HttpResponse, HttpResponseBadRequest
from django.urls import reverse
from django.utils.html import escape
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from helios_auth.utils import AuthenticationError, format_recipient, localized_setting

logger = logging.getLogger(__name__)

METADATA_CACHE_SECONDS = 3600
SESSION_KEY = 'saml_request_id'


def _setting(name, default=None):
  return getattr(settings, name, default) or default


def sp_entity_id():
  return _setting('SAML_SP_ENTITY_ID', settings.SECURE_URL_HOST + '/auth/saml/metadata')


def sp_acs_url():
  return _setting('SAML_SP_ACS_URL', settings.SECURE_URL_HOST + '/auth/saml/acs')


class SAMLAuthSystem:
  STATUS_UPDATES = False

  def __init__(self, name='saml'):
    self.name = name
    self._lock = threading.Lock()
    self._idp = None
    self._idp_fetched_at = 0

  @property
  def LOGIN_MESSAGE(self):
    return localized_setting('SAML_LOGIN_MESSAGE') or _('Log in with single sign-on')

  @property
  def CASE_INSENSITIVE_USER_ID(self):
    # email addresses are case-insensitive in practice
    return _setting('SAML_ATTRIBUTE_USER_ID', 'nameid') == _setting('SAML_ATTRIBUTE_EMAIL')

  # --- configuration ---

  def _idp_settings(self):
    from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser as Parser
    with self._lock:
      if self._idp is None or time.time() - self._idp_fetched_at > METADATA_CACHE_SECONDS:
        entity_id = _setting('SAML_IDP_ENTITY_ID')
        if _setting('SAML_IDP_METADATA_FILE'):
          with open(settings.SAML_IDP_METADATA_FILE) as f:
            xml = f.read()
        else:
          xml = Parser.get_metadata(settings.SAML_IDP_METADATA_URL, timeout=10)
        if not entity_id:
          # without an explicit choice, python3-saml silently takes the first IdP,
          # which in federation metadata would trust an arbitrary IdP
          from onelogin.saml2.xml_utils import OneLogin_Saml2_XML
          idps = OneLogin_Saml2_XML.query(OneLogin_Saml2_XML.to_etree(xml), '//md:EntityDescriptor[md:IDPSSODescriptor]')
          if len(idps) != 1:
            raise ValueError("SAML IdP metadata lists %d IdPs; set SAML_IDP_ENTITY_ID" % len(idps))
        idp = Parser.parse(xml, entity_id=entity_id)
        pinned = _setting('SAML_IDP_X509_CERT')
        if pinned:
          idp['idp'].pop('x509certMulti', None)
          idp['idp']['x509cert'] = pinned
        self._idp = idp
        self._idp_fetched_at = time.time()
      return self._idp

  def saml_settings(self, with_idp=True):
    signing = bool(_setting('SAML_SP_PRIVATE_KEY'))
    base = {
      'strict': True,
      'debug': settings.DEBUG,
      'sp': {
        'entityId': sp_entity_id(),
        'assertionConsumerService': {
          'url': sp_acs_url(),
          'binding': 'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST',
        },
        'NameIDFormat': 'urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified',
        'x509cert': _setting('SAML_SP_X509_CERT', ''),
        'privateKey': _setting('SAML_SP_PRIVATE_KEY', ''),
      },
      'security': {
        'authnRequestsSigned': signing,
        'wantAssertionsSigned': True,
        'wantMessagesSigned': False,
        'wantNameId': _setting('SAML_ATTRIBUTE_USER_ID', 'nameid') == 'nameid',
        'rejectUnsolicitedResponsesWithInResponseTo': True,
        'signatureAlgorithm': 'http://www.w3.org/2001/04/xmldsig-more#rsa-sha256',
        'digestAlgorithm': 'http://www.w3.org/2001/04/xmlenc#sha256',
        'rejectDeprecatedAlgorithm': True,
      },
    }
    if not with_idp:
      base['idp'] = {'entityId': 'unused', 'singleSignOnService': {'url': 'https://unused.invalid/'}, 'x509cert': ''}
      return base
    from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser as Parser
    return Parser.merge_settings(base, self._idp_settings())

  def _request_data(self, request, post_data=None):
    # describe the request as the IdP addressed it (the ACS URL), so that the
    # Destination check works even though we process it at /auth/after/
    acs = urlparse(sp_acs_url())
    return {
      'https': 'on' if acs.scheme == 'https' else 'off',
      'http_host': acs.hostname,
      'server_port': acs.port or (443 if acs.scheme == 'https' else 80),
      'script_name': '',
      'path_info': acs.path,
      'get_data': {},
      'post_data': post_data or {},
    }

  def _auth(self, request, post_data=None):
    from onelogin.saml2.auth import OneLogin_Saml2_Auth
    return OneLogin_Saml2_Auth(self._request_data(request, post_data), old_settings=self.saml_settings())

  # --- auth system interface ---

  def get_auth_url(self, request, redirect_url):
    auth = self._auth(request)
    url = auth.login()
    request.session[SESSION_KEY] = auth.get_last_request_id()
    return url

  def get_user_info_after_auth(self, request):
    request_id = request.session.pop(SESSION_KEY, None)
    saml_response = request.POST.get('SAMLResponse')
    if not saml_response:
      return None
    if not request_id:
      raise AuthenticationError(_("This sign-in has expired or was already used. Please start the sign-in again."))

    auth = self._auth(request, {'SAMLResponse': saml_response})
    auth.process_response(request_id=request_id)
    if auth.get_errors() or not auth.is_authenticated():
      logger.warning("SAML response rejected: %s (%s)", auth.get_errors(), auth.get_last_error_reason())
      raise AuthenticationError(_("The sign-in response could not be verified. Please try again."))
    # python3-saml only compares InResponseTo when the response carries one, so an
    # IdP-initiated (unsolicited) response would be accepted by any session with a
    # pending request: login CSRF and cross-session replay. Require it explicitly.
    if auth.get_last_response_in_response_to() != request_id:
      logger.warning("SAML response rejected: InResponseTo %r does not match the pending request",
                     auth.get_last_response_in_response_to())
      raise AuthenticationError(_("The sign-in response did not match this sign-in attempt. Please try again."))

    attrs = auth.get_attributes()

    def first(attr_setting, default_name=None):
      name = _setting(attr_setting, default_name)
      if not name:
        return None
      if name == 'nameid':
        return auth.get_nameid()
      values = attrs.get(name) or []
      return values[0] if values else None

    user_id = first('SAML_ATTRIBUTE_USER_ID', 'nameid')
    if not user_id:
      raise AuthenticationError(_("Your identity provider did not send the attribute needed to sign in."))
    email = first('SAML_ATTRIBUTE_EMAIL')
    name = first('SAML_ATTRIBUTE_NAME') or " ".join(
      p for p in (first('SAML_ATTRIBUTE_GIVEN_NAME'), first('SAML_ATTRIBUTE_SURNAME')) if p) or user_id

    # the IdP's entity ID (strict validation has checked the response's Issuer
    # against it); used e.g. for election address prefixes
    info = {'name': name, 'issuer': auth.get_settings().get_idp_data()['entityId']}
    if email:
      info['email'] = email
    return {'type': self.name, 'user_id': user_id, 'name': name, 'info': info, 'token': {}}

  def do_logout(self, user):
    return None

  def update_status(self, token, message):
    pass

  def send_message(self, user_id, name, user_info, subject, body):
    email = (user_info or {}).get('email')
    if not email:
      logger.warning("not emailing %s user: no email address on file", self.name)
      return
    send_mail(subject, body, settings.SERVER_EMAIL, [format_recipient(name, email)], fail_silently=False)

  def check_constraint(self, eligibility, user_info):
    pass

  def can_create_election(self, user_id, user_info):
    return True


# --- views ---

@csrf_exempt
@require_POST
def acs(request):
  """Bounce the IdP's cross-site POST to /auth/after/ as a same-site POST."""
  saml_response = request.POST.get('SAMLResponse')
  if not saml_response:
    return HttpResponseBadRequest("missing SAMLResponse")
  after = settings.SECURE_URL_HOST + reverse('auth@after')
  html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>%s</title></head>
<body onload="document.forms[0].submit()">
<form method="POST" action="%s">
<input type="hidden" name="SAMLResponse" value="%s">
<noscript><p>%s</p><input type="submit" value="%s"></noscript>
</form></body></html>""" % (escape(_("Signing in…")), escape(after), escape(saml_response),
                            escape(_("JavaScript is disabled. Please press the button to continue.")), escape(_("Continue")))
  response = HttpResponse(html)
  response['Cache-Control'] = 'no-store'
  return response


def metadata(request):
  """SP metadata, to register this site with the IdP."""
  from onelogin.saml2.settings import OneLogin_Saml2_Settings
  saml_settings = OneLogin_Saml2_Settings(SAMLAuthSystem().saml_settings(with_idp=False), sp_validation_only=True)
  xml = saml_settings.get_sp_metadata()
  errors = saml_settings.validate_metadata(xml)
  if errors:
    return HttpResponse("invalid SP metadata: %s" % ", ".join(errors), status=500, content_type='text/plain')
  return HttpResponse(xml, content_type='application/samlmetadata+xml')
