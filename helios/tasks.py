"""
Background tasks for Helios, run by the database task worker

2010-08-01
ben@adida.net
"""
import copy
import logging
from django.conf import settings
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _, ngettext
from django_tasks import task
from urllib.parse import urlparse

from . import organizations
from . import signals
from . import utils
from .models import CastVote, Election, Voter, VoterFile, EmailOptOut
from .view_utils import render_text_template


def _language(language):
    """Messages are rendered in the language of the person whose action sends
    them (passed in by the view), or the site default for older queued tasks."""
    return translation.override(language or settings.LANGUAGE_CODE)


def _voter_email(voter):
    return voter.voter_email or (voter.user and voter.user.user_id)


def voter_opted_out(voter):
    """True if the voter's address has opted out of all Helios emails."""
    voter_email = _voter_email(voter)
    return bool(voter_email) and EmailOptOut.is_opted_out(voter_email)


def voter_unsubscribe_vars(voter):
    """Template variables for the unsubscribe link at the end of a voter email."""
    voter_email = _voter_email(voter)
    if not voter_email:
        return {}
    unsubscribe_code = utils.generate_email_confirmation_code(voter_email, 'optout')
    unsubscribe_path = reverse('optout_confirm', kwargs={'email': voter_email, 'code': unsubscribe_code})
    return {
        'unsubscribe_url': f"{settings.URL_HOST}{unsubscribe_path}",
        'unsubscribe_code': unsubscribe_code,
    }


@task(priority=10)  # ahead of bulk voter emails
def cast_vote_verify_and_store(cast_vote_id, status_update_message=None, language=None, **kwargs):
    with _language(language):
        _cast_vote_verify_and_store(cast_vote_id, status_update_message)


def _cast_vote_verify_and_store(cast_vote_id, status_update_message):
    cast_vote = CastVote.objects.get(id=cast_vote_id)
    result = cast_vote.verify_and_store()

    voter = cast_vote.voter
    election = voter.election
    user = voter.get_user()

    if result:
        # send the signal (its receiver mails the voter a confirmation, server_ui/glue.py)
        with organizations.mail_context(election):
            signals.vote_cast.send(sender=election, election=election, user=user, voter=voter, cast_vote=cast_vote)

        if status_update_message and user.can_update_status():
            user.update_status(status_update_message)
    else:
        logger = logging.getLogger(__name__)
        logger.error("Failed to verify and store %d" % cast_vote_id)


@task()
def voters_email(election_id, subject_template, body_template, extra_vars={},
                 voter_constraints_include=None, voter_constraints_exclude=None, language=None):
    """
    voter_constraints_include are conditions on including voters
    voter_constraints_exclude are conditions on excluding voters
    """
    election = Election.objects.get(id=election_id)

    # select the right list of voters
    voters = election.voter_set.all()
    if voter_constraints_include:
        voters = voters.filter(**voter_constraints_include)
    if voter_constraints_exclude:
        voters = voters.exclude(**voter_constraints_exclude)

    for voter in voters:
        single_voter_email.enqueue(voter.uuid, subject_template, body_template, extra_vars, language=language)


@task()
def voters_notify(election_id, notification_template, extra_vars={}, language=None):
    election = Election.objects.get(id=election_id)
    for voter in election.voter_set.all():
        single_voter_notify.enqueue(voter.uuid, notification_template, extra_vars, language=language)


@task()
def single_voter_email(voter_uuid, subject_template, body_template, extra_vars={}, language=None):
    voter = Voter.objects.get(uuid=voter_uuid)

    if voter_opted_out(voter):
        logger = logging.getLogger(__name__)
        logger.info(f"Skipping email to opted-out voter {voter.uuid}")
        return

    the_vars = copy.copy(extra_vars)
    the_vars.update({'election': voter.election})
    the_vars.update({'voter': voter})
    the_vars.update(voter_unsubscribe_vars(voter))

    # the email comes from the election's organization (sender name, Reply-To and
    # signature, in the language of the email); set here, as the task runs without the request
    with _language(language), organizations.mail_context(voter.election):
        subject = render_text_template(subject_template, the_vars)
        body = render_text_template(body_template, the_vars)
        voter.send_message(subject, body)


@task()
def single_voter_notify(voter_uuid, notification_template, extra_vars={}, language=None):
    voter = Voter.objects.get(uuid=voter_uuid)

    the_vars = copy.copy(extra_vars)
    the_vars.update({'voter': voter})

    with _language(language), organizations.mail_context(voter.election):
        notification = render_text_template(notification_template, the_vars)
        voter.send_notification(notification)


@task()
def election_compute_tally(election_id, language=None):
    election = Election.objects.get(id=election_id)
    election.compute_tally()

    with _language(language):
        subject = _("encrypted tally computed")
        body = _("""
The encrypted tally for election %(election)s has been computed.

--
Helios
""") % {'election': election.name}
    election_notify_admin.enqueue(election_id=election_id, subject=subject, body=body)

    if election.has_helios_trustee():
        tally_helios_decrypt.enqueue(election_id=election.id, language=language)


@task()
def tally_helios_decrypt(election_id, language=None):
    election = Election.objects.get(id=election_id)
    election.helios_trustee_decrypt()
    with _language(language):
        subject = _('Helios Decrypt')
        body = _("""
Helios has decrypted its portion of the tally
for election %(election)s.

--
Helios
""") % {'election': election.name}
    election_notify_admin.enqueue(election_id=election_id, subject=subject, body=body)


@task()
def voter_file_process(voter_file_id, language=None):
    with _language(language):
        _voter_file_process(voter_file_id)


def _voter_file_process(voter_file_id):
    voter_file = VoterFile.objects.get(id=voter_file_id)
    # a requeued task may find the file already processed; reprocessing would reset its counts
    if voter_file.processing_finished_at:
        return
    voter_file.process()

    skipped = ""
    if voter_file.num_skipped:
        skipped = ngettext("""
%(count)d row in the file was not added, because those voters were already
on the voter list (or listed twice in the file), or have opted out of
Helios emails. Opted-out voters are listed in a separate email.
""", """
%(count)d rows in the file were not added, because those voters were already
on the voter list (or listed twice in the file), or have opted out of
Helios emails. Opted-out voters are listed in a separate email.
""", voter_file.num_skipped) % {'count': voter_file.num_skipped}

    created = ngettext("%(count)d voter has been created.",
                       "%(count)d voters have been created.",
                       voter_file.num_voters) % {'count': voter_file.num_voters}

    election_notify_admin.enqueue(election_id=voter_file.election.id,
                                subject=_('voter file processed'),
                                body=_("""
Your voter file upload for election %(election)s
has been processed.

%(created)s
%(skipped)s
--
Helios
""") % {'election': voter_file.election.name, 'created': created, 'skipped': skipped})


@task()
def notify_admin_opted_out_voters(election_id, opted_out_voters, language=None):
    election = Election.objects.get(id=election_id)
    
    if not opted_out_voters:
        return

    with _language(language):
        _notify_admin_opted_out_voters(election, opted_out_voters)


def _notify_admin_opted_out_voters(election, opted_out_voters):
    subject = _("Opted-out voters not added to election %(election)s") % {'election': election.name}
    
    body = ngettext("""
The following %(count)d voter could not be added to election "%(election)s" 
because they have opted out of receiving Helios emails:

""", """
The following %(count)d voters could not be added to election "%(election)s" 
because they have opted out of receiving Helios emails:

""", len(opted_out_voters)) % {'count': len(opted_out_voters), 'election': election.name}
    
    for voter in opted_out_voters:
        body += _("- %(name)s (%(email)s) [ID: %(voter_id)s, Type: %(voter_type)s]") % {
            'name': voter['name'], 'email': voter['email'], 'voter_id': voter['voter_id'], 'voter_type': voter['voter_type']} + "\n"
    
    optin_path = reverse('optin_form')
    optin_url = f"{settings.URL_HOST}{optin_path}"
    
    body += _("""

These voters will need to opt back in before they can be added to elections.
They can opt back in at: %(optin_url)s

--
Helios
""") % {'optin_url': optin_url}
    
    election.admin.send_message(subject, body)


@task()
def election_notify_admin(election_id, subject, body):
    election = Election.objects.get(id=election_id)
    election.admin.send_message(subject, body)
