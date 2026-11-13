# Single sign-on (SAML and OIDC)

How Helios signs in election administrators with your institution's single sign-on: what to
request from the identity provider (IdP), how to configure Helios, how to switch from SAML to
OIDC, and what to do when sign-in fails. All variables are listed in
[configuration.md](configuration.md). The examples use the host `vote.example.org`, the IdP
`idp.example.org` and the auth system name `campus`.

Voters normally don't use SSO: they receive per-election passwords by email. SSO is for election
administrators, and for voters of elections whose voter list uses SSO accounts.

## Contents

1. [How identities work](#1-how-identities-work)
2. [What Helios needs from the IdP](#2-what-helios-needs-from-the-idp)
3. [SAML](#3-saml)
4. [OIDC](#4-oidc)
5. [Switching from SAML to OIDC](#5-switching-from-saml-to-oidc)
6. [Troubleshooting](#6-troubleshooting)

## 1. How identities work

Helios stores every user as a pair **(auth system name, user id)**. A recommended setup:

- the auth system name is one name of your choice, e.g. **`campus`**, whichever protocol is
  behind it (`SAML_AUTH_SYSTEM_NAME=campus` or `OIDC_AUTH_SYSTEM_NAME=campus`), and
- the user id is the **email address** sent by the IdP. When the user id is the email address,
  lookups ignore upper/lower case.

Elections, voter-group memberships and admin rights hang on this pair. As long as both parts stay
the same, the protocol can change without anyone losing access. If either changes (another name,
or `sub` instead of the email address as user id), Helios sees new users: administrators would
no longer see their elections.

Only one protocol can be configured under a name at a time. If both `SAML_IDP_*` and
`OIDC_DISCOVERY_URL` are set with the same name, Helios refuses to start.

The `campus` system must be listed in `AUTH_ENABLED_SYSTEMS`. With `AUTH_DEFAULT_SYSTEM=campus`
the landing page shows a single login button. The button text comes from `SAML_LOGIN_MESSAGE` /
`OIDC_LOGIN_MESSAGE`, with a `_DE` variant for the German interface. Where Helios lists users
or login systems with an icon, it uses `login-icons/<name>.png` from `SITE_STATIC_DIR` (see
[configuration.md](configuration.md#10-customizing-the-site)), else the built-in
`helios_auth/media/login-icons/<name>.png`, and a neutral default icon if neither exists. `.svg`
works as well as `.png`.

## 2. What Helios needs from the IdP

| What | Why | SAML attribute (example) | OIDC claim |
|---|---|---|---|
| Email address | user id and address for Helios emails | `urn:mace:dir:attribute-def:mail` (or `urn:oid:0.9.2342.19200300.100.1.3`) | `email` |
| Email verified | an unverified address must not become a user id | (implied: institutional IdP) | `email_verified` = `true` |
| Name | shown in the interface and in emails | `givenName`, `sn` | `name`, or `given_name` + `family_name` |

The email address must be **stable**: the same person must always get the same address. If the
IdP can send different addresses for one person (e.g. an alias one day and the primary address
the next), that person ends up with two Helios users.

## 3. SAML

Helios is a SAML service provider (SP). It uses SP-initiated login (HTTP-Redirect to the IdP,
HTTP-POST back) and requires signed assertions with SHA-256 or stronger. SAML single logout is
not supported: logging out of Helios ends only the Helios session.

**Install** the extra: `uv sync --frozen --no-dev --extra saml`. The published container image
(`ghcr.io/linuxrrze/helios-server`) contains it; when building yourself, use
`docker build --build-arg UV_EXTRAS="--extra saml" .`.

**Registration at the IdP.** Ask your identity provider team to register Helios as an SP:

| | Value |
|---|---|
| SP entity ID | `https://vote.example.org/auth/saml/metadata` |
| Assertion consumer service (HTTP-POST) | `https://vote.example.org/auth/saml/acs` |
| SP metadata | `https://vote.example.org/auth/saml/metadata` |
| Attributes to release | `mail`, `givenName`, `sn` |

If Helios replaces an earlier SAML integration that is already registered, you can keep that
registration: set `SAML_SP_ENTITY_ID` and `SAML_SP_ACS_URL` to the registered values. Helios
serves the ACS at the path of `SAML_SP_ACS_URL` (e.g. `/saml2_auth/acs/`); no proxy rule is
needed for that.

**Configuration** (`.env`, or the environment of the web and worker processes):

```sh
AUTH_ENABLED_SYSTEMS=campus
AUTH_DEFAULT_SYSTEM=campus
SAML_AUTH_SYSTEM_NAME=campus
SAML_LOGIN_MESSAGE="Log in with Example University SSO"
SAML_LOGIN_MESSAGE_DE="Mit Example University SSO anmelden"
SAML_IDP_METADATA_URL=https://idp.example.org/idp/shibboleth
# defaults: <SECURE_URL_HOST>/auth/saml/metadata and <SECURE_URL_HOST>/auth/saml/acs
# SAML_SP_ENTITY_ID=https://vote.example.org/auth/saml/metadata
# SAML_SP_ACS_URL=https://vote.example.org/auth/saml/acs
SAML_ATTRIBUTE_USER_ID=urn:mace:dir:attribute-def:mail
SAML_ATTRIBUTE_EMAIL=urn:mace:dir:attribute-def:mail
SAML_ATTRIBUTE_GIVEN_NAME=urn:mace:dir:attribute-def:givenName
SAML_ATTRIBUTE_SURNAME=urn:mace:dir:attribute-def:sn
```

Use the attribute names your IdP actually sends (SAML2 IdPs often use the `urn:oid:` form).

**Further settings**, only if needed:

- `SAML_IDP_METADATA_FILE`: a local copy of the IdP metadata instead of the URL.
- `SAML_IDP_ENTITY_ID`: required if the metadata lists several IdPs (federation metadata).
  Helios refuses to guess.
- `SAML_IDP_X509_CERT`: pin the IdP signing certificate instead of trusting the one in the
  metadata.
- `SAML_SP_X509_CERT`, `SAML_SP_PRIVATE_KEY`: an SP key pair to sign requests and decrypt
  encrypted assertions, if the IdP requires that.

Helios fetches the IdP metadata at the first login and keeps it for an hour. After a certificate
change at the IdP, logins can fail for up to an hour. Restarting the web process fetches the
metadata again.

## 4. OIDC

Helios is an OpenID Connect relying party using the authorization code flow with PKCE (S256). It
verifies the ID token against the IdP's published keys (signature, issuer, audience, expiry,
nonce) and accepts only asymmetric signatures (RS*, PS*, ES*, EdDSA). Access and refresh tokens
are not stored.

**Request to your identity provider team.** Ask for a client with these properties:

| | Value |
|---|---|
| Client type | confidential (with client secret), authorization code flow |
| Redirect URI | `https://vote.example.org/auth/after/` |
| Scopes | `openid email profile` |
| Claims in the ID token | `email`, `email_verified`, `name` (or `given_name` + `family_name`) |
| ID token signature | RS256 or another asymmetric algorithm |
| Logout (optional, see below) | post-logout redirect URI `https://vote.example.org/auth/logout/done` |

Also ask:

- If you switch from SAML: **is `email` the same address as the SAML `mail` attribute?** It
  must be, for the same person, or administrators lose their elections (section 1).
- **Does the IdP send `email_verified`?** If not, Helios refuses sign-in, unless
  `OIDC_TRUST_UNVERIFIED_EMAIL=1` is set. Set that only for an institutional IdP whose addresses
  are authoritative.
- **Does the IdP support RP-initiated logout** (`end_session_endpoint` in the discovery
  document)? If so, register the post-logout redirect URI above and set
  `OIDC_LOGOUT_AT_PROVIDER=1`. Logging out of Helios then also ends the SSO session, so the next
  person at a shared computer is not signed in automatically.
- The **discovery URL** (`https://<idp>/.well-known/openid-configuration`), **client ID** and
  **client secret**.

**Configuration:**

```sh
AUTH_ENABLED_SYSTEMS=campus
AUTH_DEFAULT_SYSTEM=campus
OIDC_AUTH_SYSTEM_NAME=campus
OIDC_LOGIN_MESSAGE="Log in with Example University SSO"
OIDC_LOGIN_MESSAGE_DE="Mit Example University SSO anmelden"
OIDC_DISCOVERY_URL=https://idp.example.org/.well-known/openid-configuration
OIDC_CLIENT_ID=...
OIDC_CLIENT_SECRET=...
OIDC_USER_ID_CLAIM=email           # same identities as a SAML setup keyed on the email address
# OIDC_SCOPES="openid email profile"   (default)
# OIDC_TRUST_UNVERIFIED_EMAIL=1        only if the IdP sends no email_verified
# OIDC_LOGOUT_AT_PROVIDER=1            only with end_session_endpoint + registered URI
```

For a new installation without earlier users, the default `OIDC_USER_ID_CLAIM=sub` is the
stable choice; `email` is needed to keep identities from SAML (section 5).

The configuration contains the client secret: keep the file readable only for the
administrators and the service account that runs Helios.

## 5. Switching from SAML to OIDC

If administrators already log in with SAML, OIDC can take over under the **same auth system
name**, so that they keep their elections. The switch only changes the configuration. No data
is migrated, and switching back to SAML is possible at any time.

This requires that the OIDC user id equals the SAML user id: with
`SAML_ATTRIBUTE_USER_ID` set to the email attribute, use `OIDC_USER_ID_CLAIM=email`.

**Before**

- [ ] Your identity provider team has registered the client (section 4) and confirmed that
      `email` equals the SAML email attribute.
- [ ] Discovery URL, client ID and secret are at hand.
- [ ] Optional dry run with a separate name, while SAML stays active as `campus`: set
      `OIDC_AUTH_SYSTEM_NAME=oidc-test` with the OIDC settings, add `oidc-test` to
      `AUTH_ENABLED_SYSTEMS`, restart, and open `https://vote.example.org/auth/start/oidc-test`.
      A successful login shows the name at the top right. This creates a separate test user
      (`oidc-test`, your address) without access to your elections. Remove the test settings
      afterwards.
- [ ] No election is open or in the middle of a step that needs an administrator, and nobody is
      logged in who would be interrupted.

**Switch**

1. Back up the configuration (`.env`).
2. Comment out **all** `SAML_*` lines and add the OIDC lines (section 4).
3. Restart the web and worker processes (`docker compose up -d`).
4. If Helios does not start, look at its log (`docker compose logs web`). An
   `ImproperlyConfigured` error about SAML and OIDC means a `SAML_IDP_*` line is still active.

**After**

- [ ] Log in as an existing administrator: the start page and
      `https://vote.example.org/helios/elections/administered` ("Elections you Administer")
      list the old elections.
- [ ] No duplicate users were created. In the database (`docker compose exec db psql -U helios helios`):
      ```sql
      SELECT lower(user_id), count(*) FROM helios_auth_user
       WHERE user_type = 'campus' GROUP BY 1 HAVING count(*) > 1;
      ```
      This must return no rows. The check ignores case, because Helios also treats email user
      ids that way. Two users that differ only in case make the login of that person fail.
      Merge them with (look at the `--dry-run` output first):
      ```sh
      docker compose run --rm web python manage.py merge_duplicate_users --dry-run
      ```
      Such duplicates come from old data, not from the switch itself. The same check is worth
      running once with SAML.
- [ ] Log out. With `OIDC_LOGOUT_AT_PROVIDER=1`, the IdP logout page appears and leads back to
      Helios, and the next login asks for the password again.
- [ ] Ask one other administrator to log in.

**Back to SAML:** restore the backed-up configuration, then restart.

## 6. Troubleshooting

Users see the message in the left column. The web process log tells the cause, e.g.
`docker compose logs --since 1h web | grep -i -E "login|saml|oidc"`. Rejected logins are logged
as `login callback rejected for auth system campus: ...`. The lines before that give the
details, e.g. `SAML response rejected: [...]` or `OIDC token endpoint returned 401: ...`.

| Message to the user | Likely cause | What to do |
|---|---|---|
| "The sign-in service cannot be reached right now." | Helios could not fetch the IdP metadata or discovery document (log: `could not start login with auth system campus`). | Check that the URL is reachable from the server or container (`curl`, proxy, DNS, firewall). |
| "This sign-in link has expired or was already used." / "This sign-in has expired or was already used." | Back button or refresh on the callback, a second login tab, or the session cookie was lost. | Start the login again. If it happens to everyone: check HTTPS and the session cookie settings (`SESSION_COOKIE_SAMESITE` must stay `Lax`, the site must be reached via the `SECURE_URL_HOST` address). |
| "The sign-in was cancelled or refused by the identity provider." | OIDC: the IdP returned an error, e.g. `access_denied`, `invalid_scope`, `unauthorized_client`. The log shows it. | Check the client registration (redirect URI, scopes) with your identity provider team. |
| "The identity provider did not accept the sign-in." | OIDC: the token endpoint rejected the code. The log has the response, typically a wrong client secret or a redirect URI mismatch. | Check `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` and the registered redirect URI. |
| "Your identity provider did not confirm your email address …" | OIDC: `email_verified` is missing or false. | Ask your identity provider team to send it, or set `OIDC_TRUST_UNVERIFIED_EMAIL=1` (section 4). |
| "Your identity provider did not send the attribute needed to sign in …" | The email attribute or claim is not released for Helios. | SAML: attribute release at the IdP. OIDC: scope `email` and the claim in the ID token. |
| "The sign-in response could not be verified." | SAML: signature, audience, destination or validity time wrong. The log lists the reasons. | Clock on the server (`timedatectl`), entity ID and ACS URL as registered, IdP certificate changed (restart the web process). |
| "The sign-in response did not match this sign-in attempt." | The response belongs to another login attempt, e.g. an IdP-initiated SAML login or an old OIDC response. | Start the login from Helios, not from an IdP portal. |
| "The sign-in token was issued for a different application." | OIDC: the ID token is for another client (`aud`/`azp`). | Check `OIDC_CLIENT_ID`. |
| Login works, but an administrator sees no elections | The user id differs from before: other address, other auth system name, or `OIDC_USER_ID_CLAIM` is not `email`. | Compare `SELECT user_type, user_id FROM helios_auth_user WHERE user_id ILIKE '%name%';` and fix the configuration. Don't edit users by hand. |
