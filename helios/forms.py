"""
Forms for Helios
"""

from django import forms
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from .fields import DateTimeLocalField
from .models import Election


class ElectionForm(forms.Form):
  short_name = forms.SlugField(max_length=40, label=_('Short name'), help_text=_('Letters, digits and hyphens, no spaces. It becomes part of the election\'s web address, e.g. board-2026.'))
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
  voting_starts_at = DateTimeLocalField(label=_('Voting starts at'), help_text = _('Date and time in UTC. Leave empty to open voting as soon as you freeze the ballot.'),
                                   required=False)
  voting_ends_at = DateTimeLocalField(label=_('Voting ends at'), help_text = _('Date and time in UTC. Leave empty to end voting yourself, by computing the tally.'),
                                   required=False)

class ElectionTimeExtensionForm(forms.Form):
  voting_extended_until = DateTimeLocalField(label=_('Voting extended until'), help_text = _('Date and time in UTC.'),
                                   required=False)
  
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
  voter_id = forms.CharField(max_length=50, label=_("Voter ID"))
  password = forms.CharField(widget=forms.PasswordInput(), max_length=100, label=_('Password'))

class VoterPasswordResendForm(forms.Form):
  voter_id = forms.CharField(max_length=50, label=_("Voter ID"), help_text=_("Enter the voter ID you were assigned for this election"))

