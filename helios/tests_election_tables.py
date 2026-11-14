"""
The elections in "My elections", "My votes" and on the start page are shown as
a table (helios/templates/_election_table.html): name, status, voting period,
the numbers of voters and cast votes (administered elections only) and the
organization (only when organizations are configured).
"""

import datetime
import re
import uuid

from django.test import TestCase, override_settings

from helios import models
from helios_auth import models as auth_models


ORGANIZATIONS = {'unia': {'name': 'University A'}}


class ElectionTableTests(TestCase):
  fixtures = ['users.json', 'election.json']

  def setUp(self):
    self.user = auth_models.User.objects.get(user_id='ben@adida.net', user_type='google')
    template = models.Election.objects.all()[0]
    now = datetime.datetime.utcnow()
    self.open = self.make_election(template, 'Board 2026', frozen_at=now - datetime.timedelta(days=1),
                                   voting_ends_at=datetime.datetime(2099, 11, 20, 17, 0))
    for i in range(2):
      models.Voter.objects.create(uuid=str(uuid.uuid4()), election=self.open, voter_login_id='v%d@example.org' % i,
                                  voter_name='Voter %d' % i)
    self.scheduled = self.make_election(template, 'Faculty council, group of the students', frozen_at=now,
                                        voting_starts_at=datetime.datetime(2099, 12, 1, 8, 0),
                                        organization='unia')
    self.draft = self.make_election(template, 'Teaching committee')
    template.delete()
    session = self.client.session
    session['user'] = {'type': self.user.user_type, 'user_id': self.user.user_id}
    session.save()

  def make_election(self, template, name, **fields):
    election = models.Election.objects.get(pk=template.pk)
    election.pk = None
    election.uuid = str(uuid.uuid4())
    election.short_name = 'e-%s' % election.uuid[:8]
    election.name = name
    election.admin = self.user
    election.frozen_at = None
    election.voting_starts_at = None
    election.voting_ends_at = None
    election.archived_at = None
    for key, value in fields.items():
      setattr(election, key, value)
    election.save()
    models.Voter.objects.create(uuid=str(uuid.uuid4()), election=election, user=self.user,
                                voter_login_id=self.user.user_id, voter_name='Ben')
    return election

  def get(self, url, **kwargs):
    response = self.client.get(url, **kwargs)
    self.assertEqual(response.status_code, 200, url)
    return response.content.decode()

  def tables(self, html):
    return re.findall(r'<table class="election-table">.*?</table>', html, re.S)

  def header(self, table):
    return re.findall(r'<th scope="col"[^>]*>([^<]*)</th>', table)

  def row(self, table, name):
    for row in re.findall(r'<tr>(.*?)</tr>', table, re.S):
      if '>%s</a>' % name in row:
        return row
    self.fail('no row for %s' % name)

  def test_administered_is_a_table_with_counts(self):
    [table] = self.tables(self.get('/helios/elections/administered'))
    self.assertEqual(self.header(table), ['Name', 'Status', 'Voting', 'Voters', 'Cast votes'])
    row = self.row(table, 'Board 2026')
    self.assertIn('<th scope="row" class="election-name"><a href="/helios/elections/%s/view">' % self.open.uuid, row)
    self.assertIn('status-open', row)
    self.assertRegex(row, r'until <time class="tz-timestamp" datetime="2099-11-20T17:00Z"')
    # Ben and the two password voters; nobody has voted yet
    self.assertIn('<td class="num">3</td>', row)
    self.assertIn('<td class="num">0</td>', row)

  def test_voting_period_column(self):
    [table] = self.tables(self.get('/helios/elections/administered'))
    self.assertRegex(self.row(table, 'Faculty council, group of the students'),
                     r'from <time class="tz-timestamp" datetime="2099-12-01T08:00Z"')
    self.assertIn('aria-label="no voting period set">&ndash;</span>', self.row(table, 'Teaching committee'))

  def test_voted_is_a_table_without_counts(self):
    [table] = self.tables(self.get('/helios/elections/voted'))
    self.assertEqual(self.header(table), ['Name', 'Status', 'Voting'])
    self.assertNotIn('class="num"', table)

  def test_start_page_tables(self):
    html = self.get('/')
    administered, voted = self.tables(html)
    self.assertEqual(self.header(administered), ['Name', 'Status', 'Voting', 'Voters', 'Cast votes'])
    self.assertEqual(self.header(voted), ['Name', 'Status', 'Voting'])
    self.assertEqual(html.count('home-section home-section-wide'), 3)

  @override_settings(ORGANIZATIONS=ORGANIZATIONS)
  def test_organization_column_only_with_organizations(self):
    [table] = self.tables(self.get('/helios/elections/administered'))
    self.assertEqual(self.header(table)[-1], 'Organization')
    self.assertIn('<td>University A</td>', self.row(table, 'Faculty council, group of the students'))
    # elections of the default organization show the operator's name
    self.assertNotIn('<td>University A</td>', self.row(table, 'Board 2026'))

  def test_no_organization_column_without_organizations(self):
    [table] = self.tables(self.get('/helios/elections/administered'))
    self.assertNotIn('Organization', table)

  def test_names_are_escaped(self):
    self.draft.name = '<b>Committee</b>'
    self.draft.save()
    html = self.get('/helios/elections/administered')
    self.assertIn('&lt;b&gt;Committee&lt;/b&gt;', html)
    self.assertNotIn('<b>Committee</b>', html)

  def test_german(self):
    [table] = self.tables(self.get('/helios/elections/administered', HTTP_ACCEPT_LANGUAGE='de'))
    self.assertEqual(self.header(table), ['Name', 'Status', 'Stimmabgabe', 'Wahlberechtigte', 'Abgegebene Stimmen'])
    self.assertIn('bis <time', self.row(table, 'Board 2026'))
