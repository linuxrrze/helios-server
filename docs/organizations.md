# Organizations: corporate identity per election

Several organizations (e.g. universities, faculties, associations) can share one Helios
server and still show their own corporate identity (CI) to their voters: logos, colors,
site title, imprint, privacy policy, accessibility statement, further footer links and the
contact address. The server, its host name and its logins stay the same for everyone.

- Every election belongs to one organization. Its pages (for voters, trustees and its
  administrators) show that organization's CI.
- All other pages (start page, login, "My elections", creating an election, voter groups,
  site administration, the operator's legal pages) show the CI of the **default
  organization**: the operator of the server, configured with the variables of
  [configuration.md, section 5](configuration.md#5-site-and-interface) (`SITE_TITLE`,
  `SITE_CSS_URL`, `ORGANIZATION_NAME`, `IMPRINT_URL`, ...).
- The voting booth and the ballot verifier are not themed (yet): they look the same for
  all elections.
- Emails are not themed yet either: sender, signature and opt-out list are shared.

CI is not a separation of tenants: all organizations share the database, the site
administrators, the logins and the email opt-out list. If an organization requires its
data to be kept apart, run a separate Helios instance for it instead.

Without `ORGANIZATIONS_FILE`, there is only the default organization and nothing changes.

## Contents

1. [Configuration file](#1-configuration-file)
2. [Which organization an election belongs to](#2-which-organization-an-election-belongs-to)
3. [One mapping for organizations and address prefixes](#3-one-mapping-for-organizations-and-address-prefixes)
4. [Start page](#4-start-page)
5. [Assigning existing elections](#5-assigning-existing-elections)
6. [Migrating from a single-organization setup](#6-migrating-from-a-single-organization-setup)

## 1. Configuration file

The operator maintains the organizations in a JSON file and points `ORGANIZATIONS_FILE` to
it (a path inside the container with Docker Compose, e.g. `/site/organizations.json`; on the
host with systemd). Keep it with the site directory (see
[configuration.md, section 10](configuration.md#10-customizing-the-site)), so that it is
reviewed and deployed like the other site files. Helios reads it at start; after changing
it, restart the web and worker processes. **Errors in the file stop Helios from starting**,
with a message naming the organization and key.

Example with two universities:

```json
{
  "organizations": {
    "unia": {
      "name": {"en": "University A", "de": "Universität A"},
      "url": "https://www.uni-a.example.org",
      "short_name_prefix": "unia",
      "match": {
        "idps": ["https://idp.uni-a.example.org/realms/staff"],
        "email_domains": ["uni-a.example.org"]
      },
      "site_title": {"en": "Elections at University A", "de": "Wahlen der Universität A"},
      "css_url": "/static/site/unia/site.css",
      "main_logo_url": "/static/site/unia/logo.svg",
      "header_logo_url": "/static/site/unia/logo-small.svg",
      "footer_logo_url": "/static/site/unia/logo-white.svg",
      "imprint_url": "https://www.uni-a.example.org/imprint",
      "privacy_url": "https://www.uni-a.example.org/privacy-elections",
      "accessibility_url": "https://www.uni-a.example.org/accessibility",
      "footer_links": [
        {"url": "https://www.uni-a.example.org/electoral-office", "text": {"en": "Electoral office", "de": "Wahlamt"}}
      ],
      "help_email": "elections@uni-a.example.org"
    },
    "unib": {
      "name": "University B",
      "url": "https://www.uni-b.example.org",
      "short_name_prefix": "unib",
      "match": {"idps": ["saml"], "email_domains": ["uni-b.example.org"]},
      "css_url": "/static/site/unib/site.css",
      "footer_logo_url": "/static/site/unib/logo-white.svg",
      "dark_mode": "auto"
    }
  }
}
```

The keys of `organizations` are the **slugs**: 1 to 50 lowercase letters, digits or
hyphens (not at the start or end). Elections store the slug, so don't rename it once
elections use it.

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Name of the organization: in the header (linked to `url`), as alt text of its logos, and in the site administrators' list. Text or per language (see below). |
| `url` | | Home page of the organization (`https://...`). The header link is shown only with `name` and `url`. |
| `short_name_prefix` | | Prefix of the addresses of its elections (`<URL_HOST>/helios/e/<prefix>-<name>`): 1 to 20 lowercase letters or digits, unique. |
| `match` | | Who belongs to the organization: `idps` (identity providers: issuers / entity IDs or auth system names) and `email_domains` (subdomains included). See [section 3](#3-one-mapping-for-organizations-and-address-prefixes). Each identity provider and domain may appear in one organization only. |
| `site_title` | | Site title on the election's pages (page title, navigation, alt text of the main logo). Text or per language. |
| `css_url` | | Stylesheet loaded after the default layout, usually setting the CSS variables listed in [configuration.md, section 5](configuration.md#5-site-and-interface). Must be a path on this server (e.g. under `/static/site/`). |
| `dark_mode` | | `auto` or `off`, as `DARK_MODE`. With its own `css_url` an organization defaults to `off` (like `SITE_CSS_URL`), otherwise it follows `DARK_MODE`. |
| `main_logo_url` | | Logo above the page content. Path on this server. |
| `header_logo_url` | | Small logo (16 px high) in the header bar, instead of the name. Path on this server. |
| `footer_logo_url` | | Logo on the dark footer background (use a light version). Path on this server. |
| `imprint_url`, `privacy_url`, `accessibility_url` | | Footer links "Imprint", "Privacy", "Accessibility": `https://...` or a path on this server. `""` hides the link. |
| `footer_links` | | Further footer links: `[{"url": "...", "text": "..."}]`, text or per language. `[]` hides the operator's links. |
| `help_email` | | Address of the footer link "Contact" and of error hints on the election's pages. `""` hides the link. |

Other keys are refused, so that typos don't go unnoticed.

**Texts** (`name`, `site_title`, the `text` of footer links) are either a plain text, used
for all languages, or an object with a text per interface language, e.g.
`{"en": "University A", "de": "Universität A"}`. A language without an entry gets the
plain text / the `LANGUAGE_CODE` entry / English.

**What an organization doesn't set** is taken from the default organization for the
layout: `site_title`, `css_url`, `dark_mode`, `main_logo_url`, the legal links,
`footer_links` and `help_email`. Set a value to `""` (or `[]`) to hide the operator's item
instead; e.g. an organization with its own legal pages usually sets all three links. The
items that identify an organization are never taken from the operator: `name`, `url`,
`header_logo_url` and `footer_logo_url`.

**Files.** Stylesheets, logos and fonts must be served by this server: the Content Security
Policy only allows `'self'`, and the configuration refuses other hosts for `css_url` and
the logos. Put them into the site directory, one folder per organization:

```
site/static/                     -> SITE_STATIC_DIR, served at /static/site/
├── site.css, logo.svg ...       (the operator's)
├── unia/
│   ├── site.css
│   ├── logo.svg
│   ├── logo-small.svg
│   └── logo-white.svg
└── unib/
    ├── site.css
    └── logo-white.svg
```

An organization's legal pages can be external pages or templates in `SITE_TEMPLATES_DIR`
served under your own paths, e.g. by overriding `server_ui/templates/imprint.html` for the
operator and linking the organizations' pages on their own websites.

## 2. Which organization an election belongs to

- **When an election is created**, it gets the organization of the person creating it,
  determined from their login (see [section 3](#3-one-mapping-for-organizations-and-address-prefixes)).
  People matching no organization create elections of the default organization. A copy of
  an election keeps the organization of the original.
- **Only site administrators can change it**: on the election's page under "More
  actions", a site administrator sees the organization and can choose another one (also
  after the ballot is frozen; the organization is not part of the election's fingerprint).
  The election's own administrators see the organization, but can't change it: otherwise
  anyone could send out an election that looks like another organization's (phishing).
- An election whose slug is no longer in the file is shown in the default CI, and stays
  reachable.

The CI is shown on all pages rendered for an election: the election's page, questions,
voter list, trustees, results, the voters' password login, the trustees' pages, and the
administrators' pages of the election (edit, voters, trustees, administrators, freeze,
tally).

## 3. One mapping for organizations and address prefixes

The same rules assign a person to an organization and give the prefix of their election
addresses, so that there is only one mapping to maintain. Helios looks for the first
match in this order:

1. **The identity provider** they logged in with, first its **issuer** (OIDC issuer or SAML
   entity ID), then the **auth system name** (e.g. `saml`, `oidc`, `ldap`, or the name set
   with `OIDC_AUTH_SYSTEM_NAME`): the `idps` of the organizations, then
   `ELECTION_SHORT_NAME_PREFIXES_BY_IDP`. For the same key, the organization wins.
2. **The email domain** of their email address (or of a login ID such as
   `jdoe@uni-a.example.org`): the `email_domains` of the organizations and
   `ELECTION_SHORT_NAME_PREFIXES`. The most specific domain wins
   (`med.uni-a.example.org` before `uni-a.example.org`); for the same domain, the
   organization wins.
3. Otherwise `ELECTION_SHORT_NAME_PREFIX`.

A rule of an organization gives that organization and its `short_name_prefix`. A rule of
the `ELECTION_SHORT_NAME_PREFIX*` settings gives its prefix, and **the organization whose
`short_name_prefix` that is** (else the default organization). So existing prefix rules
keep working unchanged, and assign people to an organization as soon as an organization
with that prefix exists.

Keys are compared as in the prefix settings: identity providers exactly apart from a
trailing `/`, email domains regardless of case. Helios stores the issuer when someone logs
in with OIDC or SAML; for people who have not logged in since this was added, only the auth
system name is known. Like the prefix, the organization is a matter of appearance only:
it grants no permissions.

## 4. Start page

The start page keeps the operator's CI. With an organizations file, its list of
**featured elections** is filtered: people who are logged in see the featured elections of
their own organization only (determined as in section 3; people of the default
organization also see those of organizations no longer in the file), and visitors who are
not logged in see none. The lists of one's own elections and votes are not filtered.
Without an organizations file, everyone sees all featured elections as before.

## 5. Assigning existing elections

Elections created before the organizations file belong to the default organization.
`assign_organizations` assigns them; always look at a dry run first:

```sh
# by the organization of their creator (identity provider, email domain), as for new elections
python manage.py assign_organizations --dry-run
python manage.py assign_organizations

# by the prefix of their address (unia-..., UNIB-...)
python manage.py assign_organizations --by prefix --dry-run

# only one organization
python manage.py assign_organizations --only unia

# explicitly, by UUID or address; "" for the default organization
python manage.py assign_organizations --set unib unib-board-2026 0b9b2c34-...
python manage.py assign_organizations --set "" unib-board-2026
```

Without `--overwrite`, only elections of the default organization are changed; elections
for which no organization is found stay as they are. Deleted elections are skipped unless
`--include-deleted` is given. With Docker Compose, run it as
`docker compose exec web python manage.py assign_organizations ...`.

## 6. Migrating from a single-organization setup

1. Update. Without `ORGANIZATIONS_FILE` everything looks as before: the global settings are
   the default organization, all elections belong to it.
2. Decide what the default organization is: either keep the existing settings as they are
   (the organization that has used the server so far stays the default), or turn them into
   a neutral operator CI and give the existing organization an entry in the file whose
   `css_url`, logos and legal links point to its existing files (the stylesheet with CSS
   variables keeps working unchanged).
3. Move `ELECTION_SHORT_NAME_PREFIXES_BY_IDP` / `ELECTION_SHORT_NAME_PREFIXES` into the
   `short_name_prefix` and `match` of the organizations. This is optional: as long as an
   organization's `short_name_prefix` equals the prefix of an existing rule, the rule
   already assigns people to it. Remove the old variables once the file has the same rules,
   so that there is only one place to maintain.
4. Assign the existing elections (section 5), dry run first. Their addresses, links and
   fingerprints don't change.
5. For a new organization: add a folder under the site's static directory and an entry in
   the file, and restart.
