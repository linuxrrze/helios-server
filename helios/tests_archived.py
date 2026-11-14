"""
Archived elections in the lists of one's elections ("My elections", "My votes"):
hidden by default, shown with ?archived=1, marked with an "Archived" badge.
"""

import datetime
import uuid

from django.test import TestCase

from helios import models
from helios_auth import models as auth_models


class ArchivedElectionListTests(TestCase):
  fixtures = ['users.json', 'election.json']

  def setUp(self):
    self.user = auth_models.User.objects.get(user_id='ben@adida.net', user_type='google')
    template = models.Election.objects.all()[0]
    self.active = self.make_election(template, 'Active election')
    self.archived = self.make_election(template, 'Archived election', archived=True)
    # the fixture election is not needed here
    template.delete()
    session = self.client.session
    session['user'] = {'type': self.user.user_type, 'user_id': self.user.user_id}
    session.save()

  def make_election(self, template, name, archived=False):
    election = models.Election.objects.get(pk=template.pk)
    election.pk = None
    election.uuid = str(uuid.uuid4())
    election.short_name = 'e-%s' % election.uuid[:8]
    election.name = name
    election.admin = self.user
    election.archived_at = datetime.datetime.utcnow() if archived else None
    election.save()
    models.Voter.objects.create(uuid=str(uuid.uuid4()), election=election, user=self.user,
                                voter_login_id=self.user.user_id, voter_name='Ben')
    return election

  def get(self, url):
    response = self.client.get(url)
    self.assertEqual(response.status_code, 200, url)
    return response.content.decode()

  def test_administered_hides_archived_by_default(self):
    html = self.get('/helios/elections/administered')
    self.assertIn('Active election', html)
    self.assertNotIn('Archived election', html)
    self.assertIn('Also show archived elections', html)
    self.assertIn('1 archived election hidden', html)
    self.assertNotIn(' checked', html)

  def test_administered_shows_archived_on_request(self):
    html = self.get('/helios/elections/administered?archived=1')
    self.assertIn('Active election', html)
    self.assertIn('Archived election', html)
    self.assertIn(' checked', html)
    self.assertNotIn('archived election hidden', html)
    self.assertIn('<span class="status-badge status-archived">Archived</span>', html)

  def test_voted_hides_archived_by_default(self):
    html = self.get('/helios/elections/voted')
    self.assertIn('Active election', html)
    self.assertNotIn('Archived election', html)
    self.assertIn('Also show archived elections', html)

  def test_voted_shows_archived_on_request(self):
    html = self.get('/helios/elections/voted?archived=1')
    self.assertIn('Archived election', html)
    self.assertIn('status-archived', html)

  def test_no_option_without_archived_elections(self):
    self.archived.delete()
    html = self.get('/helios/elections/administered')
    self.assertIn('Active election', html)
    self.assertNotIn('Also show archived elections', html)

  def test_active_election_has_no_archived_badge(self):
    html = self.get('/helios/elections/administered?archived=1')
    self.assertEqual(html.count('status-archived'), 1)

  def test_only_archived_elections(self):
    self.active.delete()
    html = self.get('/helios/elections/administered')
    # no misleading "no elections yet", and the way to the archived one
    self.assertIn('All your elections are archived.', html)
    self.assertNotIn("You don't administer any elections yet.", html)
    self.assertIn('Also show archived elections', html)

  def test_german(self):
    response = self.client.get('/helios/elections/administered', HTTP_ACCEPT_LANGUAGE='de')
    html = response.content.decode()
    self.assertIn('Auch archivierte Wahlen anzeigen', html)
    self.assertIn('1 archivierte Wahl ausgeblendet', html)
