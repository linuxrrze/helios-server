"""
Background tasks for Helios, run by the database task worker

2010-08-01
ben@adida.net
"""
import copy
import logging
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _, ngettext
from django_tasks import task
from urllib.parse import urlparse

from . import mail
from . import organizations
from . import signals
from .mail_messages import voter_unsubscribe_vars  # noqa: F401 (imported from here before)
from .models import CastVote, EmailBatch, Election, OutgoingEmail, Voter, VoterFile
from .view_utils import render_text_template


def _language(language):
    """Messages are rendered in the language of the person whose action sends
    them (passed in by the view), or the site default for older queued tasks."""
    return translation.override(language or settings.LANGUAGE_CODE)


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
def voters_email(election_id, subject_template=None, body_template=None, extra_vars=None,
                 voter_constraints_include=None, voter_constraints_exclude=None, language=None,
                 batch_id=None):
    """
    The emails of the bulk mailing BATCH_ID (an EmailBatch, with the templates and
    variables) to the voters of the election, one OutgoingEmail and task each.
    voter_constraints_include are conditions on including voters
    voter_constraints_exclude are conditions on excluding voters

    Safe to run again (requeue_stuck_tasks): it adds and queues only the emails
    that are still missing, in one transaction with their tasks (send_outgoing_email).
    Tasks queued before EmailBatch existed pass the templates instead of BATCH_ID.
    """
    election = Election.objects.get(id=election_id)
    if batch_id is None:
        batch = mail.create_batch(election, subject_template, body_template, extra_vars, language=language)
    else:
        batch = EmailBatch.objects.get(id=batch_id, election=election)

    # select the right list of voters
    voters = election.voter_set.all()
    if voter_constraints_include:
        voters = voters.filter(**voter_constraints_include)
    if voter_constraints_exclude:
        voters = voters.exclude(**voter_constraints_exclude)

    with transaction.atomic():
        already = set(batch.emails.values_list('id', flat=True))
        queued_voters = set(batch.emails.values_list('voter_id', flat=True))
        OutgoingEmail.objects.bulk_create(
            [OutgoingEmail(batch=batch, election=election, voter_id=voter_id, user_id=user_id, kind=OutgoingEmail.KIND_BULK)
             for voter_id, user_id in voters.values_list('id', 'user_id') if voter_id not in queued_voters],
            batch_size=1000)
        for email_id in batch.emails.filter(voter__isnull=False).values_list('id', flat=True):
            if email_id not in already:
                mail.enqueue(email_id, OutgoingEmail.KIND_BULK)


@task()
def voters_notify(election_id, notification_template, extra_vars={}, language=None):
    election = Election.objects.get(id=election_id)
    for voter in election.voter_set.all():
        single_voter_notify.enqueue(voter.uuid, notification_template, extra_vars, language=language)


@task()
def send_outgoing_email(outgoing_email_id):
    """
    Send one email (an OutgoingEmail, see helios.mail). On a temporary error the
    email is deferred and this task queued again for later; the task itself
    only fails on a bug, not on an error of the mail server.
    """
    mail.send_queued(outgoing_email_id)


@task()
def single_voter_email(voter_uuid, subject_template=None, body_template=None, extra_vars=None, language=None,
                       outgoing_email_id=None):
    """
    Queued before send_outgoing_email existed: the email to one voter, of a bulk
    mailing (OUTGOING_EMAIL_ID) or of the templates given here.
    """
    if outgoing_email_id is None:
        voter = Voter.objects.get(uuid=voter_uuid)
        email = mail.new_email(OutgoingEmail.KIND_BULK, voter=voter, language=language or '',
                               params={'subject_template': subject_template, 'body_template': body_template,
                                       'extra_vars': extra_vars or {}})
        outgoing_email_id = email.id
    mail.send_queued(outgoing_email_id)


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
""") % {'election': election.name}
    election_notify_admin.enqueue(election_id=election_id, subject=subject, body=body, language=language)

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
""") % {'election': election.name}
    election_notify_admin.enqueue(election_id=election_id, subject=subject, body=body, language=language)


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
%(skipped)s""") % {'election': voter_file.election.name, 'created': created, 'skipped': skipped},
                                language=translation.get_language())


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
""") % {'optin_url': optin_url}

    notify_election_admin(election, subject, body)


@task()
def election_notify_admin(election_id, subject, body, language=None):
    election = Election.objects.get(id=election_id)
    with _language(language):
        notify_election_admin(election, subject, body)


def notify_election_admin(election, subject, body):
    """
    An email to the administrator of ELECTION about it (voter file processed,
    tally computed, trustee uploads, ...), queued to be sent in the background.
    Like the emails to its voters, it comes from the election's organization:
    sender name, Reply-To and its signature, which is added here (in the active
    language). Bodies queued before the signature was added here end with their
    own one ("--" line) and keep it.
    """
    if '\n--\n' not in body:
        body = '%s\n\n--\n%s\n' % (body.rstrip(), organizations.for_election(election).mail_signature)
    return mail.queue(OutgoingEmail.KIND_ADMIN_INFO, election=election, user=election.admin,
                      params={'subject': subject, 'body': body})
