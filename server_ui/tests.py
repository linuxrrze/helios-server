import os
from django.test import TestCase


class StaticPagesTests(TestCase):
    """The built-in pages linked from the layout must render."""

    def test_static_pages_render(self):
        for path in ['/about', '/docs', '/faq', '/privacy']:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_site_pages_missing_by_default(self):
        # imprint and accessibility statement exist only if the installation provides them
        for path in ['/imprint', '/accessibility']:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)


class SiteCustomizationTests(TestCase):
    """Branding and legal pages come from settings and SITE_TEMPLATES_DIR, not from code."""

    def test_neutral_defaults(self):
        content = self.client.get('/').content.decode()
        self.assertIn('href="/static/layout.css"', content)
        self.assertIn('href="/privacy"', content)
        self.assertNotIn('Imprint', content)
        self.assertNotIn('Accessibility', content)
        self.assertNotIn('class="orghome"', content)
        # no empty logo box taking up room in the footer bar
        self.assertNotIn('<div class="logo">', content)

    def test_branding_and_footer_links_from_settings(self):
        with self.settings(ORGANIZATION_NAME='Example University', ORGANIZATION_URL='https://www.example.org',
                           ORGANIZATION_LOGO_URL='/static/site/org.svg', FOOTER_LOGO_URL='/static/site/org-white.svg',
                           SITE_CSS_URL='/static/site/site.css', IMPRINT_URL='https://www.example.org/imprint',
                           PRIVACY_URL='', ACCESSIBILITY_URL='/accessibility',
                           FOOTER_LINKS=[{'url': 'https://help.example.org', 'text': 'Help desk'}]):
            content = self.client.get('/').content.decode()
        self.assertIn('<link rel="stylesheet" href="/static/site/site.css">', content)
        self.assertIn('<a href="https://www.example.org"><img src="/static/site/org.svg" height="16" alt="Example University"></a>', content)
        self.assertIn('<img src="/static/site/org-white.svg" alt="Example University">', content)
        self.assertIn('<a href="https://www.example.org/imprint">Imprint</a>', content)
        self.assertIn('<a href="/accessibility">Accessibility</a>', content)
        self.assertIn('<a href="https://help.example.org">Help desk</a>', content)
        self.assertNotIn('href="/privacy"', content)

    def test_organization_name_is_localized(self):
        with self.settings(ORGANIZATION_NAME='Example University', ORGANIZATION_URL='https://www.example.org',
                           LOCALIZED_SETTINGS={'ORGANIZATION_NAME': {'de': 'Beispiel-Universität'}}):
            content = self.client.get('/', HTTP_ACCEPT_LANGUAGE='de').content.decode()
        self.assertIn('>Beispiel-Universität</a>', content)

    def test_site_templates_dir_adds_and_overrides_templates(self):
        import tempfile
        from django.conf import settings as django_settings
        with tempfile.TemporaryDirectory() as site_dir:
            pages = os.path.join(site_dir, 'server_ui', 'templates')
            os.makedirs(pages)
            for name, text in [('imprint', 'Site imprint'), ('privacy', 'Site privacy policy')]:
                with open(os.path.join(pages, name + '.html'), 'w') as f:
                    f.write("{%% extends 'server_ui/templates/base.html' %%}{%% block content %%}%s{%% endblock %%}" % text)
            templates = [dict(django_settings.TEMPLATES[0], DIRS=[site_dir] + django_settings.TEMPLATES[0]['DIRS'])]
            with self.settings(TEMPLATES=templates):
                self.assertContains(self.client.get('/imprint'), 'Site imprint')
                self.assertContains(self.client.get('/privacy'), 'Site privacy policy')
                self.assertEqual(self.client.get('/accessibility').status_code, 404)


class LandingPageTests(TestCase):

    def test_single_login_button_when_logged_out(self):
        # the layout has its own login button; upstream's login box
        # used to be rendered as well, giving two buttons that did the same
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertEqual(content.count('class="button"><span>Log in</span>'), 1)
        self.assertNotIn('/auth/start/', content)  # login-box links


class VoteCastEmailTests(TestCase):
    """The confirmation email sent by server_ui.glue after a ballot is stored."""
    fixtures = ['users.json']

    def setUp(self):
        import uuid
        from helios import models
        from helios_auth.models import User
        admin = User.objects.get(user_id='ben@adida.net', user_type='google')
        self.election, _ = models.Election.get_or_create(
            short_name='cast-mail', name='Dekanswahl', description='', admin=admin)
        # a voter from a voter file, without a user account
        self.voter = models.Voter.objects.create(
            uuid=str(uuid.uuid4()), election=self.election,
            voter_email='voter@example.com', voter_name='Erika Muster')
        self.cast_vote = models.CastVote(voter=self.voter, vote_hash='HASH123', vote_tinyhash='tiny123')

    def _send(self):
        from server_ui.glue import vote_cast_send_message
        vote_cast_send_message(user=self.voter.get_user(), voter=self.voter,
                               election=self.election, cast_vote=self.cast_vote)

    def test_voter_without_account_gets_mail(self):
        from django.core import mail
        self._send()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn('voter@example.com', message.to[0])
        self.assertEqual(message.subject, 'Dekanswahl - vote cast')
        self.assertIn('HASH123', message.body)
        self.assertIn('/optout/', message.body)

    def test_subject_in_german(self):
        from django.core import mail
        from django.utils import translation
        with translation.override('de'):
            self._send()
        self.assertEqual(mail.outbox[0].subject, 'Dekanswahl – Stimme abgegeben')

    def test_opted_out_voter_gets_no_mail(self):
        from django.core import mail
        from helios.models import EmailOptOut
        EmailOptOut.add_opt_out('voter@example.com')
        self._send()
        self.assertEqual(len(mail.outbox), 0)

    def test_mail_failure_does_not_raise(self):
        from unittest import mock
        with mock.patch('helios_auth.auth_systems.password.send_mail', side_effect=OSError('SMTP down')):
            with self.assertLogs('server_ui.glue', level='ERROR'):
                self._send()


class EmailSettingsFromEnvironmentTests(TestCase):
    """
    settings.py ignores the environment while tests run, so load it in a
    separate interpreter with the variables set.
    """

    def _settings(self, **env):
        import json
        import os
        import subprocess
        import sys
        environ = {k: v for k, v in os.environ.items() if k not in ('EMAIL_USE_TLS', 'EMAIL_USE_SSL', 'EMAIL_TIMEOUT', 'EMAIL_SUBJECT_PREFIX')}
        environ.update(env)
        code = ("import json, settings; print(json.dumps({k: getattr(settings, k) for k in "
                "('EMAIL_HOST_USER', 'EMAIL_USE_TLS', 'EMAIL_USE_SSL', 'EMAIL_TIMEOUT', 'EMAIL_SUBJECT_PREFIX')}))")
        result = subprocess.run([sys.executable, '-c', code], env=environ, capture_output=True, text=True,
                                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return result, (json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 else None)

    def test_defaults(self):
        result, values = self._settings()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values['EMAIL_USE_SSL'], False)
        self.assertEqual(values['EMAIL_TIMEOUT'], 60)
        self.assertEqual(values['EMAIL_SUBJECT_PREFIX'], '[Helios] ')

    def test_subject_prefix(self):
        for value, expected in (('[Helios test]', '[Helios test] '), ('[X] ', '[X] '), ('', '')):
            result, values = self._settings(EMAIL_SUBJECT_PREFIX=value)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(values['EMAIL_SUBJECT_PREFIX'], expected)

    def test_authenticated_relay_with_implicit_tls(self):
        result, values = self._settings(EMAIL_HOST_USER='helios', EMAIL_USE_SSL='1', EMAIL_TIMEOUT='20')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values, {'EMAIL_HOST_USER': 'helios', 'EMAIL_USE_TLS': False,
                                  'EMAIL_USE_SSL': True, 'EMAIL_TIMEOUT': 20, 'EMAIL_SUBJECT_PREFIX': '[Helios] '})

    def test_starttls_and_implicit_tls_together_refused(self):
        result, _ = self._settings(EMAIL_USE_TLS='1', EMAIL_USE_SSL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('EMAIL_USE_TLS', result.stderr)


class DockerEntrypointTests(TestCase):
    """docker-entrypoint.py reports configuration errors in one line, without a
    stack trace, and otherwise runs the command."""

    GOOD_SECRET = 'k' * 50

    def _run(self, command, **env):
        import os
        import subprocess
        import sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        environ = {k: v for k, v in os.environ.items() if k not in ('SECRET_KEY', 'EMAIL_OPTOUT_SECRET')}
        environ['DEBUG'] = '0'
        environ.update(env)
        return subprocess.run([sys.executable, os.path.join(root, 'docker-entrypoint.py')] + command,
                              env=environ, capture_output=True, text=True, cwd=root)

    def test_configuration_error_in_one_line(self):
        import sys
        result = self._run([sys.executable, '-c', 'print("started")'], EMAIL_OPTOUT_SECRET=self.GOOD_SECRET)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('started', result.stdout)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
        self.assertTrue(result.stderr.startswith('Helios configuration error: SECRET_KEY is not set'), result.stderr)

    def test_runs_command_when_configuration_is_valid(self):
        import sys
        result = self._run([sys.executable, '-c', 'print("started")'],
                           SECRET_KEY=self.GOOD_SECRET, EMAIL_OPTOUT_SECRET=self.GOOD_SECRET)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'started')


class SecretSettingsFromEnvironmentTests(TestCase):
    """
    With DEBUG=0, settings.py refuses missing, placeholder and short secrets
    and says why, without revealing the value. Loaded in a separate
    interpreter like EmailSettingsFromEnvironmentTests.
    """

    GOOD_SECRET = 'k' * 50

    def _load(self, **env):
        import os
        import subprocess
        import sys
        environ = {k: v for k, v in os.environ.items() if k not in ('SECRET_KEY', 'EMAIL_OPTOUT_SECRET')}
        environ['DEBUG'] = '0'
        environ.update({k: v for k, v in env.items() if v is not None})
        return subprocess.run([sys.executable, '-c', 'import settings'], env=environ, capture_output=True, text=True,
                              cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def assertRefused(self, result, *fragments):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ImproperlyConfigured', result.stderr)
        for fragment in fragments:
            self.assertIn(fragment, result.stderr)
        self.assertIn('secrets.token_urlsafe(50)', result.stderr)

    def test_good_secrets_accepted(self):
        result = self._load(SECRET_KEY=self.GOOD_SECRET, EMAIL_OPTOUT_SECRET=self.GOOD_SECRET)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_secret_key(self):
        result = self._load(EMAIL_OPTOUT_SECRET=self.GOOD_SECRET)
        self.assertRefused(result, 'SECRET_KEY is not set')

    def test_empty_secret_key(self):
        result = self._load(SECRET_KEY='', EMAIL_OPTOUT_SECRET=self.GOOD_SECRET)
        self.assertRefused(result, 'SECRET_KEY is not set')

    def test_placeholder_secret_key(self):
        result = self._load(SECRET_KEY='CHANGE-ME', EMAIL_OPTOUT_SECRET=self.GOOD_SECRET)
        self.assertRefused(result, 'SECRET_KEY is still the placeholder')

    def test_short_secret_key_names_length_not_value(self):
        result = self._load(SECRET_KEY='s3cr3t-but-short', EMAIL_OPTOUT_SECRET=self.GOOD_SECRET)
        self.assertRefused(result, 'SECRET_KEY is only 16 characters long', 'at least 32', '$$')
        self.assertNotIn('s3cr3t-but-short', result.stderr)

    def test_missing_optout_secret(self):
        result = self._load(SECRET_KEY=self.GOOD_SECRET)
        self.assertRefused(result, 'EMAIL_OPTOUT_SECRET is not set')

    def test_placeholder_optout_secret(self):
        result = self._load(SECRET_KEY=self.GOOD_SECRET, EMAIL_OPTOUT_SECRET='CHANGE-ME')
        self.assertRefused(result, 'EMAIL_OPTOUT_SECRET is still the placeholder')


class DarkModeTests(TestCase):
    """Dark colors only with DARK_MODE=auto (default without a site stylesheet)"""

    def test_auto_marks_the_pages(self):
        from django.test import override_settings
        with override_settings(DARK_MODE='auto'):
            html = self.client.get('/').content.decode()
            self.assertIn('color-scheme-auto', html)
            self.assertIn('<meta name="color-scheme" content="light dark">', html)
            # the error pages too
            html = self.client.get('/no/such/page').content.decode()
            self.assertIn('class="color-scheme-auto"', html)

    def test_off_keeps_light_colors(self):
        from django.test import override_settings
        with override_settings(DARK_MODE='off'):
            html = self.client.get('/').content.decode()
            self.assertNotIn('color-scheme-auto', html)
            self.assertNotIn('name="color-scheme"', html)

    def test_booth_follows_dark_mode(self):
        """The static booth links /booth-dark-mode.css, which has the dark colors only with DARK_MODE=auto"""
        import os
        from django.conf import settings
        from django.test import override_settings
        with open(os.path.join(settings.ROOT_PATH, 'heliosbooth', 'vote.html')) as f:
            booth = f.read()
        self.assertIn('<link rel="stylesheet" type="text/css" href="/booth-dark-mode.css" />', booth)
        # after the booth's own stylesheets, so that it overrides their colors
        self.assertGreater(booth.index('/booth-dark-mode.css'), booth.index('css/booth-ui.css'))
        with override_settings(DARK_MODE='auto'):
            response = self.client.get('/booth-dark-mode.css')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], 'text/css; charset=utf-8')
            self.assertEqual(response['Cache-Control'], 'no-cache')
            css = response.content.decode()
            self.assertIn('@media (prefers-color-scheme: dark)', css)
            self.assertIn('color-scheme: dark', css)
        with override_settings(DARK_MODE='off'):
            response = self.client.get('/booth-dark-mode.css')
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('prefers-color-scheme', response.content.decode())
            self.assertNotIn('{', response.content.decode())

    def _dark_mode(self, **env):
        import os
        import subprocess
        import sys
        environ = {k: v for k, v in os.environ.items() if k not in ('DARK_MODE', 'SITE_CSS_URL')}
        environ.update(env)
        result = subprocess.run([sys.executable, '-c', 'import settings; print(settings.DARK_MODE)'], env=environ,
                                capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().splitlines()[-1]

    def test_default_depends_on_site_stylesheet(self):
        self.assertEqual(self._dark_mode(), 'auto')
        self.assertEqual(self._dark_mode(SITE_CSS_URL='/static/site/site.css'), 'off')
        self.assertEqual(self._dark_mode(SITE_CSS_URL='/static/site/site.css', DARK_MODE='auto'), 'auto')
        self.assertEqual(self._dark_mode(DARK_MODE='off'), 'off')
        self.assertEqual(self._dark_mode(DARK_MODE='nonsense'), 'off')
