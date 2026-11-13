"""
Main navigation and breadcrumbs (helios/templatetags/navigation_tags.py,
server_ui/templates/_site_nav.html and _breadcrumbs.html).
"""

import re
import uuid

from django.test import TestCase, override_settings

from helios import models
from helios.security import HELIOS_TRUSTEE_UUID
from helios_auth import models as auth_models


def _section(html, start_marker):
  """The <nav> element that starts with START_MARKER, or '' if there is none."""
  start = html.find(start_marker)
  if start < 0:
    return ''
  return html[start:html.index('</nav>', start)]


class NavigationTests(TestCase):
  fixtures = ['users.json', 'election.json']

  def setUp(self):
    self.election = models.Election.objects.all()[0]
    self.user = auth_models.User.objects.get(user_id='ben@adida.net', user_type='google')

  def login(self, user=None):
    user = user or self.user
    session = self.client.session
    session['user'] = {'type': user.user_type, 'user_id': user.user_id}
    session.save()

  def get(self, url, **kwargs):
    response = self.client.get(url, **kwargs)
    self.assertEqual(response.status_code, 200, url)
    return response.content.decode()

  def site_nav(self, html):
    return _section(html, '<nav class="site-nav"')

  def breadcrumbs(self, html):
    return _section(html, '<nav class="breadcrumbs"')

  def crumbs(self, html):
    """The trail as a list of (label, href or None for the current page)."""
    trail = []
    for item in re.findall(r'<li>(.*?)</li>', self.breadcrumbs(html), re.S):
      link = re.match(r'<a href="([^"]*)">(.*)</a>$', item.strip(), re.S)
      if link:
        trail.append((link.group(2), link.group(1)))
      else:
        current = re.match(r'<span aria-current="page">(.*)</span>$', item.strip(), re.S)
        self.assertIsNotNone(current, item)
        trail.append((current.group(1), None))
    return trail

  # --- header ----------------------------------------------------------------

  def test_anonymous_start_page_has_home_link_and_no_menu(self):
    nav = self.site_nav(self.get('/'))
    self.assertIn('aria-label="Main navigation"', nav)
    self.assertRegex(nav, r'<a class="site-name" href="/">[^<]+</a>')
    self.assertIn('<a href="/" aria-current="page">Home</a>', nav)
    self.assertNotIn('/helios/elections/administered', nav)
    self.assertNotIn('<details', nav)

  def test_logged_in_user_gets_own_elections_and_a_phone_menu(self):
    self.login()
    nav = self.site_nav(self.get('/helios/elections/administered'))
    self.assertIn('<a href="/">Home</a>', nav)
    # listed twice: as a row and in the phone menu
    self.assertEqual(nav.count('href="/helios/elections/administered" aria-current="page">My elections</a>'), 2)
    self.assertIn('href="/helios/elections/new">Create election</a>', nav)
    self.assertIn('href="/helios/elections/voted">My votes</a>', nav)
    self.assertIn('<details class="site-nav-menu">', nav)
    self.assertIn('<summary>Menu</summary>', nav)

  def test_header_keeps_login_info_and_language_switcher(self):
    self.login()
    html = self.get('/')
    self.assertIn('class="logininfo"', html)
    self.assertIn('class="logout"', html)
    self.assertIn('class="language-switcher"', html)
    self.assertIn('/static/navigation.css', html)

  @override_settings(SITE_CSS_URL='/static/site/site.css')
  def test_site_stylesheet_comes_after_the_navigation_styles(self):
    html = self.get('/')
    self.assertLess(html.index('/static/navigation.css'), html.index('/static/site/site.css'))

  # --- breadcrumbs -------------------------------------------------------------

  def test_no_breadcrumbs_on_the_start_page(self):
    self.assertEqual(self.breadcrumbs(self.get('/')), '')

  def test_election_page_trail(self):
    html = self.get('/helios/elections/%s/view' % self.election.uuid)
    self.assertIn('aria-label="Breadcrumb"', self.breadcrumbs(html))
    self.assertEqual(self.crumbs(html), [('Home', '/'), (self.election.name, None)])

  def test_election_subpage_trail(self):
    view_url = '/helios/elections/%s/view' % self.election.uuid
    html = self.get('/helios/elections/%s/voters/list' % self.election.uuid)
    self.assertEqual(self.crumbs(html), [('Home', '/'), (self.election.name, view_url), ('Voters', None)])
    html = self.get('/helios/elections/%s/questions' % self.election.uuid)
    self.assertEqual(self.crumbs(html), [('Home', '/'), (self.election.name, view_url), ('Questions', None)])

  def test_trustee_page_trail(self):
    trustee = models.Trustee(uuid=str(uuid.uuid4()), election=self.election, name='Tess Trustee', email='tess@example.org')
    trustee.save()
    session = self.client.session
    session[HELIOS_TRUSTEE_UUID] = trustee.uuid
    session.save()
    html = self.get('/helios/elections/%s/trustees/%s/home' % (self.election.uuid, trustee.uuid))
    self.assertEqual(self.crumbs(html), [
      ('Home', '/'),
      (self.election.name, '/helios/elections/%s/view' % self.election.uuid),
      ('Trustees', '/helios/elections/%s/trustees/view' % self.election.uuid),
      ('Trustee Tess Trustee', None),
    ])

  def test_election_name_is_escaped(self):
    self.election.name = 'A <b>bold</b> election'
    self.election.save()
    html = self.get('/helios/elections/%s/view' % self.election.uuid)
    self.assertIn('A &lt;b&gt;bold&lt;/b&gt; election', self.breadcrumbs(html))

  def test_own_elections_trail(self):
    self.login()
    html = self.get('/helios/elections/administered')
    self.assertEqual(self.crumbs(html), [('Home', '/'), ('My elections', None)])

  def test_german_labels(self):
    html = self.get('/helios/elections/%s/voters/list' % self.election.uuid, HTTP_ACCEPT_LANGUAGE='de')
    crumbs = self.breadcrumbs(html)
    self.assertIn('aria-label="Brotkrümelnavigation"', crumbs)
    self.assertEqual(self.crumbs(html)[0], ('Startseite', '/'))
    self.assertIn('aria-label="Hauptnavigation"', self.site_nav(html))


class ErrorPageTests(TestCase):

  def test_not_found_page_links_to_the_start_page(self):
    response = self.client.get('/no/such/page')
    self.assertEqual(response.status_code, 404)
    self.assertIn('<a href="/">Go to the start page</a>', response.content.decode())
