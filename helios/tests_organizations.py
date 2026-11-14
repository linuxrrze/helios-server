"""
Tests for the corporate identity (CI) of organizations (helios/organizations.py).
"""

import os
import re

from django.conf import settings
from django.test import TestCase, override_settings

from helios import organizations


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
