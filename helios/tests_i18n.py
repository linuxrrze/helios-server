"""
Tests for the English/German user interface.
"""

import uuid

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import translation

from helios import models, tasks
from helios_auth.models import User


class LanguageSelectionTests(TestCase):

  def test_english_by_default(self):
    content = self.client.get('/').content.decode()
    self.assertIn('<html lang="en"', content)
    self.assertIn('Log in', content)

  def test_browser_language(self):
    content = self.client.get('/', HTTP_ACCEPT_LANGUAGE='de-DE,de;q=0.9').content.decode()
    self.assertIn('<html lang="de"', content)
    self.assertIn('Anmelden', content)
    self.assertIn('Datenschutz', content)

  def test_switcher_overrides_browser_language(self):
    response = self.client.post('/i18n/setlang/', {'language': 'de', 'next': '/about'})
    self.assertRedirects(response, '/about', fetch_redirect_response=False)
    content = self.client.get('/', HTTP_ACCEPT_LANGUAGE='en').content.decode()
    self.assertIn('<html lang="de"', content)
    # the switcher offers the other language, named in that language
    self.assertIn('value="en" lang="en">English</button>', content)

  def test_switcher_rejects_foreign_redirect(self):
    response = self.client.post('/i18n/setlang/', {'language': 'de', 'next': 'https://evil.example/'})
    self.assertEqual(response['Location'], '/')

  def test_javascript_catalog(self):
    # used by the voting booth, the verifiers and the question editor
    response = self.client.get('/jsi18n/', HTTP_ACCEPT_LANGUAGE='de')
    self.assertEqual(response.status_code, 200)
    self.assertIn('Frage hinzuf\\u00fcgen', response.content.decode())

  def test_catalogs_compiled(self):
    with translation.override('de'):
      self.assertEqual(translation.gettext('Log out'), 'Abmelden')
      self.assertEqual(translation.ngettext('%(counter)s vote still being processed.',
                                            '%(counter)s votes still being processed.', 2) % {'counter': 2},
                       '2 Stimmen werden noch verarbeitet.')
      self.assertEqual(translation.pgettext('election type', 'referendum'), 'Abstimmung')


class LocalizedSettingTests(TestCase):

  @override_settings(WELCOME_MESSAGE='Welcome to the vote server.',
                     LOCALIZED_SETTINGS={'WELCOME_MESSAGE': {'de': 'Willkommen beim Abstimmungsserver.'}})
  def test_welcome_message_per_language(self):
    self.assertContains(self.client.get('/', HTTP_ACCEPT_LANGUAGE='en'), 'Welcome to the vote server.')
    self.assertContains(self.client.get('/', HTTP_ACCEPT_LANGUAGE='de'), 'Willkommen beim Abstimmungsserver.')

  @override_settings(WELCOME_MESSAGE='Welcome to the vote server.', LOCALIZED_SETTINGS={})
  def test_falls_back_to_plain_setting(self):
    self.assertContains(self.client.get('/', HTTP_ACCEPT_LANGUAGE='de'), 'Welcome to the vote server.')


class EmailLanguageTests(TestCase):

  def setUp(self):
    admin = User.objects.create(user_type='password', user_id='admin', name='Admin',
                                info={'password': 'x', 'email': 'admin@example.org'})
    election = models.Election.objects.create(admin=admin, uuid=str(uuid.uuid4()), short_name='test',
                                              name='Test Election', election_type='election')
    self.voter = models.Voter(uuid=str(uuid.uuid4()), election=election, voter_login_id='v1',
                              voter_name='Vera Voter', voter_email='vera@example.org')
    self.voter.generate_password()
    self.voter.save()

  def _send(self, language):
    tasks.single_voter_email.call(self.voter.uuid, 'email/password_resend_subject.txt',
                                  'email/password_resend_body.txt',
                                  {'election_vote_url': 'https://vote.example/v'}, language=language)
    return mail.outbox[-1]

  def test_email_in_language_of_sender(self):
    self.assertTrue(self._send('de').subject.startswith('Ihre Zugangsdaten'))
    self.assertTrue(self._send('en').subject.startswith('Your voting credentials'))

  def test_older_queued_tasks_use_site_language(self):
    with self.settings(LANGUAGE_CODE='de'):
      self.assertTrue(self._send(None).subject.startswith('Ihre Zugangsdaten'))

  def test_resend_page_queues_mail_in_page_language(self):
    election = self.voter.election
    self.client.get('/helios/elections/%s/view' % election.uuid)
    session = self.client.session
    session['csrf_token'] = 'tok'
    session.save()
    response = self.client.post('/helios/elections/%s/password_voter_resend' % election.uuid,
                                {'csrf_token': 'tok', 'voter_id': 'v1'}, HTTP_ACCEPT_LANGUAGE='de')
    self.assertEqual(response.status_code, 200)
    from helios.tests import run_queued_tasks
    run_queued_tasks()
    self.assertTrue(mail.outbox[-1].subject.startswith('Ihre Zugangsdaten'), mail.outbox[-1].subject)
