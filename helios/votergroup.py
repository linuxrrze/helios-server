"""
Voter groups: forms and the pure logic that turns a group
into the list of password voters for a new election.

A member with N votes gets N voter rows ("seats"), a member with 0 votes (no
voting right, e.g. a deputy or an advisory member of a board) none. Before
the election is created, the administrator can delegate any seat to another
member of the group; that seat is then issued to the delegate instead.

The members can be exported to and imported from a CSV file (see
parse_member_csv and plan_import).
"""

import codecs
import csv
import io
import re

from django import forms
from django.forms import formset_factory
from django.utils.translation import gettext, gettext_lazy as _

from .models import VoterGroupMember

MAX_VOTES_PER_MEMBER = 100


class DateInput(forms.DateInput):
  """The browser's date picker; the value is sent as YYYY-MM-DD."""
  input_type = 'date'

  def __init__(self, attrs=None):
    super().__init__(attrs=attrs, format='%Y-%m-%d')


class VoterGroupForm(forms.Form):
  """A new group, or the settings of a group. Everything but the short name
  is optional (for a board: its long name and term of office)."""
  short_name = forms.SlugField(max_length=100, label=_('Short name'), help_text=_('no spaces, e.g. faculty-council'))
  name = forms.CharField(max_length=250, required=False, label=_('Name'),
                         help_text=_('The full name, shown as the title of the group, e.g. Faculty council of the Faculty of Sciences.'))
  name_suffix = forms.CharField(max_length=100, required=False, label=_('Name suffix'),
                                help_text=_('Shown after the name, e.g. the term of office in short form: 2025-2027.'))
  term_starts_on = forms.DateField(required=False, widget=DateInput, label=_('Term of office begins on'))
  term_ends_on = forms.DateField(required=False, widget=DateInput, label=_('Term of office ends on'),
                                 help_text=_('After this day, the group is marked as "term ended"; it can still be used.'))

  def clean(self):
    cleaned = super().clean()
    starts, ends = cleaned.get('term_starts_on'), cleaned.get('term_ends_on')
    if starts and ends and ends < starts:
      self.add_error('term_ends_on', _('The term of office cannot end before it begins.'))
    return cleaned


class VoterGroupMemberForm(forms.Form):
  first_name = forms.CharField(max_length=100, label=_('First name'))
  last_name = forms.CharField(max_length=100, label=_('Last name'))
  email = forms.EmailField(max_length=100, label=_('Email'))
  votes = forms.IntegerField(min_value=0, max_value=MAX_VOTES_PER_MEMBER, initial=1, label=_('Votes'),
                             help_text=_('0 for members without a voting right, e.g. deputy or advisory members: they get no vote in an election prepared from the group.'))
  initials = forms.CharField(max_length=20, required=False, label=_('Initials'),
                             help_text=_('A short identifier, e.g. the initials.'))
  person_group = forms.CharField(max_length=100, required=False, label=_('Person group'),
                                 help_text=_('e.g. Professors, Students; the member list is sorted by it.'))
  status = forms.ChoiceField(choices=VoterGroupMember.STATUS_CHOICES, initial=VoterGroupMember.STATUS_MEMBER,
                             label=_('Status'))

  def __init__(self, data=None, *args, **kwargs):
    if data is not None and not data.get('status'):
      # a client that doesn't know the field (or an empty cell in an import): a regular member
      data = data.copy()
      data['status'] = VoterGroupMember.STATUS_MEMBER
    super().__init__(data, *args, **kwargs)

  def clean(self):
    cleaned = super().clean()
    for field in ('first_name', 'last_name', 'initials', 'person_group'):
      if isinstance(cleaned.get(field), str):
        cleaned[field] = cleaned[field].strip()
    return cleaned


class VoterGroupAdminForm(forms.Form):
  email = forms.CharField(max_length=100, label=_('Email'),
                          help_text=_('The person must have logged in to Helios at least once before they can be added.'))


class TransferOwnershipForm(forms.Form):
  """The new owner, one of the group's additional administrators (CANDIDATES).
  The template renders the fields itself (a radio list of the candidates)."""
  new_owner = forms.ChoiceField(label=_('New owner'))
  keep_previous_owner = forms.BooleanField(required=False, initial=True)

  def __init__(self, *args, candidates=(), **kwargs):
    super().__init__(*args, **kwargs)
    self.fields['new_owner'].choices = [(str(u.pk), u.pretty_name) for u in candidates]


class TransferOwnershipByEmailForm(forms.Form):
  """For site administrators: the new owner may be any user who can create
  elections, found by email (see helios.view_utils.find_user_to_add_as_admin)."""
  email = forms.CharField(max_length=100, label=_('Email of the new owner'),
                          help_text=_('The person must have logged in to Helios at least once and be allowed to create elections.'))
  keep_previous_owner = forms.BooleanField(required=False, initial=True,
                                           label=_('Keep the previous owner as an administrator of the group'))


class PrepareElectionAdminsForm(forms.Form):
  add_group_admins = forms.BooleanField(
    required=False, initial=True,
    label=_('Add the other administrators of the group to the election administrators'),
    help_text=_('You become the creator of the election either way. Changes to the group later on do not change the election.'))


class SeatForm(forms.Form):
  """One voter row of the election being prepared."""
  first_name = forms.CharField(max_length=100)
  last_name = forms.CharField(max_length=100)
  email = forms.EmailField(max_length=100)
  login_id = forms.CharField(max_length=100)
  delegate_to = forms.ChoiceField(choices=(), required=False)

  def __init__(self, *args, **kwargs):
    delegate_choices = kwargs.pop('delegate_choices')
    super().__init__(*args, **kwargs)
    self.fields['delegate_to'].choices = delegate_choices


class BaseSeatFormSet(forms.BaseFormSet):
  def clean(self):
    if any(self.errors):
      return
    seen = set()
    for form in self.forms:
      login_id = form.cleaned_data.get('login_id')
      if login_id in seen:
        raise forms.ValidationError(gettext("Login IDs must be unique: %(login_id)s is used twice.") % {'login_id': login_id})
      seen.add(login_id)


SeatFormSet = formset_factory(SeatForm, formset=BaseSeatFormSet, extra=0)


def delegate_choices(members):
  return [('', gettext('No delegation'))] + [
    (m.uuid, "%s, %s <%s>" % (m.last_name, m.first_name, m.email)) for m in members]


def next_login_id(first_name, last_name, taken):
  """Initials plus the lowest free counter, e.g. JD1, JD2, ..."""
  prefix = (first_name[:1] + last_name[:1]).upper() or 'V'
  n = 1
  while "%s%d" % (prefix, n) in taken:
    n += 1
  return "%s%d" % (prefix, n)


def _seat(first_name, last_name, email, taken):
  login_id = next_login_id(first_name, last_name, taken)
  taken.add(login_id)
  return {'first_name': first_name, 'last_name': last_name, 'email': email,
          'login_id': login_id, 'delegate_to': ''}


def initial_seats(members):
  """One seat per vote of every member (none for members with 0 votes)."""
  taken, seats = set(), []
  for m in members:
    for _ in range(m.votes):
      seats.append(_seat(m.first_name, m.last_name, m.email, taken))
  return seats


def apply_delegations(seats, members_by_uuid):
  """
  Replace every delegated seat with a seat for the delegate, keeping the
  order of non-delegated seats and appending delegated ones at the end.
  `seats` are cleaned SeatForm dicts.
  """
  kept = [dict(s, delegate_to='') for s in seats if not s.get('delegate_to')]
  taken = {s['login_id'] for s in kept}
  for s in seats:
    uuid = s.get('delegate_to')
    if uuid:
      m = members_by_uuid[uuid]
      kept.append(_seat(m.first_name, m.last_name, m.email, taken))
  return kept


##
## CSV export and import of the members
##
## The file has a header row; the columns are named in English (as exported)
## or German, in any order. Members are matched by email (case-insensitive):
## those already in the group are updated, the others added; with
## remove_missing, members not in the file are removed. A column that is
## missing leaves the field of existing members alone (new members get the
## default); an empty cell in a present column sets the default (votes 1,
## status member, no initials or person group).
##

# in the order of the export
CSV_COLUMNS = ['first_name', 'last_name', 'email', 'votes', 'initials', 'person_group', 'status']
REQUIRED_COLUMNS = ['first_name', 'last_name', 'email']
# accepted header names, compared without case, spaces and punctuation (see _header_key)
CSV_HEADERS = {
  'first_name': ['first_name', 'first name', 'given name', 'Vorname'],
  'last_name': ['last_name', 'last name', 'family name', 'surname', 'Nachname', 'Name'],
  'email': ['email', 'email address', 'E-Mail', 'E-Mail-Adresse'],
  'votes': ['votes', 'Stimmen', 'Anzahl Stimmen'],
  'initials': ['initials', 'Kürzel', 'Kuerzel'],
  'person_group': ['person_group', 'person group', 'Personengruppe'],
  'status': ['status'],
}
# accepted status words (also the keys and labels of STATUS_CHOICES), compared without case
STATUS_WORDS = {
  'member': ['Mitglied'],
  'deputy': ['deputy', 'Ersatzmitglied', 'Stellvertreter', 'Stellvertreterin', 'Stellvertretung'],
  'advisory': ['advisory', 'beratend', 'beratendes Mitglied'],
  'guest': ['Gast'],
}
DEFAULTS = {'votes': 1, 'initials': '', 'person_group': '', 'status': VoterGroupMember.STATUS_MEMBER}

MAX_IMPORT_BYTES = 1024 * 1024
MAX_IMPORT_ROWS = 2000

# a cell starting with one of these is taken as a formula by spreadsheets: the
# export prefixes it with an apostrophe, which the import removes again
FORMULA_START = ('=', '+', '-', '@', '\t', '\r')


class MemberImportForm(forms.Form):
  members_file = forms.FileField(label=_('CSV file'))
  remove_missing = forms.BooleanField(
    required=False, label=_('Remove members who are not in the file'),
    help_text=_('Without this, members who are not in the file stay in the group.'))


def _header_key(text):
  return re.sub(r'[\W_]+', '', text or '').lower()


def _header_map():
  return {_header_key(alias): column for column, aliases in CSV_HEADERS.items() for alias in aliases}


def _status_map():
  words = {}
  for key, label in VoterGroupMember.STATUS_CHOICES:
    for word in [key, str(label)] + STATUS_WORDS.get(key, []):
      words[word.strip().lower()] = key
  return words


def csv_safe(value):
  value = str(value)
  return "'" + value if value.startswith(FORMULA_START) else value


def _csv_unescape(value):
  return value[1:] if value.startswith("'") and value[1:].startswith(FORMULA_START) else value


def write_members_csv(out, members):
  """The members as CSV (header row with CSV_COLUMNS) to the text stream OUT."""
  writer = csv.writer(out)
  writer.writerow(CSV_COLUMNS)
  for m in members:
    writer.writerow([csv_safe(getattr(m, column)) for column in CSV_COLUMNS])


def decode_csv(data):
  """The text of an uploaded file: UTF-8 (with or without byte order mark),
  UTF-16 with byte order mark (Excel's "Unicode text"), else Windows-1252 /
  Latin-1 (older spreadsheet exports)."""
  if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
    text = data.decode('utf-16')
  else:
    try:
      text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
      try:
        text = data.decode('cp1252')
      except UnicodeDecodeError:
        text = data.decode('latin-1')
  return text.replace('\x00', '')


def sniff_delimiter(header_line):
  """';', tab or ',' (the most frequent one in the header line)."""
  counts = {d: header_line.count(d) for d in (';', '\t', ',')}
  best = max(counts, key=lambda d: counts[d])
  return best if counts[best] else ','


def parse_member_csv(data):
  """
  Parse the uploaded CSV file DATA (bytes). Returns (rows, columns, errors,
  ignored): ROWS, a list of dicts with 'line' and the given columns' cleaned
  values (rows with errors left out); COLUMNS, the columns present; ERRORS,
  a list of (line or None, message); IGNORED, the header names not
  understood.
  """
  errors = []
  if len(data) > MAX_IMPORT_BYTES:
    return [], [], [(None, gettext('The file is too large (at most %(size)s kB).') % {'size': MAX_IMPORT_BYTES // 1024})], []
  text = decode_csv(data)
  first_line = next((line for line in text.splitlines() if line.strip()), '')
  if not first_line:
    return [], [], [(None, gettext('The file is empty.'))], []
  reader = csv.reader(io.StringIO(text, newline=''), delimiter=sniff_delimiter(first_line))

  header = None
  for cells in reader:
    if any(c.strip() for c in cells):
      header = cells
      break
  headers = _header_map()
  columns, ignored, index = [], [], {}
  for i, title in enumerate(header):
    column = headers.get(_header_key(title))
    if column is None:
      if title.strip():
        ignored.append(title.strip())
    elif column in index:
      errors.append((reader.line_num, gettext('The column %(column)s appears twice.') % {'column': title.strip()}))
    else:
      index[column] = i
      columns.append(column)
  missing = [c for c in REQUIRED_COLUMNS if c not in index]
  if missing:
    errors.append((reader.line_num, gettext(
      'The header row lacks the column(s) %(columns)s. Expected are first_name, last_name and email '
      '(or Vorname, Nachname and E-Mail), optionally votes, initials, person_group and status.') % {
        'columns': ', '.join(missing)}))
  if errors:
    return [], columns, errors, ignored

  statuses = _status_map()
  rows, seen, num_rows = [], {}, 0
  try:
    for cells in reader:
      if not any(c.strip() for c in cells):
        continue
      line = reader.line_num
      num_rows += 1
      if num_rows > MAX_IMPORT_ROWS:
        return [], columns, [(None, gettext('The file has too many rows (at most %(rows)s).') % {'rows': MAX_IMPORT_ROWS})], ignored
      values = {c: _csv_unescape(cells[i].strip()) if i < len(cells) else '' for c, i in index.items()}
      row_errors = []

      status = values.get('status')
      if status:
        values['status'] = statuses.get(status.lower())
        if values['status'] is None:
          row_errors.append(gettext('Unknown status "%(status)s" (possible: %(choices)s).') % {
            'status': status, 'choices': ', '.join(str(label) for _key, label in VoterGroupMember.STATUS_CHOICES)})
      form_data = {c: values.get(c) or DEFAULTS.get(c, '') for c in CSV_COLUMNS}
      if not row_errors:
        form = VoterGroupMemberForm(form_data)
        if not form.is_valid():
          for field, messages in form.errors.items():
            label = form.fields[field].label if field in form.fields else ''
            row_errors.extend('%s: %s' % (label, m) if label else m for m in messages)
      if not row_errors:
        cleaned = form.cleaned_data
        email = cleaned['email'].lower()
        if email in seen:
          row_errors.append(gettext('The address %(email)s is already in line %(line)s.') % {
            'email': cleaned['email'], 'line': seen[email]})
        else:
          seen[email] = line
      if row_errors:
        errors.extend((line, e) for e in row_errors)
      else:
        rows.append(dict({c: cleaned[c] for c in columns}, line=line))
  except csv.Error as e:
    errors.append((reader.line_num, gettext('The file cannot be read as CSV: %(error)s') % {'error': e}))
  if not rows and not errors:
    errors.append((None, gettext('The file contains no members.')))
  return rows, columns, errors, ignored


def plan_import(members, rows, columns, remove_missing):
  """
  What importing ROWS (from parse_member_csv) into a group with MEMBERS does:
  a dict with 'add' (rows), 'update' (list of (member, {field: (old, new)})),
  'remove' (members), 'unchanged' (number) and 'errors' ((line, message), for
  addresses that several members of the group have).
  """
  by_email = {}
  for m in members:
    by_email.setdefault(m.email.lower(), []).append(m)
  plan = {'add': [], 'update': [], 'remove': [], 'unchanged': 0, 'errors': []}
  for row in rows:
    existing = by_email.get(row['email'].lower(), [])
    if len(existing) > 1:
      plan['errors'].append((row['line'], gettext(
        'Several members of the group have the address %(email)s; please delete all but one of them first.') % {
          'email': row['email']}))
      continue
    if not existing:
      plan['add'].append(row)
      continue
    member = existing[0]
    changes = {c: (getattr(member, c), row[c]) for c in columns if getattr(member, c) != row[c]}
    if changes:
      plan['update'].append((member, changes))
    else:
      plan['unchanged'] += 1
  if remove_missing:
    in_file = {r['email'].lower() for r in rows}
    plan['remove'] = [m for m in members if m.email.lower() not in in_file]
  return plan


def new_member_fields(row):
  """The fields of a member added from ROW (defaults for missing columns)."""
  return {c: row.get(c, DEFAULTS.get(c, '')) for c in CSV_COLUMNS}
