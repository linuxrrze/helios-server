from helios import election_url_names as election, stats_url_names as stats

__all__ = [
    "election", "stats",
    "COOKIE_TEST", "COOKIE_TEST_2", "COOKIE_NO",
    "ELECTION_SHORTCUT", "ELECTION_SHORTCUT_VOTE", "CAST_VOTE_SHORTCUT", "CAST_VOTE_FULLHASH_SHORTCUT",
    "TRUSTEE_LOGIN",
    "ELECTIONS_PARAMS", "ELECTIONS_VERIFIER", "ELECTIONS_VERIFIER_SINGLE_BALLOT",
    "ELECTIONS_NEW", "ELECTIONS_NEW_GUIDED", "ELECTIONS_ADMINISTERED", "ELECTIONS_VOTED",
]

COOKIE_TEST="cookie@test"
COOKIE_TEST_2="cookie@test2"
COOKIE_NO="cookie@no"

ELECTION_SHORTCUT="shortcut@election"
ELECTION_SHORTCUT_VOTE="shortcut@election@vote"
CAST_VOTE_SHORTCUT="shortcut@vote"
CAST_VOTE_FULLHASH_SHORTCUT="shortcut-fullhash@vote"

TRUSTEE_LOGIN="trustee@login"

ELECTIONS_PARAMS="elections@params"
ELECTIONS_VERIFIER="elections@verifier"
ELECTIONS_VERIFIER_SINGLE_BALLOT="elections@verifier@single-ballot"
ELECTIONS_NEW="elections@new"
ELECTIONS_NEW_GUIDED="elections@new@guided"
ELECTIONS_ADMINISTERED="elections@administered"
ELECTIONS_VOTED="elections@voted"

# voter groups
VOTERGROUP_ADMINISTERED="votergroup@administered"
VOTERGROUP_NEW="votergroup@new"
VOTERGROUP_VIEW="votergroup@view"
VOTERGROUP_MEMBERADD="votergroup@memberadd"
VOTERGROUP_MEMBERDELETE="votergroup@memberdelete"
VOTERGROUP_PREPAREELECTION="votergroup@prepareelection"
VOTERGROUP_ADMINS="votergroup@admins"
VOTERGROUP_ADMINS_ADD="votergroup@admins@add"
VOTERGROUP_ADMINS_REMOVE="votergroup@admins@remove"
