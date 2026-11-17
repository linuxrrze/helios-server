"""
What each kind of email (OutgoingEmail.kind) says, rendered when it is sent by
helios.mail (in the language of the email, inside the mail context of its
election). Emails with a password or a secret link (voter password, trustee
homepage, opt-out code, account password) are only rendered here and never
stored; subject and body of the others are given when they are queued (params).

compose(email) -> (subject, body, recipient); recipient None: the voter's or
user's address, from their auth system (helios.mail).
"""
import copy

from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext as _

from . import organizations
from .models import CastVote, OutgoingEmail
from .view_utils import render_text_template


class Obsolete(Exception):
  """the email no longer makes sense (args[0]: why, for last_error_code)"""


def voter_email_address(voter):
  """the bare address of the voter (to check opt-outs with), or None"""
  return voter.voter_email or (voter.user and voter.user.user_id)


def voter_unsubscribe_vars(voter):
  """Template variables for the unsubscribe link at the end of a voter email."""
  from . import utils
  voter_email = voter_email_address(voter)
  if not voter_email:
    return {}
  unsubscribe_code = utils.generate_email_confirmation_code(voter_email, 'optout')
  unsubscribe_path = reverse('optout_confirm', kwargs={'email': voter_email, 'code': unsubscribe_code})
  return {
    'unsubscribe_url': f"{settings.URL_HOST}{unsubscribe_path}",
    'unsubscribe_code': unsubscribe_code,
  }


def voter_templates(email):
  """(subject template, body template, variables) of a templated email to a voter"""
  if email.batch_id:
    batch = email.batch
    return batch.subject_template, batch.body_template, batch.extra_vars
  return email.params.get('subject_template'), email.params.get('body_template'), email.params.get('extra_vars')


def _voter_email(email):
  """a bulk mailing, or the voter's password resent: from templates"""
  subject_template, body_template, extra_vars = voter_templates(email)
  voter = email.voter
  the_vars = copy.copy(extra_vars or {})
  the_vars.update({'election': voter.election, 'voter': voter})
  the_vars.update(voter_unsubscribe_vars(voter))
  return (render_text_template(subject_template, the_vars),
          render_text_template(body_template, the_vars), None)


def _vote_cast(email):
  """the confirmation of a cast vote (templates in server_ui/templates/email/)"""
  from .views import get_castvote_url
  cast_vote = CastVote.objects.filter(id=email.params.get('cast_vote_id'), voter=email.voter).first()
  if cast_vote is None:
    raise Obsolete('vote_removed')
  election, voter = email.election, email.voter
  extra_vars = {
    'election': election,
    'voter': voter,
    'cast_vote': cast_vote,
    'cast_vote_url': get_castvote_url(cast_vote),
    'custom_subject': _("%(election_name)s - vote cast") % {'election_name': election.name},
  }
  extra_vars.update(voter_unsubscribe_vars(voter))
  return (render_text_template('email/cast_vote_subject.txt', extra_vars),
          render_text_template('email/cast_vote_body.txt', extra_vars), None)


def _given(email):
  """subject and body given when the email was queued (no secrets in them)"""
  return email.params['subject'], email.params['body'], (email.recipient or None)


def _trustee(email):
  """the link to the trustee's homepage (with the trustee's secret)"""
  from . import url_names
  election, trustee = email.election, email.trustee
  url = settings.SECURE_URL_HOST + reverse(url_names.TRUSTEE_LOGIN, args=[election.short_name, trustee.email, trustee.secret])
  subject = _("your trustee homepage for %(election)s") % {'election': election.name}
  body = _("""

You are a trustee for %(election)s.

Your trustee dashboard is at

  %(url)s

--
%(signature)s
""") % {'election': election.name, 'url': url, 'signature': organizations.for_election(election).mail_signature}
  return subject, body, "%s <%s>" % (trustee.name, trustee.email)


def _confirmation_url(email, action):
  from . import utils
  from email.utils import parseaddr
  address = parseaddr(email.recipient)[1]
  code = utils.generate_email_confirmation_code(address, action)
  return settings.URL_HOST + reverse('%s_confirm' % action, kwargs={'email': address, 'code': code})


def _optout_confirm(email):
  subject = _("Confirm your opt-out from Helios emails")
  body = _("""
Please confirm that you want to opt out of all Helios voting system emails.

Click this link to confirm: %(confirmation_url)s

If you did not request this, please ignore this email.

--
Helios Voting System
""") % {'confirmation_url': _confirmation_url(email, 'optout')}
  return subject, body, email.recipient


def _optin_confirm(email):
  subject = _("Confirm your opt-in to Helios emails")
  body = _("""
Please confirm that you want to opt back in to Helios voting system emails.

Click this link to confirm: %(confirmation_url)s

If you did not request this, please ignore this email.

--
Helios Voting System
""") % {'confirmation_url': _confirmation_url(email, 'optin')}
  return subject, body, email.recipient


def _batch_summary(email):
  """how a bulk mailing went, for its administrator (counts as of now)"""
  from . import mail_status, url_names
  from .models import EmailBatch
  from .timezones import format_local
  batch = EmailBatch.objects.filter(id=email.params.get('batch_id'), election=email.election).first()
  if batch is None:
    raise Obsolete('batch_removed')
  election = email.election
  counts = mail_status.counts(batch.emails.all())
  url = settings.SECURE_URL_HOST + reverse(url_names.election.ELECTION_EMAILS, args=[election.uuid])
  subject = _("Mailing sent: %(election)s") % {'election': election.name}
  lines = [
    (_("sent"), counts['sent']),
    (_("failed"), counts['failed']),
    (_("bounced so far"), counts['bounced']),
    (_("not sent (opted out, address suppressed, no longer applicable)"), counts['skipped']),
  ]
  body = _("""Your mailing %(mailing)s of %(date)s to the voters of %(election)s has been sent.

%(counts)s

Bounces can still arrive in the coming hours and days. Which voters were not
reached and why, how to correct a voter's address and how to send the email
again to the voters not reached:

  %(url)s

--
%(signature)s
""") % {'mailing': mail_status.batch_label(batch), 'date': format_local(batch.created_at), 'election': election.name,
        'counts': '\n'.join('  %s: %d' % (label, n) for label, n in lines), 'url': url,
        'signature': organizations.for_election(election).mail_signature}
  return subject, body, None


def _password_reminder(email):
  from helios_auth.auth_systems.password import password_reminder
  return password_reminder(email.user)


COMPOSERS = {
  OutgoingEmail.KIND_BULK: _voter_email,
  OutgoingEmail.KIND_VOTER_PASSWORD: _voter_email,
  OutgoingEmail.KIND_VOTE_CAST: _vote_cast,
  OutgoingEmail.KIND_VOTE_REMOVED: _given,
  OutgoingEmail.KIND_ADMIN_INFO: _given,
  OutgoingEmail.KIND_TRUSTEE: _trustee,
  OutgoingEmail.KIND_OPTOUT_CONFIRM: _optout_confirm,
  OutgoingEmail.KIND_OPTIN_CONFIRM: _optin_confirm,
  OutgoingEmail.KIND_PASSWORD_REMINDER: _password_reminder,
  OutgoingEmail.KIND_BATCH_SUMMARY: _batch_summary,
}


def compose(email):
  """(subject, body, recipient or None) of EMAIL; may raise Obsolete"""
  subject, body, recipient = COMPOSERS[email.kind](email)
  # subject up until the first newline (as helios_auth.models.User.send_message)
  return subject.split("\n")[0], body, recipient
