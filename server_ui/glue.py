"""
Glue some events together
"""

import logging

import helios.signals
from helios import mail
from helios.models import OutgoingEmail

logger = logging.getLogger(__name__)


def vote_cast_send_message(user, voter, election, cast_vote, **kwargs):
    """
    Confirmation email after a ballot is stored. Runs inside the cast-vote task,
    which already runs in the voter's language: the email is queued in it and sent
    by its own task (helios.mail, templates in server_ui/templates/email/), so that
    an error of the mail server never touches the cast vote.
    """
    # The ballot is already stored: a failure here must not fail the cast-vote task.
    try:
        mail.queue(OutgoingEmail.KIND_VOTE_CAST, election=election, voter=voter, user=user,
                   params={'cast_vote_id': cast_vote.id})
    except Exception:
        logger.exception("Could not queue vote-cast email for voter %s", voter.uuid)


def election_tallied(election, **kwargs):
    pass


def glue():
    helios.signals.vote_cast.connect(vote_cast_send_message)
    helios.signals.election_tallied.connect(election_tallied)
