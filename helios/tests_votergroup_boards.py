"""
Tests for voter groups as boards: name and term of office of a group,
initials, person group and status of its members, members without a vote, and
the CSV export and import of the members.
"""

import codecs
import csv
import datetime
import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from helios import votergroup as vg
from helios.models import Election, Voter, VoterGroup, VoterGroupLog, VoterGroupMember
from helios_auth.models import User


class _BoardTestCase(TestCase):
  """The group "council" owned by Olga with the additional administrator Carl
  and the members Anna (2 votes) and Bea (1 vote)."""

  def setUp(self):
    def user(user_id, name, **kw):
      return User.objects.create(user_type='password', user_id=user_id, name=name,
                                 info={'password': 'x', 'email': '%s@example.org' % user_id}, **kw)
    self.owner = user('owner', 'Olga Owner')
    self.coadmin = user('coadmin', 'Carl Coadmin')
    self.stranger = user('stranger', 'Sam Stranger')
    self.siteadmin = user('siteadmin', 'Sita Admin', admin_p=True)
    self.group = VoterGroup.objects.create(admin=self.owner, uuid='g1', short_name='council')
    self.group.admins.add(self.coadmin)
    self.anna = self.member('m1', 'Anna', 'Arp', 'anna@example.org', votes=2)
    self.bea = self.member('m2', 'Bea', 'Bach', 'bea@example.org')

  def member(self, uuid, first_name, last_name, email, group=None, **kw):
    return VoterGroupMember.objects.create(group=group or self.group, uuid=uuid, first_name=first_name,
                                           last_name=last_name, email=email, **kw)

  def login(self, user):
    self.client.get("/")
    session = self.client.session
    session['user'] = {'type': user.user_type, 'user_id': user.user_id}
    session['csrf_token'] = 'tok'
    session.save()

  def provisioned_group(self):
    group = VoterGroup.objects.create(admin=self.owner, uuid='p1', short_name='idm-board', provisioned_by='scim:idm',
                                      provisioned_ref='ref1')
    group.admins.add(self.coadmin)
    self.member('pm1', 'Paul', 'Prov', 'paul@example.org', group=group)
    return group

  def assertRedirectsTo(self, response, path):
    self.assertEqual(response.status_code, 302)
    self.assertTrue(response['Location'].endswith(path), response['Location'])

  def reload(self):
    self.group = VoterGroup.objects.get(pk=self.group.pk)
    return self.group

  def actions(self):
    return [e.action for e in self.reload().get_log()]


class BoardModelTests(_BoardTestCase):

  def test_defaults(self):
    self.assertEqual((self.group.name, self.group.name_suffix, self.group.term_starts_on, self.group.term_ends_on),
                     ('', '', None, None))
    self.assertEqual(self.group.display_name, 'council')
    self.assertFalse(self.group.term_ended)
    self.assertEqual((self.anna.initials, self.anna.person_group, self.anna.status), ('', '', 'member'))

  def test_display_name_and_summary(self):
    self.group.name, self.group.name_suffix = 'Faculty council', '2025-2027'
    self.group.term_starts_on, self.group.term_ends_on = datetime.date(2025, 10, 1), datetime.date(2027, 9, 30)
    self.assertEqual(self.group.display_name, 'Faculty council')
    self.assertEqual(self.group.details_summary(), 'Faculty council · 2025-2027 · 2025-10-01 – 2027-09-30')
    self.assertEqual(VoterGroup(short_name='x').details_summary(), '—')

  def test_term_ended_from_the_day_after(self):
    today = datetime.date.today()
    self.group.term_ends_on = today
    self.assertFalse(self.group.term_ended)
    self.group.term_ends_on = today - datetime.timedelta(days=1)
    self.assertTrue(self.group.term_ended)

  def test_member_order_by_person_group_then_name(self):
    self.member('m3', 'Carl', 'Arp', 'carl@example.org', person_group='Students')
    self.member('m4', 'Dora', 'Zorn', 'dora@example.org', person_group='Professors')
    self.member('m5', 'Emil', 'Berg', 'emil@example.org', person_group='Professors')
    self.member('m6', 'Abe', 'Arp', 'abe@example.org', person_group='Students')
    self.assertEqual([m.uuid for m in self.group.get_members()], ['m5', 'm4', 'm6', 'm3', 'm1', 'm2'])

  def test_list_counts_members(self):
    group = VoterGroup.get_by_user_as_admin(self.coadmin).get()
    self.assertEqual(group.num_members, 2)


class NoVoteTests(_BoardTestCase):
  """Members with 0 votes have no voting right."""

  def _election_post(self):
    seats = vg.initial_seats(self.group.get_members())
    data = {'csrf_token': 'tok', 'form-TOTAL_FORMS': str(len(seats)), 'form-INITIAL_FORMS': str(len(seats)),
            'short_name': 'council-2026', 'name': 'Council 2026', 'description': 'd',
            'election_type': 'election', 'help_email': 'owner@example.org'}
    for i, s in enumerate(seats):
      for k, v in s.items():
        data['form-%d-%s' % (i, k)] = v
    return data

  def test_no_seats_for_zero_votes(self):
    self.member('m3', 'Dan', 'Deputy', 'dan@example.org', votes=0, status='deputy')
    seats = vg.initial_seats(self.group.get_members())
    self.assertEqual([s['email'] for s in seats], ['anna@example.org', 'anna@example.org', 'bea@example.org'])

  def test_add_member_with_zero_votes(self):
    self.login(self.owner)
    response = self.client.post('/helios/votergroup/memberadd/g1', {
      'csrf_token': 'tok', 'first_name': 'Dan', 'last_name': 'Deputy', 'email': 'dan@example.org', 'votes': '0',
      'initials': 'DD', 'person_group': 'Staff', 'status': 'deputy'})
    self.assertEqual(response.status_code, 302)
    dan = VoterGroupMember.objects.get(email='dan@example.org')
    self.assertEqual((dan.votes, dan.initials, dan.person_group, dan.status), (0, 'DD', 'Staff', 'deputy'))
    response = self.client.post('/helios/votergroup/memberadd/g1', {
      'csrf_token': 'tok', 'first_name': 'X', 'last_name': 'Y', 'email': 'x@example.org', 'votes': '-1'})
    self.assertEqual(response.status_code, 200)
    self.assertFalse(VoterGroupMember.objects.filter(email='x@example.org').exists())

  def test_prepare_shows_members_without_vote_and_issues_no_voter(self):
    self.member('m3', 'Dan', 'Deputy', 'dan@example.org', votes=0, status='deputy')
    self.login(self.owner)
    response = self.client.get('/helios/votergroup/prepareelection/g1')
    self.assertContains(response, '1 member has no voting right (0 votes)')
    self.assertContains(response, 'Deputy, Dan')
    self.assertContains(response, 'no vote')
    self.assertEqual(response.context['form'].total_form_count(), 3)
    response = self.client.post('/helios/votergroup/prepareelection/g1', self._election_post())
    self.assertEqual(response.status_code, 302)
    voters = Voter.objects.filter(election__short_name='council-2026')
    self.assertEqual(voters.count(), 3)
    self.assertFalse(voters.filter(voter_email='dan@example.org').exists())

  def test_prepare_refused_when_nobody_has_a_vote(self):
    VoterGroupMember.objects.update(votes=0)
    self.login(self.owner)
    response = self.client.get('/helios/votergroup/prepareelection/g1')
    self.assertContains(response, 'None of the members of the voter group has a vote')
    self.assertNotContains(response, 'id="create_election_form"')
    # also when rows are posted anyway
    data = self._election_post()
    data.update({'form-TOTAL_FORMS': '1', 'form-INITIAL_FORMS': '1', 'form-0-first_name': 'Anna',
                 'form-0-last_name': 'Arp', 'form-0-email': 'anna@example.org', 'form-0-login_id': 'AA1',
                 'form-0-delegate_to': ''})
    response = self.client.post('/helios/votergroup/prepareelection/g1', data)
    self.assertEqual(response.status_code, 200)
    self.assertContains(response, 'None of the members of the voter group has a vote')
    self.assertFalse(Election.objects.exists())

  def test_member_table_marks_zero_votes(self):
    self.member('m3', 'Dan', 'Deputy', 'dan@example.org', votes=0)
    self.login(self.owner)
    self.assertContains(self.client.get('/helios/votergroup/view/g1'), 'no voting right')


class GroupSettingsTests(_BoardTestCase):

  def post(self, **data):
    return self.client.post('/helios/votergroup/edit/g1', dict({'csrf_token': 'tok', 'short_name': 'council'}, **data))

  def test_edit_and_log(self):
    self.login(self.coadmin)
    response = self.client.get('/helios/votergroup/edit/g1')
    self.assertContains(response, 'value="council"')
    self.assertContains(response, 'type="date"')
    response = self.post(name='Faculty council of the Faculty of Sciences', name_suffix='2025-2027',
                         term_starts_on='2025-10-01', term_ends_on='2027-09-30')
    self.assertEqual(response.status_code, 302)
    group = self.reload()
    self.assertEqual(group.name, 'Faculty council of the Faculty of Sciences')
    self.assertEqual(group.term_ends_on, datetime.date(2027, 9, 30))
    entry = group.get_log().first()
    self.assertEqual((entry.action, entry.actor), (VoterGroupLog.DETAILS_CHANGED, self.coadmin))
    self.assertEqual(entry.message, 'Name and term of office changed: Faculty council of the Faculty of Sciences'
                                    ' · 2025-2027 · 2025-10-01 – 2027-09-30')
    # nothing changed: no entry; a new short name: renamed
    self.post(name='Faculty council of the Faculty of Sciences', name_suffix='2025-2027',
              term_starts_on='2025-10-01', term_ends_on='2027-09-30')
    self.assertEqual(self.actions(), [VoterGroupLog.DETAILS_CHANGED])
    self.post(short_name='faculty-council', name='Faculty council of the Faculty of Sciences', name_suffix='2025-2027',
              term_starts_on='2025-10-01', term_ends_on='2027-09-30')
    self.assertEqual(self.actions(), [VoterGroupLog.RENAMED, VoterGroupLog.DETAILS_CHANGED])
    self.assertEqual(self.group.short_name, 'faculty-council')

  def test_log_in_german(self):
    self.group.append_log(VoterGroupLog.DETAILS_CHANGED, actor=self.owner, details='Senate')
    self.login(self.owner)
    self.assertContains(self.client.get('/helios/votergroup/view/g1', HTTP_ACCEPT_LANGUAGE='de'),
                        'Name und Amtszeit geändert: Senate')

  def test_end_before_start_is_refused(self):
    self.login(self.owner)
    response = self.post(term_starts_on='2027-10-01', term_ends_on='2025-09-30')
    self.assertEqual(response.status_code, 200)
    self.assertContains(response, 'The term of office cannot end before it begins.')
    self.assertIsNone(self.reload().term_starts_on)
    self.assertEqual(self.actions(), [])

  def test_short_name_taken(self):
    VoterGroup.objects.create(admin=self.owner, uuid='g2', short_name='senate')
    self.login(self.coadmin)
    response = self.post(short_name='senate', name='Senate')
    self.assertContains(response, 'The owner of the group already has a voter group named senate')
    self.assertEqual((self.reload().short_name, self.group.name), ('council', ''))

  def test_create_with_details(self):
    self.login(self.owner)
    self.client.post('/helios/votergroup/new', {'csrf_token': 'tok', 'short_name': 'senate', 'name': 'Senate',
                                                'term_starts_on': '2025-04-01'})
    group = VoterGroup.objects.get(short_name='senate')
    self.assertEqual((group.name, group.term_starts_on), ('Senate', datetime.date(2025, 4, 1)))
    self.assertEqual([e.action for e in group.get_log()], [VoterGroupLog.DETAILS_CHANGED, VoterGroupLog.CREATED])

  def test_access(self):
    self.login(self.owner)
    self.assertEqual(self.post(csrf_token='wrong', name='X').status_code, 400)
    self.login(self.stranger)
    self.assertEqual(self.client.get('/helios/votergroup/edit/g1').status_code, 404)
    self.assertEqual(self.post(name='X').status_code, 404)
    self.login(self.siteadmin)
    self.assertEqual(self.client.get('/helios/votergroup/edit/g1').status_code, 403)
    self.assertEqual(self.post(name='X').status_code, 403)
    self.assertEqual(self.reload().name, '')
    self.provisioned_group()
    self.login(self.coadmin)
    self.assertEqual(self.client.get('/helios/votergroup/edit/p1').status_code, 403)
    response = self.client.post('/helios/votergroup/edit/p1', {'csrf_token': 'tok', 'short_name': 'x', 'name': 'X'})
    self.assertEqual(response.status_code, 403)
    self.assertEqual(VoterGroup.objects.get(uuid='p1').name, '')

  def test_shown_on_page_and_in_list(self):
    self.group.name, self.group.name_suffix = 'Faculty council of the Faculty of Sciences', '2025-2027'
    self.group.term_starts_on, self.group.term_ends_on = datetime.date(2025, 10, 1), datetime.date(2099, 9, 30)
    self.group.save()
    self.login(self.owner)
    content = self.client.get('/helios/votergroup/view/g1').content.decode()
    self.assertIn('<h1 class="title">Faculty council of the Faculty of Sciences <span class="muted">2025-2027</span>', content)
    self.assertIn('<span class="short-name">council</span>', content)
    self.assertIn('Oct. 1, 2025 – Sept. 30, 2099', content)
    self.assertNotIn('Term ended', content)
    content = self.client.get('/helios/votergroup/administered').content.decode()
    self.assertIn('>Faculty council of the Faculty of Sciences</a> 2025-2027', content)
    self.assertIn('<span class="short-name muted">council</span>', content)
    self.assertIn('<td class="num">2</td>', content)

  def test_term_ended_badge(self):
    self.group.term_ends_on = datetime.date.today() - datetime.timedelta(days=1)
    self.group.save()
    self.login(self.owner)
    self.assertContains(self.client.get('/helios/votergroup/view/g1'), 'Term ended')
    self.assertContains(self.client.get('/helios/votergroup/administered'), 'Term ended')
    self.assertContains(self.client.get('/helios/votergroup/view/g1', HTTP_ACCEPT_LANGUAGE='de'), 'Amtszeit beendet')
    # everything keeps working
    self.assertEqual(self.client.get('/helios/votergroup/prepareelection/g1').status_code, 200)
    self.login(self.siteadmin)
    self.assertContains(self.client.get('/helios/votergroup/view/g1'), 'Term ended')


class MemberEditTests(_BoardTestCase):

  def post(self, uuid='m1', **data):
    fields = {'csrf_token': 'tok', 'first_name': 'Anna', 'last_name': 'Arp', 'email': 'anna@example.org', 'votes': '2',
              'initials': '', 'person_group': '', 'status': 'member'}
    return self.client.post('/helios/votergroup/memberedit/%s' % uuid, dict(fields, **data))

  def test_edit_and_log(self):
    self.login(self.coadmin)
    response = self.client.get('/helios/votergroup/memberedit/m1')
    self.assertContains(response, 'value="anna@example.org"')
    self.assertContains(response, 'Deputy member')
    with self.assertLogs('helios.votergroup', 'INFO') as logs:
      response = self.post(votes='0', initials='AA', person_group='Professors', status='advisory')
    self.assertEqual(response.status_code, 302)
    anna = VoterGroupMember.objects.get(uuid='m1')
    self.assertEqual((anna.votes, anna.initials, anna.person_group, anna.status), (0, 'AA', 'Professors', 'advisory'))
    entry = self.reload().get_log().get()
    self.assertEqual((entry.action, entry.actor, entry.message),
                     (VoterGroupLog.MEMBER_CHANGED, self.coadmin, 'Member a***@example.org changed, now with 0 vote(s)'))
    self.assertNotIn('anna@example.org', logs.output[0])
    # without changes: no entry
    self.post(votes='0', initials='AA', person_group='Professors', status='advisory')
    self.assertEqual(self.actions(), [VoterGroupLog.MEMBER_CHANGED])

  def test_duplicate_addresses_are_refused(self):
    self.login(self.owner)
    response = self.client.post('/helios/votergroup/memberadd/g1', {
      'csrf_token': 'tok', 'first_name': 'Anna', 'last_name': 'Again', 'email': 'ANNA@example.org', 'votes': '1'})
    self.assertEqual(response.status_code, 200)
    self.assertContains(response, 'Another member of the group already has this email address.')
    self.assertEqual(VoterGroupMember.objects.filter(email__iexact='anna@example.org').count(), 1)
    # changing an address to another member's is refused, keeping one's own (in other case) is not
    response = self.post(uuid='m2', first_name='Bea', last_name='Bach', email='Anna@Example.org', votes='1')
    self.assertContains(response, 'Another member of the group already has this email address.')
    self.assertEqual(VoterGroupMember.objects.get(uuid='m2').email, 'bea@example.org')
    self.assertEqual(self.post(email='ANNA@example.org', votes='3').status_code, 302)
    self.assertEqual(VoterGroupMember.objects.get(uuid='m1').votes, 3)
    # the same address in another group is fine
    VoterGroup.objects.create(admin=self.owner, uuid='g2', short_name='senate')
    response = self.client.post('/helios/votergroup/memberadd/g2', {
      'csrf_token': 'tok', 'first_name': 'Anna', 'last_name': 'Arp', 'email': 'anna@example.org', 'votes': '1'})
    self.assertEqual(response.status_code, 302)
    # in German
    response = self.client.post('/helios/votergroup/memberadd/g1', {
      'csrf_token': 'tok', 'first_name': 'Bea', 'last_name': 'B', 'email': 'bea@example.org', 'votes': '1'},
      HTTP_ACCEPT_LANGUAGE='de')
    self.assertContains(response, 'Ein anderes Mitglied der Gruppe hat bereits diese E-Mail-Adresse.')

  def test_older_duplicates_can_still_be_edited(self):
    self.member('m3', 'Anna', 'Arp', 'Anna@example.org', votes=1)
    self.login(self.owner)
    response = self.post(uuid='m3', email='Anna@example.org', votes='0', status='deputy')
    self.assertEqual(response.status_code, 302)
    self.assertEqual(VoterGroupMember.objects.get(uuid='m3').status, 'deputy')
    # but not to the address of yet another member
    response = self.post(uuid='m3', email='bea@example.org', votes='0')
    self.assertContains(response, 'Another member of the group already has this email address.')

  def test_table_shows_new_columns_only_when_used(self):
    self.login(self.owner)
    content = self.client.get('/helios/votergroup/view/g1').content.decode()
    self.assertNotIn('>Person group<', content)
    self.assertIn('/helios/votergroup/memberedit/m1', content)
    VoterGroupMember.objects.filter(uuid='m2').update(person_group='Students', status='guest', initials='BB')
    content = self.client.get('/helios/votergroup/view/g1').content.decode()
    for text in ['>Person group<', '>Status<', '>Initials<', '>Students<', '>Guest<', '>BB<']:
      self.assertIn(text, content)
    # Bea (Students) before Anna (no person group)
    self.assertLess(content.index('Bach, Bea'), content.index('Arp, Anna'))

  def test_access(self):
    self.login(self.owner)
    self.assertEqual(self.post(csrf_token='wrong', votes='5').status_code, 400)
    self.login(self.stranger)
    self.assertEqual(self.client.get('/helios/votergroup/memberedit/m1').status_code, 404)
    self.assertEqual(self.post(votes='5').status_code, 404)
    self.assertEqual(self.client.get('/helios/votergroup/memberedit/nope').status_code, 404)
    self.login(self.siteadmin)
    self.assertEqual(self.client.get('/helios/votergroup/memberedit/m1').status_code, 403)
    self.assertEqual(self.post(votes='5').status_code, 403)
    self.assertEqual(VoterGroupMember.objects.get(uuid='m1').votes, 2)
    self.provisioned_group()
    self.login(self.coadmin)
    self.assertEqual(self.client.get('/helios/votergroup/memberedit/pm1').status_code, 403)
    self.assertEqual(self.post(uuid='pm1', first_name='Paul', last_name='Prov', email='paul@example.org',
                               votes='5').status_code, 403)
    self.assertEqual(VoterGroupMember.objects.get(uuid='pm1').votes, 1)
    self.assertNotContains(self.client.get('/helios/votergroup/view/p1'), 'memberedit')


class CsvExportTests(_BoardTestCase):

  def test_export(self):
    VoterGroupMember.objects.filter(uuid='m2').update(person_group='Students', status='deputy', initials='BB', votes=0)
    self.member('m3', '=cmd', 'Evil', 'evil@example.org')
    self.login(self.coadmin)
    response = self.client.get('/helios/votergroup/export/g1')
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
    self.assertIn('members-council.csv', response['Content-Disposition'])
    content = response.content.decode('utf-8')
    self.assertTrue(content.startswith('﻿'))
    rows = list(csv.reader(io.StringIO(content[1:])))
    self.assertEqual(rows, [
      ['first_name', 'last_name', 'email', 'votes', 'initials', 'person_group', 'status'],
      ['Bea', 'Bach', 'bea@example.org', '0', 'BB', 'Students', 'deputy'],
      ['Anna', 'Arp', 'anna@example.org', '2', '', '', 'member'],
      ["'=cmd", 'Evil', 'evil@example.org', '1', '', '', 'member'],
    ])

  def test_access(self):
    self.login(self.stranger)
    self.assertEqual(self.client.get('/helios/votergroup/export/g1').status_code, 404)
    self.login(self.siteadmin)
    response = self.client.get('/helios/votergroup/export/g1')
    self.assertEqual(response.status_code, 403)
    self.assertNotIn(b'anna@example.org', response.content)
    self.assertNotContains(self.client.get('/helios/votergroup/view/g1'), '/helios/votergroup/export/')
    # a provisioned group can be exported by its administrators
    self.provisioned_group()
    self.login(self.coadmin)
    response = self.client.get('/helios/votergroup/export/p1')
    self.assertContains(response, 'paul@example.org')


class CsvImportTests(_BoardTestCase):

  def upload(self, content, remove_missing=False, group='g1', encoding='utf-8', headers=None, **extra):
    data = content.encode(encoding) if isinstance(content, str) else content
    post = {'csrf_token': 'tok', 'members_file': SimpleUploadedFile('members.csv', data, content_type='text/csv')}
    if remove_missing:
      post['remove_missing'] = 'on'
    post.update(extra)
    return self.client.post('/helios/votergroup/import/%s' % group, post, **(headers or {}))

  def preview(self, group='g1'):
    return self.client.get('/helios/votergroup/import/%s/preview' % group)

  def confirm(self, group='g1', **extra):
    response = self.preview(group)
    data = {'csrf_token': 'tok', 'confirm': '1', 'digest': response.context['digest']}
    data.update(extra)
    return self.client.post('/helios/votergroup/import/%s/preview' % group, data)

  def emails(self):
    return sorted(m.email for m in self.group.get_members())

  def test_english_headers_with_commas(self):
    self.login(self.owner)
    response = self.upload('first_name,last_name,email,votes,initials,person_group,status\n'
                           'Anna,Arp,ANNA@example.org,3,AA,Professors,member\n'
                           'Bea,Bach,bea@example.org,1,,,\n'
                           'Cleo,Cole,cleo@example.org,0,CC,Students,advisory\n')
    self.assertRedirectsTo(response, '/helios/votergroup/import/g1/preview')
    response = self.preview()
    plan = response.context['plan']
    self.assertEqual((len(plan['add']), len(plan['update']), len(plan['remove']), plan['unchanged']), (1, 1, 0, 1))
    self.assertContains(response, 'Cole, Cleo')
    self.assertContains(response, '2 &rarr; 3')
    # nothing changed yet
    self.assertEqual(self.emails(), ['anna@example.org', 'bea@example.org'])
    with self.assertLogs('helios.votergroup', 'INFO') as logs:
      response = self.confirm()
    self.assertRedirectsTo(response, '/helios/votergroup/view/g1')
    self.assertEqual(self.emails(), ['ANNA@example.org', 'bea@example.org', 'cleo@example.org'])
    anna = VoterGroupMember.objects.get(uuid='m1')
    self.assertEqual((anna.votes, anna.initials, anna.person_group), (3, 'AA', 'Professors'))
    cleo = VoterGroupMember.objects.get(email='cleo@example.org')
    self.assertEqual((cleo.votes, cleo.status, cleo.person_group), (0, 'advisory', 'Students'))
    entry = self.reload().get_log().get()
    self.assertEqual(entry.message, 'Members imported from a file: 1 added, 1 updated, 0 removed')
    self.assertEqual(entry.params, {'added': 1, 'updated': 1, 'removed': 0})
    self.assertIn('1 added, 1 updated, 0 removed', logs.output[0])
    self.assertNotIn('example.org', logs.output[0].split('by')[0])
    # the notice on the group's page, once; the pending import is gone
    self.assertContains(self.client.get('/helios/votergroup/view/g1'), 'Members imported: 1 added')
    self.assertNotContains(self.client.get('/helios/votergroup/view/g1'), 'Members imported: 1 added')
    self.assertNotIn('votergroup_import', self.client.session)

  def test_german_headers_semicolons_bom_and_windows_encoding(self):
    self.login(self.owner)
    content = ('Vorname;Nachname;E-Mail;Stimmen;Kürzel;Personengruppe;Status\r\n'
               'Jürgen;Müller;juergen@example.org;0;JM;Wissenschaftliche Mitarbeitende;Ersatzmitglied\r\n'
               'Gisela;Groß;gisela@example.org;;GG;Studierende;beratend\r\n'
               'Hans;Huber;hans@example.org;1;;;Gast\r\n'
               'Bea;Bach;bea@example.org;1;;;Mitglied\r\n')
    for encoding, data in [('utf-8-bom', codecs.BOM_UTF8 + content.encode('utf-8')), ('cp1252', content.encode('cp1252')),
                           ('utf-16', content.encode('utf-16'))]:
      VoterGroupMember.objects.filter(email__in=['juergen@example.org', 'gisela@example.org', 'hans@example.org']).delete()
      self.upload(data)
      plan = self.preview().context['plan']
      self.assertEqual((len(plan['add']), len(plan['update']), plan['unchanged']), (3, 0, 1), encoding)
      self.confirm()
      juergen = VoterGroupMember.objects.get(email='juergen@example.org')
      self.assertEqual((juergen.first_name, juergen.last_name, juergen.votes, juergen.status, juergen.person_group),
                       ('Jürgen', 'Müller', 0, 'deputy', 'Wissenschaftliche Mitarbeitende'), encoding)
      gisela = VoterGroupMember.objects.get(email='gisela@example.org')
      self.assertEqual((gisela.last_name, gisela.votes, gisela.status), ('Groß', 1, 'advisory'), encoding)
      self.assertEqual(VoterGroupMember.objects.get(email='hans@example.org').status, 'guest')

  def test_name_column_and_missing_columns_keep_values(self):
    self.login(self.owner)
    # "Name" is the last name; without a votes column Anna keeps her 2 votes
    self.upload('Vorname,Name,E-Mail,Bemerkung\nAnna,Arp-Berg,anna@example.org,x\nNeu,Mann,neu@example.org,y\n')
    response = self.preview()
    self.assertContains(response, 'Ignored columns: Bemerkung.')
    self.assertContains(response, 'left unchanged for existing members: Votes, Initials, Person group, Status')
    self.confirm()
    anna = VoterGroupMember.objects.get(uuid='m1')
    self.assertEqual((anna.last_name, anna.votes), ('Arp-Berg', 2))
    new = VoterGroupMember.objects.get(email='neu@example.org')
    self.assertEqual((new.votes, new.status), (1, 'member'))

  def test_remove_only_when_asked(self):
    self.login(self.owner)
    content = 'first_name,last_name,email\nAnna,Arp,anna@example.org\n'
    self.upload(content)
    self.assertEqual(self.preview().context['plan']['remove'], [])
    self.assertContains(self.preview(), 'Members who are not in the file stay in the group.')
    self.upload(content, remove_missing=True)
    response = self.preview()
    self.assertEqual([m.uuid for m in response.context['plan']['remove']], ['m2'])
    self.assertContains(response, 'Bach, Bea')
    self.confirm()
    self.assertEqual(self.emails(), ['anna@example.org'])
    self.assertEqual(self.reload().get_log().get().params, {'added': 0, 'updated': 0, 'removed': 1})

  def test_errors_with_line_numbers_reject_everything(self):
    self.login(self.owner)
    response = self.upload('first_name,last_name,email,votes,status\n'
                           'Neu,Mann,neu@example.org,1,member\n'
                           '\n'
                           'Bad,Mail,not-an-address,1,member\n'
                           'Bad,Status,bs@example.org,1,chair\n'
                           'Bad,Votes,bv@example.org,many,member\n'
                           'Too,Many,tm@example.org,101,member\n'
                           'Neu,Again,NEU@example.org,1,member\n')
    self.assertEqual(response.status_code, 200)
    lines = [line for line, _message in response.context['errors']]
    self.assertEqual(lines, [4, 5, 6, 7, 8])
    self.assertContains(response, 'Line 4:')
    self.assertContains(response, 'Unknown status')
    self.assertContains(response, 'already in line 2')
    self.assertContains(response, 'The file was not imported.')
    self.assertNotIn('votergroup_import', self.client.session)
    self.assertEqual(self.emails(), ['anna@example.org', 'bea@example.org'])
    self.assertEqual(self.actions(), [])
    # in German
    response = self.upload('Vorname;Nachname;E-Mail\nA;B;kaputt\n', headers={'HTTP_ACCEPT_LANGUAGE': 'de'})
    self.assertContains(response, 'Zeile 2:')

  def test_file_level_errors(self):
    self.login(self.owner)
    response = self.upload('vorname,email\nAnna,anna@example.org\n')
    self.assertContains(response, 'The header row lacks the column(s) last_name.')
    response = self.upload('first_name,last_name,email\n')
    self.assertContains(response, 'The file contains no members.')
    response = self.upload('')
    self.assertEqual(response.status_code, 200)
    self.assertFalse(response.context['form'].is_valid())
    with mock.patch.object(vg, 'MAX_IMPORT_ROWS', 2):
      response = self.upload('first_name,last_name,email\nA,B,a@example.org\nC,D,c@example.org\nE,F,e@example.org\n')
      self.assertContains(response, 'too many rows (at most 2)')
    with mock.patch.object(vg, 'MAX_IMPORT_BYTES', 10):
      response = self.upload('first_name,last_name,email\nA,B,a@example.org\n')
      self.assertContains(response, 'The file is too large')
    self.assertEqual(self.actions(), [])

  def test_ambiguous_address_in_group(self):
    self.member('m3', 'Anna', 'Arp', 'Anna@example.org')
    self.login(self.owner)
    response = self.upload('first_name,last_name,email\nAnna,Arp,anna@example.org\n')
    self.assertContains(response, 'Several members of the group have the address anna@example.org')

  def test_members_changed_after_preview(self):
    self.login(self.owner)
    self.upload('first_name,last_name,email,votes\nAnna,Arp,anna@example.org,5\n')
    digest = self.preview().context['digest']
    VoterGroupMember.objects.filter(uuid='m1').update(votes=4)
    response = self.client.post('/helios/votergroup/import/g1/preview', {'csrf_token': 'tok', 'digest': digest})
    self.assertEqual(response.status_code, 200)
    self.assertContains(response, 'The members of the group changed after the preview was shown.')
    self.assertEqual(VoterGroupMember.objects.get(uuid='m1').votes, 4)
    self.confirm()
    self.assertEqual(VoterGroupMember.objects.get(uuid='m1').votes, 5)

  def test_cancel_and_missing_pending_import(self):
    self.login(self.owner)
    self.assertRedirectsTo(self.preview(), '/helios/votergroup/import/g1')
    self.upload('first_name,last_name,email\nNeu,Mann,neu@example.org\n')
    response = self.client.post('/helios/votergroup/import/g1/preview', {'csrf_token': 'tok', 'cancel': '1'})
    self.assertRedirectsTo(response, '/helios/votergroup/view/g1')
    self.assertNotIn('votergroup_import', self.client.session)
    self.assertEqual(self.emails(), ['anna@example.org', 'bea@example.org'])
    # a pending import is only for its group
    VoterGroup.objects.create(admin=self.owner, uuid='g2', short_name='senate')
    self.upload('first_name,last_name,email\nNeu,Mann,neu@example.org\n')
    self.assertRedirectsTo(self.preview('g2'), '/helios/votergroup/import/g2')

  def test_export_then_import_changes_nothing(self):
    VoterGroupMember.objects.filter(uuid='m2').update(person_group='Students', status='deputy', initials='BB', votes=0)
    self.member('m3', '-Dash', 'Evil', 'evil@example.org')
    self.login(self.owner)
    exported = self.client.get('/helios/votergroup/export/g1').content
    self.upload(exported, remove_missing=True)
    response = self.preview()
    plan = response.context['plan']
    self.assertEqual((len(plan['add']), len(plan['update']), len(plan['remove']), plan['unchanged']), (0, 0, 0, 3))
    self.assertContains(response, 'The file contains no changes.')

  def test_provisioned_group_refused(self):
    self.provisioned_group()
    self.login(self.coadmin)
    self.assertEqual(self.client.get('/helios/votergroup/import/p1').status_code, 403)
    self.assertEqual(self.upload('first_name,last_name,email\nA,B,a@example.org\n', group='p1').status_code, 403)
    self.assertEqual(self.preview('p1').status_code, 403)
    self.assertEqual(VoterGroupMember.objects.filter(group__uuid='p1').count(), 1)
    self.assertNotContains(self.client.get('/helios/votergroup/view/p1'), '/helios/votergroup/import/')

  def test_access_and_csrf(self):
    content = 'first_name,last_name,email\nNeu,Mann,neu@example.org\n'
    self.login(self.owner)
    self.assertEqual(self.upload(content, csrf_token='wrong').status_code, 400)
    self.upload(content)
    response = self.client.post('/helios/votergroup/import/g1/preview', {'csrf_token': 'wrong', 'digest': 'x'})
    self.assertEqual(response.status_code, 400)
    self.assertEqual(self.client.get('/helios/votergroup/import/g1').status_code, 200)
    self.login(self.stranger)
    for url in ['/helios/votergroup/import/g1', '/helios/votergroup/import/g1/preview']:
      self.assertEqual(self.client.get(url).status_code, 404, url)
    self.assertEqual(self.upload(content).status_code, 404)
    self.login(self.siteadmin)
    for url in ['/helios/votergroup/import/g1', '/helios/votergroup/import/g1/preview']:
      self.assertEqual(self.client.get(url).status_code, 403, url)
    self.assertEqual(self.upload(content).status_code, 403)
    self.assertEqual(self.emails(), ['anna@example.org', 'bea@example.org'])

  def test_breadcrumbs(self):
    self.login(self.owner)
    self.upload('first_name,last_name,email\nNeu,Mann,neu@example.org\n')
    response = self.preview()
    self.assertContains(response, 'href="/helios/votergroup/import/g1"')
    self.assertContains(response, 'href="/helios/votergroup/view/g1"')


class CsvParseTests(TestCase):

  def test_delimiter(self):
    self.assertEqual(vg.sniff_delimiter('a;b;c'), ';')
    self.assertEqual(vg.sniff_delimiter('a,b,c'), ',')
    self.assertEqual(vg.sniff_delimiter('a\tb\tc'), '\t')
    self.assertEqual(vg.sniff_delimiter('email'), ',')

  def test_quoted_fields_and_formula_prefix(self):
    rows, columns, errors, ignored = vg.parse_member_csv(
      b'"first_name";"last_name";"email"\n"\'=A";"Doe; Jr.";"a@example.org"\n')
    self.assertEqual(errors, [])
    self.assertEqual(rows, [{'first_name': '=A', 'last_name': 'Doe; Jr.', 'email': 'a@example.org', 'line': 2}])
    self.assertEqual(vg.csv_safe('@x'), "'@x")
    self.assertEqual(vg.csv_safe("O'Brien"), "O'Brien")

  def test_status_words(self):
    rows, _columns, errors, _ignored = vg.parse_member_csv(
      'first_name,last_name,email,status\n'
      'A,B,a@example.org,Deputy member\nC,D,c@example.org,BERATENDES MITGLIED\nE,F,e@example.org,guest\n'.encode())
    self.assertEqual(errors, [])
    self.assertEqual([r['status'] for r in rows], ['deputy', 'advisory', 'guest'])
