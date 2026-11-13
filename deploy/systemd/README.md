# Helios with systemd and nginx

Installation directly on a server, without containers: gunicorn and the task worker as systemd
services, nginx in front of them, PostgreSQL on the same host. For Docker Compose, see
[../docker-compose.yml](../docker-compose.yml) instead.

| File | Installed as |
|---|---|
| `helios.env.example` | `/etc/helios/helios.env` (all configuration and secrets) |
| `helios-web.service` | `/etc/systemd/system/helios-web.service` (gunicorn, unix socket) |
| `helios-worker.service` | `/etc/systemd/system/helios-worker.service` (background tasks) |
| `nginx-helios.conf` | `/etc/nginx/sites-available/helios` |

Stack: Debian 12/13 or Ubuntu 24.04, Python 3.13 (installed by uv), PostgreSQL ≥ 13. Background
tasks are queued in the database, so there is no message broker. Nothing runs as root.

Replace `vote.example.org` in `nginx-helios.conf` with your host name. All variables are
explained in [docs/configuration.md](../../docs/configuration.md), single sign-on in
[docs/sso.md](../../docs/sso.md).

## Fresh install

```sh
# system packages (libldap/libsasl: python-ldap is a dependency)
apt install -y postgresql nginx git build-essential libldap2-dev libsasl2-dev
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# service user and code
useradd --system --home-dir /opt/helios --create-home --shell /usr/sbin/nologin helios
sudo -u helios git clone https://github.com/linuxrrze/helios-server.git /opt/helios/helios-server
cd /opt/helios/helios-server
sudo -u helios uv sync --frozen --no-dev --extra saml      # --extra saml: only for SAML login

# database: a normal (non-superuser) role owning the database, peer auth over the socket
sudo -u postgres createuser helios
sudo -u postgres createdb -O helios helios

# configuration
install -d -m 0750 -o root -g helios /etc/helios
install -m 0640 -o root -g helios deploy/systemd/helios.env.example /etc/helios/helios.env
$EDITOR /etc/helios/helios.env            # replace every CHANGE-ME

# schema and checks
sudo -u helios sh -c 'set -a; . /etc/helios/helios.env; set +a; .venv/bin/python manage.py migrate'
sudo -u helios sh -c 'set -a; . /etc/helios/helios.env; set +a; .venv/bin/python manage.py check --deploy'
# expected warnings: security.W003 (Helios has its own CSRF checks) and security.W004 (HSTS is
# sent by Helios' own middleware when HSTS=1)

# services
cp deploy/systemd/helios-web.service deploy/systemd/helios-worker.service /etc/systemd/system/
cp deploy/systemd/nginx-helios.conf /etc/nginx/sites-available/helios
ln -s ../sites-available/helios /etc/nginx/sites-enabled/
usermod -aG helios www-data               # nginx reaches the gunicorn socket through the group
nginx -t && systemctl daemon-reload
systemctl enable --now helios-web helios-worker && systemctl reload nginx
```

## Updating

```sh
cd /opt/helios/helios-server
sudo -u helios git pull
sudo -u helios uv sync --frozen --no-dev --extra saml
sudo -u helios sh -c 'set -a; . /etc/helios/helios.env; set +a; .venv/bin/python manage.py migrate'
systemctl restart helios-web helios-worker
```

Don't update while an election is open.

Configuration changes take effect after `systemctl restart helios-web helios-worker`.

> These files have not been tested on a real host as part of this repository (`nginx -t`,
> `systemd-analyze verify`). Check them on a staging system first.
