"""
Where an election stands, for the election page: a status label for everyone,
and for administrators the steps from setup to the released result with the
one action that comes next.

Only reads the election; every action still goes through its own view and
checks there.
"""

from django.urls import reverse
from django.utils.translation import gettext as _, ngettext

from helios import url_names


def election_status(election):
  """(css key, label) for the badge next to the election name"""
  if election.result_released_at:
    return 'released', _('Result released')
  if not election.frozen_at:
    return 'draft', _('In preparation')
  if election.tallying_started_at or election.encrypted_tally:
    return 'tallying', _('Counting')
  if election.voting_has_stopped():
    return 'closed', _('Voting closed')
  if not election.voting_has_started():
    return 'scheduled', _('Opens soon')
  return 'open', _('Voting open')


def _url(name, election):
  return reverse(name, args=[election.uuid])


def admin_steps(election, trustees):
  """
  The administrator's steps, in order. Each step is a dict with title, detail
  (where it stands), hint (why it matters), url and state: 'done', 'current'
  (the first step not done) or 'todo'.
  """
  names = url_names.election
  num_questions = len(election.questions or [])
  num_voters = election.num_voters
  frozen = election.frozen_at is not None
  trustees_without_key = [t for t in trustees if t.public_key is None]

  steps = []

  if num_questions:
    detail = ngettext('%(count)d question', '%(count)d questions', num_questions) % {'count': num_questions}
  else:
    detail = _('No questions yet')
  steps.append({'key': 'questions', 'title': _('Ballot questions'), 'detail': detail,
                'hint': _('What voters are asked and which answers they can choose.'),
                'url': _url(names.ELECTION_QUESTIONS, election), 'done': num_questions > 0})

  if election.openreg:
    detail = _('Anyone who can log in may vote')
  elif num_voters:
    detail = ngettext('%(count)d voter', '%(count)d voters', num_voters) % {'count': num_voters}
  else:
    detail = _('No voters yet')
  steps.append({'key': 'voters', 'title': _('Voters'), 'detail': detail,
                'hint': _('Who may vote. Upload a list of voters as a CSV file.'),
                'url': _url(names.ELECTION_VOTERS_LIST_PRETTY, election),
                'done': election.openreg or num_voters > 0})

  if not trustees:
    detail = _('No trustees yet')
  elif trustees_without_key:
    detail = ngettext('Waiting for the key of %(names)s', 'Waiting for the keys of %(names)s',
                      len(trustees_without_key)) % {'names': ', '.join(t.name for t in trustees_without_key)}
  else:
    detail = ngettext('%(count)d trustee, key ready', '%(count)d trustees, keys ready', len(trustees)) % {'count': len(trustees)}
  steps.append({'key': 'trustees', 'title': _('Trustees'), 'detail': detail,
                'hint': _('Who holds the keys that decrypt the result. Helios itself is a trustee unless you change it.'),
                'url': _url(names.ELECTION_TRUSTEES_VIEW, election),
                'done': bool(trustees) and not trustees_without_key})

  if frozen:
    detail = _('Done: the ballot can no longer be changed')
  else:
    detail = _('Not yet')
  steps.append({'key': 'freeze', 'title': _('Freeze the ballot and open the election'), 'detail': detail,
                'hint': _('Fixes questions, answers and trustees for good. Voting opens now or at the scheduled time.'),
                'url': None if frozen else _url(names.ELECTION_FREEZE, election), 'done': frozen})

  voting_stopped = bool(election.voting_has_stopped())
  if frozen:
    num_cast = election.num_cast_votes
    if election.openreg:
      detail = ngettext('%(count)d ballot cast', '%(count)d ballots cast', num_cast) % {'count': num_cast}
    else:
      detail = ngettext('%(cast)d of %(total)d voter has voted', '%(cast)d of %(total)d voters have voted',
                        num_voters) % {'cast': num_cast, 'total': num_voters}
  else:
    detail = _('Not yet')
  steps.append({'key': 'voting', 'title': _('Voting'), 'detail': detail,
                'hint': _('Send voters their invitation and follow the turnout in the ballot tracking center.'),
                'url': _url(names.ELECTION_VOTERS_LIST_PRETTY, election) if frozen else None,
                'done': frozen and voting_stopped})

  has_result = bool(election.result) and election.has_complete_result
  if has_result:
    detail = _('Done: only you can see the result so far') if not election.result_released_at else _('Done')
  elif election.encrypted_tally:
    num_decrypted = len([t for t in trustees if t.decryption_factors])
    detail = _('%(done)d of %(total)d trustees have decrypted their share') % {'done': num_decrypted, 'total': len(trustees)}
  elif election.tallying_started_at:
    detail = _('Under way')
  else:
    detail = _('Not yet')
  steps.append({'key': 'tally', 'title': _('Count the votes'), 'detail': detail,
                'hint': _('Ends voting and adds up the encrypted ballots; the trustees then decrypt only the totals.'),
                'url': None, 'done': has_result})

  steps.append({'key': 'release', 'title': _('Release the result'),
                'detail': _('Done: the result is public') if election.result_released_at else _('Not yet'),
                'hint': _('Makes the result visible to everyone and lets you notify the voters.'),
                'url': None, 'done': bool(election.result_released_at)})

  current_found = False
  for step in steps:
    if step['done']:
      step['state'] = 'done'
    elif not current_found:
      step['state'] = 'current'
      current_found = True
    else:
      step['state'] = 'todo'
  return steps


ISSUE_PAGES = {
  'questions': url_names.election.ELECTION_QUESTIONS,
  'trustees': url_names.election.ELECTION_TRUSTEES_VIEW,
  'trustee keypairs': url_names.election.ELECTION_TRUSTEES_VIEW,
  'voters': url_names.election.ELECTION_VOTERS_LIST_PRETTY,
}


def issues_with_links(election):
  """issues_before_freeze, each with the page where it can be fixed"""
  issues = []
  for issue in election.issues_before_freeze:
    page = ISSUE_PAGES.get(issue['type'])
    issues.append(dict(issue, url=_url(page, election) if page else None))
  return issues
