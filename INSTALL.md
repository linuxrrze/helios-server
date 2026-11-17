# Helios Server Installation

## Prerequisites

* Install PostgreSQL 12+

* Download helios-server

* `cd` into the helios-server directory

## Python Setup

* Install Python 3.13 including dev packages

```
sudo apt install python3 python3-dev
```

* Install uv (modern Python package manager)

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

* You'll also need Postgres dev libraries. For example on Ubuntu:

```
sudo apt install libpq-dev
```

* Install dependencies (uv creates a virtual environment automatically)

```
uv sync
```

## Database Setup

* Reset database

```
./reset.sh
```

## Running the Server

* Start server

```
uv run python manage.py runserver
```

* Start the background task worker, which verifies cast votes, sends emails,
  processes voter files and computes tallies.

```
uv run python manage.py db_worker
```

## Background Tasks

Background tasks are queued in the PostgreSQL database (with `django-tasks` and its
`django-tasks-db` backend); there is no separate message broker. Every task goes
through this queue, so without a running `db_worker`, cast votes stay unverified,
emails aren't sent and tallies aren't computed, in development too.

* The worker runs one task at a time. To run more at once, start more `db_worker`
  processes; each task is claimed by only one of them.

* Cast vote verification runs ahead of other waiting tasks such as bulk voter emails.

* Finished tasks stay in the database; prune them regularly, e.g. daily:

```
uv run python manage.py prune_db_task_results
```

* A worker killed mid-task leaves that task stuck as running. Put tasks running longer than 120 minutes back in the queue (a bulk voter email then sends only its emails still missing); this also queues again emails still to be sent whose task is gone:

```
uv run python manage.py requeue_stuck_tasks --older-than 120
```

* The stats page shows how many tasks are waiting, running, stuck and failed in
  the last day, and how many votes are waiting for verification; "force it"
  re-queues them.

* Each email is sent by its own task. When the mail server does not take it for
  now (greylisting, mailbox full, server unreachable), the email is deferred and
  tried again later, with growing waits (`HELIOS_EMAIL_RETRY_BASE`,
  `HELIOS_EMAIL_RETRY_CAP`), until `HELIOS_EMAIL_MAX_ATTEMPTS` or
  `HELIOS_EMAIL_MAX_AGE` ([docs/configuration.md](docs/configuration.md#4-email)). When
  the mail server cannot be reached or refuses the login or sender address,
  sending is paused for all emails and resumes by itself (the pause does not count
  towards `HELIOS_EMAIL_MAX_AGE`, so the waiting emails still go out); a refused login is also
  reported to `ADMIN_EMAIL` (as far as that mail gets through) and Rollbar, and
  logged as an error. After fixing the mail settings, send the waiting emails
  right away:

```
uv run python manage.py resume_email
```

## Bounces

Optional, only when Helios sends over SMTP. Each email then goes out with its own envelope
sender `bounce+<id>-<mac>@bounces.example.org`, so that a bounce tells which email (and voter) it
is about; `From:` stays `DEFAULT_FROM_EMAIL`. Settings: `HELIOS_BOUNCE_DOMAIN` and
`HELIOS_BOUNCE_SECRET` ([docs/configuration.md](docs/configuration.md#4-email)).

DNS and mail server (the domain names are examples; the sender domain is that of `DEFAULT_FROM_EMAIL`):

* **A subdomain of the sender domain** as bounce domain, e.g. `bounces.example.org` for
  `helios@example.org`. DMARC (relaxed alignment) accepts the SPF result of the envelope domain
  when it has the same organizational domain as `From:`; DKIM signatures of `example.org` stay
  aligned as before.
* **MX** for the bounce domain, pointing to the mail server that receives the bounces:
  `bounces.example.org. MX 10 mx.example.org.`
* **SPF** for the bounce domain (it is now the envelope domain the receivers check), listing
  the server(s) Helios sends through: `bounces.example.org. TXT "v=spf1 ip4:192.0.2.25 -all"`
  (or `include:` of your relay's record).
* **DMARC**: the policy of `example.org` covers the subdomain (`sp=`); nothing else to add.
* **Delivery into one mailbox**: all `bounce+…@bounces.example.org` addresses must arrive in one
  IMAP mailbox. With Postfix: `recipient_delimiter = +` (in `main.cf`), and the bounce domain
  as a virtual mailbox domain with the mailbox `bounce@bounces.example.org` (e.g. Dovecot via
  LMTP), so that `bounce+4711-…@` is delivered to `bounce@`. Do not let a spam filter reject or
  discard bounces (they come from `<>`), and do not let the mailbox forward them elsewhere.
* The relay Helios sends through must allow the envelope sender `bounce+…@bounces.example.org`
  for the login of `EMAIL_HOST_USER` (an authenticating relay often restricts senders).

Reading the bounces: set `HELIOS_BOUNCE_IMAP_HOST`, `HELIOS_BOUNCE_IMAP_USER` and
`HELIOS_BOUNCE_IMAP_PASSWORD` (IMAP over TLS) and run every few minutes

```
uv run python manage.py process_bounces
```

With systemd: `deploy/systemd/helios-bounces.timer` ([deploy/systemd/README.md](deploy/systemd/README.md#bounces-optional)).
With Docker Compose: the commented-out `bounces` service in `deploy/docker-compose.yml`, or a cron
job on the host (`docker compose exec -T worker python manage.py process_bounces`). Each run
prints how many mails went to which folder; it fails (non-zero exit status) when the mailbox
cannot be reached or bounce handling is not configured.

To check the setup, send an email to a voter with a non-existent address at a domain you
control (e.g. `nobody-12345@example.org`): after the next run, the election's "email delivery"
page lists the voter as bounced ("5.1.1 recipient unknown") and the address as suppressed.

## Deleting old elections

Once a ballot has been cast, election administrators can no longer delete an election, only
archive it; site administrators still can (the election's log records it). Deleted elections
are only hidden. To delete elections from the database for good after a retention period, set
`HELIOS_ELECTION_RETENTION_DAYS` ([docs/configuration.md](docs/configuration.md#3-database);
default `0`: never) and run daily

```
uv run python manage.py purge_elections --yes
```

Without `--yes` it lists what it would delete. With systemd: `deploy/systemd/helios-purge.timer`
([deploy/systemd/README.md](deploy/systemd/README.md#deleting-old-elections-optional)).
With Docker Compose: a cron job on the host (`docker compose exec -T worker python manage.py purge_elections --yes`).

## Upgrading from Celery and RabbitMQ

Older versions of Helios used Celery with RabbitMQ. To upgrade:

* Deploy at a quiet time, since tasks still waiting in RabbitMQ won't be run.

* Run `uv run python manage.py migrate` to create the task table, and replace the
  Celery worker with `db_worker` (see `Procfile`).

* On the stats page, use "force it" to re-queue any cast votes that were still unverified.

* Remove RabbitMQ and the `CELERY_BROKER_URL` setting.

## Deploying

* **Heroku** uses the `Procfile`.

* **Fly.io** uses `fly.toml` and the `Dockerfile`; the steps for a first deploy are at
  the top of `fly.toml`. To move an existing Heroku deployment, see
  [docs/migrating-heroku-to-fly.md](docs/migrating-heroku-to-fly.md).

* **Docker Compose** (own server, with PostgreSQL, task worker and schema migration):
  see [deploy/docker-compose.yml](deploy/docker-compose.yml) and copy
  [deploy/.env.example](deploy/.env.example) to `deploy/.env`.

* **systemd and nginx** (own server, without containers): see
  [deploy/systemd/README.md](deploy/systemd/README.md), with unit files, nginx configuration
  and [deploy/systemd/helios.env.example](deploy/systemd/helios.env.example) for
  `/etc/helios/helios.env`. Which configuration file is for which setup:
  [docs/configuration.md](docs/configuration.md).

* Either way, set `DEBUG=0` in production: it defaults to on, which enables the
  `devlogin` auth system.

## Configuration

Helios is configured with environment variables. All of them, including the site's
branding, footer links and imprint/privacy pages, are listed in
[docs/configuration.md](docs/configuration.md). Single sign-on with SAML or OpenID Connect
is described in [docs/sso.md](docs/sso.md), provisioning of users, roles and voter groups
by an identity management system (SCIM 2.0) in [docs/scim.md](docs/scim.md).

## Google Auth Configuration

To get Google Auth working:

* Go to https://console.developers.google.com

* Create an application

* Set up OAuth2 credentials as a web application, with your origin, e.g. `https://myhelios.example.com`, and your auth callback, which based on our example is `https://myhelios.example.com/auth/after/`

* In the developer console, enable the Google People API

* Set the `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` configuration variables accordingly
