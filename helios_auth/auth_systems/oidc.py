"""
Generic OpenID Connect authentication (authorization code flow with PKCE).

Works with any OIDC provider that publishes a discovery document. Settings:

  OIDC_DISCOVERY_URL     e.g. https://idp.example.org/.well-known/openid-configuration
  OIDC_CLIENT_ID
  OIDC_CLIENT_SECRET
  OIDC_SCOPES            default "openid email profile"
  OIDC_USER_ID_CLAIM     claim used as the Helios user id, default "sub".
                         Use "email" to keep the same identities as a previous
                         auth system that used email addresses (e.g. SAML).
  OIDC_TRUST_UNVERIFIED_EMAIL
                         when OIDC_USER_ID_CLAIM is "email", sign-in is refused
                         unless the provider asserts email_verified=true. Set to
                         True only for an institutional IdP whose email
                         addresses are authoritative.
  OIDC_LOGIN_MESSAGE     text on the login button (OIDC_LOGIN_MESSAGE_DE etc. per language)
  OIDC_LOGOUT_AT_PROVIDER
                         on logout, also end the session at the provider
                         (RP-initiated logout via end_session_endpoint), so the
                         next person at a shared computer is not signed in
                         automatically. Register <SECURE_URL_HOST>/auth/logout/done
                         as post-logout redirect URI with the provider.

The ID token is verified against the provider's published keys (signature,
issuer, audience, expiry, nonce). Access and refresh tokens are not stored; with
OIDC_LOGOUT_AT_PROVIDER the ID token is kept in the session (not the database)
as id_token_hint for the logout request.
"""

import base64
import hashlib
import logging
import secrets
import threading
import time
from urllib.parse import urlencode, quote

import jwt
import requests
from django.conf import settings
from django.core.mail import send_mail
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext as _

from helios_auth.utils import AuthenticationError, format_recipient, localized_setting

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 10
DISCOVERY_CACHE_SECONDS = 3600
# asymmetric algorithms only: HS* would make the client secret a signing key
ALLOWED_ALGORITHMS = ['RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512', 'ES256', 'ES384', 'ES512', 'EdDSA']
SESSION_KEY = 'oidc_auth'


def _endpoint_url(endpoint, params):
  return endpoint + ('&' if '?' in endpoint else '?') + urlencode(params)


def _is_true(value):
  # email_verified is a boolean in the spec, but some providers send "true"
  return value is True or (isinstance(value, str) and value.lower() == 'true')


class OIDCAuthSystem:
  STATUS_UPDATES = False
  LOGOUT_TAKES_RETURN_URL = True

  def __init__(self, name='oidc'):
    self.name = name
    self._lock = threading.Lock()
    self._discovery = None
    self._discovery_fetched_at = 0
    self._jwks_client = None

  @property
  def LOGIN_MESSAGE(self):
    return localized_setting('OIDC_LOGIN_MESSAGE') or _('Log in with single sign-on')

  @property
  def CASE_INSENSITIVE_USER_ID(self):
    return self._user_id_claim() == 'email'

  def _user_id_claim(self):
    return getattr(settings, 'OIDC_USER_ID_CLAIM', 'sub') or 'sub'

  # --- provider metadata ---

  def discovery(self):
    with self._lock:
      if self._discovery is None or time.time() - self._discovery_fetched_at > DISCOVERY_CACHE_SECONDS:
        response = requests.get(settings.OIDC_DISCOVERY_URL, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        doc = response.json()
        for key in ('issuer', 'authorization_endpoint', 'token_endpoint', 'jwks_uri'):
          if not doc.get(key):
            raise ValueError("OIDC discovery document lacks %s" % key)
        self._discovery = doc
        self._discovery_fetched_at = time.time()
        self._jwks_client = jwt.PyJWKClient(doc['jwks_uri'], cache_keys=True, timeout=HTTP_TIMEOUT)
      return self._discovery

  # --- auth system interface ---

  def get_auth_url(self, request, redirect_url):
    doc = self.discovery()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    request.session[SESSION_KEY] = {'state': state, 'nonce': nonce, 'verifier': verifier, 'redirect_uri': redirect_url}
    params = {
      'response_type': 'code',
      'client_id': settings.OIDC_CLIENT_ID,
      'redirect_uri': redirect_url,
      'scope': getattr(settings, 'OIDC_SCOPES', 'openid email profile'),
      'state': state,
      'nonce': nonce,
      'code_challenge': challenge,
      'code_challenge_method': 'S256',
    }
    return _endpoint_url(doc['authorization_endpoint'], params)

  def get_user_info_after_auth(self, request):
    pending = request.session.pop(SESSION_KEY, None)

    if 'error' in request.GET:
      logger.warning("OIDC provider returned error %r: %r", request.GET.get('error'), request.GET.get('error_description'))
      raise AuthenticationError(_("The sign-in was cancelled or refused by the identity provider. Please try again."))
    if 'code' not in request.GET:
      return None
    if not pending or not secrets.compare_digest(pending['state'], request.GET.get('state', '')):
      raise AuthenticationError(_("This sign-in link has expired or was already used. Please start the sign-in again."))

    doc = self.discovery()
    # RFC 6749 2.3.1: client credentials are form-urlencoded before HTTP Basic encoding
    response = requests.post(doc['token_endpoint'], timeout=HTTP_TIMEOUT,
      auth=(quote(settings.OIDC_CLIENT_ID, safe=''), quote(settings.OIDC_CLIENT_SECRET, safe='')),
      data={
        'grant_type': 'authorization_code',
        'code': request.GET['code'],
        'redirect_uri': pending['redirect_uri'],
        'code_verifier': pending['verifier'],
      })
    if response.status_code != 200:
      logger.warning("OIDC token endpoint returned %s: %s", response.status_code, response.text[:500])
      raise AuthenticationError(_("The identity provider did not accept the sign-in. Please try again."))
    id_token = response.json().get('id_token')
    if not id_token:
      raise AuthenticationError(_("The identity provider did not return an ID token."))

    claims = self._verify_id_token(id_token, pending['nonce'])
    user = self._user_from_claims(claims)
    if getattr(settings, 'OIDC_LOGOUT_AT_PROVIDER', False):
      # helios_auth.views.after puts this dict into the session; only 'token'
      # and 'info' go to the database
      user['id_token_hint'] = id_token
    return user

  def _verify_id_token(self, id_token, nonce):
    doc = self.discovery()
    signing_key = self._jwks_client.get_signing_key_from_jwt(id_token)
    algorithms = [a for a in doc.get('id_token_signing_alg_values_supported', ['RS256']) if a in ALLOWED_ALGORITHMS]
    claims = jwt.decode(id_token, key=signing_key.key, algorithms=algorithms,
                        audience=settings.OIDC_CLIENT_ID, issuer=doc['issuer'], leeway=60,
                        options={'require': ['exp', 'iat', 'iss', 'aud', 'sub']})
    aud = claims['aud']
    if (isinstance(aud, list) and len(aud) > 1 or 'azp' in claims) and claims.get('azp') != settings.OIDC_CLIENT_ID:
      raise AuthenticationError(_("The sign-in token was issued for a different application."))
    if not secrets.compare_digest(str(claims.get('nonce', '')), nonce):
      raise AuthenticationError(_("The sign-in response did not match this sign-in attempt. Please try again."))
    return claims

  def _user_from_claims(self, claims):
    id_claim = self._user_id_claim()
    email = claims.get('email')
    if (id_claim == 'email' or email) and not _is_true(claims.get('email_verified')) \
        and not getattr(settings, 'OIDC_TRUST_UNVERIFIED_EMAIL', False):
      if id_claim == 'email':
        raise AuthenticationError(_("Your identity provider did not confirm your email address, which is needed to sign in."))
      email = None  # don't send election mail to an unverified address

    user_id = claims.get(id_claim)
    if not user_id:
      raise AuthenticationError(_("Your identity provider did not send the attribute needed to sign in (%(attribute)s).") % {'attribute': id_claim})
    # keep the provider's case: lookups are case-insensitive for email ids, and
    # older voters' published hashes are derived from the stored user_id
    user_id = str(user_id)

    name = claims.get('name') or " ".join(p for p in (claims.get('given_name'), claims.get('family_name')) if p) or user_id
    # issuer: verified against the discovery document; identifies the IdP even if
    # the auth system name is kept across a change of provider (election address prefixes)
    info = {'name': name, 'issuer': claims['iss']}
    if email:
      info['email'] = email
    return {'type': self.name, 'user_id': user_id, 'name': name, 'info': info, 'token': {}}

  def do_logout(self, user, return_url='/'):
    """
    Called after the local logout. With OIDC_LOGOUT_AT_PROVIDER, send the user
    to the provider's end_session_endpoint, which returns to /auth/logout/done
    and from there to return_url (carried in state).
    """
    if not getattr(settings, 'OIDC_LOGOUT_AT_PROVIDER', False):
      return None
    try:
      endpoint = self.discovery().get('end_session_endpoint')
    except Exception:
      logger.warning("OIDC discovery failed during logout, logging out locally only", exc_info=True)
      return None
    if not endpoint:
      logger.warning("OIDC_LOGOUT_AT_PROVIDER is set but the provider has no end_session_endpoint")
      return None

    from helios_auth.url_names import AUTH_LOGOUT_DONE
    params = {
      'client_id': settings.OIDC_CLIENT_ID,
      'post_logout_redirect_uri': settings.SECURE_URL_HOST + reverse(AUTH_LOGOUT_DONE),
      'state': return_url,
    }
    id_token_hint = (user or {}).get('id_token_hint')
    if id_token_hint:
      params['id_token_hint'] = id_token_hint
    return HttpResponseRedirect(_endpoint_url(endpoint, params))

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
