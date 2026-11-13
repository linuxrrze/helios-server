"""
Forms for Helios
"""

from django import forms
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from . import short_names, timezones
from .fields import LocalDateTimeField
from .models import Election


class LocalTimesMixin:
  """
  Date fields (LocalDateTimeField) take local time in the election time zone:
  say which zone in their hint, and tell the browser script (timezone-display.js).
  """
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    zone = timezones.zone_name()
    zone_hint = _('Local time in %(zone)s (currently %(offset)s).') % {
      'zone': zone, 'offset': timezones.current_offset_label()}
    for field in self.fields.values():
      if isinstance(field, LocalDateTimeField):
        field.widget.attrs['data-time-zone'] = zone
        field.help_text = '%s %s' % (zone_hint, field.help_text) if field.help_text else zone_hint


class ElectionAddressInput(forms.TextInput):
  """
  Text box for the chosen part of an election's address, with the fixed part
  (https://<host>/helios/e/ and the organization's prefix, if any) shown in front
  of it, and a preview of the whole link (helios/media/election-address.js).
  """
  template_name = 'helios/widgets/election_address.html'

  def __init__(self, attrs=None):
    super().__init__(attrs={'autocapitalize': 'none', 'autocomplete': 'off', 'spellcheck': 'false', **(attrs or {})})
    self.address_prefix = ''
    self.election = None

  def get_context(self, name, value, attrs):
    context = super().get_context(name, value, attrs)
    widget_attrs = context['widget']['attrs']
    if widget_attrs.get('id'):
      # screen readers read the fixed part in front of the box with the hint
      widget_attrs['aria-describedby'] = ' '.join(
        [widget_attrs['id'] + '_base'] + widget_attrs.get('aria-describedby', '').split())
    base = short_names.base_url()
    election = self.election
    context['widget'].update({
      'address_base': base,
      'address_prefix': self.address_prefix,
      # what an empty box stands for: the UUID, known only for an existing election
      'automatic_url': base + election.uuid if election else '',
      'current_url': base + election.short_name if election else '',
      # unchanged, an existing address is kept as it is (also one without the prefix)
      'keep_value': ElectionForm.shown_short_name(election, self.address_prefix) if election else '',
    })
    return context


class ElectionForm(LocalTimesMixin, forms.Form):
  short_name = forms.SlugField(
    max_length=40, required=False, widget=ElectionAddressInput,
    label=_('Address of the election'),
    help_text=_('The link to the election that voters get in their invitation and that you can pass on. '
                'Letters, digits, hyphens and underscores, no spaces, e.g. board-2026. '
                'Leave empty to have one created automatically (a long random code).'))
  name = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'size':60}), label=_('Name'), help_text=_('The name voters see, e.g. Board Election 2026.'))
  description = forms.CharField(max_length=4000, widget=forms.Textarea(attrs={'cols': 70, 'wrap': 'soft'}), required=False, label=_('Description'), help_text=_('Shown on the election page. Explain what is being decided and by whom.'))
  election_type = forms.ChoiceField(label=_("Type"), choices = Election.ELECTION_TYPES)
  use_voter_aliases = forms.BooleanField(required=False, initial=False, label=_('Use voter aliases'), help_text=_('The public ballot tracking center then shows aliases such as "V12" instead of the voters\' names.'))
  #use_advanced_audit_features = forms.BooleanField(required=False, initial=True, help_text='disable this only if you want a simple election with reduced security but a simpler user interface')
  randomize_answer_order = forms.BooleanField(required=False, initial=False, label=_('Randomize answer order'), help_text=_('Each voter sees the answers in a different random order, so that no answer profits from being listed first.'))
  private_p = forms.BooleanField(required=False, initial=False, label=_("Private election"), help_text=_('A private election is only visible to registered voters.'))
  help_email = forms.CharField(required=False, initial="", label=_("Help Email Address"), help_text=_('An email address voters should contact if they need help.'))
  
  if settings.ALLOW_ELECTION_INFO_URL:
    election_info_url = forms.CharField(required=False, initial="", label=_("Election Info Download URL"), help_text=_("the URL of a PDF document that contains extra election information, e.g. candidate bios and statements"))
  
  # times
  voting_starts_at = LocalDateTimeField(label=_('Voting starts at'), help_text = _('Leave empty to open voting as soon as you freeze the ballot.'),
                                   required=False)
  voting_ends_at = LocalDateTimeField(label=_('Voting ends at'), help_text = _('Leave empty to end voting yourself, by computing the tally.'),
                                   required=False)

  def __init__(self, *args, short_name_prefix='', election=None, **kwargs):
    """
    short_name_prefix: the organization's prefix put in front of the chosen
    address (short_names.prefix_for_user); election: the election being edited.
    """
    super().__init__(*args, **kwargs)
    self.short_name_prefix = short_name_prefix
    self.election = election
    if 'short_name' in self.fields:
      widget = self.fields['short_name'].widget
      widget.address_prefix = short_name_prefix
      widget.election = election
      if election and election.short_name != election.uuid:
        self.fields['short_name'].help_text = '%s %s' % (
          self.fields['short_name'].help_text,
          _('Links that have already been sent, e.g. to trustees, stop working if you change it.'))

  @staticmethod
  def shown_short_name(election, prefix):
    """the value of the address box for an existing election"""
    if election.short_name == election.uuid:
      return ''
    if prefix and election.short_name.startswith(prefix + '-'):
      return election.short_name[len(prefix) + 1:]
    # chosen before the prefix was configured (or by another organization's administrator)
    return election.short_name

  def clean_short_name(self):
    """
    The short name to store: the chosen name with the prefix, or '' for the
    election's UUID. An unchanged existing name is kept as it is.
    """
    name = self.cleaned_data.get('short_name') or ''
    if not name:
      return ''
    election = self.election
    if election and name == self.shown_short_name(election, self.short_name_prefix):
      return election.short_name
    if self.short_name_prefix:
      short_name = short_names.with_prefix(self.short_name_prefix, name)
    else:
      reserved = short_names.reserved_prefix(name)
      if reserved:
        raise forms.ValidationError(_('Addresses starting with "%(prefix)s-" are reserved for another organization. Please choose a different one.') % {'prefix': reserved})
      short_name = name
    if short_names.is_taken(short_name, election):
      raise forms.ValidationError(_('This address is already taken. Please choose a different one.'))
    return short_name

class ElectionTimeExtensionForm(LocalTimesMixin, forms.Form):
  voting_extended_until = LocalDateTimeField(label=_('Voting extended until'), required=False)
  
class EmailVotersForm(forms.Form):
  subject = forms.CharField(max_length=80, label=_('Subject'))
  body = forms.CharField(max_length=4000, widget=forms.Textarea, label=_('Body'), help_text=_('Your own message. It appears where <BODY> is shown in the preview.'))
  send_to = forms.ChoiceField(label=_("Send To"), initial="all", choices= [('all', _('all voters')), ('voted', _('voters who have cast a ballot')), ('not-voted', _('voters who have not yet cast a ballot'))])

  # placeholders shown in the email preview, which voters would receive literally
  PREVIEW_PLACEHOLDERS = ['<SUBJECT>', '<BODY>', '<VOTER_NAME>', '<VOTER_LOGIN_ID>', '<VOTER_PASSWORD>', '<SMART_TRACKER>']

  def clean(self):
    cleaned_data = super().clean()
    for field in ['subject', 'body']:
      placeholders = [p for p in self.PREVIEW_PLACEHOLDERS if p in cleaned_data.get(field, '')]
      if placeholders:
        self.add_error(field, _("Remove %(placeholders)s: it is only shown in the preview, and voters would receive it as is. Helios fills in each voter's details itself, so this box is only for your own message.") % {'placeholders': ", ".join(placeholders)})
    return cleaned_data

class TallyNotificationEmailForm(forms.Form):
  subject = forms.CharField(max_length=80, label=_('Subject'))
  body = forms.CharField(max_length=2000, widget=forms.Textarea, required=False, label=_('Body'))
  send_to = forms.ChoiceField(label=_("Send To"), choices= [('all', _('all voters')), ('voted', _('only voters who cast a ballot')), ('none', _('no one -- are you sure about this?'))])

class VoterPasswordForm(forms.Form):
  voter_id = forms.CharField(max_length=50, label=_("Voter ID"),
                             widget=forms.TextInput(attrs={'autocomplete': 'username', 'autocapitalize': 'none', 'spellcheck': 'false'}))
  password = forms.CharField(widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}), max_length=100, label=_('Password'))

class VoterPasswordResendForm(forms.Form):
  voter_id = forms.CharField(max_length=50, label=_("Voter ID"), help_text=_("Enter the voter ID you were assigned for this election"),
                             widget=forms.TextInput(attrs={'autocomplete': 'username', 'autocapitalize': 'none', 'spellcheck': 'false'}))

