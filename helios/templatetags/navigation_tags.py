"""
Site navigation for server_ui/templates/base.html:

  {% site_nav %}     main navigation in the header (start page, and for logged-in
                     users their elections, votes and voter groups)
  {% breadcrumbs %}  "Home > election > page" trail on the pages below the start page

Both work from the request (URL name, user) and the page's context (election,
trustee, group), so the page templates themselves need no changes. A page can
replace the trail with its own {% block breadcrumbs %}.
"""

from django import template
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _, gettext_lazy

from helios import election_url_names as election_names, stats_url_names as stats_names, url_names as names
from helios.security import can_create_election
from helios_auth.security import get_user

register = template.Library()

# pages of an election: URL name -> (label, parent). The parent is the key of
# the page one level up; None means the election page itself.
ELECTION_PAGES = {
  election_names.ELECTION_EDIT: (gettext_lazy('Update'), None),
  election_names.ELECTION_EXTEND: (gettext_lazy('Extend Voting'), None),
  election_names.ELECTION_QUESTIONS: (gettext_lazy('Questions'), None),
  election_names.ELECTION_FREEZE: (gettext_lazy('Freeze Ballot'), None),
  election_names.ELECTION_COMPUTE_TALLY: (gettext_lazy('Compute Tally'), None),
  election_names.ELECTION_COMBINE_DECRYPTIONS: (gettext_lazy('Compute Tally'), None),
  election_names.ELECTION_RELEASE_RESULT: (gettext_lazy('Release Result'), None),
  election_names.ELECTION_AUDITED_BALLOTS: (gettext_lazy('Audited Ballots'), None),
  election_names.ELECTION_REGISTER: (gettext_lazy('Register'), None),
  election_names.ELECTION_CAST: (gettext_lazy('Vote'), None),
  election_names.ELECTION_CAST_CONFIRM: (gettext_lazy('Cast your ballot'), None),
  election_names.ELECTION_PASSWORD_VOTER_LOGIN: (gettext_lazy('Cast your ballot'), None),
  election_names.ELECTION_PASSWORD_VOTER_RESEND: (gettext_lazy('Resend voting credentials'), None),
  election_names.ELECTION_CAST_DONE: (gettext_lazy('Vote Successfully Cast!'), None),
  names.CAST_VOTE_SHORTCUT: (gettext_lazy('Ballot'), None),
  names.CAST_VOTE_FULLHASH_SHORTCUT: (gettext_lazy('Ballot'), None),

  election_names.ELECTION_ADMINS_LIST: (gettext_lazy('Administrators'), None),
  election_names.ELECTION_ADMINS_ADD: (gettext_lazy('Add administrator'), election_names.ELECTION_ADMINS_LIST),
  election_names.ELECTION_ADMINS_REMOVE: (gettext_lazy('Remove administrator'), election_names.ELECTION_ADMINS_LIST),

  election_names.ELECTION_VOTERS_LIST_PRETTY: (gettext_lazy('Voters'), None),
  election_names.ELECTION_VOTERS_UPLOAD: (gettext_lazy('Bulk Upload Voters'), election_names.ELECTION_VOTERS_LIST_PRETTY),
  election_names.ELECTION_VOTERS_ELIGIBILITY: (gettext_lazy('Voter Eligibility'), election_names.ELECTION_VOTERS_LIST_PRETTY),
  election_names.ELECTION_VOTERS_EMAIL: (gettext_lazy('Contact Voters'), election_names.ELECTION_VOTERS_LIST_PRETTY),

  election_names.ELECTION_TRUSTEES_VIEW: (gettext_lazy('Trustees'), None),
  election_names.ELECTION_TRUSTEES_NEW: (gettext_lazy('New Trustee'), election_names.ELECTION_TRUSTEES_VIEW),
  # the trustee's own page; its label is the trustee's name
  election_names.ELECTION_TRUSTEE_HOME: (None, election_names.ELECTION_TRUSTEES_VIEW),
  election_names.ELECTION_TRUSTEE_KEY_GENERATOR: (gettext_lazy('Key Setup'), election_names.ELECTION_TRUSTEE_HOME),
  election_names.ELECTION_TRUSTEE_CHECK_SK: (gettext_lazy('Check Secret Key'), election_names.ELECTION_TRUSTEE_HOME),
  election_names.ELECTION_TRUSTEE_DECRYPT_AND_PROVE: (gettext_lazy('Partial decryption'), election_names.ELECTION_TRUSTEE_HOME),
}

# pages outside an election: URL name -> (label, parent)
OTHER_PAGES = {
  names.ELECTIONS_ADMINISTERED: (gettext_lazy('My elections'), None),
  names.ELECTIONS_VOTED: (gettext_lazy('My votes'), None),
  names.ELECTIONS_NEW: (gettext_lazy('Create election'), None),
  names.ELECTIONS_NEW_GUIDED: (gettext_lazy('Create election'), None),
  names.VOTERGROUP_ADMINISTERED: (gettext_lazy('Voter groups'), None),
  names.VOTERGROUP_NEW: (gettext_lazy('Create a new voter group'), names.VOTERGROUP_ADMINISTERED),
  # a group's page; its label is the group's name
  names.VOTERGROUP_VIEW: (None, names.VOTERGROUP_ADMINISTERED),
  names.VOTERGROUP_MEMBERADD: (gettext_lazy('Add a member'), names.VOTERGROUP_VIEW),
  names.VOTERGROUP_PREPAREELECTION: (gettext_lazy('Create election'), names.VOTERGROUP_VIEW),
  stats_names.STATS_HOME: (gettext_lazy('Admin'), None),
  stats_names.STATS_ELECTIONS: (gettext_lazy('Elections'), stats_names.STATS_HOME),
  stats_names.STATS_ELECTIONS_PROBLEMS: (gettext_lazy('Problematic Elections'), stats_names.STATS_HOME),
  stats_names.STATS_RECENT_VOTES: (gettext_lazy('Recent Votes'), stats_names.STATS_HOME),
  stats_names.STATS_USER_SEARCH: (gettext_lazy('User Search'), stats_names.STATS_HOME),
  stats_names.STATS_DELETED_ELECTIONS: (gettext_lazy('Deleted Elections'), stats_names.STATS_HOME),
}


def _url_name(context):
  request = getattr(context, 'request', None)
  match = getattr(request, 'resolver_match', None)
  return getattr(match, 'url_name', None)


def _reverse(name, *args):
  try:
    return reverse(name, args=args)
  except NoReverseMatch:
    return None


def _election_crumb(name, context, election):
  """(label, url) of the election page NAME, or None if it cannot be shown."""
  if name not in ELECTION_PAGES:
    return None
  label, _parent = ELECTION_PAGES[name]
  if name.startswith('election@trustee@') or name == election_names.ELECTION_TRUSTEE_HOME:
    trustee = context.get('trustee')
    if not getattr(trustee, 'uuid', None):
      return None
    if label is None:
      label = _('Trustee %(name)s') % {'name': trustee.name}
    return label, _reverse(name, election.uuid, trustee.uuid)
  return label, _reverse(name, election.uuid)


def _other_crumb(name, context):
  label, _parent = OTHER_PAGES[name]
  if name in (names.VOTERGROUP_VIEW, names.VOTERGROUP_MEMBERADD, names.VOTERGROUP_PREPAREELECTION):
    group = context.get('group')
    if not getattr(group, 'uuid', None):
      return None
    if label is None:
      label = group.short_name
    return label, _reverse(name, group.uuid)
  return label, _reverse(name)


def build_breadcrumbs(context):
  """The trail for the current page as a list of (label, url); the last entry
  is the current page. Empty on the start page and on pages that are not known."""
  name = _url_name(context)
  election = context.get('election')
  trail = []

  if getattr(election, 'uuid', None) and (name in ELECTION_PAGES or name == election_names.ELECTION_VIEW):
    current = name
    while current in ELECTION_PAGES:
      crumb = _election_crumb(current, context, election)
      if crumb is None:
        break
      trail.insert(0, crumb)
      current = ELECTION_PAGES[current][1]
    trail.insert(0, (election.name, _reverse(election_names.ELECTION_VIEW, election.uuid)))
  elif name in OTHER_PAGES:
    current = name
    while current in OTHER_PAGES:
      crumb = _other_crumb(current, context)
      if crumb is None:
        break
      trail.insert(0, crumb)
      current = OTHER_PAGES[current][1]
  else:
    return []

  if not trail:
    return []
  # the last entry is the page itself: shown, not linked
  trail[-1] = (trail[-1][0], None)
  return [(_('Home'), '/')] + trail


@register.inclusion_tag('server_ui/templates/_breadcrumbs.html', takes_context=True)
def breadcrumbs(context):
  return {'crumbs': build_breadcrumbs(context)}


def build_site_nav(context):
  """Entries of the main navigation as dicts with label, url and current."""
  request = getattr(context, 'request', None)
  path = getattr(request, 'path', None)
  items = [(_('Home'), '/')]
  user = get_user(request) if request is not None else None
  if user:
    if can_create_election(request):
      items += [
        (_('My elections'), _reverse(names.ELECTIONS_ADMINISTERED)),
        (_('Create election'), _reverse(names.ELECTIONS_NEW_GUIDED)),
        (_('Voter groups'), _reverse(names.VOTERGROUP_ADMINISTERED)),
      ]
    items.append((_('My votes'), _reverse(names.ELECTIONS_VOTED)))
  return [{'label': label, 'url': url, 'current': url == path} for label, url in items if url]


@register.inclusion_tag('server_ui/templates/_site_nav.html', takes_context=True)
def site_nav(context):
  return {'items': build_site_nav(context), 'settings': context.get('settings')}


@register.simple_tag(takes_context=True)
def current_request(context):
  """The request of the page ({% current_request as request %}), or None: the
  language switcher sends the visitor back to request.get_full_path and keeps
  all input of a page that answers a POST (server_ui/media/language-switch.js)."""
  return getattr(context, 'request', None)
