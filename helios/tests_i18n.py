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


class LanguageSwitchKeepsInputTests(TestCase):
  """
  Switching the language reloads the page; server_ui/media/language-switch.js keeps
  the input in sessionStorage until the page has been loaded again and asks only
  if something cannot be kept (passwords, files, secret keys, data-no-preserve).
  The script is run in Node with a minimal DOM stub.
  """

  NODE_HARNESS = r"""
const script = require('fs').readFileSync(0, 'utf8');

function el(tag, props, parent) {
  const e = Object.assign({tagName: tag.toUpperCase(), nodeType: 1, attrs: {}, parentNode: parent || null,
    type: '', name: '', id: '', value: '', defaultValue: '', checked: false, defaultChecked: false,
    disabled: false, readOnly: false, form: null, events: [],
    classList: {contains: (c) => (e.className || '').split(' ').indexOf(c) !== -1},
    hasAttribute: (a) => a in e.attrs, getAttribute: (a) => (a in e.attrs ? e.attrs[a] : null),
    dispatchEvent: (evt) => { e.events.push(evt.type); }}, props || {});
  return e;
}

// a page: switcher form, a page form with several fields, a secret area
function page(store, opts) {
  opts = opts || {};
  const listeners = {};
  const switcher = el('form', {className: 'language-switcher', listeners: [],
    addEventListener(t, f) { this.listeners.push(f); }});
  switcher.attrs = {'data-page-method': opts.method || 'GET',
    'data-confirm-lost': 'LOST', 'data-confirm-partial': 'PARTIAL'};
  const form = el('form', {id: 'f'});
  const secretArea = el('div', {attrs: {'data-no-preserve': ''}}, form);
  const f = {
    csrf: el('input', {type: 'hidden', name: 'csrf_token', value: 'tok', defaultValue: 'tok', form: switcher}, switcher),
    lang: el('button', {type: 'submit', name: 'language', value: 'de', form: switcher}, switcher),
    name: el('input', {type: 'text', name: 'name', form}, form),
    desc: el('textarea', {name: 'description', form}, form),
    flag: el('input', {type: 'checkbox', name: 'private_p', value: 'on', form}, form),
    pw: el('input', {type: 'password', name: 'pw', form}, form),
    file: el('input', {type: 'file', name: 'upload', form}, form),
    step: el('input', {type: 'hidden', name: 'step', value: 'basics', defaultValue: 'basics', form}, form),
    key: el('textarea', {name: 'key', form}, secretArea),
    shown: el('textarea', {name: 'shown', readOnly: true, form}, form),
    prefilled: el('input', {type: 'text', name: 'prefilled', value: 'server', defaultValue: 'server', form}, form),
  };
  const fields = Object.values(f).filter(x => x.tagName !== 'BUTTON');
  global.document = {
    readyState: 'complete', forms: [switcher, form],
    addEventListener: (t, fn) => (listeners[t] = listeners[t] || []).push(fn),
    querySelectorAll: (sel) => sel === 'form.language-switcher' ? [switcher] : fields,
  };
  global.Event = function (type) { this.type = type; };
  global.window = {
    location: {pathname: opts.path || '/helios/elections/new', search: ''},
    pageXOffset: 0, pageYOffset: opts.scroll || 0, scrolled: null,
    scrollTo(x, y) { this.scrolled = [x, y]; },
    confirm: (msg) => { p.asked.push(msg); return !!opts.answer; },
  };
  Object.defineProperty(global.window, 'sessionStorage', {get() {
    if (!store) throw new Error('storage disabled');
    return store;
  }});
  const p = {f, asked: [], listeners};
  p.api = (function () { eval(script); return HeliosFormState; })();
  p.fire = (type, target) => (listeners[type] || []).forEach(fn => fn({type, target}));
  p.type = (field, value) => { field.value = value; p.fire('input', field); };
  p.switchLanguage = () => {
    let prevented = false;
    const evt = {currentTarget: switcher, target: switcher, preventDefault: () => { prevented = true; }};
    switcher.listeners.forEach(fn => fn(evt));
    return !prevented;
  };
  return p;
}

function storage() {
  const data = {};
  return {data, getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); },
          removeItem: (k) => { delete data[k]; }};
}

const tick = () => new Promise(r => setTimeout(r, 5));
const r = {};
(async () => {
  // typed into normal fields: no question, kept, restored after the reload
  let store = storage();
  let p = page(store, {scroll: 300});
  p.type(p.f.name, 'Board election');
  p.type(p.f.desc, 'Line 1\nLine 2');
  p.f.flag.checked = true; p.fire('change', p.f.flag);
  r.switched = p.switchLanguage();
  r.asked = p.asked.length;
  const saved = JSON.parse(store.data['helios-language-switch']);
  r.savedFields = saved.fields;
  r.savedText = store.data['helios-language-switch'];
  p = page(store, {});
  await tick();
  r.restored = {name: p.f.name.value, desc: p.f.desc.value, flag: p.f.flag.checked, prefilled: p.f.prefilled.value};
  r.events = p.f.name.events;
  r.scrolled = window.scrolled;
  r.leftAfterRestore = Object.keys(store.data).length;

  // a password typed: ask; "Cancel" stays on the page and stores nothing
  store = storage();
  p = page(store, {answer: false});
  p.type(p.f.name, 'x'); p.type(p.f.pw, 'secret-password');
  r.pwSwitched = p.switchLanguage();
  r.pwAsked = p.asked;
  r.pwStored = Object.keys(store.data).length;
  // "OK": switch, keep the name, never the password
  p = page(store, {answer: true});
  p.type(p.f.name, 'x'); p.type(p.f.pw, 'secret-password');
  r.pwSwitchedOk = p.switchLanguage();
  r.pwSavedText = store.data['helios-language-switch'] || '';

  // a secret key in a data-no-preserve area: ask, not stored
  store = storage();
  p = page(store, {answer: true});
  p.type(p.f.key, '{"x": "123"}');
  r.keySwitched = p.switchLanguage();
  r.keyAsked = p.asked;
  r.keyStored = Object.keys(store.data).length;

  // a page that has state only in JavaScript (generated key): ask
  store = storage();
  p = page(store, {answer: false});
  p.api.markUnsaved();
  r.markedSwitched = p.switchLanguage();
  r.markedAsked = p.asked;

  // nothing typed: no question, nothing stored
  store = storage();
  p = page(store, {});
  r.cleanSwitched = p.switchLanguage();
  r.cleanAsked = p.asked.length;
  r.cleanStored = Object.keys(store.data).length;

  // page answering a POST (form shown again with errors): all fields are kept
  store = storage();
  p = page(store, {method: 'POST'});
  p.switchLanguage();
  r.postFields = JSON.parse(store.data['helios-language-switch']).fields;

  // saved state of another page or too old: not used, but deleted
  store = storage();
  p = page(store, {});
  p.type(p.f.name, 'elsewhere');
  p.switchLanguage();
  p = page(store, {path: '/other'});
  await tick();
  r.otherPage = p.f.name.value;
  r.otherPageLeft = Object.keys(store.data).length;
  store.data['helios-language-switch'] = JSON.stringify({url: '/helios/elections/new', time: Date.now() - 3600000, fields: {'#f|name|1': 'old'}, hooks: {}});
  p = page(store, {});
  await tick();
  r.expired = p.f.name.value;
  r.expiredLeft = Object.keys(store.data).length;

  // hook for fields built by JavaScript, registered after the restore has run
  store = storage();
  p = page(store, {});
  p.api.register('editor', {save: () => ({rows: ['a', 'b']}), restore: () => {}});
  p.switchLanguage();
  p = page(store, {});
  await tick();
  let got = null;
  p.api.register('editor', {save: () => null, restore: (d) => { got = d; }});
  r.hookData = got;

  // without sessionStorage: as before, ask when something was typed
  p = page(null, {answer: false});
  p.type(p.f.name, 'x');
  r.noStorageSwitched = p.switchLanguage();
  r.noStorageAsked = p.asked;
  p = page(null, {});
  r.noStorageClean = p.switchLanguage() && p.asked.length === 0;

  console.log(JSON.stringify(r));
})();
"""

  def _run(self):
    import json, shutil, subprocess
    from django.conf import settings
    node = shutil.which('node')
    if not node:
      self.skipTest('node not installed')
    with open(settings.ROOT_PATH + '/server_ui/media/language-switch.js') as f:
      script = f.read()
    result = subprocess.run([node, '-e', self.NODE_HARNESS], input=script, capture_output=True, text=True)
    self.assertEqual(result.returncode, 0, result.stderr)
    return json.loads(result.stdout)

  def test_input_is_kept_without_asking(self):
    r = self._run()
    self.assertTrue(r['switched'])
    self.assertEqual(r['asked'], 0)
    self.assertEqual(r['savedFields'], {'#f|name|1': 'Board election', '#f|description|1': 'Line 1\nLine 2',
                                        '#f|private_p=on|1': True})
    self.assertEqual(r['restored'], {'name': 'Board election', 'desc': 'Line 1\nLine 2', 'flag': True, 'prefilled': 'server'})
    self.assertEqual(r['events'], ['input', 'change'])  # dependent display is updated
    self.assertEqual(r['scrolled'], [0, 300])
    self.assertEqual(r['leftAfterRestore'], 0)          # storage is empty again

  def test_secrets_are_never_stored(self):
    r = self._run()
    for text in (r['savedText'], r['pwSavedText']):
      self.assertNotIn('csrf', text)
      self.assertNotIn('tok', text)
      self.assertNotIn('secret-password', text)
      self.assertNotIn('"#f|step', text)
    self.assertNotIn('"#f|pw', r['pwSavedText'])
    self.assertIn('"#f|name|1":"x"', r['pwSavedText'])
    self.assertFalse(r['pwSwitched'])                   # asked, "Cancel": stay
    self.assertEqual(r['pwAsked'], ['PARTIAL'])
    self.assertEqual(r['pwStored'], 0)
    self.assertTrue(r['pwSwitchedOk'])
    self.assertTrue(r['keySwitched'])                   # data-no-preserve area
    self.assertEqual(r['keyAsked'], ['PARTIAL'])
    self.assertEqual(r['keyStored'], 0)
    self.assertFalse(r['markedSwitched'])               # HeliosFormState.markUnsaved()
    self.assertEqual(r['markedAsked'], ['PARTIAL'])
    # a page answering a POST keeps all fields, but still no secrets or read-only output
    self.assertEqual(sorted(r['postFields']), ['#f|description|1', '#f|name|1', '#f|prefilled|1', '#f|private_p=on|1'])

  def test_nothing_typed_nothing_stored(self):
    r = self._run()
    self.assertTrue(r['cleanSwitched'])
    self.assertEqual(r['cleanAsked'], 0)
    self.assertEqual(r['cleanStored'], 0)

  def test_only_for_the_same_page_and_shortly(self):
    r = self._run()
    self.assertEqual(r['otherPage'], '')
    self.assertEqual(r['otherPageLeft'], 0)
    self.assertEqual(r['expired'], '')
    self.assertEqual(r['expiredLeft'], 0)

  def test_hooks_for_javascript_forms(self):
    r = self._run()
    self.assertEqual(r['hookData'], {'rows': ['a', 'b']})

  def test_without_storage_asks_as_before(self):
    r = self._run()
    self.assertFalse(r['noStorageSwitched'])
    self.assertEqual(r['noStorageAsked'], ['LOST'])
    self.assertTrue(r['noStorageClean'])

  def _switcher(self, response):
    import re
    match = re.search(r'<form class="language-switcher".*?</form>', response.content.decode(), re.S)
    self.assertIsNotNone(match, 'switcher missing')
    return match.group(0)

  def test_switcher_markup(self):
    switcher = self._switcher(self.client.get('/about?x=1', HTTP_ACCEPT_LANGUAGE='en'))
    self.assertIn('data-page-method="GET"', switcher)
    self.assertIn('<input type="hidden" name="next" value="/about?x=1" />', switcher)
    self.assertIn('data-confirm-lost="Unsaved input on this page will be lost. Switch the language anyway?"', switcher)
    self.assertIn('data-confirm-partial="Some of your input on this page, such as passwords, files or secret keys, '
                  'is not kept when the language is switched. Switch the language anyway?"', switcher)
    self.assertIn('<script src="/static/language-switch.js"></script>', self.client.get('/').content.decode())
    self.assertEqual(self.client.get('/static/language-switch.js').status_code, 200)

  def test_switcher_returns_to_the_page(self):
    response = self.client.post('/i18n/setlang/', {'language': 'de', 'next': '/about?x=1'})
    self.assertEqual(response.status_code, 302)
    self.assertEqual(response['Location'], '/about?x=1')

  def test_questions_are_translated(self):
    switcher = self._switcher(self.client.get('/', HTTP_ACCEPT_LANGUAGE='de'))
    self.assertIn('data-confirm-lost="Ungespeicherte Eingaben auf dieser Seite gehen verloren. Sprache trotzdem wechseln?"', switcher)
    self.assertIn('data-confirm-partial="Einige Ihrer Eingaben auf dieser Seite', switcher)

  def test_pages_with_secrets_opt_out(self):
    from django.conf import settings
    root = settings.ROOT_PATH + '/helios/templates/'
    for name in ('election_keygenerator', 'trustee_decrypt_and_prove', 'trustee_check_sk', 'election_new_2',
                 '_castconfirm_password'):
      with open(root + name + '.html') as f:
        self.assertIn('data-no-preserve', f.read(), name)
    with open(root + 'election_keygenerator.html') as f:
      self.assertIn('HeliosFormState.markUnsaved()', f.read())
    with open(root + 'election_questions.html') as f:
      text = f.read()
    self.assertIn('<div id="questions" data-preserve-custom>', text)
    self.assertIn("HeliosFormState.register('questions'", text)

  def test_booth_has_no_switcher(self):
    # the voting booth is a static page without the switcher: switching the language
    # there cannot lose a ballot, and no ballot choice is ever written to storage
    from django.conf import settings
    for path in ('/heliosbooth/vote.html', '/heliosbooth/single-ballot-verify.html', '/heliosverifier/verify.html'):
      with open(settings.ROOT_PATH + path) as f:
        text = f.read()
      self.assertNotIn('language-switch', text, path)
      self.assertNotIn('sessionStorage', text, path)
