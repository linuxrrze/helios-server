"""
Glue some events together
"""

import logging

from django.utils.translation import gettext as _

import helios.signals
import helios.views
from helios import mail
from helios.models import OutgoingEmail
from helios.tasks import voter_unsubscribe_vars
from helios.view_utils import render_text_template

logger = logging.getLogger(__name__)


def vote_cast_send_message(user, voter, election, cast_vote, **kwargs):
    """
    Confirmation email after a ballot is stored. Runs inside the cast-vote task,
    which already renders in the voter's language. Voters without a user account
    get a stub password user from Voter.get_user(), which mails voter_email.
    """
    email = mail.new_email(OutgoingEmail.KIND_VOTE_CAST, election=election, voter=voter, user=user)
    if mail.skip_opted_out(email, voter):
        return

    extra_vars = {
        'election': election,
        'voter': voter,
        'cast_vote': cast_vote,
        'cast_vote_url': helios.views.get_castvote_url(cast_vote),
        'custom_subject': _("%(election_name)s - vote cast") % {'election_name': election.name},
    }
    extra_vars.update(voter_unsubscribe_vars(voter))
    subject = render_text_template('email/cast_vote_subject.txt', extra_vars)
    body = render_text_template('email/cast_vote_body.txt', extra_vars)

    # The ballot is already stored: a mail failure must not fail the cast-vote task.
    try:
        mail.deliver(email, user, subject, body)
    except Exception:
        logger.exception("Could not send vote-cast email for voter %s", voter.uuid)


def election_tallied(election, **kwargs):
    pass


def glue():
    helios.signals.vote_cast.connect(vote_cast_send_message)
    helios.signals.election_tallied.connect(election_tallied)
