# Configuration

Helios reads its configuration from environment variables: with Docker Compose from `.env`
(see [deploy/.env.example](../deploy/.env.example), which lists every variable), otherwise
from the environment of the web and worker processes (e.g. a systemd `EnvironmentFile`, Fly.io
secrets or Heroku config vars). This is the complete list.

**Which file?** You need exactly one, depending on how Helios runs. Both templates contain the
same Helios settings; they differ only where the setup differs.

| | Docker Compose | systemd + nginx |
|---|---|---|
| File | `deploy/.env` (template [deploy/.env.example](../deploy/.env.example)) | `/etc/helios/helios.env` (template [deploy/systemd/helios.env.example](../deploy/systemd/helios.env.example)) |
| Read by | Compose, for `${...}` in `docker-compose.yml` and as the containers' environment | `helios-web.service` and `helios-worker.service` (`EnvironmentFile`), and by `sh` for `manage.py` commands |
| Only here | `HELIOS_IMAGE`, `POSTGRES_PASSWORD` (Compose itself, not Helios) | `DATABASE_URL`, e.g. `postgres://helios@/helios` over the local socket |
| Database | `DATABASE_URL` is set by `docker-compose.yml` (overrides the file) | in the file |
| Local mail server | `EMAIL_HOST=host.docker.internal` (`localhost` is the container itself) | `EMAIL_HOST=localhost` |
| Paths (`SITE_TEMPLATES_DIR`, `SITE_STATIC_DIR`, `SAML_IDP_METADATA_FILE`) | inside the container, e.g. `/site/static` (mounted volume) | on the host, e.g. `/opt/helios/site/static` |
| Literal `$` in a value | write `$$` (Compose expands `${...}`) | use single quotes (`sh` expands `$` in double quotes) |
| Multi-line values (PEM) | `"\n"` inside double quotes | base64 content on one line, without BEGIN/END lines |
| Apply changes | `docker compose up -d` | `systemctl restart helios-web helios-worker` |

On Fly.io and Heroku there is no file: set the same variables as secrets / config vars.
Single sign-on is described in more detail in [sso.md](sso.md).

**Rules that apply to all variables**

- **Switches are on only with the value `1`.** `true`, `yes` or `on` count as **off**.
- Variables that are not set fall back to the default in the table. Some defaults are upstream
  examples that must never be used in production, e.g. the sender address `ben@adida.net`.
  Those are marked ⚠.
- While the test suite runs (`manage.py test`), the environment is ignored and the defaults
  apply.
- Changes take effect after restarting the web and worker processes (with Docker Compose:
  `docker compose up -d`).

## Contents

1. [Core and security](#1-core-and-security)
2. [URLs](#2-urls)
3. [Database](#3-database)
4. [Email](#4-email)
5. [Site and interface](#5-site-and-interface)
6. [Login](#6-login)
7. [Monitoring](#7-monitoring)
8. [Container](#8-container)
9. [Read but without effect](#9-read-but-without-effect)
10. [Customizing the site](#10-customizing-the-site)

## 1. Core and security

| Variable | Default | Meaning |
|---|---|---|
| `DEBUG` | `1` ⚠ (image: `0`) | **Must be `0` in production.** With `1` (also when the variable is missing outside the container image), Django shows detailed error pages, the auth system `devlogin` is enabled (log in as *any* user without a password), and the checks for `SECRET_KEY` and `EMAIL_OPTOUT_SECRET` are skipped. |
| `SECRET_KEY` | `replaceme` ⚠ | Signs sessions and other data. At least 32 random characters. With `DEBUG=0`, Helios refuses to start with the default or a shorter value. Changing it logs everybody out. |
| `EMAIL_OPTOUT_SECRET` | placeholder ⚠ | Key for the confirmation codes in opt-out/opt-in links. Required with `DEBUG=0`. Changing it invalidates links in emails already sent. |
| `ALLOWED_HOSTS` | `localhost` | Host names under which Helios answers, comma-separated, e.g. `vote.example.org`. Other host names get *400 Bad Request*. |
| `SSL` | `0` | `1`: redirect HTTP to HTTPS, send the session cookie only over HTTPS, and trust the `X-Forwarded-Proto: https` header of the reverse proxy. The proxy must set this header (and overwrite one sent by the client). Without it, `SSL=1` causes an endless redirect. |
| `HSTS` | `0` | `1`: send `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`. This tells browsers to use only HTTPS for a year, **for all subdomains too**. |
| `SESSION_COOKIE_SAMESITE` | `Lax` | Don't change it: `Lax` is part of Helios' CSRF protection, and the SAML and OIDC logins rely on it. |
| `CSP_REPORT_ONLY` | `0` | `1`: report Content Security Policy violations without enforcing the policy (for testing). |
| `CSP_REPORT_URI` | – | URL that receives CSP violation reports. |
| `TIME_ZONE` | `America/Los_Angeles` | Only for automatic timestamps such as the creation date of an election. Election start/end times are always UTC. |
| `LANGUAGE_CODE` | `en` | Interface language (`en` or `de`) for visitors whose browser prefers neither. Everyone can switch with the language button. Emails use the language of the person whose action sends them. |
| `ADMIN_EMAIL` | – | Receives an email for every server error (500). Use a team address. Without it, errors appear only in the log. |
| `ADMIN_NAME` | empty | Name to go with `ADMIN_EMAIL`. |

## 2. URLs

| Variable | Default | Meaning |
|---|---|---|
| `URL_HOST` | `http://localhost:8000` | Public base URL, without a trailing slash. Used for links in emails and pages. |
| `SECURE_URL_HOST` | value of `URL_HOST` | Base URL for voting and login. **Never change it once elections exist:** it is part of each election's cast URL and ballot data. It is also the base of the SSO addresses registered at the IdP: OIDC `…/auth/after/` and `…/auth/logout/done`, SAML defaults `…/auth/saml/acs` and `…/auth/saml/metadata`. |

Usually both are the same, e.g. `https://vote.example.org`.

## 3. Database

Helios needs PostgreSQL.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | – | Connection URL, e.g. `postgres://helios:PASSWORD@db/helios`, or `postgres://helios@/helios` for the local unix socket. Without it: database `helios` via the local socket as the current OS user. |
| `DATABASE_SSL_REQUIRE` | `1` | `1`: require TLS to the database. Set it to `0` for the local socket or a database container on the same host without TLS, otherwise the connection fails. Only used together with `DATABASE_URL`. |

## 4. Email

Helios sends invitations and reminders (with voter ID and password), vote confirmations,
trustee mails, results, opt-out confirmations and error mails. Almost all of them are sent by
the task worker (`manage.py db_worker`).

**SMTP** (the default):

| Variable | Default | Meaning |
|---|---|---|
| `EMAIL_HOST` | `localhost` | SMTP server. In a container, `localhost` is the container itself: use the relay's host name. |
| `EMAIL_PORT` | `2525` ⚠ | Usually `587` (STARTTLS), `465` (implicit TLS) or `25` (local MTA). |
| `EMAIL_HOST_USER` | empty | User name for SMTP authentication. Empty: no authentication. |
| `EMAIL_HOST_PASSWORD` | empty | Password for SMTP authentication. |
| `EMAIL_USE_TLS` | `0` | `1`: STARTTLS (usually port 587). |
| `EMAIL_USE_SSL` | `0` | `1`: TLS from the start of the connection (usually port 465). Helios refuses to start if both `EMAIL_USE_TLS` and `EMAIL_USE_SSL` are `1`. |
| `EMAIL_TIMEOUT` | `60` | Seconds to wait for the mail server. Without a timeout, a hanging server would block the task worker and every task queued behind it. |

**Always use `EMAIL_USE_TLS=1` or `EMAIL_USE_SSL=1` with a user name**: otherwise the password
is sent unencrypted if the server allows it.

Example for an authenticating relay:

```sh
EMAIL_HOST=smtp-relay.example.org
EMAIL_PORT=587
EMAIL_USE_TLS=1
EMAIL_HOST_USER=helios-vote
EMAIL_HOST_PASSWORD=...
```

**Sender and contact addresses:**

| Variable | Default | Meaning |
|---|---|---|
| `DEFAULT_FROM_EMAIL` | `ben@adida.net` ⚠ | Sender address of all mails, as `From:` and as SMTP envelope sender. An authenticating relay often allows a user to send only from certain addresses: make sure this one is allowed. |
| `DEFAULT_FROM_NAME` | `Ben for Helios` ⚠ | Sender name. |
| `HELP_EMAIL_ADDRESS` | `help@heliosvoting.org` ⚠ | Contact address shown to users (also the "Contact" link in the footer). |
| `HELIOS_VOTER_EMAIL_CUTOFF_WEEKS` | `3` | Weeks after the tally from which administrators can no longer email the voters of an election. |

**Other ways to send** (instead of SMTP):

| Variable | Default | Meaning |
|---|---|---|
| `EMAIL_USE_CONSOLE` | `0` | `1`: print mails to the log instead of sending them. Only works with `DEBUG=1`. |
| `MAILGUN_API_KEY` | – | Send via the Mailgun API instead of SMTP. |
| `EMAIL_USE_AWS` | `0` | `1`: send via Amazon SES, even if `MAILGUN_API_KEY` is set. Credentials come from the usual AWS variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, read by boto3). The region is always `us-east-1`. |

## 5. Site and interface

Texts marked "per language" have variants for each interface language: `SITE_TITLE_DE` is used
instead of `SITE_TITLE` when the page is in German (likewise `_EN`). Without a variant, the
plain variable is used.

| Variable | Default | Meaning |
|---|---|---|
| `SITE_TITLE` | `Helios Voting` | Site name in the header, page titles and emails. Per language. |
| `WELCOME_MESSAGE` | `This is the default message` | Text on the start page. May contain HTML (it is output as is, so only trusted content). Per language. |
| `MAIN_LOGO_URL` | `/static/logo.png` | Helios logo above the page content (the default is the built-in Helios logo). |
| `SHOW_LOGIN_OPTIONS` | `1` | `0`: hide the login buttons. Administrators then have to open `/auth/` themselves. |
| `SHOW_USER_INFO` | `1` | `0`: don't show who created an election, e.g. in the list on the start page. |
| `ALLOW_ELECTION_INFO_URL` | `0` | `1`: administrators can enter a link to further information for an election. |

**Organization and layout.** All optional: without them the site shows only the Helios logo
and the built-in pages. See [section 10](#10-customizing-the-site) for a worked example.

| Variable | Default | Meaning |
|---|---|---|
| `ORGANIZATION_NAME` | empty | Name of the operating organization, e.g. `Example University`. Shown as a link to `ORGANIZATION_URL` in the header bar, and used as the alt text of the logos. Per language. |
| `ORGANIZATION_URL` | empty | Home page of the organization. The header link appears only if both `ORGANIZATION_NAME` and `ORGANIZATION_URL` are set; the footer logo links there too. |
| `ORGANIZATION_LOGO_URL` | empty | Small logo (16 px high) shown instead of the name in the header bar. |
| `FOOTER_LOGO_URL` | empty | Larger logo in the footer (up to 240 px wide), on the dark footer background: use a light/white version. |
| `SITE_CSS_URL` | empty | Extra stylesheet, loaded after the default layout (`server_ui/media/layout.css`, served at `/static/layout.css`). Usually sets the CSS variables listed below. |
| `IMPRINT_URL` | empty | Footer link "Imprint". `/imprint` renders `server_ui/templates/imprint.html`, which only exists if the site provides it (otherwise 404); or link an external page. |
| `PRIVACY_URL` | `/privacy` | Footer link "Privacy". `/privacy` is a built-in, generic description of how Helios handles data; it is not a complete privacy policy. Replace it with your own template or link an external page. Set it empty to hide the link. |
| `ACCESSIBILITY_URL` | empty | Footer link "Accessibility". `/accessibility` renders `server_ui/templates/accessibility.html`, which only exists if the site provides it (otherwise 404); or link an external page. |
| `FOOTER_LINKS` | `[]` | Further footer links, as JSON: `[{"url": "https://help.example.org", "text": "Help desk"}]`. Invalid JSON stops Helios from starting. |
| `SITE_TEMPLATES_DIR` | empty | Directory searched before the built-in templates. It mirrors the repository layout: `<dir>/server_ui/templates/imprint.html` adds or replaces that template. Any template can be overridden this way. |
| `SITE_STATIC_DIR` | empty | Directory served at `/static/site/` (logos, stylesheets, images). `login-icons/<name>.png` in it is the icon of the auth system `<name>`, e.g. for an SSO system registered as `campus`. |

Footer links appear only when their URL is set, in this order: imprint, privacy,
accessibility, `FOOTER_LINKS`, and "Contact" (`mailto:` `HELP_EMAIL_ADDRESS`).

**CSS variables** of the default layout, to be set in `SITE_CSS_URL`:

| Variable | Default | Used for |
|---|---|---|
| `--primary-color` | `#2c4f7c` | buttons, headings on the login page, header links |
| `--primary-color-dark` | `#1d3557` | hover/focus of buttons |
| `--footer-bg-color` | `--primary-color` | footer background (the footer text is white) |
| `--meta-bg-color` | `#e9edf2` | header bar |
| `--main-bg-url` | `none` | background image behind the login box on the start page, e.g. `url("/static/site/background.jpg")` |
| `--fontfamily` | `'Roboto','Open Sans','Lato',sans-serif` | text font |

## 6. Login

| Variable | Default | Meaning |
|---|---|---|
| `AUTH_ENABLED_SYSTEMS` | `ldap,password,google,facebook` ⚠ | Enabled login systems, comma-separated, e.g. `campus` (the name under which SAML or OIDC is configured). With `DEBUG=1`, `devlogin` is added automatically. Old name: `AUTH_ENABLED_AUTH_SYSTEMS`. |
| `AUTH_DEFAULT_SYSTEM` | – | Login system offered by default; the login box then shows only this one. Old name: `AUTH_DEFAULT_AUTH_SYSTEM`. |

Voters of elections with per-election passwords don't need any of these systems: that login is
always available.

**OIDC** (enabled when `OIDC_DISCOVERY_URL` is set). Details, the request to the IdP and the
switch-over checklist are in [sso.md](sso.md).

| Variable | Default | Meaning |
|---|---|---|
| `OIDC_AUTH_SYSTEM_NAME` | `oidc` | Name of the login system, e.g. `campus`. |
| `OIDC_DISCOVERY_URL` | – | `https://<idp>/.well-known/openid-configuration` |
| `OIDC_CLIENT_ID` | – | Client ID registered at the IdP. |
| `OIDC_CLIENT_SECRET` | – | Client secret. |
| `OIDC_SCOPES` | `openid email profile` | Requested scopes. |
| `OIDC_USER_ID_CLAIM` | `sub` | Claim used as the user id. `email` keeps the identities of an earlier SAML setup that used the email address (see [sso.md](sso.md)); user ids are then compared case-insensitively. |
| `OIDC_TRUST_UNVERIFIED_EMAIL` | `0` | `1`: accept email addresses without `email_verified=true`. Only for an institutional IdP whose addresses are authoritative. |
| `OIDC_LOGOUT_AT_PROVIDER` | `0` | `1`: on logout, also end the SSO session at the IdP. Requires `end_session_endpoint` at the IdP and `<SECURE_URL_HOST>/auth/logout/done` registered as post-logout redirect URI. |
| `OIDC_LOGIN_MESSAGE` | translated default | Text on the login button. Per language. |

**SAML** (enabled when `SAML_IDP_METADATA_URL` or `SAML_IDP_METADATA_FILE` is set; requires the
`saml` extra, which the published container image contains). See [sso.md](sso.md).

| Variable | Default | Meaning |
|---|---|---|
| `SAML_AUTH_SYSTEM_NAME` | `saml` | Name of the login system, e.g. `campus`. Must differ from `OIDC_AUTH_SYSTEM_NAME` if both are configured, otherwise Helios refuses to start. |
| `SAML_IDP_METADATA_URL` | – | URL of the IdP metadata (fetched over HTTPS, cached for an hour). |
| `SAML_IDP_METADATA_FILE` | – | Path to a local copy of the IdP metadata, instead of the URL. |
| `SAML_IDP_ENTITY_ID` | – | Required if the metadata lists several IdPs (federation metadata). |
| `SAML_IDP_X509_CERT` | – | Pin the IdP signing certificate instead of trusting the one in the metadata. |
| `SAML_SP_ENTITY_ID` | `<SECURE_URL_HOST>/auth/saml/metadata` | Entity ID of Helios. |
| `SAML_SP_ACS_URL` | `<SECURE_URL_HOST>/auth/saml/acs` | Assertion consumer service URL. Helios serves it at this path, so an ACS URL already registered at the IdP (e.g. from an earlier SAML integration) can be kept. |
| `SAML_SP_X509_CERT` | – | Helios' own certificate (PEM), if the IdP requires signed requests or encrypts assertions. |
| `SAML_SP_PRIVATE_KEY` | – | The matching private key (PEM). Setting it makes Helios sign its requests. |
| `SAML_ATTRIBUTE_USER_ID` | `nameid` | Attribute used as the user id; `nameid` means the SAML NameID. |
| `SAML_ATTRIBUTE_EMAIL` | – | Attribute with the email address. If it is the same as `SAML_ATTRIBUTE_USER_ID`, user ids are compared case-insensitively. |
| `SAML_ATTRIBUTE_NAME` | – | Attribute with the full name. |
| `SAML_ATTRIBUTE_GIVEN_NAME`, `SAML_ATTRIBUTE_SURNAME` | – | Attributes for first and last name, if there is no full name. |
| `SAML_LOGIN_MESSAGE` | translated default | Text on the login button. Per language. |

**Other login systems:** `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (see
[INSTALL.md](../INSTALL.md)); `GH_CLIENT_ID`, `GH_CLIENT_SECRET` (GitHub); `GITLAB_CLIENT_ID`,
`GITLAB_CLIENT_SECRET`; `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`; `FACEBOOK_APP_ID`,
`FACEBOOK_API_KEY`, `FACEBOOK_API_SECRET`; `CAS_USERNAME`, `CAS_PASSWORD`,
`CAS_ELIGIBILITY_URL`, `CAS_ELIGIBILITY_REALM`. Each only has an effect if the system is listed
in `AUTH_ENABLED_SYSTEMS`.

**LDAP cannot be configured through the environment:** the server and search settings are fixed
in `settings.py` (an upstream example server). Don't enable `ldap` without changing the code.

## 7. Monitoring

| Variable | Default | Meaning |
|---|---|---|
| `ROLLBAR_ACCESS_TOKEN` | – | Report errors to Rollbar. |

## 8. Container

Only for the image built from the `Dockerfile`:

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Port gunicorn listens on (4 workers). |
| `DEBUG` | `0` | The image sets `DEBUG=0`, unlike `settings.py` (see section 1). The container therefore refuses to start without `SECRET_KEY` and `EMAIL_OPTOUT_SECRET`. Set `DEBUG=1` explicitly for development only. |

The image starts the web server by default. Run the other processes with the same image and
environment:

- task worker: `python manage.py db_worker` (one container is enough);
- schema migration: `python manage.py migrate --noinput`, before starting web and worker after
  each update.

[deploy/docker-compose.yml](../deploy/docker-compose.yml) does all of this, with a PostgreSQL
container: it sets `DATABASE_URL` itself and reads everything else from `.env`. The template
[deploy/.env.example](../deploy/.env.example) contains every variable of this page, grouped by
topic. In a container, `EMAIL_HOST=localhost` is the container itself: use a relay with login
(section 4) or `EMAIL_HOST=host.docker.internal` for an MTA on the host.

The image is built with the SAML extra by the GitHub workflow
`.github/workflows/container.yml`, scanned with Trivy and published as
`ghcr.io/linuxrrze/helios-server` (forks: `ghcr.io/<owner>/<repo>`) with these tags:
`:<full commit SHA>` and `:master`/`:latest` for each push to master, `:1.2.3` and `:1.2` for a
tag `v1.2.3`. Pin the commit SHA or a version in production. Or build it yourself:

```sh
docker build -t helios-server --build-arg UV_EXTRAS="--extra saml" .
```

## 9. Read but without effect

`MASTER_HELIOS` is read by `settings.py`, but nothing uses it. Setting it changes nothing.

## 10. Customizing the site

The layout, logos, colors and legal pages are adapted without changing the code: a site
directory holds templates and static files, and a few variables point to them.

**Example site directory**

```
site/
├── templates/                     -> SITE_TEMPLATES_DIR
│   └── server_ui/templates/
│       ├── imprint.html
│       ├── accessibility.html
│       └── privacy.html           (replaces the built-in generic page)
└── static/                        -> SITE_STATIC_DIR, served at /static/site/
    ├── logo.svg
    ├── logo-white.svg
    ├── site.css
    ├── background.jpg
    └── login-icons/campus.png     (icon of the auth system "campus")
```

Each page extends the base layout and fills the `title` and `content` blocks, e.g.
`site/templates/server_ui/templates/imprint.html`:

```django
{% extends 'server_ui/templates/base.html' %}
{% load i18n %}
{% block title %}{% translate "Imprint" %}{% endblock %}
{% block content %}
<h1>{% translate "Imprint" %}</h1>
<p>Example University, 1 University Road, Example City</p>
<p>Contact: <a href="mailto:helios@example.org">helios@example.org</a></p>
{% endblock %}
```

Use `{% get_current_language as LANGUAGE_CODE %}` and `{% if LANGUAGE_CODE == 'de' %}` for
texts in several languages. A template in the site directory replaces the built-in one of the
same path, so any other page (e.g. `server_ui/templates/index.html`) can be replaced too; copy
the original as a starting point and check it after each update.

`site/static/site.css` sets the CSS variables of section 5:

```css
:root {
  --primary-color: #005a9c;
  --primary-color-dark: #003f6e;
  --footer-bg-color: #1f2a36;
  --meta-bg-color: #eef2f6;
  --main-bg-url: url("/static/site/background.jpg");
  --fontfamily: system-ui, sans-serif;
}
```

Fonts and images must come from the site itself: the Content Security Policy allows only
`'self'` (and `data:` images).

**Variables**

```sh
ORGANIZATION_NAME="Example University"
ORGANIZATION_URL=https://www.example.org
ORGANIZATION_LOGO_URL=/static/site/logo.svg
FOOTER_LOGO_URL=/static/site/logo-white.svg
SITE_CSS_URL=/static/site/site.css
IMPRINT_URL=/imprint
PRIVACY_URL=/privacy
ACCESSIBILITY_URL=/accessibility
FOOTER_LINKS=[{"url": "https://help.example.org", "text": "Help desk"}]
SITE_TEMPLATES_DIR=/site/templates
SITE_STATIC_DIR=/site/static
```

**With Docker Compose**, put `site/` next to `docker-compose.yml` and enable the commented-out
`./site:/site:ro` volume in the shared `x-helios` block of
[deploy/docker-compose.yml](../deploy/docker-compose.yml) (it applies to web and worker; the
worker renders emails too), and the `SITE_*` lines in `.env`. Changed
templates take effect after `docker compose restart web worker`.

**Behind nginx**, `/static/site/` can also be served directly from the directory instead of
by Helios:

```nginx
location /static/site/ {
    alias /srv/helios/site/static/;
}
```
