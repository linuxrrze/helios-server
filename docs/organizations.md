# Organizations: corporate identity per election

Several organizations (e.g. universities, faculties, associations) can share one Helios
server and still show their own corporate identity (CI) to their voters: logos, colors,
site title, imprint, privacy policy, accessibility statement, further footer links and the
contact address. The server, its host name and its logins stay the same for everyone.

- Every election belongs to one organization. Its pages for voters and trustees, and its
  public pages, show that organization's CI.
- All other pages (start page, login, "My elections", creating an election, voter groups,
  the administration pages of an election, site administration) show the CI of the
  organization whose **entry link** (`https://<host>/<slug>/`) the visitor opened
  ([section 9](#9-entry-links)), else the CI of the **default organization**: the
  operator of the server, configured with the variables of
  [configuration.md, section 5](configuration.md#5-site-and-interface) (`SITE_TITLE`,
  `SITE_CSS_URL`, `ORGANIZATION_NAME`, `IMPRINT_URL`, ...). The operator's legal pages
  (`/imprint`, `/privacy`, `/accessibility`) always show the operator's CI.
- The voting booth shows the colors, font and name or logo of the election's organization
  ([section 7](#7-voting-booth)). The ballot verifiers stay neutral: they look the same
  for all elections.
- Emails to the voters, trustees and administrators of an election show the organization
  as sender name, with its Reply-To address and signature ([section 8](#8-emails)). The
  sender address and the opt-out list are shared.

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
7. [Voting booth](#7-voting-booth)
8. [Emails](#8-emails)
9. [Entry links](#9-entry-links)
10. [Setup: a neutral operator and one entry link per organization](#10-setup-a-neutral-operator-and-one-entry-link-per-organization)

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
      "help_email": "elections@uni-a.example.org",
      "colors": {
        "primary": "#00447c",
        "accent": "#e6edf5",
        "dark": {"primary": "#9cc3ff", "primary_contrast": "#10161d"}
      },
      "font_url": "/static/site/unia/sans.woff2",
      "mail": {
        "from_name": {"en": "Electoral office, University A", "de": "Wahlamt der Universität A"},
        "reply_to": "elections@uni-a.example.org",
        "signature": {"en": "Electoral office\nUniversity A", "de": "Wahlamt\nUniversität A"}
      }
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
elections use it. The slug is also the organization's entry link `/<slug>/`
([section 9](#9-entry-links)), so it can't be a path of the server: `helios`, `auth`,
`booth`, `verifier`, `static`, `scim`, `admin`, `about`, `faq`, `docs`, `imprint`,
`privacy`, `accessibility`, `optout`, `i18n` and a few more are refused (the list is
`RESERVED_ORGANIZATION_SLUGS` in `settings.py`; at start, Helios also compares the slugs
with the first path segments of its URL configuration).

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
| `help_email` | | Address of the footer link "Contact" and of error hints on the election's pages, and the help address in the voting booth (unless the election has its own). `""` hides the link. |
| `colors` | | Colors of the voting booth: `primary`, `primary_contrast`, `accent`, `accent_contrast`, `link`, `background`, and `dark` with the same keys. See [section 7](#7-voting-booth). |
| `font_url` | | Font of the booth's text: a font file on this server (`.woff2`, `.woff`, `.ttf`, `.otf`). |
| `mail` | | Emails of its elections: `from_name` (sender name; text or per language), `reply_to` (address), `signature` (text or per language, may have several lines). See [section 8](#8-emails). |

Other keys are refused, so that typos don't go unnoticed.

**Texts** (`name`, `site_title`, the `text` of footer links) are either a plain text, used
for all languages, or an object with a text per interface language, e.g.
`{"en": "University A", "de": "Universität A"}`. A language without an entry gets the
plain text / the `LANGUAGE_CODE` entry / English.

**What an organization doesn't set** is taken from the default organization for the
layout: `site_title`, `css_url`, `dark_mode`, `main_logo_url`, the legal links,
`footer_links`, `help_email`, the booth's `colors` (as a whole, from `BOOTH_COLORS`) and
`font_url` (`BOOTH_FONT_URL`), and in `mail` the sender name (`DEFAULT_FROM_NAME`) and the
signature (`EMAIL_SIGNATURE`; there is no default Reply-To). Set a value to `""` (or `[]`) to hide the operator's item
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
│   ├── sans.woff2
│   ├── logo.svg
│   ├── logo-small.svg
│   └── logo-white.svg
└── unib/
    ├── site.css
    └── logo-white.svg
```

An organization's legal pages can be external pages, or pages served by Helios from
templates in `SITE_TEMPLATES_DIR` (see [section 9](#organizations-legal-pages)): set
`imprint_url` etc. to `/<slug>/imprint`. The operator's own pages are
`server_ui/templates/imprint.html` etc. in the same directory.

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

The organization's CI is shown on the pages of an election meant for voters and trustees:

- the public pages: the election's page (with its results), questions, voter list,
  trustees, the bulletin board and audited ballots, the cast-vote and "not started" /
  "tallied" pages;
- for voters: the password login and its "resend" page, confirming and having cast the
  vote;
- for trustees: their home page, key generation, key check, decryption.

The election's administrators see these pages in the same CI (e.g. the election's page
with its administration steps). The election's administration pages (edit, uploading
voters and emailing them, adding trustees, administrators, freeze, extend, tally and
releasing the result) are part of the administration around the elections: they show the
CI of the visitor's entry organization, else the operator's
([section 9](#9-entry-links)). The list of pages is in
`ELECTION_CI_TEMPLATES` (`helios/view_utils.py`): a page shows the election's
organization's CI only if it is listed there, and then whatever entry link the visitor
used.

**Site administrators** see the organization of each election in the list of elections
(`/helios/stats/elections`) and can filter it by organization; "default organization"
there includes elections whose slug is no longer in the file.

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

The start page `/` shows the operator's CI. With an organizations file, its list of
**featured elections** is filtered: people who are logged in see the featured elections of
their own organization only (determined as in section 3; people of the default
organization also see those of organizations no longer in the file), and visitors who are
not logged in see none. The lists of one's own elections and votes are not filtered.
Without an organizations file, everyone sees all featured elections as before.

An organization's entry link `/<slug>/` is its start page, in its CI: it shows the
featured elections of that organization, to everyone (also to visitors who are not
logged in, and to people of another organization). See [section 9](#9-entry-links).

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

## 7. Voting booth

The booth (`/booth/vote.html`) is a static page, the same for every election, and with the
systemd setup served by nginx. It loads the stylesheet of the election it is opened for,
`/helios/elections/<uuid>/booth-theme.css`, which the server generates from the election's
organization:

- `colors` become CSS custom properties (`--booth-primary` etc., defined in
  `heliosbooth/css/booth-ui.css`):

  | Key | Used for |
  |---|---|
  | `primary` | filled and outlined buttons, the current step, chosen answers (their hover and background shades are derived from it) |
  | `primary_contrast` | text on filled buttons |
  | `accent` | background of the header and footer bars |
  | `accent_contrast` | their text: the booth's title, the election fingerprint, the exit link |
  | `link` | links |
  | `background` | page background around the booth |

  Colors are given as `#rrggbb`, `#rgb` or `rgb(r, g, b)`. Each color must have a contrast of
  at least 4.5:1 with the color it is shown with (the configured one, else the built-in
  one): `primary_contrast` with `primary`, `primary` and `link` with the booth's
  background (white, dark: `#1b222b`), `accent_contrast` with `accent`. Otherwise Helios
  doesn't start, and the message names both colors and the contrast.
- `dark` holds the colors in dark mode, with the same keys. They are used only if the
  organization's dark mode is `auto` (see `dark_mode`); then the booth uses its built-in
  dark colors with the organization's dark colors on top. In dark mode, `primary` is also
  the color of outlined buttons on the dark background, so it is usually a light color, with
  a dark `primary_contrast`. Without `dark`, the booth shows its built-in dark colors.
- `font_url` becomes an `@font-face` for the booth's text. Election fingerprints and ballot
  trackers keep their monospace font.
- The organization's name, or its own `main_logo_url` (never the operator's), is shown in
  the booth's header. It comes from the election's `/meta`, which isn't part of the
  election's fingerprint.

Without `colors` and `font_url` (and without `BOOTH_COLORS` / `BOOTH_FONT_URL`), the stylesheet
contains only the built-in dark colors, as `/booth-dark-mode.css` did before, and the booth
looks exactly as before.

**Integrity.** The booth must work the same for every election, and nothing an
organization configures may hide or change the election fingerprint, the audit option or
the steps of casting a ballot. Therefore:

- there is no stylesheet or script of an organization in the booth: the server writes the
  stylesheet itself, and only CSS custom properties with a validated color (`#rrggbb` after
  normalization) and one `@font-face` with a validated path go into it;
- colors with too little contrast are refused, so the fingerprint in the header and footer
  bars stays readable;
- the font gets a fixed family name and is not used for fingerprints and trackers;
- the name and logo are set as text and attributes, never as HTML;
- the booth links the stylesheet only for an address of the form `/helios/elections/<uuid>`
  on the same server; the Content Security Policy allows stylesheets, images and fonts from
  this server only (`style-src`, `img-src`, `font-src 'self'`).

The stylesheet is checked like the election's other data (private elections only for their
voters) and revalidated on every load (`Cache-Control: private, no-cache` with an ETag), so
a change of the election's organization or of the configuration (after a restart) applies
the next time the booth is opened. If it can't be loaded, the booth works with its built-in
colors.

The ballot verifiers (`/verifier/verify.html`, `/booth/single-ballot-verify.html`) stay
neutral: they are independent tools and look the same for all elections.

When nginx serves `/booth/` itself, it must send the Content Security Policy too: see
[deploy/systemd/README.md](../deploy/systemd/README.md#security-headers-of-the-booth-and-verifier).

## 8. Emails

Emails to the voters and trustees of an election come from its organization:

- the emails to voters (to all, a group or one voter, including password credentials and the
  "resend credentials" email), the confirmation after a ballot was cast, the notice that a
  vote was removed, and the trustee's link to their page;
- the notes to the election's administrator: voter file processed, voters not added
  because they opted out, encrypted tally computed, Helios trustee decrypted its part,
  a trustee uploaded their public key or their partial decryption.

They get:

- **sender name** `mail.from_name` in the language of the email, with the global sender
  address: `"Electoral office, University A" <DEFAULT_FROM_EMAIL>`. The address stays the
  same for all organizations, so SPF, DKIM and DMARC keep working; an organization can't
  set its own sender address.
- **Reply-To** `mail.reply_to`, so that replies reach the organization rather than the
  operator.
- **signature** `mail.signature` (after `--`), instead of `EMAIL_SIGNATURE` (default
  `Helios`). The administrator notes used to end with a fixed `Helios`; they now end with
  the signature of the election's organization (for the default organization
  `EMAIL_SIGNATURE`).

The emails of the default organization's elections, and of organizations without `mail`,
look as before (sender `DEFAULT_FROM_NAME`, no Reply-To, signature `EMAIL_SIGNATURE`).

Not about one election, and so always from the operator (also for visitors of an entry
link): opt-out and opt-in confirmations, password reminders (signed with the operator's site
title), and the login services' welcome emails. The opt-out list is shared: who opts out
receives no emails of any organization.

The values are checked at start: `from_name` must be one line without control characters
(at most 100 characters), so it can't add header lines; `reply_to` must be a plain email
address. Helios sets the organization when the email is sent, also in the background worker,
and applies it in its email backend to emails with the default sender address only.

## 9. Entry links

Every organization in the file has an **entry link**: `https://<host>/<slug>/`, e.g.
`https://helios.example.org/unia/`. It is the organization's start page, in its CI, with
its featured elections ([section 4](#4-start-page)). Publish it on the organization's
website, in its intranet or in the emails announcing elections.

Opening it selects the organization as the visitor's **entry organization**. From then on,
all pages that are not an election's pages for voters and trustees show its CI:

| Pages | CI |
|---|---|
| start page `/<slug>/`, login (also with password and the choice of the login service), "My elections", "My votes", creating an election (wizard and form), voter groups, the administration pages of an election, site administration, About / FAQ / Docs, "page not found" | the entry organization |
| an election's pages for voters and trustees, and its public pages ([section 2](#2-which-organization-an-election-belongs-to)) | always the **election's** organization, whatever entry link was used |
| voting booth | the election's organization ([section 7](#7-voting-booth)) |
| ballot verifiers | neutral, the same for everyone |
| `/imprint`, `/privacy`, `/accessibility` | always the operator |
| `/<slug>/imprint`, `/<slug>/privacy`, `/<slug>/accessibility` | that organization (see below) |
| start page `/`, and every page without an entry organization | the operator (default organization) |
| server error page | the operator |

An election's pages never follow the entry link, so nobody can make an election look like
another organization's by sending a link to the election together with an entry link.

**Navigation.** While an entry organization is active, the "Home" links, the site title
and the logo lead to `/<slug>/`. On an election's pages for voters and trustees they lead to
the start page of the organization whose CI is shown, i.e. the election's: its entry link,
or `/` for an election of the default organization. Opening `/` returns to the operator's
CI: it ends the entry organization. Opening another organization's entry link switches to it. Logging in
and out keeps the entry organization: the login returns to the page where it was started,
and logging out doesn't touch it.

**The cookie.** The entry organization is kept in a cookie of its own, not in the session,
so that visitors who are not logged in need no session for it and it survives logging out:

| | |
|---|---|
| Name | `helios_organization` |
| Value | the slug, e.g. `unia` (no personal data) |
| Lifetime | 90 days, renewed on each visit of the entry link; removed by opening `/` |
| Attributes | `Path=/`, `HttpOnly`, `SameSite=Lax`, `Secure` over HTTPS (with `SSL=1` always) |

Helios checks the value against the slugs in the file on every request and ignores
anything else (an organization removed from the file, a changed value). Pages showing a
CI send `Vary: Cookie`. It is a technically necessary cookie (it only stores a display
choice the visitor made); the generic privacy page (`/privacy`) mentions it when an
organizations file is configured. If you replace that page with your own, mention it there.
Without an organizations file, Helios sets no such cookie and `/<anything>/` stays
"page not found".

### Organizations' legal pages

An organization can have its imprint, privacy policy and accessibility statement on this
server, in its own CI. Put the templates into `SITE_TEMPLATES_DIR`:

```
site/templates/                          -> SITE_TEMPLATES_DIR
├── server_ui/templates/
│   ├── imprint.html                     the operator's: /imprint
│   ├── privacy.html                     the operator's: /privacy (a generic one is built in)
│   └── accessibility.html               the operator's: /accessibility
└── organizations/
    ├── unia/
    │   ├── imprint.html                 /unia/imprint
    │   ├── privacy.html                 /unia/privacy
    │   └── accessibility.html           /unia/accessibility
    └── unib/
        └── imprint.html                 /unib/imprint (/unib/privacy: page not found)
```

Each page is served only if its template exists (otherwise "page not found"), always in
that organization's CI, also without its entry link; it doesn't select the entry
organization. A template extends the layout:

```html
{% extends "server_ui/templates/base.html" %}
{% load i18n %}
{% block title %}{% translate "Imprint" %} &ndash; {{ ci.site_title }}{% endblock %}
{% block content %}
<h1>{% translate "Imprint" %}</h1>
<p>University A, Example Street 1, 12345 Example City ...</p>
{% endblock %}
```

Then link the pages in the organization's entry: `"imprint_url": "/unia/imprint"`,
`"privacy_url": "/unia/privacy"`, `"accessibility_url": "/unia/accessibility"`. Pages in
several languages can use `{% get_current_language as LANGUAGE_CODE %}` and
`{% if LANGUAGE_CODE == "de" %}...{% endif %}`.

## 10. Setup: a neutral operator and one entry link per organization

The usual setup for a server shared by several organizations: `https://helios.example.org/`
shows a neutral CI of the operator, and each organization has its own entry link, e.g.
`https://helios.example.org/unia/`, showing its CI.

1. **The global settings are the neutral operator** (the default organization,
   [configuration.md, section 5](configuration.md#5-site-and-interface)): `SITE_TITLE`
   (e.g. "Elections at example.org"), `ORGANIZATION_NAME`, `ORGANIZATION_URL`, a neutral
   `SITE_CSS_URL` and logos, `HELP_EMAIL_ADDRESS` of the operator's support,
   `DEFAULT_FROM_NAME` and `EMAIL_SIGNATURE`. The operator's legal pages: templates
   `server_ui/templates/imprint.html`, `privacy.html` and `accessibility.html` in
   `SITE_TEMPLATES_DIR`, with `IMPRINT_URL=/imprint`, `PRIVACY_URL=/privacy`,
   `ACCESSIBILITY_URL=/accessibility` (or links to the operator's website).
2. **One entry per organization** in the organizations file
   ([section 1](#1-configuration-file)). Choose the slug carefully: it is the entry link
   and stays in the elections. Give it at least `name`, `url`, `site_title`.
3. **Its files**: stylesheet, logos and font under `SITE_STATIC_DIR/<slug>/`
   (`css_url`, `main_logo_url`, `header_logo_url`, `footer_logo_url`, `font_url`), and the
   booth's `colors`.
4. **Its legal pages**: either links to its website, or templates under
   `SITE_TEMPLATES_DIR/organizations/<slug>/` with `imprint_url: "/<slug>/imprint"` etc.
   ([section 9](#organizations-legal-pages)). Set all three links (or `""` to hide one):
   unset links fall back to the operator's.
5. **Who belongs to it**: `match.idps` with the issuer (OIDC) or entity ID (SAML) of its
   identity provider, if it has its own, and/or `match.email_domains`. This assigns the
   people who log in through it to the organization: their new elections belong to it,
   and they see its featured elections on `/`.
6. **The address prefix** of its elections: `short_name_prefix` (e.g. `unia` for
   `/helios/e/unia-board-2026`). With the same identity provider in `match.idps`, everyone
   logging in through it gets the prefix ([section 3](#3-one-mapping-for-organizations-and-address-prefixes)).
7. **Its emails**: `mail.from_name`, `mail.reply_to` and `mail.signature`
   ([section 8](#8-emails)), and its `help_email`.
8. Restart the web and worker processes, open `https://helios.example.org/unia/` and
   check the start page, the login and the footer links; then assign existing elections
   ([section 5](#5-assigning-existing-elections)).
9. Publish the entry link: on the organization's website and in its announcements. The
   emails to voters link the elections themselves, which show the election's
   organization anyway.
