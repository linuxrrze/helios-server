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


class LanguageSwitchConfirmTests(TestCase):
  """
  Switching the language reloads the page; the switcher asks first when the
  page has unsaved input. The script is run in Node with a minimal DOM stub.
  """

  NODE_HARNESS = r'''
const script = require('fs').readFileSync(0, 'utf8');
const listeners = {};
const doc = {addEventListener: (t, f) => (listeners[t] = listeners[t] || []).push(f)};
const switcherForm = {className: 'language-switcher', parentNode: null,
  listeners: [], addEventListener(t, f) { this.listeners.push(f); }};
const switcherButton = {className: '', parentNode: switcherForm};
const pageForm = {className: '', parentNode: null};
const pageField = {className: 'field', parentNode: pageForm};
doc.querySelectorAll = () => [switcherForm];
let confirmAnswer = false, asked = 0;
global.document = doc;
global.window = {confirm: (msg) => { asked++; global.lastMessage = msg; return confirmAnswer; }};
eval(script);
function fire(type, target) { (listeners[type] || []).forEach(f => f({target})); }
function switchLanguage() {
  let prevented = false;
  const evt = {target: switcherForm, preventDefault: () => { prevented = true; }};
  switcherForm.listeners.forEach(f => f(evt));
  return prevented;
}
const r = {};
r.clean = switchLanguage(); r.askedClean = asked;
fire('input', switcherButton); r.switcherInputIgnored = !switchLanguage() && asked === 0;
fire('input', pageField); confirmAnswer = false; r.cancelled = switchLanguage();
confirmAnswer = true; r.confirmed = !switchLanguage();
fire('submit', pageForm); asked = 0; r.afterSave = !switchLanguage() && asked === 0;
fire('change', pageField); confirmAnswer = false; r.change = switchLanguage();
r.message = global.lastMessage;
console.log(JSON.stringify(r));
'''

  def _run_switcher_script(self, language):
    import json, re, shutil, subprocess
    node = shutil.which('node')
    if not node:
      self.skipTest('node not installed')
    page = self.client.get('/', HTTP_ACCEPT_LANGUAGE=language).content.decode()
    match = re.search(r'<form class="language-switcher".*?</form>\s*<script>(.*?)</script>', page, re.S)
    self.assertIsNotNone(match, 'switcher script missing')
    result = subprocess.run([node, '-e', self.NODE_HARNESS], input=match.group(1), capture_output=True, text=True)
    self.assertEqual(result.returncode, 0, result.stderr)
    return json.loads(result.stdout)

  def test_asks_only_with_unsaved_input(self):
    r = self._run_switcher_script('en')
    self.assertFalse(r['clean'])               # nothing typed: switch without asking
    self.assertEqual(r['askedClean'], 0)
    self.assertTrue(r['switcherInputIgnored'])  # the switcher itself does not count
    self.assertTrue(r['cancelled'])             # typed + "Cancel": stay on the page
    self.assertTrue(r['confirmed'])             # typed + "OK": switch
    self.assertTrue(r['afterSave'])             # after submitting the page form: no question
    self.assertTrue(r['change'])                # select/checkbox changes count too
    self.assertEqual(r['message'], 'Unsaved input on this page will be lost. Switch the language anyway?')

  def test_question_is_translated(self):
    r = self._run_switcher_script('de')
    self.assertEqual(r['message'], 'Ungespeicherte Eingaben auf dieser Seite gehen verloren. Sprache trotzdem wechseln?')
