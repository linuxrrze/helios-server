"""
Tests for the generic OIDC and SAML auth systems.
"""

import base64
import datetime
import json
import time
import unittest
import uuid
from unittest import mock
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.test import RequestFactory, TestCase, override_settings

from helios_auth.auth_systems.oidc import OIDCAuthSystem, SESSION_KEY as OIDC_SESSION_KEY
from helios_auth.utils import AuthenticationError

try:
  import onelogin.saml2  # noqa
  from helios_auth.auth_systems import saml
  HAVE_SAML = True
except ImportError:
  HAVE_SAML = False


def _rsa_key():
  return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Session(dict):
  modified = False


ISSUER = 'https://idp.example.org'
DISCOVERY = {
  'issuer': ISSUER,
  'authorization_endpoint': ISSUER + '/authorize',
  'token_endpoint': ISSUER + '/token',
  'jwks_uri': ISSUER + '/jwks',
  'id_token_signing_alg_values_supported': ['RS256'],
}


@override_settings(OIDC_DISCOVERY_URL=ISSUER + '/.well-known/openid-configuration',
                   OIDC_CLIENT_ID='helios', OIDC_CLIENT_SECRET='s3cret', OIDC_USER_ID_CLAIM='email',
                   OIDC_TRUST_UNVERIFIED_EMAIL=False, SECURE_URL_HOST='https://vote.example.org')
class OIDCTests(TestCase):

  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    cls.key = _rsa_key()
    cls.other_key = _rsa_key()

  def setUp(self):
    self.system = OIDCAuthSystem('campus')
    get = mock.patch('helios_auth.auth_systems.oidc.requests.get')
    self.requests_get = get.start()
    self.addCleanup(get.stop)
    self.requests_get.return_value.json.return_value = DISCOVERY
    self.requests_get.return_value.raise_for_status.return_value = None
    jwks = mock.patch('helios_auth.auth_systems.oidc.jwt.PyJWKClient')
    self.jwks_client = jwks.start()
    self.addCleanup(jwks.stop)
    self.signing_key = mock.Mock(key=self.key.public_key())
    self.jwks_client.return_value.get_signing_key_from_jwt.return_value = self.signing_key
    self.rf = RequestFactory()

  def _start(self):
    request = self.rf.get('/auth/start/campus')
    request.session = _Session()
    url = self.system.get_auth_url(request, 'https://vote.example.org/auth/after/')
    return request.session, parse_qs(urlparse(url).query)

  def _id_token(self, nonce, key=None, **overrides):
    now = int(time.time())
    claims = {'iss': ISSUER, 'aud': 'helios', 'sub': 'u123', 'iat': now, 'exp': now + 300, 'nonce': nonce,
              'email': 'Erika.Mustermann@example.org', 'email_verified': True, 'given_name': 'Erika', 'family_name': 'Mustermann'}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key or self.key, algorithm='RS256')

  def _callback(self, session, params, id_token):
    request = self.rf.get('/auth/after/', params)
    request.session = session
    with mock.patch('helios_auth.auth_systems.oidc.requests.post') as post:
      post.return_value.status_code = 200
      post.return_value.json.return_value = {'id_token': id_token, 'access_token': 'at'}
      result = self.system.get_user_info_after_auth(request)
      return result, post

  def test_auth_url_uses_pkce_state_and_nonce(self):
    session, query = self._start()
    pending = session[OIDC_SESSION_KEY]
    self.assertEqual(query['state'], [pending['state']])
    self.assertEqual(query['nonce'], [pending['nonce']])
    self.assertEqual(query['code_challenge_method'], ['S256'])
    self.assertEqual(query['client_id'], ['helios'])

  def test_successful_login(self):
    session, query = self._start()
    nonce = session[OIDC_SESSION_KEY]['nonce']
    verifier = session[OIDC_SESSION_KEY]['verifier']
    user, post = self._callback(session, {'code': 'c', 'state': query['state'][0]}, self._id_token(nonce))
    self.assertEqual(user['type'], 'campus')
    self.assertEqual(user['user_id'], 'Erika.Mustermann@example.org')  # provider case kept
    self.assertEqual(user['name'], 'Erika Mustermann')
    self.assertEqual(user['info']['email'], 'Erika.Mustermann@example.org')
    self.assertEqual(user['info']['issuer'], ISSUER)
    # PKCE: the verifier sent to the token endpoint matches the challenge sent to the IdP
    import hashlib
    sent = post.call_args.kwargs['data']['code_verifier']
    self.assertEqual(sent, verifier)
    self.assertEqual(base64.urlsafe_b64encode(hashlib.sha256(sent.encode()).digest()).rstrip(b'=').decode(),
                     query['code_challenge'][0])
    self.assertNotIn(OIDC_SESSION_KEY, session)  # single use
    self.assertTrue(self.system.CASE_INSENSITIVE_USER_ID)

  def test_state_mismatch_rejected(self):
    session, query = self._start()
    with self.assertRaises(AuthenticationError):
      self._callback(session, {'code': 'c', 'state': 'forged'}, self._id_token(session[OIDC_SESSION_KEY]['nonce']))

  def test_replayed_callback_rejected(self):
    session, query = self._start()
    nonce = session[OIDC_SESSION_KEY]['nonce']
    params = {'code': 'c', 'state': query['state'][0]}
    self._callback(session, params, self._id_token(nonce))
    with self.assertRaises(AuthenticationError):
      self._callback(session, params, self._id_token(nonce))

  def test_nonce_mismatch_rejected(self):
    session, query = self._start()
    with self.assertRaises(AuthenticationError):
      self._callback(session, {'code': 'c', 'state': query['state'][0]}, self._id_token('other-nonce'))

  def test_bad_signature_rejected(self):
    session, query = self._start()
    token = self._id_token(session[OIDC_SESSION_KEY]['nonce'], key=self.other_key)
    with self.assertRaises(jwt.InvalidSignatureError):
      self._callback(session, {'code': 'c', 'state': query['state'][0]}, token)

  def test_wrong_audience_and_issuer_rejected(self):
    for override, error in (({'aud': 'someone-else'}, jwt.InvalidAudienceError),
                            ({'iss': 'https://evil.example'}, jwt.InvalidIssuerError),
                            ({'exp': int(time.time()) - 3600}, jwt.ExpiredSignatureError)):
      with self.subTest(override=override):
        session, query = self._start()
        token = self._id_token(session[OIDC_SESSION_KEY]['nonce'], **override)
        with self.assertRaises(error):
          self._callback(session, {'code': 'c', 'state': query['state'][0]}, token)

  def test_unverified_email_refused_as_user_id(self):
    session, query = self._start()
    token = self._id_token(session[OIDC_SESSION_KEY]['nonce'], email_verified=None)
    with self.assertRaises(AuthenticationError):
      self._callback(session, {'code': 'c', 'state': query['state'][0]}, token)

  @override_settings(OIDC_USER_ID_CLAIM='sub')
  def test_sub_as_user_id_drops_unverified_email(self):
    session, query = self._start()
    token = self._id_token(session[OIDC_SESSION_KEY]['nonce'], email_verified=False)
    user, _ = self._callback(session, {'code': 'c', 'state': query['state'][0]}, token)
    self.assertEqual(user['user_id'], 'u123')
    self.assertNotIn('email', user['info'])

  def test_multi_audience_requires_azp(self):
    session, query = self._start()
    token = self._id_token(session[OIDC_SESSION_KEY]['nonce'], aud=['helios', 'other-app'])
    with self.assertRaises(AuthenticationError):
      self._callback(session, {'code': 'c', 'state': query['state'][0]}, token)
    session, query = self._start()
    token = self._id_token(session[OIDC_SESSION_KEY]['nonce'], aud=['helios', 'other-app'], azp='helios')
    user, _ = self._callback(session, {'code': 'c', 'state': query['state'][0]}, token)
    self.assertEqual(user['type'], 'campus')

  def test_hs256_token_rejected(self):
    # an HS256 token "signed" with the client secret must not be accepted
    session, query = self._start()
    now = int(time.time())
    token = jwt.encode({'iss': ISSUER, 'aud': 'helios', 'sub': 'x', 'iat': now, 'exp': now + 60,
                        'nonce': session[OIDC_SESSION_KEY]['nonce']}, 's3cret' * 6, algorithm='HS256')
    with self.assertRaises(jwt.PyJWTError):
      self._callback(session, {'code': 'c', 'state': query['state'][0]}, token)

  def test_provider_error_is_user_facing(self):
    session, _ = self._start()
    request = self.rf.get('/auth/after/', {'error': 'access_denied'})
    request.session = session
    with self.assertRaises(AuthenticationError):
      self.system.get_user_info_after_auth(request)

  def test_email_verified_as_string_accepted(self):
    # some providers send email_verified as the string "true"
    session, query = self._start()
    token = self._id_token(session[OIDC_SESSION_KEY]['nonce'], email_verified='true')
    user, _ = self._callback(session, {'code': 'c', 'state': query['state'][0]}, token)
    self.assertEqual(user['user_id'], 'Erika.Mustermann@example.org')
    session, query = self._start()
    token = self._id_token(session[OIDC_SESSION_KEY]['nonce'], email_verified='false')
    with self.assertRaises(AuthenticationError):
      self._callback(session, {'code': 'c', 'state': query['state'][0]}, token)

  def test_id_token_kept_only_for_provider_logout(self):
    session, query = self._start()
    token = self._id_token(session[OIDC_SESSION_KEY]['nonce'])
    user, _ = self._callback(session, {'code': 'c', 'state': query['state'][0]}, token)
    self.assertNotIn('id_token_hint', user)
    self.assertEqual(user['token'], {})
    with override_settings(OIDC_LOGOUT_AT_PROVIDER=True):
      session, query = self._start()
      token = self._id_token(session[OIDC_SESSION_KEY]['nonce'])
      user, _ = self._callback(session, {'code': 'c', 'state': query['state'][0]}, token)
    self.assertEqual(user['id_token_hint'], token)
    self.assertEqual(user['token'], {})  # what goes to the database

  def test_logout_stays_local_by_default(self):
    self.requests_get.return_value.json.return_value = dict(DISCOVERY, end_session_endpoint=ISSUER + '/logout')
    self.assertIsNone(self.system.do_logout({'type': 'campus', 'id_token_hint': 'tok'}, return_url='/x'))

  @override_settings(OIDC_LOGOUT_AT_PROVIDER=True)
  def test_logout_at_provider(self):
    self.requests_get.return_value.json.return_value = dict(DISCOVERY, end_session_endpoint=ISSUER + '/logout')
    response = self.system.do_logout({'type': 'campus', 'id_token_hint': 'tok'}, return_url='/helios/elections/abc/view')
    url = urlparse(response['Location'])
    self.assertEqual(url.scheme + '://' + url.netloc + url.path, ISSUER + '/logout')
    query = parse_qs(url.query)
    self.assertEqual(query['post_logout_redirect_uri'], ['https://vote.example.org/auth/logout/done'])
    self.assertEqual(query['state'], ['/helios/elections/abc/view'])
    self.assertEqual(query['id_token_hint'], ['tok'])
    self.assertEqual(query['client_id'], ['helios'])

  @override_settings(OIDC_LOGOUT_AT_PROVIDER=True)
  def test_logout_without_end_session_endpoint_stays_local(self):
    self.assertIsNone(self.system.do_logout({'type': 'campus'}, return_url='/'))
    self.requests_get.side_effect = OSError('IdP down')
    self.system._discovery = None
    self.assertIsNone(self.system.do_logout({'type': 'campus'}, return_url='/'))


@override_settings(OIDC_DISCOVERY_URL=ISSUER + '/.well-known/openid-configuration',
                   OIDC_CLIENT_ID='helios', OIDC_CLIENT_SECRET='s3cret', OIDC_USER_ID_CLAIM='email',
                   OIDC_TRUST_UNVERIFIED_EMAIL=False, SECURE_URL_HOST='https://vote.example.org')
class OIDCEndToEndTests(TestCase):
  """Login and logout through Helios' own views, with OIDC registered as "campus"."""

  def setUp(self):
    from helios_auth import views as auth_views
    from helios_auth.auth_systems import AUTH_SYSTEMS
    self.system = OIDCAuthSystem('campus')
    patcher = mock.patch.dict(AUTH_SYSTEMS, {'campus': self.system})
    patcher.start()
    self.addCleanup(patcher.stop)
    enabled = mock.patch.object(auth_views, 'ENABLED_AUTH_SYSTEMS', list(auth_views.ENABLED_AUTH_SYSTEMS) + ['campus'])
    enabled.start()
    self.addCleanup(enabled.stop)
    get = mock.patch('helios_auth.auth_systems.oidc.requests.get')
    self.requests_get = get.start()
    self.addCleanup(get.stop)
    self.requests_get.return_value.json.return_value = dict(DISCOVERY, end_session_endpoint=ISSUER + '/logout')
    self.requests_get.return_value.raise_for_status.return_value = None

  def _login(self):
    """Run the login through the views, with the ID token step stubbed out."""
    response = self.client.get('/auth/start/campus', {'return_url': '/helios/'})
    self.assertEqual(response.status_code, 302)
    state = parse_qs(urlparse(response['Location']).query)['state'][0]
    user = {'type': 'campus', 'user_id': 'erika@example.org', 'name': 'Erika', 'info': {'email': 'erika@example.org'}, 'token': {}}
    with mock.patch('helios_auth.auth_systems.oidc.requests.post') as post, \
         mock.patch.object(OIDCAuthSystem, '_verify_id_token', return_value={}), \
         mock.patch.object(OIDCAuthSystem, '_user_from_claims', return_value=dict(user)):
      post.return_value.status_code = 200
      post.return_value.json.return_value = {'id_token': 'the-id-token'}
      return self.client.get('/auth/after/', {'code': 'c', 'state': state})

  def test_login_gets_a_new_session_key(self):
    self.client.get('/auth/start/campus')  # creates the session
    session_key_before = self.client.session.session_key
    self.assertTrue(session_key_before)
    response = self._login()
    self.assertEqual(response.status_code, 302)
    self.assertEqual(self.client.session['user']['user_id'], 'erika@example.org')
    self.assertNotEqual(self.client.session.session_key, session_key_before)

  def test_unreachable_provider_shows_error_page(self):
    self.requests_get.side_effect = OSError('IdP down')
    response = self.client.get('/auth/start/campus', {'return_url': '/helios/'})
    self.assertEqual(response.status_code, 400)
    self.assertNotIn('user', self.client.session)

  @override_settings(OIDC_LOGOUT_AT_PROVIDER=True)
  def test_logout_round_trip_through_provider(self):
    self._login()
    self.assertEqual(self.client.session['user']['id_token_hint'], 'the-id-token')
    response = self.client.get('/auth/logout', {'return_url': '/helios/elections/abc/view'})
    self.assertNotIn('user', self.client.session)
    query = parse_qs(urlparse(response['Location']).query)
    self.assertTrue(response['Location'].startswith(ISSUER + '/logout?'))
    self.assertEqual(query['id_token_hint'], ['the-id-token'])
    # the provider sends the user back with the state
    response = self.client.get('/auth/logout/done', {'state': query['state'][0]})
    self.assertEqual(response['Location'], '/helios/elections/abc/view')

  def test_logout_done_only_redirects_on_site(self):
    for state in ('https://evil.example/', '//evil.example/x', ''):
      with self.subTest(state=state):
        response = self.client.get('/auth/logout/done', {'state': state})
        self.assertEqual(response['Location'], '/')

  def test_logout_of_unconfigured_auth_system(self):
    # e.g. a session from SAML ("saml") after switching to OIDC
    session = self.client.session
    session['user'] = {'type': 'no-longer-configured', 'user_id': 'x'}
    session.save()
    response = self.client.get('/auth/logout', {'return_url': '/helios/'})
    self.assertEqual(response.status_code, 302)
    self.assertEqual(response['Location'], '/helios/')
    self.assertNotIn('user', self.client.session)


# --- SAML ---

IDP_ENTITY = 'https://idp.example.org/saml'
SP_ACS = 'https://vote.example.org/saml2_auth/acs/'
SP_ENTITY = 'https://vote.example.org/saml2_auth/acs/'
MAIL = 'urn:mace:dir:attribute-def:mail'


def _self_signed(key, cn):
  name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
  now = datetime.datetime.now(datetime.timezone.utc)
  cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
  return cert.public_bytes(serialization.Encoding.PEM).decode()


def _pem_body(pem):
  return "".join(l for l in pem.splitlines() if not l.startswith('-----'))


@unittest.skipUnless(HAVE_SAML, "python3-saml not installed (uv sync --extra saml)")
class _SAMLBase(TestCase):
  """IdP key pair, metadata and a builder for signed responses."""

  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    cls.idp_key = _rsa_key()
    cls.idp_cert = _self_signed(cls.idp_key, 'idp')
    cls.idp_key_pem = cls.idp_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption()).decode()
    cls.metadata = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" xmlns:ds="http://www.w3.org/2000/09/xmldsig#" entityID="%s">
  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data><ds:X509Certificate>%s</ds:X509Certificate></ds:X509Data></ds:KeyInfo></md:KeyDescriptor>
    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" Location="https://idp.example.org/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>""" % (IDP_ENTITY, _pem_body(cls.idp_cert))

  def setUp(self):
    import tempfile, os
    fd, self.metadata_file = tempfile.mkstemp(suffix='.xml')
    with os.fdopen(fd, 'w') as f:
      f.write(self.metadata)
    self.addCleanup(os.unlink, self.metadata_file)
    self.settings_override = override_settings(
      SAML_IDP_METADATA_FILE=self.metadata_file, SAML_IDP_METADATA_URL='', SAML_SP_ENTITY_ID=SP_ENTITY,
      SAML_SP_ACS_URL=SP_ACS, SAML_ATTRIBUTE_USER_ID=MAIL, SAML_ATTRIBUTE_EMAIL=MAIL,
      SAML_ATTRIBUTE_GIVEN_NAME='urn:mace:dir:attribute-def:givenName',
      SAML_ATTRIBUTE_SURNAME='urn:mace:dir:attribute-def:sn', SECURE_URL_HOST='https://vote.example.org')
    self.settings_override.enable()
    self.addCleanup(self.settings_override.disable)
    self.system = saml.SAMLAuthSystem('campus')
    self.rf = RequestFactory()

  def _start(self):
    request = self.rf.get('/auth/start/campus')
    request.session = _Session()
    url = self.system.get_auth_url(request, 'unused')
    self.assertTrue(url.startswith('https://idp.example.org/sso?SAMLRequest='))
    return request.session

  def _response(self, in_response_to, sign=True, audience=SP_ENTITY, mail='Erika.Mustermann@example.org',
                not_on_or_after_minutes=5, destination=SP_ACS):
    from onelogin.saml2.utils import OneLogin_Saml2_Utils
    now = datetime.datetime.now(datetime.timezone.utc)
    fmt = lambda d: d.strftime('%Y-%m-%dT%H:%M:%SZ')
    later = fmt(now + datetime.timedelta(minutes=not_on_or_after_minutes))
    aid = '_' + uuid.uuid4().hex
    irt_attr = ' InResponseTo="%s"' % in_response_to if in_response_to else ''
    assertion = """<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="%(aid)s" Version="2.0" IssueInstant="%(now)s">
<saml:Issuer>%(idp)s</saml:Issuer>
<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:2.0:nameid-format:transient">_t123</saml:NameID>
<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><saml:SubjectConfirmationData NotOnOrAfter="%(later)s" Recipient="%(acs)s"%(irt_attr)s/></saml:SubjectConfirmation></saml:Subject>
<saml:Conditions NotBefore="%(before)s" NotOnOrAfter="%(later)s"><saml:AudienceRestriction><saml:Audience>%(aud)s</saml:Audience></saml:AudienceRestriction></saml:Conditions>
<saml:AuthnStatement AuthnInstant="%(now)s" SessionIndex="_s1"><saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:Password</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>
<saml:AttributeStatement>
<saml:Attribute Name="%(mail_attr)s"><saml:AttributeValue>%(mail)s</saml:AttributeValue></saml:Attribute>
<saml:Attribute Name="urn:mace:dir:attribute-def:givenName"><saml:AttributeValue>Erika</saml:AttributeValue></saml:Attribute>
<saml:Attribute Name="urn:mace:dir:attribute-def:sn"><saml:AttributeValue>Mustermann</saml:AttributeValue></saml:Attribute>
</saml:AttributeStatement></saml:Assertion>""" % dict(
      aid=aid, now=fmt(now), before=fmt(now - datetime.timedelta(minutes=1)), later=later, idp=IDP_ENTITY,
      acs=SP_ACS, irt_attr=irt_attr, aud=audience, mail=mail, mail_attr=MAIL)
    if sign:
      assertion = OneLogin_Saml2_Utils.add_sign(assertion, self.idp_key_pem, self.idp_cert,
        sign_algorithm='http://www.w3.org/2001/04/xmldsig-more#rsa-sha256',
        digest_algorithm='http://www.w3.org/2001/04/xmlenc#sha256')
      assertion = assertion.decode() if isinstance(assertion, bytes) else assertion
      assertion = assertion.split('?>', 1)[-1].strip() if assertion.startswith('<?xml') else assertion
    response = """<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_%(rid)s" Version="2.0" IssueInstant="%(now)s" Destination="%(dest)s"%(irt_attr)s>
<saml:Issuer>%(idp)s</saml:Issuer><samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>%(assertion)s</samlp:Response>""" % dict(
      rid=uuid.uuid4().hex, now=fmt(now), dest=destination, irt_attr=irt_attr, idp=IDP_ENTITY, assertion=assertion)
    return base64.b64encode(response.encode()).decode()

  def _after(self, session, saml_response):
    request = self.rf.post('/auth/after/', {'SAMLResponse': saml_response})
    request.session = session
    return self.system.get_user_info_after_auth(request)


@unittest.skipUnless(HAVE_SAML, "python3-saml not installed (uv sync --extra saml)")
class SAMLTests(_SAMLBase):

  def test_successful_login(self):
    session = self._start()
    user = self._after(session, self._response(session[saml.SESSION_KEY]))
    self.assertEqual(user['type'], 'campus')
    self.assertEqual(user['user_id'], 'Erika.Mustermann@example.org')
    self.assertEqual(user['name'], 'Erika Mustermann')
    self.assertEqual(user['info']['email'], 'Erika.Mustermann@example.org')
    self.assertEqual(user['info']['issuer'], IDP_ENTITY)
    self.assertTrue(self.system.CASE_INSENSITIVE_USER_ID)

  def test_unsigned_assertion_rejected(self):
    session = self._start()
    with self.assertRaises(AuthenticationError):
      self._after(session, self._response(session[saml.SESSION_KEY], sign=False))

  def test_tampered_assertion_rejected(self):
    session = self._start()
    xml = base64.b64decode(self._response(session[saml.SESSION_KEY])).decode()
    xml = xml.replace('Erika.Mustermann@example.org', 'admin@example.org')
    with self.assertRaises(AuthenticationError):
      self._after(session, base64.b64encode(xml.encode()).decode())

  def test_response_for_other_request_rejected(self):
    session = self._start()
    with self.assertRaises(AuthenticationError):
      self._after(session, self._response('_some-other-request'))

  def test_replay_rejected(self):
    session = self._start()
    response = self._response(session[saml.SESSION_KEY])
    self._after(session, response)
    with self.assertRaises(AuthenticationError):
      self._after(session, response)

  def test_response_without_started_login_rejected(self):
    session = _Session()  # no sign-in was started in this session
    with self.assertRaises(AuthenticationError):
      self._after(session, self._response('_x'))

  def test_unsolicited_response_rejected_even_with_pending_request(self):
    # a validly signed IdP-initiated response (no InResponseTo) must not complete
    # a sign-in that the victim's browser was made to start (login CSRF)
    session = self._start()
    with self.assertRaises(AuthenticationError):
      self._after(session, self._response(None))

  def test_response_cannot_be_replayed_into_another_session(self):
    session = self._start()
    response = self._response(session[saml.SESSION_KEY])
    self._after(session, response)
    other = self._start()
    with self.assertRaises(AuthenticationError):
      self._after(other, response)

  def test_federation_metadata_requires_entity_id(self):
    second = self.metadata.replace(IDP_ENTITY, 'https://other-idp.example/saml').split('?>', 1)[-1]
    federation = ('<md:EntitiesDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata">%s%s</md:EntitiesDescriptor>'
                  % (self.metadata.split('?>', 1)[-1], second))
    with open(self.metadata_file, 'w') as f:
      f.write(federation)
    with self.assertRaises(ValueError):
      saml.SAMLAuthSystem('campus').get_auth_url(self._session_request(), 'unused')
    with override_settings(SAML_IDP_ENTITY_ID=IDP_ENTITY):
      self.assertTrue(saml.SAMLAuthSystem('campus').get_auth_url(self._session_request(), 'unused'))

  def _session_request(self):
    request = self.rf.get('/auth/start/campus')
    request.session = _Session()
    return request

  def test_wrong_audience_destination_or_expired_rejected(self):
    for kwargs in ({'audience': 'https://other-sp.example'}, {'destination': 'https://other-sp.example/acs'},
                   {'not_on_or_after_minutes': -10}):
      with self.subTest(**kwargs):
        session = self._start()
        with self.assertRaises(AuthenticationError):
          self._after(session, self._response(session[saml.SESSION_KEY], **kwargs))

  def test_acs_bounces_same_site_with_escaping(self):
    response = saml.acs(self.rf.post('/saml2_auth/acs/', {'SAMLResponse': 'abc"><script>x</script>'}))
    self.assertEqual(response.status_code, 200)
    body = response.content.decode()
    self.assertIn('action="https://vote.example.org/auth/after/"', body)
    self.assertNotIn('<script>x', body)
    self.assertEqual(response['Cache-Control'], 'no-store')

  def test_acs_requires_post(self):
    self.assertEqual(saml.acs(self.rf.get('/saml2_auth/acs/')).status_code, 405)

  def test_sp_metadata(self):
    response = saml.metadata(self.rf.get('/auth/saml/metadata'))
    self.assertEqual(response.status_code, 200, response.content)
    self.assertIn(SP_ACS.encode(), response.content)
    self.assertIn(SP_ENTITY.encode(), response.content)


@unittest.skipUnless(HAVE_SAML, "python3-saml not installed (uv sync --extra saml)")
class SAMLEndToEndTests(_SAMLBase):
  """Full browser flow through Helios' own views, with SAML registered as "campus"."""

  def setUp(self):
    super().setUp()
    from helios_auth import views as auth_views
    from helios_auth.auth_systems import AUTH_SYSTEMS
    patcher = mock.patch.dict(AUTH_SYSTEMS, {'campus': self.system})
    patcher.start()
    self.addCleanup(patcher.stop)
    enabled = mock.patch.object(auth_views, 'ENABLED_AUTH_SYSTEMS', list(auth_views.ENABLED_AUTH_SYSTEMS) + ['campus'])
    enabled.start()
    self.addCleanup(enabled.stop)

  def test_login_flow_matches_existing_fau_user(self):
    from helios_auth.models import User
    # a user created by the 2020 fork's SAML integration, with different case
    existing = User.objects.create(user_type='campus', user_id='Erika.MUSTERMANN@example.org', name='E M', info={'email': 'x'})

    response = self.client.get('/auth/start/campus', {'return_url': '/helios/elections/administered'})
    self.assertEqual(response.status_code, 302)
    self.assertTrue(response['Location'].startswith('https://idp.example.org/sso?SAMLRequest='))
    request_id = self.client.session[saml.SESSION_KEY]

    # the IdP posts cross-site to the ACS, which bounces the response same-site
    bounce = saml.acs(self.rf.post('/saml2_auth/acs/', {'SAMLResponse': self._response(request_id)}))
    self.assertEqual(bounce.status_code, 200)
    import re
    posted = re.search(r'name="SAMLResponse" value="([^"]+)"', bounce.content.decode()).group(1)

    response = self.client.post('/auth/after/', {'SAMLResponse': posted})
    self.assertEqual(response.status_code, 302, response.content[:500])
    self.assertEqual(self.client.session['user']['type'], 'campus')
    self.assertEqual(User.objects.filter(user_type='campus').count(), 1)
    existing.refresh_from_db()
    self.assertEqual(existing.info['email'], 'Erika.Mustermann@example.org')

  def test_after_without_started_login_does_not_log_in(self):
    response = self.client.post('/auth/after/', {'SAMLResponse': self._response('_x')})
    self.assertNotIn('user', self.client.session)
