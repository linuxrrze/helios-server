"""
Tests for the corporate identity (CI) of organizations (helios/organizations.py).
"""

import datetime
import io
import json
import os
import re
import runpy
import tempfile
import uuid
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import translation

import settings as project_settings
from helios import organizations, short_names
from django.template.loader import get_template

from helios.models import Election, Trustee
from helios.security import HELIOS_TRUSTEE_UUID
from helios.view_utils import ELECTION_CI_TEMPLATES
from helios_auth.models import User


class DefaultOrganizationTests(TestCase):
  """Without configuration, the CI is the global settings: the pages look as before."""

  def test_default_reads_the_settings(self):
    with self.settings(SITE_TITLE='Example Elections', ORGANIZATION_NAME='Example Operator',
                       ORGANIZATION_URL='https://operator.example.org', ORGANIZATION_LOGO_URL='/static/site/op.svg',
                       FOOTER_LOGO_URL='/static/site/op-white.svg', MAIN_LOGO_URL='/static/site/main.svg',
                       SITE_CSS_URL='/static/site/site.css', DARK_MODE='off', IMPRINT_URL='/imprint',
                       PRIVACY_URL='/privacy', ACCESSIBILITY_URL='', HELP_EMAIL_ADDRESS='help@example.org',
                       FOOTER_LINKS=[{'url': 'https://help.example.org', 'text': 'Help desk'}]):
      ci = organizations.default()
      self.assertTrue(ci.is_default)
      self.assertEqual(ci.slug, '')
      self.assertEqual(ci.site_title, 'Example Elections')
      self.assertEqual(ci.name, 'Example Operator')
      self.assertEqual(ci.url, 'https://operator.example.org')
      self.assertEqual(ci.header_logo_url, '/static/site/op.svg')
      self.assertEqual(ci.footer_logo_url, '/static/site/op-white.svg')
      self.assertEqual(ci.main_logo_url, '/static/site/main.svg')
      self.assertEqual(ci.css_url, '/static/site/site.css')
      self.assertEqual(ci.color_scheme_class, '')
      self.assertEqual(ci.imprint_url, '/imprint')
      self.assertEqual(ci.privacy_url, '/privacy')
      self.assertEqual(ci.accessibility_url, '')
      self.assertEqual(ci.help_email, 'help@example.org')
      self.assertEqual(ci.footer_links, [{'url': 'https://help.example.org', 'text': 'Help desk'}])
    with self.settings(DARK_MODE='auto'):
      self.assertEqual(organizations.default().color_scheme_class, 'color-scheme-auto')

  def test_default_texts_follow_the_language(self):
    with self.settings(SITE_TITLE='Elections', LOCALIZED_SETTINGS={'SITE_TITLE': {'de': 'Wahlen'}}):
      self.assertContains(self.client.get('/', HTTP_ACCEPT_LANGUAGE='de'), '<title>Wahlen</title>')
      self.assertContains(self.client.get('/', HTTP_ACCEPT_LANGUAGE='en'), '<title>Elections</title>')

  def test_pages_show_the_settings(self):
    with self.settings(SITE_TITLE='Example Elections', MAIN_LOGO_URL='/static/site/main.svg',
                       HELP_EMAIL_ADDRESS='help@example.org'):
      for path in ['/', '/about', '/privacy', '/auth/password/login']:
        with self.subTest(path=path):
          response = self.client.get(path)
          self.assertContains(response, 'Example Elections')
          self.assertContains(response, '<img src="/static/site/main.svg" class="helios-logo" alt="Example Elections">')
          self.assertContains(response, 'href="mailto:help@example.org"')

  def test_error_pages_without_context(self):
    from django.template.loader import get_template
    with self.settings(SITE_TITLE='Example Elections'):
      self.assertIn('Example Elections', get_template('500.html').render())

  def test_templates_read_the_ci_not_the_settings(self):
    """Theming values come from `ci`, so that an election's pages can show its organization's CI."""
    names = ('SITE_TITLE', 'SITE_CSS_URL', 'MAIN_LOGO_URL', 'ORGANIZATION_NAME', 'ORGANIZATION_URL',
             'ORGANIZATION_LOGO_URL', 'FOOTER_LOGO_URL', 'IMPRINT_URL', 'PRIVACY_URL', 'ACCESSIBILITY_URL',
             'FOOTER_LINKS', 'HELP_EMAIL_ADDRESS', 'DARK_MODE')
    pattern = re.compile(r'settings\.(%s)\b|localized_setting "(%s)"' % ('|'.join(names), '|'.join(names)))
    offenders = []
    for top in ('helios', 'helios_auth', 'server_ui', 'templates', 'helios_scim'):
      for root, dirs, files in os.walk(os.path.join(settings.ROOT_PATH, top)):
        for name in files:
          if name.endswith('.html'):
            path = os.path.join(root, name)
            with open(path, encoding='utf-8') as f:
              if pattern.search(f.read()):
                offenders.append(os.path.relpath(path, settings.ROOT_PATH))
    self.assertEqual(offenders, [])


##
## phase 1: organizations per election
##

def _config(**organizations_):
  return json.dumps({'organizations': organizations_})


UNIA = {
  'name': {'en': 'University A', 'de': 'Universität A'},
  'url': 'https://www.uni-a.example.org',
  'short_name_prefix': 'unia',
  'match': {'idps': ['https://idp.uni-a.example.org/'], 'email_domains': ['uni-a.example.org']},
  'site_title': {'en': 'University A elections', 'de': 'Wahlen der Universität A'},
  'css_url': '/static/site/unia/site.css',
  'main_logo_url': '/static/site/unia/logo.svg',
  'header_logo_url': '/static/site/unia/logo-small.svg',
  'footer_logo_url': '/static/site/unia/logo-white.svg',
  'imprint_url': 'https://www.uni-a.example.org/imprint',
  'privacy_url': 'https://www.uni-a.example.org/privacy',
  'accessibility_url': '',
  'footer_links': [{'url': 'https://www.uni-a.example.org/electoral-office', 'text': {'en': 'Electoral office', 'de': 'Wahlamt'}}],
  'help_email': 'elections@uni-a.example.org',
}
UNIB = {'name': 'University B', 'short_name_prefix': 'unib', 'match': {'idps': ['saml']}}
ORGS = project_settings.parse_organizations(_config(unia=UNIA, unib=UNIB))


def _user(email=None, user_id='jdoe', user_type='password', issuer=None):
  info = {'email': email} if email else {}
  if issuer is not None:
    info['issuer'] = issuer
  return SimpleNamespace(info=info, user_id=user_id, user_type=user_type)


class ConfigurationTests(TestCase):

  def test_empty(self):
    self.assertEqual(project_settings.parse_organizations(''), {})
    self.assertEqual(project_settings.load_organizations_file(''), {})
    self.assertEqual(settings.ORGANIZATIONS, {})
    self.assertFalse(organizations.enabled())

  def test_normalized(self):
    orgs = project_settings.parse_organizations(_config(
      unia=UNIA, unib={'name': ' University B ', 'match': {'idps': [' saml '], 'email_domains': ['@Uni-B.example.org.']}}))
    self.assertEqual(list(orgs), ['unia', 'unib'])
    self.assertEqual(orgs['unia']['name'], {'en': 'University A', 'de': 'Universität A'})
    self.assertEqual(orgs['unia']['match'], {'idps': ['https://idp.uni-a.example.org'], 'email_domains': ['uni-a.example.org']})
    self.assertEqual(orgs['unib'], {'name': {'': 'University B'},
                                    'match': {'idps': ['saml'], 'email_domains': ['uni-b.example.org']}})

  def test_invalid(self):
    for config, message in [
        ('{"organizations": ', 'not valid JSON'),
        ('[]', 'expected a JSON object'),
        ('{"orgs": {}}', 'expected a JSON object'),
        (_config(**{'Uni A': {'name': 'A'}}), 'is not a valid organization slug'),
        (_config(**{'-unia': {'name': 'A'}}), 'is not a valid organization slug'),
        (_config(unia='University A'), 'must be a JSON object'),
        (_config(unia={'url': 'https://a.example.org'}), 'needs a "name"'),
        (_config(unia={'name': 'A', 'colour': '#fff'}), 'unknown key(s) colour'),
        (_config(unia={'name': ''}), '"name" of "unia" must be a text'),
        (_config(unia={'name': {'fr': 'A'}}), '"name" of "unia" must be a text'),
        (_config(unia={'name': 'A', 'url': 'javascript:alert(1)'}), 'must be an http(s) URL'),
        (_config(unia={'name': 'A', 'css_url': 'https://cdn.example.net/a.css'}), 'must be a path on this site'),
        (_config(unia={'name': 'A', 'css_url': '//cdn.example.net/a.css'}), 'must be a path on this site'),
        (_config(unia={'name': 'A', 'main_logo_url': ''}), 'must be a path on this site'),
        (_config(unia={'name': 'A', 'header_logo_url': '/static/site/a.svg" onload="x'}), 'must be a path on this site'),
        (_config(unia={'name': 'A', 'imprint_url': 'javascript:alert(1)'}), 'must be an http(s) URL or a path'),
        (_config(unia={'name': 'A', 'footer_links': [{'url': 'https://a.example.org'}]}), 'must be a list of'),
        (_config(unia={'name': 'A', 'footer_links': [{'url': '', 'text': 'x'}]}), 'must be an http(s) URL or a path'),
        (_config(unia={'name': 'A', 'help_email': 'help'}), 'must be an email address'),
        (_config(unia={'name': 'A', 'dark_mode': 'on'}), 'must be one of auto, off'),
        (_config(unia={'name': 'A', 'short_name_prefix': 'Uni-A'}), 'is not a valid prefix'),
        (_config(unia={'name': 'A', 'match': {'issuers': []}}), 'must be an object with "idps"'),
        (_config(unia={'name': 'A', 'match': {'idps': 'saml'}}), 'must be a list of texts'),
        (_config(unia={'name': 'A', 'match': {'idps': ['my saml']}}), 'is not an auth system name or issuer'),
        (_config(unia={'name': 'A', 'match': {'email_domains': ['jane@a.example.org']}}), 'is not an email domain'),
        (_config(unia={'name': 'A', 'short_name_prefix': 'uni'}, unib={'name': 'B', 'short_name_prefix': 'uni'}),
         "the prefix 'uni' is given for both \"unia\" and \"unib\""),
        (_config(unia={'name': 'A', 'match': {'idps': ['saml']}}, unib={'name': 'B', 'match': {'idps': ['saml/']}}),
         'the identity provider'),
        (_config(unia={'name': 'A', 'match': {'email_domains': ['example.org']}},
                 unib={'name': 'B', 'match': {'email_domains': ['Example.org']}}), 'the email domain'),
    ]:
      with self.subTest(config=config):
        with self.assertRaisesMessage(ImproperlyConfigured, message):
          project_settings.parse_organizations(config)

  def test_documented_example_is_valid(self):
    with open(os.path.join(settings.ROOT_PATH, 'docs', 'organizations.md'), encoding='utf-8') as f:
      example = re.search(r'```json\n(.*?)```', f.read(), re.S).group(1)
    orgs = project_settings.parse_organizations(example)
    self.assertEqual(list(orgs), ['unia', 'unib'])
    with override_settings(ORGANIZATIONS=orgs):
      self.assertEqual(organizations.match_user(_user(user_type='saml')), ('unib', 'unib'))

  def test_missing_file(self):
    with self.assertRaisesMessage(ImproperlyConfigured, 'cannot be read'):
      project_settings.load_organizations_file('/nonexistent/organizations.json')

  def _run_settings(self, **env):
    env = dict(os.environ, DEBUG='1', **env)
    with mock.patch.dict(os.environ, env, clear=True), mock.patch('sys.argv', ['manage.py', 'runserver']):
      return runpy.run_path(os.path.join(settings.ROOT_PATH, 'settings.py'))

  def test_read_from_file_at_start(self):
    with tempfile.NamedTemporaryFile('w', suffix='.json') as f:
      f.write(_config(unia=UNIA))
      f.flush()
      values = self._run_settings(ORGANIZATIONS_FILE=f.name)
    self.assertEqual(list(values['ORGANIZATIONS']), ['unia'])

  def test_invalid_file_stops_start(self):
    with tempfile.NamedTemporaryFile('w', suffix='.json') as f:
      f.write(_config(unia={'name': 'A', 'css_url': 'https://cdn.example.net/a.css'}))
      f.flush()
      with self.assertRaisesMessage(ImproperlyConfigured, 'ORGANIZATIONS_FILE %s: "css_url" of "unia"' % f.name):
        self._run_settings(ORGANIZATIONS_FILE=f.name)


class OrganizationCITests(TestCase):

  @override_settings(ORGANIZATIONS=ORGS)
  def test_configured_values_and_languages(self):
    unia = organizations.get('unia')
    self.assertFalse(unia.is_default)
    self.assertEqual(unia.name, 'University A')
    self.assertEqual(unia.site_title, 'University A elections')
    with translation.override('de'):
      self.assertEqual(unia.name, 'Universität A')
      self.assertEqual(unia.footer_links, [{'url': 'https://www.uni-a.example.org/electoral-office', 'text': 'Wahlamt'}])
    self.assertEqual(unia.css_url, '/static/site/unia/site.css')
    # an own stylesheet turns dark mode off, like SITE_CSS_URL
    self.assertEqual(unia.dark_mode, 'off')
    self.assertEqual(unia.accessibility_url, '')
    self.assertEqual(unia.help_email, 'elections@uni-a.example.org')

  @override_settings(ORGANIZATIONS=ORGS, SITE_TITLE='Example Elections', MAIN_LOGO_URL='/static/site/main.svg',
                     ORGANIZATION_NAME='Example Operator', ORGANIZATION_URL='https://operator.example.org',
                     FOOTER_LOGO_URL='/static/site/op-white.svg', PRIVACY_URL='/privacy', DARK_MODE='auto',
                     HELP_EMAIL_ADDRESS='help@example.org')
  def test_layout_falls_back_to_the_default_but_identity_does_not(self):
    unib = organizations.get('unib')
    self.assertEqual(unib.site_title, 'Example Elections')
    self.assertEqual(unib.main_logo_url, '/static/site/main.svg')
    self.assertEqual(unib.privacy_url, '/privacy')
    self.assertEqual(unib.help_email, 'help@example.org')
    self.assertEqual(unib.dark_mode, 'auto')
    self.assertEqual(unib.name, 'University B')
    self.assertEqual(unib.url, '')
    self.assertEqual(unib.footer_logo_url, '')

  @override_settings(ORGANIZATIONS=ORGS)
  def test_unknown_slug_is_the_default(self):
    self.assertTrue(organizations.get('gone').is_default)
    self.assertTrue(organizations.get('').is_default)
    self.assertEqual([o.slug for o in organizations.all_organizations()], ['unia', 'unib'])


class MatchUserTests(TestCase):
  """One mapping for the organization and the address prefix (helios.organizations.match_user)."""

  def test_without_configuration(self):
    self.assertEqual(organizations.match_user(_user('jdoe@uni-a.example.org')), ('', ''))
    self.assertEqual(organizations.match_user(None), ('', ''))

  @override_settings(ORGANIZATIONS=ORGS)
  def test_organization_rules(self):
    match = organizations.match_user
    self.assertEqual(match(_user('jdoe@uni-a.example.org')), ('unia', 'unia'))
    self.assertEqual(match(_user('jdoe@cs.uni-a.example.org')), ('unia', 'unia'))
    self.assertEqual(match(_user(user_id='jdoe@uni-a.example.org')), ('unia', 'unia'))
    self.assertEqual(match(_user(user_type='saml')), ('unib', 'unib'))
    self.assertEqual(match(_user(user_type='oidc', issuer='https://idp.uni-a.example.org')), ('unia', 'unia'))
    self.assertEqual(match(_user('jdoe@elsewhere.example.net')), ('', ''))

  @override_settings(ORGANIZATIONS=ORGS)
  def test_identity_provider_before_email_domain(self):
    self.assertEqual(organizations.match_user(_user('jdoe@uni-a.example.org', user_type='saml')), ('unib', 'unib'))

  @override_settings(ORGANIZATIONS=ORGS, ELECTION_SHORT_NAME_PREFIXES_BY_IDP={'https://idp.uni-c.example.org': 'unic',
                                                                              'saml': 'other'})
  def test_issuer_before_auth_system_across_both_sources(self):
    # the issuer of the older setting wins over the auth system of an organization ...
    self.assertEqual(organizations.match_user(_user(user_type='saml', issuer='https://idp.uni-c.example.org/')),
                     ('', 'unic'))
    # ... and for the same key, the organization wins over the older setting
    self.assertEqual(organizations.match_user(_user(user_type='saml')), ('unib', 'unib'))

  @override_settings(ORGANIZATIONS=ORGS, ELECTION_SHORT_NAME_PREFIXES={'med.uni-a.example.org': 'unimed',
                                                                        'uni-a.example.org': 'old',
                                                                        'uni-b.example.org': 'unib'})
  def test_email_domain_most_specific_across_both_sources(self):
    match = organizations.match_user
    self.assertEqual(match(_user('jdoe@med.uni-a.example.org')), ('', 'unimed'))
    # equally specific: the organization's rule
    self.assertEqual(match(_user('jdoe@uni-a.example.org')), ('unia', 'unia'))
    # an older rule with an organization's prefix belongs to that organization
    self.assertEqual(match(_user('jdoe@uni-b.example.org')), ('unib', 'unib'))

  @override_settings(ORGANIZATIONS=ORGS, ELECTION_SHORT_NAME_PREFIX='unib')
  def test_default_prefix(self):
    self.assertEqual(organizations.match_user(_user('jdoe@elsewhere.example.net')), ('unib', 'unib'))
    self.assertEqual(organizations.match_user(None), ('unib', 'unib'))

  @override_settings(ORGANIZATIONS=ORGS, ELECTION_SHORT_NAME_PREFIXES={'uni-c.example.org': 'unic'})
  def test_prefixes(self):
    self.assertEqual(short_names.prefix_for_user(_user('jdoe@uni-a.example.org')), 'unia')
    self.assertEqual(short_names.all_prefixes(), {'unia', 'unib', 'unic'})
    self.assertEqual(short_names.reserved_prefix('UNIB-board'), 'unib')


class OrganizationTestCase(TestCase):

  def setUp(self):
    self.unia_user = self.make_user('unia-admin', 'admin@uni-a.example.org')
    self.other_user = self.make_user('other-admin', 'admin@elsewhere.example.net')
    self.site_admin = self.make_user('site-admin', 'root@example.org', admin_p=True)

  def make_user(self, user_id, email, admin_p=False):
    return User.objects.create(user_type='password', user_id=user_id, name=user_id, admin_p=admin_p,
                               info={'password': 'x', 'email': email})

  def make_election(self, admin, organization='', **kwargs):
    election_uuid = str(uuid.uuid4())
    params = dict(admin=admin, uuid=election_uuid, short_name=kwargs.pop('short_name', election_uuid),
                  name=kwargs.pop('name', 'Board election'), description='d', organization=organization,
                  cast_url='http://localhost:8000/helios/elections/%s/cast' % election_uuid)
    params.update(kwargs)
    return Election.objects.create(**params)

  def login(self, user):
    self.client.get('/')
    session = self.client.session
    session['user'] = {'type': user.user_type, 'user_id': user.user_id}
    session['csrf_token'] = 'tok'
    session.save()


@override_settings(ORGANIZATIONS=ORGS)
class ElectionOrganizationTests(OrganizationTestCase):

  def election_data(self, **kwargs):
    return dict({'csrf_token': 'tok', 'name': 'Board election', 'description': 'd',
                 'election_type': 'election', 'help_email': 'help@example.org', 'short_name': ''}, **kwargs)

  def test_new_election_gets_the_creators_organization(self):
    self.login(self.unia_user)
    self.client.post('/helios/elections/new', self.election_data(name='A election'))
    self.assertEqual(Election.objects.get(name='A election').organization, 'unia')
    self.login(self.other_user)
    self.client.post('/helios/elections/new', self.election_data(name='Other election'))
    self.assertEqual(Election.objects.get(name='Other election').organization, '')

  def test_organization_is_not_a_form_field(self):
    self.login(self.other_user)
    self.client.post('/helios/elections/new', self.election_data(name='Other election', organization='unia'))
    self.assertEqual(Election.objects.get(name='Other election').organization, '')
    election = Election.objects.get(name='Other election')
    self.client.post('/helios/elections/%s/edit' % election.uuid, self.election_data(organization='unia'))
    election.refresh_from_db()
    self.assertEqual(election.organization, '')

  def test_copy_keeps_the_organization(self):
    election = self.make_election(self.other_user, organization='unia')
    self.login(self.other_user)
    self.client.post('/helios/elections/%s/copy' % election.uuid, {'csrf_token': 'tok'})
    self.assertEqual(Election.objects.get(name='Copy of Board election').organization, 'unia')

  def test_not_in_the_fingerprint(self):
    election = self.make_election(self.other_user)
    before = election.toJSON()
    election.organization = 'unia'
    self.assertEqual(election.toJSON(), before)

  def set_organization(self, user, election, slug):
    self.login(user)
    return self.client.post('/helios/elections/%s/set_organization' % election.uuid,
                            {'csrf_token': 'tok', 'organization': slug})

  def test_only_site_administrators_can_change_it(self):
    election = self.make_election(self.unia_user, organization='unia')
    # the election's own administrator can't
    self.assertEqual(self.set_organization(self.unia_user, election, 'unib').status_code, 403)
    election.refresh_from_db()
    self.assertEqual(election.organization, 'unia')
    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    self.assertContains(response, 'Corporate identity of the pages: <strong>University A</strong>.')
    self.assertContains(response, 'Only site administrators can change it.')
    self.assertNotContains(response, 'set_organization')

    # a site administrator can, also back to the default
    self.login(self.site_admin)
    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    self.assertContains(response, 'action="/helios/elections/%s/set_organization"' % election.uuid)
    self.assertContains(response, '<option value="unia" selected>University A</option>', html=False)
    self.assertEqual(self.set_organization(self.site_admin, election, 'unib').status_code, 302)
    election.refresh_from_db()
    self.assertEqual(election.organization, 'unib')
    self.set_organization(self.site_admin, election, '')
    election.refresh_from_db()
    self.assertEqual(election.organization, '')

  def test_unknown_organization_is_refused(self):
    election = self.make_election(self.unia_user, organization='unia')
    self.assertEqual(self.set_organization(self.site_admin, election, 'nope').status_code, 400)
    election.refresh_from_db()
    self.assertEqual(election.organization, 'unia')

  def test_change_needs_a_csrf_protected_post(self):
    election = self.make_election(self.unia_user, organization='unia')
    self.login(self.site_admin)
    url = '/helios/elections/%s/set_organization' % election.uuid
    self.assertEqual(self.client.get(url).status_code, 400)
    self.assertEqual(self.client.post(url, {'organization': 'unib'}).status_code, 400)
    election.refresh_from_db()
    self.assertEqual(election.organization, 'unia')

  @override_settings(ORGANIZATIONS={})
  def test_nothing_shown_without_configuration(self):
    election = self.make_election(self.site_admin)
    self.login(self.site_admin)
    response = self.client.get('/helios/elections/%s/view' % election.uuid)
    self.assertNotContains(response, 'Corporate identity')
    self.assertNotContains(response, 'set_organization')


@override_settings(ORGANIZATIONS=ORGS, SITE_TITLE='Example Elections', SITE_CSS_URL='/static/site/operator.css',
                   PRIVACY_URL='/privacy', HELP_EMAIL_ADDRESS='help@example.org')
class ElectionPagesTests(OrganizationTestCase):
  """The pages of an election show its organization's CI, all others the default CI."""

  UNIA_MARKS = ['<link rel="stylesheet" href="/static/site/unia/site.css">',
                '<img src="/static/site/unia/logo.svg" class="helios-logo" alt="University A elections">',
                '<a href="https://www.uni-a.example.org"><img src="/static/site/unia/logo-small.svg" height="16" alt="University A"></a>',
                '<a href="https://www.uni-a.example.org/imprint">Imprint</a>',
                '<a href="https://www.uni-a.example.org/electoral-office">Electoral office</a>',
                'href="mailto:elections@uni-a.example.org"']
  DEFAULT_MARKS = ['<link rel="stylesheet" href="/static/site/operator.css">', 'alt="Example Elections"',
                   'href="mailto:help@example.org"', 'href="/privacy"']

  def assertCI(self, response, unia):
    content = response.content.decode()
    self.assertEqual(response.status_code, 200)
    for mark in self.UNIA_MARKS:
      (self.assertIn if unia else self.assertNotIn)(mark, content)
    for mark in self.DEFAULT_MARKS:
      (self.assertNotIn if unia else self.assertIn)(mark, content)

  def test_voter_and_trustee_pages(self):
    election = self.make_election(self.unia_user, organization='unia')
    base = '/helios/elections/%s' % election.uuid
    for path in ['/view', '/questions', '/voters/list', '/trustees/view']:
      with self.subTest(path=path):
        self.assertCI(self.client.get(base + path), unia=True)
    # the public pages look the same for the election's administrators
    self.login(self.unia_user)
    for path in ['/view', '/questions', '/voters/list']:
      with self.subTest(path=path, admin=True):
        self.assertCI(self.client.get(base + path), unia=True)

  def test_voter_login_of_a_private_election(self):
    election = self.make_election(self.unia_user, organization='unia', private_p=True)
    self.assertCI(self.client.get('/helios/elections/%s/password_voter_login' % election.uuid), unia=True)

  def test_trustee_pages(self):
    election = self.make_election(self.unia_user, organization='unia')
    trustee = Trustee.objects.create(election=election, uuid=str(uuid.uuid4()), name='Trustee', email='t@example.org',
                                     secret='s')
    self.client.get('/')
    session = self.client.session
    session[HELIOS_TRUSTEE_UUID] = trustee.uuid
    session.save()
    base = '/helios/elections/%s/trustees/%s' % (election.uuid, trustee.uuid)
    for path in ['/home', '/check-sk', '/keygenerator']:
      with self.subTest(path=path):
        self.assertCI(self.client.get(base + path), unia=True)

  def test_administration_pages_keep_the_default(self):
    """The election's own administration pages are the operator's, like the rest of the administration."""
    election = self.make_election(self.unia_user, organization='unia')
    base = '/helios/elections/%s' % election.uuid
    self.login(self.unia_user)
    for path in ['/edit', '/admins/', '/admins/add', '/trustees/new', '/voters/upload', '/freeze', '/extend']:
      with self.subTest(path=path):
        self.assertCI(self.client.get(base + path), unia=False)
    election.frozen_at = datetime.datetime.utcnow()
    election.questions = []
    election.save()
    for path in ['/compute_tally', '/voters/email']:
      with self.subTest(path=path):
        self.assertCI(self.client.get(base + path), unia=False)

  def test_opted_in_templates_exist(self):
    for name in ELECTION_CI_TEMPLATES:
      with self.subTest(name=name):
        get_template('helios/templates/%s.html' % name)

  def test_german(self):
    election = self.make_election(self.unia_user, organization='unia')
    response = self.client.get('/helios/elections/%s/view' % election.uuid, HTTP_ACCEPT_LANGUAGE='de')
    self.assertContains(response, 'alt="Wahlen der Universität A"')
    self.assertContains(response, '>Wahlamt</a>')

  def test_default_organization_and_unknown_slug(self):
    for organization in ('', 'gone'):
      election = self.make_election(self.unia_user, organization=organization)
      with self.subTest(organization=organization):
        self.assertCI(self.client.get('/helios/elections/%s/view' % election.uuid), unia=False)

  def test_other_pages_keep_the_default(self):
    self.make_election(self.unia_user, organization='unia', featured_p=True)
    self.assertCI(self.client.get('/'), unia=False)
    self.assertCI(self.client.get('/auth/'), unia=False)
    self.login(self.unia_user)
    for path in ['/', '/helios/elections/administered', '/helios/elections/new', '/about']:
      with self.subTest(path=path):
        self.assertCI(self.client.get(path), unia=False)
    self.login(self.site_admin)
    self.assertCI(self.client.get('/helios/stats/elections'), unia=False)

  def test_static_booth_and_verifier_are_the_same_for_all(self):
    # the booth gets its election's colors from the server (booth-theme.css, /meta,
    # see BoothThemeTests); its files and the verifiers stay the same for everyone
    self.make_election(self.unia_user, organization='unia')
    for path in ['/verifier/verify.html', '/booth/vote.html', '/booth/single-ballot-verify.html']:
      with self.subTest(path=path):
        response = self.client.get(path)
        content = b''.join(response.streaming_content).decode()
        self.assertNotIn('/static/site/', content)
        self.assertNotIn('University A', content)
    for path in ['heliosverifier/verify.html', 'heliosbooth/single-ballot-verify.html']:
      with open(os.path.join(settings.ROOT_PATH, path), encoding='utf-8') as f:
        content = f.read()
      self.assertNotIn('booth-theme.css', content)
      self.assertNotIn('booth-ui.css', content)


@override_settings(ORGANIZATIONS=ORGS)
class StartPageTests(OrganizationTestCase):

  def setUp(self):
    super().setUp()
    self.make_election(self.unia_user, organization='unia', featured_p=True, name='A featured', short_name='unia-a')
    self.make_election(self.site_admin, organization='unib', featured_p=True, name='B featured', short_name='unib-b')
    self.make_election(self.site_admin, featured_p=True, name='Default featured', short_name='default-c')
    self.make_election(self.site_admin, organization='gone', featured_p=True, name='Gone featured', short_name='gone-d')
    self.make_election(self.unia_user, organization='unia', name='A not featured', short_name='unia-e')

  def featured(self):
    content = self.client.get('/').content.decode()
    return sorted(name for name in ['A featured', 'B featured', 'Default featured', 'Gone featured', 'A not featured']
                  if '>%s</a>' % name in content)

  def test_own_organization_only(self):
    self.login(self.unia_user)
    self.assertEqual(self.featured(), ['A featured', 'A not featured'])  # the latter: in "Administration"
    self.login(self.other_user)
    # the default organization also gets the elections of organizations no longer configured
    self.assertEqual(self.featured(), ['Default featured', 'Gone featured'])

  def test_none_for_anonymous_visitors(self):
    response = self.client.get('/')
    self.assertEqual(self.featured(), [])
    self.assertNotContains(response, 'There are currently no elections available.')
    self.assertContains(response, 'Log in')

  @override_settings(ORGANIZATIONS={})
  def test_unchanged_without_configuration(self):
    self.assertEqual(self.featured(), ['A featured', 'B featured', 'Default featured', 'Gone featured'])


@override_settings(ORGANIZATIONS=ORGS)
class AssignCommandTests(OrganizationTestCase):

  def run_command(self, *args):
    out = io.StringIO()
    call_command('assign_organizations', *args, stdout=out)
    return out.getvalue()

  def test_by_creator(self):
    a = self.make_election(self.unia_user)
    other = self.make_election(self.other_user)
    already = self.make_election(self.unia_user, organization='unib')
    output = self.run_command('--dry-run')
    self.assertIn('would assign %s (%s): (default) -> unia' % (a.short_name, a.uuid), output)
    self.assertIn('would change 1 election(s)', output)
    a.refresh_from_db()
    self.assertEqual(a.organization, '')
    self.assertIn('changed 1 election(s)', self.run_command())
    for election, organization in ((a, 'unia'), (other, ''), (already, 'unib')):
      election.refresh_from_db()
      self.assertEqual(election.organization, organization)
    self.run_command('--overwrite')
    already.refresh_from_db()
    self.assertEqual(already.organization, 'unia')

  def test_by_prefix(self):
    b = self.make_election(self.other_user, short_name='UNIB-board')
    other = self.make_election(self.unia_user, short_name='board')
    self.run_command('--by', 'prefix')
    b.refresh_from_db()
    other.refresh_from_db()
    self.assertEqual((b.organization, other.organization), ('unib', ''))

  def test_set_and_only(self):
    a = self.make_election(self.other_user, short_name='a-board')
    b = self.make_election(self.unia_user, short_name='b-board')
    self.run_command('--set', 'unib', 'a-board', b.uuid)
    a.refresh_from_db()
    self.assertEqual(a.organization, 'unib')
    self.run_command('--set', '', 'a-board')
    a.refresh_from_db()
    self.assertEqual(a.organization, '')
    self.assertIn('changed 0', self.run_command('--only', 'unib', '--overwrite'))

  def test_errors(self):
    self.make_election(self.other_user, short_name='a-board')
    for args, message in [(['--set', 'nope', 'a-board'], 'Unknown organization'),
                          (['--set', 'unia'], '--set needs the elections'),
                          (['--only', 'nope'], 'Unknown organization'),
                          (['no-such-election'], 'No such election')]:
      with self.subTest(args=args):
        with self.assertRaisesMessage(CommandError, message):
          self.run_command(*args)
    with override_settings(ORGANIZATIONS={}):
      with self.assertRaisesMessage(CommandError, 'No organizations are configured'):
        self.run_command()


@override_settings(ORGANIZATIONS=ORGS, SITE_TITLE='Example Elections')
class StatsElectionsTests(OrganizationTestCase):
  """The site administrators' list of elections shows and filters by organization."""

  def setUp(self):
    super().setUp()
    self.make_election(self.unia_user, organization='unia', name='A election')
    self.make_election(self.other_user, organization='unib', name='B election')
    self.make_election(self.other_user, name='Default election')
    self.make_election(self.other_user, organization='gone', name='Gone election')
    self.login(self.site_admin)

  def listed(self, **params):
    content = self.client.get('/helios/stats/elections', params).content.decode()
    return sorted(name for name in ['A election', 'B election', 'Default election', 'Gone election']
                  if '>%s</a>' % name in content)

  def test_organization_shown(self):
    response = self.client.get('/helios/stats/elections')
    self.assertContains(response, 'Organization: University A</span>')
    self.assertContains(response, 'Organization: Example Elections (default organization)</span>', count=2)
    self.assertContains(response, '<select id="search-organization" name="organization">')
    self.assertContains(response, '<option value="_default">Example Elections (default organization)</option>', html=False)

  def test_filter(self):
    self.assertEqual(self.listed(), ['A election', 'B election', 'Default election', 'Gone election'])
    self.assertEqual(self.listed(organization='unia'), ['A election'])
    self.assertEqual(self.listed(organization='_default'), ['Default election', 'Gone election'])
    self.assertEqual(self.listed(organization='unknown'), ['A election', 'B election', 'Default election', 'Gone election'])
    self.assertEqual(self.listed(organization='unib', q='Default'), [])

  def test_pager_keeps_the_filter(self):
    response = self.client.get('/helios/stats/elections', {'organization': 'unib', 'limit': 1, 'q': 'election'})
    self.assertContains(response, 'B election')
    response = self.client.get('/helios/stats/elections', {'organization': '_default', 'limit': 1})
    self.assertContains(response, 'organization=_default">next 1</a>')

  @override_settings(ORGANIZATIONS={})
  def test_nothing_shown_without_configuration(self):
    response = self.client.get('/helios/stats/elections', {'organization': 'unia'})
    self.assertNotContains(response, 'Organization')
    self.assertEqual(self.listed(organization='unia'), ['A election', 'B election', 'Default election', 'Gone election'])

  def test_site_administrators_only(self):
    self.login(self.unia_user)
    self.assertEqual(self.client.get('/helios/stats/elections').status_code, 403)


##
## phase 2: voting booth and emails
##

UNIC = {
  'name': {'en': 'University C', 'de': 'Universität C'},
  'main_logo_url': '/static/site/unic/logo.svg',
  'help_email': 'elections@uni-c.example.org',
  'dark_mode': 'auto',
  'colors': {'primary': '#00447C', 'primary_contrast': '#fff', 'accent': 'rgb(0, 68, 124)', 'accent_contrast': '#ffffff',
             'link': '#00447c', 'background': '#eef3f8',
             'dark': {'primary': '#9cc3ff', 'primary_contrast': '#10161d', 'link': '#9cc3ff'}},
  'font_url': '/static/site/unic/sans.woff2',
  'mail': {'from_name': {'en': 'Electoral office, University C', 'de': 'Wahlamt der Universität C'},
           'reply_to': 'elections@uni-c.example.org',
           'signature': {'en': 'Electoral office\nUniversity C', 'de': 'Wahlamt\nUniversität C'}},
}
ORGS2 = project_settings.parse_organizations(_config(unia=UNIA, unib=UNIB, unic=UNIC))


class PhaseTwoConfigurationTests(TestCase):

  def test_normalized(self):
    unic = ORGS2['unic']
    self.assertEqual(unic['colors'], {
      'primary': '#00447c', 'primary_contrast': '#ffffff', 'accent': '#00447c', 'accent_contrast': '#ffffff',
      'link': '#00447c', 'background': '#eef3f8',
      'dark': {'primary': '#9cc3ff', 'primary_contrast': '#10161d', 'link': '#9cc3ff'}})
    self.assertEqual(unic['font_url'], '/static/site/unic/sans.woff2')
    self.assertEqual(unic['mail']['reply_to'], 'elections@uni-c.example.org')
    self.assertEqual(unic['mail']['signature'], {'en': 'Electoral office\nUniversity C', 'de': 'Wahlamt\nUniversität C'})

  def test_invalid(self):
    def org(**keys):
      return _config(unia=dict({'name': 'A'}, **keys))
    for config, message in [
        # only colors: nothing that could end the declaration and add CSS
        (org(colors={'primary': 'red'}), '"colors" of "unia": "primary" must be a color'),
        (org(colors={'primary': '#fff; } #election_hash { display: none'}), 'must be a color'),
        (org(colors={'primary': '#00447c;'}), 'must be a color'),
        (org(colors={'primary': 'rgb(0, 0, 300)'}), 'must be a color'),
        (org(colors={'primary': 'var(--x)'}), 'must be a color'),
        (org(colors={'primary': 'url(/static/x.svg)'}), 'must be a color'),
        (org(colors={'primary': 1}), 'must be a color'),
        (org(colors={'secondary': '#000000'}), 'unknown color(s) secondary'),
        (org(colors={'dark': {'hover': '#000000'}}), '"dark": unknown color(s) hover'),
        (org(colors='#00447c'), 'must be an object'),
        (org(colors={'dark': '#000'}), '"dark": must be an object'),
        # the fingerprint (header and footer bars) and the buttons stay readable
        (org(colors={'accent': '#1f2933'}), '"accent_contrast" #1f2933 on "accent" #1f2933 has a contrast of 1.0:1'),
        (org(colors={'accent': '#00447c', 'accent_contrast': '#003366'}), 'at least 4.5:1'),
        (org(colors={'primary': '#dddddd', 'primary_contrast': '#000000'}), '"primary" #dddddd on the booth background #ffffff'),
        (org(colors={'primary_contrast': '#2c4f7c'}), '"primary_contrast" #2c4f7c on "primary"'),
        (org(colors={'link': '#ffff00'}), '"link" #ffff00'),
        (org(colors={'dark': {'link': '#1d4f91'}}), '"dark": "link" #1d4f91 on the booth background #1b222b'),
        (org(font_url='https://fonts.example.net/a.woff2'), '"font_url" of "unia" must be the path of a font file'),
        (org(font_url='/static/site/a.woff2") ; } body { display: none'), 'must be the path of a font file'),
        (org(font_url='/static/site/a.css'), 'must be the path of a font file'),
        (org(font_url='/static/site/../a.woff2'), 'must be the path of a font file'),
        (org(mail='x'), '"mail" of "unia" must be an object'),
        (org(mail={'from': 'x@example.org'}), 'must be an object with "from_name"'),
        # one header line: no header injection
        (org(mail={'from_name': 'Office\r\nBcc: everyone@example.org'}), 'from_name must not contain line breaks'),
        (org(mail={'from_name': {'en': 'Office', 'de': 'Wahlamt\nX'}}), 'from_name must not contain line breaks'),
        (org(mail={'from_name': 'Office‮'}), 'from_name must not contain line breaks'),
        (org(mail={'from_name': 'x' * 101}), 'at most 100 characters'),
        (org(mail={'from_name': ''}), 'from_name must be a text'),
        (org(mail={'reply_to': 'office@example.org\r\nBcc: x@example.org'}), 'reply_to must be an email address'),
        (org(mail={'reply_to': 'Office <office@example.org>'}), 'reply_to must be an email address'),
        (org(mail={'reply_to': ''}), 'reply_to must be an email address'),
        (org(mail={'signature': 'Office\x00'}), 'signature must not contain'),
    ]:
      with self.subTest(config=config):
        with self.assertRaisesMessage(ImproperlyConfigured, message):
          project_settings.parse_organizations(config)

  def test_global_booth_settings(self):
    values = ConfigurationTests._run_settings(self, BOOTH_COLORS='{"primary": "#00447c"}', BOOTH_FONT_URL='/static/site/a.woff2',
                                              EMAIL_SIGNATURE='Example Elections', EMAIL_SIGNATURE_DE='Beispielwahlen')
    self.assertEqual(values['BOOTH_COLORS'], {'primary': '#00447c'})
    self.assertEqual(values['BOOTH_FONT_URL'], '/static/site/a.woff2')
    self.assertEqual(values['EMAIL_SIGNATURE'], 'Example Elections')
    self.assertEqual(values['LOCALIZED_SETTINGS']['EMAIL_SIGNATURE']['de'], 'Beispielwahlen')
    with self.assertRaisesMessage(ImproperlyConfigured, 'BOOTH_COLORS: "primary" must be a color'):
      ConfigurationTests._run_settings(self, BOOTH_COLORS='{"primary": "blue"}')
    with self.assertRaisesMessage(ImproperlyConfigured, 'BOOTH_FONT_URL: must be the path of a font file'):
      ConfigurationTests._run_settings(self, BOOTH_FONT_URL='https://fonts.example.net/a.woff2')


class BoothThemeTests(OrganizationTestCase):

  def theme(self, election):
    response = self.client.get('/helios/elections/%s/booth-theme.css' % election.uuid)
    self.assertEqual(response.status_code, 200)
    return response

  def test_without_configuration_same_as_dark_mode_stylesheet(self):
    election = self.make_election(self.unia_user)
    for dark_mode in ('auto', 'off'):
      with self.subTest(dark_mode=dark_mode), self.settings(DARK_MODE=dark_mode):
        self.assertEqual(self.theme(election).content, self.client.get('/booth-dark-mode.css').content)

  @override_settings(ORGANIZATIONS=ORGS2, DARK_MODE='auto')
  def test_organization_without_colors_keeps_the_built_in_ones(self):
    # unia has its own stylesheet for the pages: like there, dark mode is off unless set
    election = self.make_election(self.unia_user, organization='unia')
    self.assertEqual(self.theme(election).content.decode(), booth_dark_mode('off'))
    election = self.make_election(self.unia_user, organization='unib')
    self.assertEqual(self.theme(election).content.decode(), booth_dark_mode('auto'))

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_organization_colors(self):
    election = self.make_election(self.unia_user, organization='unic')
    response = self.theme(election)
    self.assertEqual(response['Content-Type'], 'text/css; charset=utf-8')
    self.assertEqual(response['Cache-Control'], 'private, no-cache')
    self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
    css = response.content.decode()
    light, rest = css.split(booth_dark_mode('auto'))
    self.assertIn('  --booth-primary: #00447c;\n', light)
    self.assertIn('  --booth-outline: #00447c;\n', light)
    self.assertIn('  --booth-accent: #00447c;\n', light)
    self.assertIn('  --booth-accent-contrast: #ffffff;\n', light)
    self.assertIn('  --booth-background: #eef3f8;\n', light)
    self.assertIn('  --booth-primary-hover: #00335d;\n', light)
    # the organization's dark colors after the built-in ones, in the same media query
    self.assertTrue(rest.startswith('@media (prefers-color-scheme: dark) {\n  :root {\n    --booth-primary: #9cc3ff;\n'))
    self.assertIn('    --booth-primary-contrast: #10161d;\n', rest)
    self.assertIn('@font-face {\n  font-family: "booth-organization-font";\n  src: url("/static/site/unic/sans.woff2") format("woff2");', rest)
    self.assertIn('--booth-font: "booth-organization-font", system-ui,', rest)

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_only_custom_properties_and_a_font(self):
    """Nothing but --booth-* properties with a hex color (and the font): no selectors that could hide anything"""
    election = self.make_election(self.unia_user, organization='unic')
    generated = self.theme(election).content.decode().replace(booth_dark_mode('auto'), '')
    allowed = re.compile(r'^\s*(/\* [^*]* \*/|:root \{|\}|@media \(prefers-color-scheme: dark\) \{|@font-face \{'
                         r'|--booth-[a-z-]+: #[0-9a-f]{6};|--booth-font: "booth-organization-font", [^;{}<>]*;'
                         r'|font-family: "booth-organization-font";|src: url\("/static/site/unic/sans\.woff2"\) format\("woff2"\);'
                         r'|font-display: swap;|)$')
    for line in generated.splitlines():
      self.assertRegex(line, allowed)
    # the fingerprint and trackers keep their own font
    with open(os.path.join(settings.ROOT_PATH, 'heliosbooth', 'css', 'booth-ui.css'), encoding='utf-8') as f:
      booth_ui = f.read()
    self.assertIn('#election_hash { font-family: ui-monospace', booth_ui)
    self.assertNotIn('ui-monospace', generated)

  def test_fingerprint_readable_on_the_accent(self):
    from helios.booth_theme import css_for
    organization = organizations.Organization('unia', {'name': {'': 'A'}, 'colors': {'accent': '#c0c8d0'}, 'dark_mode': 'off'})
    css = css_for(organization)
    self.assertIn('  --booth-accent: #c0c8d0;\n  --booth-accent-muted: #1f2933;\n  --booth-accent-link: #1f2933;\n', css)

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_dark_colors_only_with_dark_mode(self):
    orgs = project_settings.parse_organizations(_config(unic=dict(UNIC, dark_mode='off')))
    election = self.make_election(self.unia_user, organization='unic')
    with self.settings(ORGANIZATIONS=orgs):
      css = self.theme(election).content.decode()
    self.assertNotIn('prefers-color-scheme', css)
    self.assertIn('--booth-primary: #00447c;', css)

  @override_settings(BOOTH_COLORS={'primary': '#00447c'}, ORGANIZATIONS=ORGS2, DARK_MODE='off')
  def test_default_organization_colors_are_inherited(self):
    for slug in ('', 'unib'):
      with self.subTest(slug=slug):
        self.assertIn('--booth-primary: #00447c;', self.theme(self.make_election(self.unia_user, organization=slug)).content.decode())
    # an organization with its own colors doesn't get the operator's
    self.assertIn('--booth-primary: #00447c;\n  --booth-outline', self.theme(self.make_election(self.unia_user, organization='unic')).content.decode())
    orgs = project_settings.parse_organizations(_config(unib=dict(UNIB, colors={})))
    with self.settings(ORGANIZATIONS=orgs):
      self.assertEqual(self.theme(self.make_election(self.unia_user, organization='unib')).content.decode(), booth_dark_mode('off'))

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_revalidated_with_etag(self):
    election = self.make_election(self.unia_user, organization='unic')
    etag = self.theme(election)['ETag']
    response = self.client.get('/helios/elections/%s/booth-theme.css' % election.uuid, HTTP_IF_NONE_MATCH=etag)
    self.assertEqual(response.status_code, 304)
    self.assertEqual(response['ETag'], etag)

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_private_election_like_its_metadata(self):
    election = self.make_election(self.unia_user, organization='unic', private_p=True)
    for path in ('booth-theme.css', 'meta'):
      response = self.client.get('/helios/elections/%s/%s' % (election.uuid, path))
      self.assertEqual(response.status_code, 302)
    self.assertEqual(self.client.get('/helios/elections/%s/booth-theme.css' % uuid.uuid4()).status_code, 404)

  def test_booth_links_the_theme_of_its_election_only(self):
    with open(os.path.join(settings.ROOT_PATH, 'heliosbooth', 'vote.html'), encoding='utf-8') as f:
      booth = f.read()
    self.assertIn(r'var match = /^\/helios\/elections\/([0-9a-fA-F-]{36})$/.exec(election_url);', booth)
    self.assertIn('href = "/helios/elections/" + match[1] + "/booth-theme.css";', booth)
    # the organization's name and logo are set as text and attributes, never as HTML
    show = booth[booth.index('BOOTH.show_organization = function'):booth.index('BOOTH.tracker_html')]
    self.assertNotIn('.html(', show)
    self.assertNotIn('innerHTML', show)
    self.assertIn("box.appendChild(document.createTextNode(organization.name));", show)
    with open(os.path.join(settings.ROOT_PATH, 'helios', 'election_urls.py'), encoding='utf-8') as f:
      self.assertIn("path('/booth-theme.css'", f.read())

  @override_settings(ORGANIZATIONS=ORGS2, HELP_EMAIL_ADDRESS='help@example.org')
  def test_metadata(self):
    def meta(election):
      return json.loads(self.client.get('/helios/elections/%s/meta' % election.uuid).content)
    # no organization: as before, with the default help address instead of a fixed one
    data = meta(self.make_election(self.unia_user))
    self.assertNotIn('organization', data)
    self.assertEqual(data['help_email'], 'help@example.org')
    data = meta(self.make_election(self.unia_user, help_email='board@example.org'))
    self.assertEqual(data['help_email'], 'board@example.org')
    # an organization's name and its own logo, for the booth's header; its help address
    data = meta(self.make_election(self.unia_user, organization='unic'))
    self.assertEqual(data['organization'], {'name': 'University C', 'logo_url': '/static/site/unic/logo.svg'})
    self.assertEqual(data['help_email'], 'elections@uni-c.example.org')
    election = self.make_election(self.unia_user, organization='unic')
    response = self.client.get('/helios/elections/%s/meta' % election.uuid, HTTP_ACCEPT_LANGUAGE='de')
    self.assertEqual(json.loads(response.content)['organization']['name'], 'Universität C')
    # never the operator's logo
    self.assertEqual(meta(self.make_election(self.unia_user, organization='unib'))['organization'],
                     {'name': 'University B', 'logo_url': ''})
    # an organization hiding the help address hides it in the booth too
    orgs = project_settings.parse_organizations(_config(unib=dict(UNIB, help_email='')))
    with self.settings(ORGANIZATIONS=orgs):
      self.assertEqual(meta(self.make_election(self.unia_user, organization='unib'))['help_email'], '')
    # not part of the election's data or fingerprint
    election = self.make_election(self.unia_user, organization='unic')
    self.assertNotIn('University C', self.client.get('/helios/elections/%s' % election.uuid).content.decode())


def booth_dark_mode(dark_mode):
  from helios.booth_theme import dark_mode_css
  return dark_mode_css(dark_mode)


@override_settings(EMAIL_BACKEND='helios.mail_backend.LoggingEmailBackend',
                   HELIOS_EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   DEFAULT_FROM_EMAIL='elections@example.org', DEFAULT_FROM_NAME='Example Elections',
                   SERVER_EMAIL='Example Elections <elections@example.org>')
class ElectionMailTests(OrganizationTestCase):
  """Emails for an election come from its organization: sender name, Reply-To and signature."""

  def make_voter(self, election, email='voter@example.net'):
    from helios.models import Voter
    voter = Voter.objects.create(uuid=str(uuid.uuid4()), election=election, voter_login_id='v1',
                                 voter_email=email, voter_name='Erika Muster')
    voter.generate_password()
    voter.save()
    return voter

  def send_voter_email(self, election, template='vote', language=None):
    from helios import tasks
    from helios.tests import run_queued_tasks
    voter = self.make_voter(election)
    tasks.single_voter_email.enqueue(voter.uuid, 'email/%s_subject.txt' % template, 'email/%s_body.txt' % template,
                                     {'custom_subject': 'Board election', 'custom_message': 'Please vote.',
                                      'election_vote_url': 'https://vote.example.org/v', 'election_url': 'https://vote.example.org/e'},
                                     language=language)
    run_queued_tasks()
    self.assertEqual(len(mail.outbox), 1)
    return mail.outbox[0]

  def test_default_organization_unchanged(self):
    message = self.send_voter_email(self.make_election(self.unia_user))
    self.assertEqual(message.from_email, 'Example Elections <elections@example.org>')
    self.assertEqual(message.reply_to, [])
    self.assertIn('\n--\nHelios\n', message.body)
    self.assertNotIn('Reply-To', message.message())

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_organization_from_a_background_task(self):
    for template in ('vote', 'info', 'result', 'simple', 'password_resend'):
      with self.subTest(template=template):
        mail.outbox = []
        message = self.send_voter_email(self.make_election(self.unia_user, organization='unic'), template)
        self.assertEqual(message.from_email, '"Electoral office, University C" <elections@example.org>')
        self.assertEqual(message.reply_to, ['elections@uni-c.example.org'])
        self.assertIn('\n--\nElectoral office\nUniversity C\n', message.body)
        self.assertNotIn('\n--\nHelios', message.body)
        headers = message.message()
        self.assertEqual(headers['From'], '"Electoral office, University C" <elections@example.org>')
        self.assertEqual(headers['Reply-To'], 'elections@uni-c.example.org')
    # the context ends with the task
    self.assertIsNone(organizations.mail_organization())

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_language_of_the_email(self):
    message = self.send_voter_email(self.make_election(self.unia_user, organization='unic'), language='de')
    self.assertIn('\n--\nWahlamt\nUniversität C\n', message.body)
    # the address stays the same, the name is encoded in the header
    from email.header import decode_header, make_header
    sender = message.message()['From']
    self.assertTrue(sender.startswith('=?utf-8?'), sender)
    self.assertEqual(str(make_header(decode_header(sender))), 'Wahlamt der Universität C <elections@example.org>')

  @override_settings(ORGANIZATIONS=ORGS2, EMAIL_SIGNATURE='Example Elections')
  def test_inherits_the_default_organization(self):
    message = self.send_voter_email(self.make_election(self.unia_user, organization='unib'))
    self.assertEqual(message.from_email, 'Example Elections <elections@example.org>')
    self.assertEqual(message.reply_to, [])
    self.assertIn('\n--\nExample Elections\n', message.body)
    orgs = project_settings.parse_organizations(_config(unib=dict(UNIB, mail={'reply_to': 'office@uni-b.example.org'})))
    mail.outbox = []
    with self.settings(ORGANIZATIONS=orgs):
      message = self.send_voter_email(self.make_election(self.unia_user, organization='unib'))
    self.assertEqual(message.from_email, 'Example Elections <elections@example.org>')
    self.assertEqual(message.reply_to, ['office@uni-b.example.org'])

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_vote_cast_confirmation_from_the_task(self):
    from helios import tasks
    from helios.models import CastVote
    from helios.tests import run_queued_tasks
    election = self.make_election(self.unia_user, organization='unic')
    voter = self.make_voter(election)
    cast_vote = CastVote.objects.create(voter=voter, vote_hash='HASH123', vote_tinyhash='tiny123')
    with mock.patch.object(CastVote, 'verify_and_store', return_value=True):
      tasks.cast_vote_verify_and_store.enqueue(cast_vote.id)
      run_queued_tasks()
    self.assertEqual(len(mail.outbox), 1)
    message = mail.outbox[0]
    self.assertIn('HASH123', message.body)
    self.assertEqual(message.from_email, '"Electoral office, University C" <elections@example.org>')
    self.assertEqual(message.reply_to, ['elections@uni-c.example.org'])
    self.assertIn('\n--\nElectoral office\nUniversity C\n', message.body)

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_trustee_and_removed_vote_emails(self):
    election = self.make_election(self.unia_user, organization='unic')
    trustee = Trustee.objects.create(election=election, uuid=str(uuid.uuid4()), name='Tara Trustee',
                                     email='trustee@example.net', secret='s3cret')
    self.login(self.unia_user)
    self.client.post('/helios/elections/%s/trustees/%s/sendurl' % (election.uuid, trustee.uuid), {'csrf_token': 'tok'})
    voter = self.make_voter(election)
    voter.vote_hash = 'HASH'
    voter.save()
    self.client.post('/helios/elections/%s/voters/%s/delete' % (election.uuid, voter.uuid), {'csrf_token': 'tok'})
    self.assertEqual([m.subject for m in mail.outbox],
                     ['your trustee homepage for Board election', 'Your vote has been removed - Board election'])
    for message in mail.outbox:
      self.assertEqual(message.from_email, '"Electoral office, University C" <elections@example.org>')
      self.assertEqual(message.reply_to, ['elections@uni-c.example.org'])
      self.assertTrue(message.body.endswith('\n--\nElectoral office\nUniversity C\n'), message.body)
    self.assertIn('/t/%s/trustee@example.net/s3cret' % election.short_name, mail.outbox[0].body)

  def assertFromUnic(self, message, signature='Electoral office\nUniversity C'):
    self.assertEqual(message.from_email, '"Electoral office, University C" <elections@example.org>')
    self.assertEqual(message.reply_to, ['elections@uni-c.example.org'])
    self.assertTrue(message.body.endswith('\n--\n%s\n' % signature), message.body)
    self.assertNotIn('\n--\nHelios', message.body)

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_admin_notifications_from_the_elections_organization(self):
    from helios import tasks
    from helios.tests import run_queued_tasks
    election = self.make_election(self.unia_user, organization='unic')
    tasks.election_notify_admin.enqueue(election_id=election.id, subject='voter file processed', body='done\n')
    run_queued_tasks()
    self.assertEqual(mail.outbox[0].body, 'done\n\n--\nElectoral office\nUniversity C\n')
    self.assertFromUnic(mail.outbox[0])
    # in the language of the person whose action sent it
    tasks.election_notify_admin.enqueue(election_id=election.id, subject='s', body='erledigt', language='de')
    run_queued_tasks()
    from email.header import decode_header, make_header
    self.assertEqual(str(make_header(decode_header(mail.outbox[1].from_email))),
                     'Wahlamt der Universität C <elections@example.org>')
    self.assertEqual(mail.outbox[1].body, 'erledigt\n\n--\nWahlamt\nUniversität C\n')
    # a body queued before the signature was added here keeps its own
    tasks.election_notify_admin.enqueue(election_id=election.id, subject='s', body='done\n\n--\nHelios\n')
    run_queued_tasks()
    self.assertEqual(mail.outbox[2].body, 'done\n\n--\nHelios\n')
    self.assertIsNone(organizations.mail_organization())

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_admin_notifications_of_the_tasks(self):
    from helios import tasks
    from helios.models import VoterFile
    from helios.tests import run_queued_tasks
    election = self.make_election(self.unia_user, organization='unic')
    # tally computed
    with mock.patch.object(Election, 'compute_tally'):
      tasks.election_compute_tally.enqueue(election_id=election.id, language='en')
      run_queued_tasks()
    # voter file processed, with the opted-out voters in their own email
    from helios.models import EmailOptOut
    EmailOptOut.add_opt_out('optedout@example.net')
    voter_file = VoterFile.objects.create(election=election, voter_file_content=(
      'password,v1,v1@example.net,Voter One\npassword,v2,optedout@example.net,Voter Two\n'))
    tasks.voter_file_process.enqueue(voter_file_id=voter_file.id, language='en')
    run_queued_tasks()
    subjects = sorted(m.subject for m in mail.outbox)
    self.assertEqual(subjects, ['Opted-out voters not added to election Board election',
                                'encrypted tally computed', 'voter file processed'])
    for message in mail.outbox:
      with self.subTest(subject=message.subject):
        self.assertEqual(message.to, ['"unia-admin" <unia-admin>'])  # the election's administrator
        self.assertFromUnic(message)

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_admin_notification_sent_by_a_view(self):
    # as the views send the trustee notes (public key uploaded, partial decryption)
    from helios import tasks
    election = self.make_election(self.unia_user, organization='unic')
    tasks.notify_election_admin(election, 'Board election - trustee pk upload', 'trustee Tara uploaded a pk.')
    self.assertEqual(mail.outbox[0].body, 'trustee Tara uploaded a pk.\n\n--\nElectoral office\nUniversity C\n')
    self.assertFromUnic(mail.outbox[0])
    with open(os.path.join(settings.ROOT_PATH, 'helios', 'views.py'), encoding='utf-8') as f:
      self.assertNotIn('election.admin.send_message', f.read())

  def test_admin_notifications_of_the_default_organization(self):
    from helios import tasks
    from helios.tests import run_queued_tasks
    election = self.make_election(self.unia_user)
    tasks.election_notify_admin.enqueue(election_id=election.id, subject='voter file processed', body='done')
    run_queued_tasks()
    self.assertEqual(mail.outbox[0].from_email, 'Example Elections <elections@example.org>')
    self.assertEqual(mail.outbox[0].reply_to, [])
    self.assertEqual(mail.outbox[0].body, 'done\n\n--\nHelios\n')

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_other_sender_addresses_are_not_touched(self):
    election = self.make_election(self.unia_user, organization='unic')
    from django.core.mail import send_mail
    with organizations.mail_context(election):
      send_mail('s', 'b', 'Other <other@example.org>', ['x@example.net'])
    self.assertEqual(mail.outbox[0].from_email, 'Other <other@example.org>')

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_emails_not_about_an_election_stay_the_operators(self):
    self.client.cookies[ENTRY] = 'unic'  # also for a visitor of an organization's entry link
    self.client.post('/optout/', {'email': 'someone@example.net'})
    self.assertEqual(len(mail.outbox), 1)
    self.assertEqual(mail.outbox[0].from_email, 'elections@example.org')
    self.assertEqual(mail.outbox[0].reply_to, [])

  @override_settings(ORGANIZATIONS=ORGS2)
  def test_preview_shows_the_organizations_signature(self):
    election = self.make_election(self.unia_user, organization='unic', frozen_at=datetime.datetime.utcnow())
    self.make_voter(election)
    self.login(self.unia_user)
    response = self.client.get('/helios/elections/%s/voters/email?template=vote' % election.uuid)
    self.assertContains(response, 'Electoral office\nUniversity C')

  def test_password_reminder_signed_with_the_site_title(self):
    User.objects.create(user_type='password', user_id='jdoe', name='J', info={'password': 'pw', 'email': 'j@example.net', 'name': 'J'})
    self.login(self.unia_user)
    with self.settings(SITE_TITLE='Example Elections', LOCALIZED_SETTINGS={'SITE_TITLE': {'de': 'Beispielwahlen'}}):
      self.client.post('/auth/password/forgot', {'username': 'jdoe', 'csrf_token': 'tok'}, HTTP_ACCEPT_LANGUAGE='de')
    self.assertTrue(mail.outbox[0].body.rstrip().endswith('Beispielwahlen'), mail.outbox[0].body)


##
## phase 3: entry links /<slug>/ and the entry organization
##

ENTRY = organizations.ENTRY_COOKIE_NAME


class EntryLinkConfigurationTests(TestCase):

  def test_reserved_slugs_are_refused(self):
    for slug in ('helios', 'auth', 'booth', 'verifier', 'static', 'scim', 'admin', 'imprint', 'privacy',
                 'accessibility', 'about', 'faq', 'docs', 'i18n', 'jsi18n', 'optout', 'robots', 'favicon'):
      with self.subTest(slug=slug):
        with self.assertRaisesMessage(ImproperlyConfigured, '"%s" cannot be an organization slug' % slug):
          project_settings.parse_organizations(_config(**{slug: {'name': 'A'}}))

  def test_reserved_paths_from_the_url_configuration(self):
    paths = organizations.reserved_paths()
    for segment in ('helios', 'auth', 'scim', 'i18n', 'jsi18n', 'booth', 'verifier', 'static', 'optout', 'optin',
                    'about', 'docs', 'faq', 'privacy', 'imprint', 'accessibility', 'booth-dark-mode.css'):
      self.assertIn(segment, paths)
    # the fixed list covers every path of the URL configuration a slug could have
    slug_re = re.compile(project_settings.ORGANIZATION_SLUG_RE)
    self.assertEqual({p for p in paths if slug_re.match(p)} - project_settings.RESERVED_ORGANIZATION_SLUGS, set())

  @override_settings(RESERVED_ORGANIZATION_SLUGS=frozenset(), ORGANIZATIONS={'optin': {'name': {'': 'A'}}})
  def test_collision_with_the_url_configuration_stops_the_start(self):
    with self.assertRaisesMessage(ImproperlyConfigured, 'the organization slug(s) optin collide with paths'):
      organizations.check_entry_slugs()
    with self.assertRaisesMessage(ImproperlyConfigured, 'optin'):
      self.client.get('/')

  @override_settings(ORGANIZATIONS=ORGS)
  def test_configured_slugs_pass(self):
    organizations.check_entry_slugs()


class WithoutOrganizationsTests(OrganizationTestCase):
  """Without an organizations file, nothing changes: no cookie, no entry links."""

  def test_no_cookie_and_no_entry_links(self):
    response = self.client.get('/')
    self.assertNotIn(ENTRY, response.cookies)
    for path in ('/unia/', '/unia', '/unia/imprint', '/x/'):
      with self.subTest(path=path):
        self.assertEqual(self.client.get(path).status_code, 404)

  def test_cookie_is_ignored(self):
    self.client.cookies[ENTRY] = 'unia'
    response = self.client.get('/about')
    self.assertNotIn('University A', response.content.decode())
    self.assertEqual(organizations.entry_organization(SimpleNamespace(COOKIES={ENTRY: 'unia'})), None)


@override_settings(ORGANIZATIONS=ORGS, SITE_TITLE='Example Elections', SITE_CSS_URL='/static/site/operator.css',
                   PRIVACY_URL='/privacy', HELP_EMAIL_ADDRESS='help@example.org')
class EntryLinkTests(OrganizationTestCase):
  """/<slug>/ shows the organization's CI on all pages that are not an election's pages for voters and trustees."""

  assertCI = ElectionPagesTests.assertCI
  UNIA_MARKS = ElectionPagesTests.UNIA_MARKS
  DEFAULT_MARKS = ElectionPagesTests.DEFAULT_MARKS

  def login(self, user):
    # without opening the start page, which would end the entry organization
    session = self.client.session
    session['user'] = {'type': user.user_type, 'user_id': user.user_id}
    session['csrf_token'] = 'tok'
    session.save()

  def enter(self, slug='unia'):
    response = self.client.get('/%s/' % slug)
    self.assertEqual(response.status_code, 200)
    return response

  def test_entry_link_sets_the_cookie(self):
    response = self.enter()
    self.assertCI(response, unia=True)
    cookie = response.cookies[ENTRY]
    self.assertEqual(cookie.value, 'unia')
    self.assertEqual(cookie['path'], '/')
    self.assertEqual(cookie['samesite'], 'Lax')
    self.assertTrue(cookie['httponly'])
    self.assertFalse(cookie['secure'])
    self.assertEqual(cookie['max-age'], organizations.ENTRY_COOKIE_MAX_AGE)
    self.assertIn('Cookie', response['Vary'])
    # home and logo links lead back to the entry link
    content = response.content.decode()
    self.assertIn('<a class="site-name" href="/unia/">', content)
    self.assertIn('<a href="/unia/"><img src="/static/site/unia/logo.svg"', content)
    self.assertNotIn('href="/"', content)

  def test_secure_cookie_over_https(self):
    response = self.client.get('/unia/', secure=True)
    self.assertTrue(response.cookies[ENTRY]['secure'])

  def test_pages_in_the_entry_organizations_ci(self):
    election = self.make_election(self.unia_user, organization='')
    self.enter()
    for path in ['/about', '/faq', '/docs', '/auth/', '/auth/password/login', '/no/such/page']:
      with self.subTest(path=path):
        response = self.client.get(path)
        content = response.content.decode()
        if path == '/no/such/page':
          self.assertEqual(response.status_code, 404)
          self.assertIn('<link rel="stylesheet" href="/static/site/unia/site.css">', content)
          self.assertIn('href="/unia/"', content)
        else:
          self.assertCI(response, unia=True)
    self.login(self.unia_user)
    base = '/helios/elections/%s' % election.uuid
    for path in ['/helios/elections/administered', '/helios/elections/voted', '/helios/elections/new',
                 '/helios/elections/new/guided', '/helios/votergroup/administered', '/helios/votergroup/new', base + '/edit', base + '/voters/upload',
                 base + '/admins/', base + '/trustees/new']:
      with self.subTest(path=path, logged_in=True):
        response = self.client.get(path)
        self.assertCI(response, unia=True)
        self.assertIn('href="/unia/"', response.content.decode())
    self.login(self.site_admin)
    self.assertCI(self.client.get('/helios/stats/elections'), unia=True)

  def test_election_pages_keep_the_elections_organization(self):
    """Anti-phishing: the cookie never changes the CI of an election's pages for voters and trustees."""
    default_election = self.make_election(self.unia_user, organization='')
    unib_election = self.make_election(self.unia_user, organization='unib')
    self.enter('unia')
    for election in (default_election, unib_election):
      for path in ['/view', '/questions', '/voters/list', '/trustees/view', '/password_voter_login']:
        with self.subTest(election=election.organization, path=path):
          content = self.client.get('/helios/elections/%s%s' % (election.uuid, path)).content.decode()
          for mark in self.UNIA_MARKS:
            self.assertNotIn(mark, content)
    # and an election of the entry organization in another organization's entry
    unia_election = self.make_election(self.unia_user, organization='unia')
    self.enter('unib')
    response = self.client.get('/helios/elections/%s/view' % unia_election.uuid)
    self.assertCI(response, unia=True)
    # the start page is still the visitor's
    self.assertIn('href="/unib/"', response.content.decode())

  def test_booth_and_verifier_unchanged(self):
    self.enter()
    for path in ['/booth/vote.html', '/verifier/verify.html']:
      with self.subTest(path=path):
        response = self.client.get(path)
        self.assertNotIn('University A', b''.join(response.streaming_content).decode())

  def test_start_page_clears_the_cookie(self):
    self.enter()
    response = self.client.get('/')
    self.assertCI(response, unia=False)
    self.assertEqual(response.cookies[ENTRY].value, '')
    self.assertEqual(response.cookies[ENTRY]['max-age'], 0)
    self.assertCI(self.client.get('/about'), unia=False)
    # without a cookie, the start page sets none
    del self.client.cookies[ENTRY]
    self.assertNotIn(ENTRY, self.client.get('/').cookies)

  def test_switching_organizations(self):
    self.enter('unia')
    response = self.enter('unib')
    self.assertEqual(response.cookies[ENTRY].value, 'unib')
    content = self.client.get('/about').content.decode()
    self.assertIn('href="/unib/"', content)
    self.assertNotIn('/static/site/unia/', content)

  def test_unknown_or_invalid_cookie_is_ignored(self):
    for value in ('gone', 'UNIA', '../unia', '', 'unia; path=/'):
      with self.subTest(value=value):
        self.client.cookies[ENTRY] = value
        response = self.client.get('/about')
        self.assertCI(response, unia=False)
        self.assertIn('<a class="site-name" href="/">', response.content.decode())
    # the start page removes it
    self.client.cookies[ENTRY] = 'gone'
    self.assertEqual(self.client.get('/').cookies[ENTRY]['max-age'], 0)

  def test_unknown_slug_is_not_found(self):
    for path in ('/unic/', '/unic', '/Unia/', '/unia/nothing', '/unia/imprint/'):
      with self.subTest(path=path):
        self.assertEqual(self.client.get(path).status_code, 404)
    # APPEND_SLASH still leads to the entry link
    self.assertRedirects(self.client.get('/unia'), '/unia/', fetch_redirect_response=False, status_code=301)

  def test_featured_elections_of_the_organization(self):
    self.make_election(self.unia_user, organization='unia', featured_p=True, name='A featured', short_name='unia-a')
    self.make_election(self.site_admin, organization='unib', featured_p=True, name='B featured', short_name='unib-b')
    self.make_election(self.site_admin, featured_p=True, name='Default featured', short_name='default-c')

    def featured(path):
      content = self.client.get(path).content.decode()
      return [name for name in ['A featured', 'B featured', 'Default featured'] if '>%s</a>' % name in content]

    # also for visitors who are not logged in
    self.assertEqual(featured('/unia/'), ['A featured'])
    self.assertEqual(featured('/unib/'), ['B featured'])
    self.assertEqual(featured('/'), [])
    # the entry organization wins over the visitor's own
    self.login(self.other_user)
    self.assertEqual(featured('/unia/'), ['A featured'])
    self.assertEqual(featured('/'), ['Default featured'])

  def test_login_returns_to_the_entry_link(self):
    content = self.enter().content.decode()
    self.assertIn('/auth/?return_url=/unia/', content)

  def test_logout_keeps_the_ci(self):
    self.enter()
    self.login(self.unia_user)
    response = self.client.get('/auth/logout', {'return_url': '/about'})
    self.assertRedirects(response, '/about', fetch_redirect_response=False)
    self.assertEqual(self.client.cookies[ENTRY].value, 'unia')
    self.assertCI(self.client.get('/about'), unia=True)

  def test_operators_legal_pages_stay_the_operators(self):
    self.enter()
    self.assertCI(self.client.get('/privacy'), unia=False)

  def test_privacy_notes_mention_the_cookie(self):
    self.assertContains(self.client.get('/privacy'), 'a cookie stores for 90 days in which organization')
    with self.settings(ORGANIZATIONS={}):
      self.assertNotContains(self.client.get('/privacy'), 'organization&#x27;s design')

  def write_page(self, site_dir, slug, page, text):
    directory = os.path.join(site_dir, 'organizations', slug)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, page + '.html'), 'w', encoding='utf-8') as f:
      f.write("{%% extends 'server_ui/templates/base.html' %%}{%% block content %%}%s{%% endblock %%}" % text)

  def test_organization_legal_pages(self):
    with tempfile.TemporaryDirectory() as site_dir:
      self.write_page(site_dir, 'unia', 'imprint', 'Imprint of University A')
      self.write_page(site_dir, 'unia', 'accessibility', 'Accessibility of University A')
      self.write_page(site_dir, 'unia', 'other', 'Not a legal page')
      templates = [dict(settings.TEMPLATES[0], DIRS=[site_dir] + settings.TEMPLATES[0]['DIRS'])]
      with self.settings(TEMPLATES=templates):
        for path, text in [('/unia/imprint', 'Imprint of University A'),
                           ('/unia/accessibility', 'Accessibility of University A')]:
          with self.subTest(path=path):
            response = self.client.get(path)
            self.assertContains(response, text)
            self.assertCI(response, unia=True)
            # the page alone selects no entry organization
            self.assertNotIn(ENTRY, response.cookies)
        for path in ('/unia/privacy', '/unia/other', '/unib/imprint', '/unic/imprint'):
          with self.subTest(path=path):
            self.assertEqual(self.client.get(path).status_code, 404)
        # the operator's pages stay the operator's
        self.assertEqual(self.client.get('/imprint').status_code, 404)
