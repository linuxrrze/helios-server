# Provisioning with SCIM 2.0

Helios can be provisioned by an identity management system (IdM) such as Keycloak, Microsoft
Entra ID or Okta over SCIM 2.0 ([RFC 7643](https://www.rfc-editor.org/rfc/rfc7643),
[RFC 7644](https://www.rfc-editor.org/rfc/rfc7644)). Helios is the SCIM *server*: the IdM
pushes users and groups to `<SECURE_URL_HOST>/scim/v2/`, and Helios derives from them

- **users**, who are then known before their first login (e.g. to be added as administrators),
- **site administrators** (members of one configured group),
- **election creators** ("Wahlleitung" in the German interface: may create and run elections;
  members of another configured group),
- **voter groups** with their members and administrators (all other groups).

Everything provisioned is marked as such ("Provisioned (SCIM: &lt;client&gt;)") and can't be
changed in Helios: changes are made in the IdM. Locally created users and voter groups work as
before.

SCIM is off by default. All settings are listed in [configuration.md](configuration.md#6-login).

## Contents

1. [Setup](#1-setup)
2. [Matching the login](#2-matching-the-login)
3. [What groups mean](#3-what-groups-mean)
4. [What is locked in Helios](#4-what-is-locked-in-helios)
5. [Deactivating and deleting](#5-deactivating-and-deleting)
6. [Connecting an IdM](#6-connecting-an-idm)
7. [Protocol support](#7-protocol-support)
8. [Security notes](#8-security-notes)

## 1. Setup

1. Generate a token for the IdM (the *client*, here named `idm`):

   ```sh
   python manage.py scim_token idm
   ```

   It prints the token (give it to the IdM, it is shown only once) and the entry for
   `SCIM_TOKENS` with the token's SHA-256 digest. Helios keeps only digests.

2. Configure Helios (environment, see [configuration.md](configuration.md)):

   ```sh
   SCIM_ENABLED=1
   SCIM_TOKENS='{"idm": "sha256:<64 hex digits>"}'
   SCIM_USER_TYPE=campus            # the auth system users log in with
   SCIM_USER_ID_ATTRIBUTE=userName  # which SCIM attribute is their user id at login
   SCIM_ADMIN_GROUP=helios-admins
   SCIM_ELECTION_CREATOR_GROUP=helios-election-creators
   SCIM_VOTERGROUP_ADMINS_SUFFIX=" (admins)"
   ELECTION_CREATORS_ONLY=1         # optional, see section 3
   ```

3. Restart Helios and run `python manage.py check`: it reports an invalid `SCIM_TOKENS`, an
   unknown `SCIM_USER_TYPE` and similar mistakes (without showing tokens).

4. Point the IdM at `https://vote.example.org/scim/v2/` with the token (section 6) and test the
   connection; most IdMs first read `/ServiceProviderConfig` or list `/Users`.

Several clients can provision the same Helios site, each with its own token(s). A client sees
and changes only what it provisioned itself; the client's name is recorded with every user and
group (`provisioned_by` = `scim:<client>`). To rotate a token, give the client a list of digests
(`{"idm": ["sha256:<old>", "sha256:<new>"]}`), switch the IdM to the new token, then remove the
old digest. Plain tokens (at least 32 characters) are accepted in `SCIM_TOKENS` too, but
digests are recommended.

After changing `SCIM_ADMIN_GROUP`, `SCIM_ELECTION_CREATOR_GROUP`, `SCIM_VOTERGROUP_ADMINS_GROUP`
or `SCIM_VOTERGROUP_ADMINS_SUFFIX`, run `python manage.py scim_sync`: groups are otherwise only
applied again when the IdM changes them.

## 2. Matching the login

Helios identifies a person by the pair (auth system, user id), e.g. (`campus`,
`jane.doe@example.org`). A provisioned user is only useful if the login later creates
**exactly the same pair**; otherwise the login creates a second, unrelated user.

- `SCIM_USER_TYPE` is the name of the auth system people log in with (`OIDC_AUTH_SYSTEM_NAME`
  or `SAML_AUTH_SYSTEM_NAME`, e.g. `campus`; default: `OIDC_AUTH_SYSTEM_NAME`).
- `SCIM_USER_ID_ATTRIBUTE` says which SCIM attribute is the user id at login:

| Login | User id at login | `SCIM_USER_ID_ATTRIBUTE` | The IdM must send |
|---|---|---|---|
| OIDC, `OIDC_USER_ID_CLAIM=email` | email address | `email` (or `userName`, if it is the address) | the address as primary (or work) email |
| OIDC, `OIDC_USER_ID_CLAIM=preferred_username` | username | `userName` | the username as `userName` |
| OIDC, `OIDC_USER_ID_CLAIM=sub` (Keycloak) | Keycloak user id | `externalId` | the Keycloak user id as `externalId` |
| OIDC with Entra ID, `OIDC_USER_ID_CLAIM=oid` | object id | `externalId` | `objectId` as `externalId` (Entra's `sub` is pairwise and can't be provisioned) |
| SAML, `SAML_ATTRIBUTE_USER_ID` = mail / eppn | address / eppn | `email` or `userName` | the same value |
| SAML with pairwise or transient NameID | not predictable | – | provisioning can't match these logins |

Email addresses are compared case-insensitively when the login uses them as id (as for login
itself). A user who logged in before being provisioned is taken over, not duplicated.

**On login**, a provisioned user keeps the name and email address from the IdM: the login
data does not overwrite them (other login data, e.g. a picture, is stored as usual). Changes of
name or address come through SCIM. If the IdM sends no address, the one from the login is used.

A change of the attribute used as id (e.g. a renamed `userName`) renames the Helios user; the
person keeps their elections and roles.

## 3. What groups mean

A SCIM group is recognized by its `displayName` (case-insensitive) or `externalId`:

| Group | Effect in Helios |
|---|---|
| `SCIM_ADMIN_GROUP` | members are **site administrators** (`admin_p`) |
| `SCIM_ELECTION_CREATOR_GROUP` | members are **election creators** ("Wahlleitung"): they may create elections, which they then run as their administrators, and voter groups |
| `SCIM_VOTERGROUP_ADMINS_GROUP` | members **administer all voter groups** of this client |
| `<name><SCIM_VOTERGROUP_ADMINS_SUFFIX>`, e.g. `Council (admins)` | members **administer the voter group** `<name>` (`Council`) |
| any other group | a **voter group** with these members, one vote each |

**Election creators.** By default, everyone who can log in may create elections, so the role
only matters with `ELECTION_CREATORS_ONLY=1`: then only site administrators and members of
`SCIM_ELECTION_CREATOR_GROUP` may create elections and voter groups. Additional administrators
of a single election are still added in Helios, per election (they are not provisioned).

**Roles.** Membership grants the role, removal from the group (or deleting the group, or
deactivating the user) revokes it. A role Helios granted otherwise (e.g. set by hand in the
database) is kept when the user is not in the group; once the group grants it too, it counts as
granted by SCIM and is revoked with the membership.

**Voter groups.** A provisioned voter group is owned by an account that stands for the client
("SCIM: idm"; nobody can log in as it). Its administrators are the members of the administrator
groups above, and only they: administrators can't be added in Helios. Site administrators who
aren't among them see only the group's overview (name, owner, number of members), as for local
groups. Administrators need the right to create
elections (see `ELECTION_CREATORS_ONLY`) to see voter groups at all. Each member has one vote;
first name, last name and email come from the user (`name.givenName`, `name.familyName`, the
primary email). Elections are prepared from a provisioned voter group as from any other: the
election gets its own copy of the voters, so later changes to the group don't affect it.

Group names are unique per client (case-insensitive) and at most 100 characters long. Members
must be users provisioned by the same client; other members (unknown ids, nested groups) are
ignored and logged.

**Group log.** Besides the server log (logger `helios.provisioning`), changes made by SCIM are
recorded in the group's log on the group's page, shown to its administrators with "SCIM:
&lt;client&gt;" as the author: created, renamed, and member updates as counts ("2 added, 1
removed; 3 administrator(s)"), without member data. Elections prepared from the group are
recorded there, too. When the group is deleted, its log goes with it; the server log keeps the
deletion.

## 4. What is locked in Helios

Provisioned entities show the badge "Provisioned (SCIM: &lt;client&gt;)" (voter group lists and
pages, election and voter group administrators, the site administrators' user search). The
server refuses, with an explanation (HTTP 403), every change to

- a provisioned voter group: adding or removing members, adding or removing administrators,
  transferring the ownership;
- a provisioned user's name and email address (kept on login, see section 2).

Roles have no editing page in Helios; `manage.py merge_duplicate_users` skips provisioned users.
Everything else stays possible: preparing elections from provisioned voter groups, adding
provisioned users as additional administrators of an election, local voter groups and users.

## 5. Deactivating and deleting

- **`active: false`** (e.g. a disabled account): the user can no longer log in (an open session
  ends with the next request), loses the roles granted by SCIM and is left out of provisioned
  voter groups, and can't be added as an administrator. Elections, votes and logs are kept.
  `active: true` restores everything.
- **DELETE /Users/&lt;id&gt;**: the user leaves all groups and the SCIM resource is gone. If
  Helios holds records about the person (voter in an election, administrator of an election or
  of a local voter group), the Helios user is **deactivated** and kept, still marked as
  provisioned, so that elections and their audit trail stay intact; otherwise it is deleted. If
  the IdM creates the person again, the deactivated user is reactivated.
- **DELETE /Groups/&lt;id&gt;**: a voter group is deleted (elections prepared from it keep their
  voters); a role group revokes the role from its members.

## 6. Connecting an IdM

The base URL is `<SECURE_URL_HOST>/scim/v2/`, authentication is the bearer token from step 1.

**Microsoft Entra ID** (enterprise application, *Provisioning*, mode *Automatic*):

- *Tenant URL*: `https://vote.example.org/scim/v2/`, *Secret token*: the token.
- Attribute mapping for users: `userName` ← `userPrincipalName` (or `mail`),
  `active` ← `Switch([IsSoftDeleted], , "False", "True", "True", "False")` (the default),
  `emails[type eq "work"].value` ← `mail`, `name.givenName` ← `givenName`,
  `name.familyName` ← `surname`, `displayName` ← `displayName`, `externalId` ← `objectId`
  (needed for `OIDC_USER_ID_CLAIM=oid` with `SCIM_USER_ID_ATTRIBUTE=externalId`). Remove
  mappings Helios doesn't need, e.g. phone numbers and addresses; they are ignored anyway.
- Groups: `displayName` ← `displayName`, `members` ← `members`. Assign the role groups and the
  groups that should become voter groups to the application, and set the scope to
  *assigned users and groups*.
- Entra's PATCH dialect (capitalized operation names, booleans as strings such as `"False"`,
  member removal with a value list, `emails[type eq "work"].value` paths) is understood.

**Keycloak** has no SCIM client of its own; use a SCIM client extension (several open-source
extensions exist). Configure it with the base URL and the bearer token, enable user and group
propagation, and check what it sends as `userName` and `externalId`: with
`OIDC_USER_ID_CLAIM=sub` the Keycloak user id must arrive as `externalId`
(`SCIM_USER_ID_ATTRIBUTE=externalId`); with `email` or `preferred_username` use `email` or
`userName`. Keycloak group names become voter groups; name the role groups as configured.

**Okta** (application with SCIM provisioning): base URL and *HTTP Header* authentication with
the token; unique identifier field `userName`; enable *Push New Users*, *Push Profile Updates*,
*Push Groups*, *Deactivate Users*.

To check the mapping, look the person up in Helios' site administration (*User Search*): the
entry shows the auth system, the user id and whether the user is provisioned. After the
person's first login there must still be only one entry.

## 7. Protocol support

| Endpoint | Methods |
|---|---|
| `/ServiceProviderConfig`, `/ResourceTypes`, `/Schemas` | GET |
| `/Users`, `/Groups` | GET (list with `filter`, `startIndex`, `count`, `attributes`, `excludedAttributes`), POST |
| `/Users/<id>`, `/Groups/<id>` | GET, PUT, PATCH, DELETE |

- Filters: `eq ne co sw ew gt ge lt le pr`, `and or not`, parentheses and value paths such as
  `members[value eq "<id>"]` or `emails[type eq "work"]`. `userName`, `displayName` and email
  addresses are compared case-insensitively, `id` and `externalId` case-sensitively. At most
  200 results per page (default 100).
- PATCH: `add`, `remove`, `replace` with and without path, value paths with filters,
  schema-prefixed paths, case-insensitive operation names and booleans as strings.
- Stored are only `userName`, `externalId`, `name` (`givenName`, `familyName`, `formatted`,
  `middleName`), `displayName`, `emails` and `active` for users, `displayName`, `externalId` and
  `members` for groups. Other attributes and extensions (e.g. the enterprise extension) are
  accepted and ignored.
- Not supported: bulk operations, sorting, ETags, password changes, `/Me`, nested groups.
- Errors use the SCIM error format (`urn:ietf:params:scim:api:messages:2.0:Error`, `scimType`
  such as `uniqueness`, `invalidFilter`, `invalidValue`, `invalidSyntax`, `noTarget`), responses
  are `application/scim+json`.

## 8. Security notes

- Tokens are compared as SHA-256 digests in constant time and never logged. Use long random
  tokens (`manage.py scim_token`) and one per client.
- The endpoints use no sessions or cookies and no CSRF tokens; every request must carry a
  token. Missing or wrong tokens get `401` and are logged (without the token). With
  `SCIM_ENABLED=0` the endpoints answer `404`.
- A client controls who is site administrator if `SCIM_ADMIN_GROUP` is set: protect the token
  and the group in the IdM accordingly. Leave the setting empty if site administrators should
  not be provisioned.
- Serve `/scim/v2/` only over HTTPS; if possible, restrict it to the IdM's addresses in the
  reverse proxy, and rate-limit it there.
- Provisioning actions are logged with masked addresses (loggers `helios.scim` and
  `helios.provisioning`, e.g. `scim:idm: user campus:12 j***@example.org created`).
- Only the attributes listed in section 7 are stored (data minimization). Deleting a user in the
  IdM deletes the Helios user unless elections refer to it (section 5).
- The mapping lives in `helios/provisioning.py` (independent of SCIM) and
  `helios_scim/mapping.py`; other sources, e.g. a directory import, can reuse the former.
