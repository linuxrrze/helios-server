"""
Tests for the corporate identity (CI) of organizations (helios/organizations.py).
"""

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
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import translation

import settings as project_settings
from helios import organizations, short_names
from helios.models import Election
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

  def test_election_pages(self):
    election = self.make_election(self.unia_user, organization='unia')
    base = '/helios/elections/%s' % election.uuid
    for path in ['/view', '/questions', '/voters/list', '/trustees/view']:
      with self.subTest(path=path):
        self.assertCI(self.client.get(base + path), unia=True)
    # also for its administrators
    self.login(self.unia_user)
    for path in ['/view', '/edit', '/voters/list', '/admins/']:
      with self.subTest(path=path, admin=True):
        self.assertCI(self.client.get(base + path), unia=True)

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

  def test_verifier_and_booth_stay_neutral(self):
    election = self.make_election(self.unia_user, organization='unia')
    for path in ['/verifier/verify.html', '/booth/vote.html', '/booth/single-ballot-verify.html']:
      with self.subTest(path=path):
        response = self.client.get(path)
        content = b''.join(response.streaming_content).decode()
        self.assertNotIn('/static/site/', content)
        self.assertNotIn('University A', content)
    meta = self.client.get('/helios/elections/%s/meta' % election.uuid).content.decode()
    self.assertNotIn('unia', meta)


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
