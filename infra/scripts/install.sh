#!/usr/bin/env bash
#
# Install Finstone on this machine. One command — Rule 3.
#
#     ./infra/scripts/install.sh
#
# Re-running it is the upgrade path: `git pull && ./infra/scripts/install.sh`.
# Everything below is written to be safe to repeat, which is why nothing here
# regenerates a secret, drops a volume, or overwrites a file it did not write.
#
#     --server-name NAME   what nginx answers to (default: this host's name)
#     --engine ENGINE      postgres (default) or sqlite
#     --no-nginx           skip the reverse proxy; publish the ports instead
#     --no-systemd         skip boot-time start and the nightly backup timer
#
# What it does NOT do: give you any authentication. There is none yet. Read the
# last thing it prints.

# shellcheck source=infra/scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

SERVER_NAME=$(hostname)
ENGINE=postgres
WITH_NGINX=1
WITH_SYSTEMD=1

while [ $# -gt 0 ]; do
    case "$1" in
        --server-name) SERVER_NAME=${2:?--server-name needs a value}; shift 2 ;;
        --engine)      ENGINE=${2:?--engine needs a value}; shift 2 ;;
        --no-nginx)    WITH_NGINX=0; shift ;;
        --no-systemd)  WITH_SYSTEMD=0; shift ;;
        -h|--help)     sed -n '2,20p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *)             die "unknown option $1" ;;
    esac
done

case "$ENGINE" in
    postgres|sqlite) ;;
    *) die "--engine must be postgres or sqlite" ;;
esac

APP_PORT=8000

say "Checking what this box has"
require_docker
note "docker      $(docker --version | cut -d, -f1)"
note "compose     $(docker compose version --short)"
note "install dir $REPO_ROOT"
note "engine      $ENGINE"

# ---------------------------------------------------------------- secrets ---
# A password is generated here, on this machine, once. It is never copied from
# a development .env: a secret that travels with a repository or between
# machines is not a secret, and the one thing worse than no password is one
# several installs share.
#
# Alphanumeric by construction. The connection string in postgres.yml is built
# by string interpolation, so a `/`, `@` or `:` in the password would not be
# escaped — it would silently produce a URL pointing somewhere else.
new_password() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40; }

say "Configuration"
if [ ! -f "$ENV_FILE" ]; then
    cp "$REPO_ROOT/.env.example" "$ENV_FILE"
    note "wrote .env from .env.example"
else
    note ".env is already here — left alone"
fi
# Unconditionally, not only on the file this script created. A .env carried
# over from a laptop arrives world-readable, and it holds the password to the
# ledger.
chmod 600 "$ENV_FILE"

set_env() {
    local key=$1 value=$2
    if grep -qE "^${key}=" "$ENV_FILE"; then
        # A value with slashes in it would end the sed expression; `|` cannot
        # appear in anything written here.
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
    fi
}

# Only if it is genuinely empty. Rotating the password on an existing install
# would leave the database volume holding the old one and nothing able to
# connect — an "upgrade" that takes the ledger away.
if [ "$ENGINE" = postgres ] && ! grep -qE '^POSTGRES_PASSWORD=.+' "$ENV_FILE"; then
    set_env POSTGRES_PASSWORD "$(new_password)"
    note "generated a Postgres password into .env (chmod 600)"
elif [ "$ENGINE" = postgres ]; then
    note "Postgres password already set — left alone"
fi

set_env FINSTONE_ENGINE "$ENGINE"

if [ "$WITH_NGINX" = 1 ]; then
    # Loopback only. With nginx in front there is no reason for the container
    # ports to be on the LAN as well, and with no authentication anywhere an
    # extra open port is the whole ledger.
    set_env FINSTONE_BIND "127.0.0.1:"
    note "stack will listen on 127.0.0.1 only; nginx is the way in"
else
    set_env FINSTONE_BIND ""
    note "no reverse proxy: port $APP_PORT will be open on every interface"
fi

# ------------------------------------------------------------------ stack ---
say "Building and starting"
note "the first build on a low-power box takes a few minutes"
compose up -d --build --remove-orphans

say "Waiting for the API"
for attempt in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${APP_PORT}/api/v1/health" >/dev/null 2>&1; then
        note "healthy after ${attempt}s"
        break
    fi
    [ "$attempt" = 60 ] && {
        compose logs --tail 40 app
        die "the API did not come up. Its last 40 log lines are above."
    }
    sleep 1
done

# ---------------------------------------------------------------- systemd ---
if [ "$WITH_SYSTEMD" = 1 ]; then
    say "Boot-time start and nightly backup"
    if ! command -v systemctl >/dev/null 2>&1; then
        note "no systemd here — skipping"
    else
        compose_args=$(compose_files | tr '\n' ' ')
        compose_cmd="$(command -v docker) compose ${compose_args}"

        install_unit() {
            local name=$1
            sed -e "s|INSTALL_DIR|${REPO_ROOT}|g" \
                -e "s|RUN_USER|${USER}|g" \
                -e "s|COMPOSE|${compose_cmd}|g" \
                "$REPO_ROOT/infra/systemd/${name}" \
                | sudo tee "/etc/systemd/system/${name}" >/dev/null
            note "installed ${name}"
        }

        install_unit finstone.service
        install_unit finstone-backup.service
        sudo cp "$REPO_ROOT/infra/systemd/finstone-backup.timer" \
                /etc/systemd/system/finstone-backup.timer
        note "installed finstone-backup.timer"

        sudo systemctl daemon-reload
        # --now on the timer only. The stack is already up, and `systemctl
        # start finstone` would run `compose up` a second time for nothing.
        sudo systemctl enable finstone.service >/dev/null
        sudo systemctl enable --now finstone-backup.timer >/dev/null
        note "next backup: $(systemctl show finstone-backup.timer -p NextElapseUSecRealtime --value)"
    fi
fi

# ------------------------------------------------------------------ nginx ---
if [ "$WITH_NGINX" = 1 ]; then
    say "Reverse proxy"
    if ! command -v nginx >/dev/null 2>&1; then
        note "nginx is not installed:  sudo apt install nginx"
        note "then re-run this script, or copy infra/nginx/finstone.conf yourself"
        WITH_NGINX=0
    else
        sed -e "s|SERVER_NAME|${SERVER_NAME}|g" \
            -e "s|APP_PORT|${APP_PORT}|g" \
            "$REPO_ROOT/infra/nginx/finstone.conf" \
            | sudo tee /etc/nginx/sites-available/finstone >/dev/null
        sudo ln -sfn /etc/nginx/sites-available/finstone \
                     /etc/nginx/sites-enabled/finstone
        note "installed /etc/nginx/sites-available/finstone for ${SERVER_NAME}"

        # Checked before reloading, so a bad substitution cannot take down
        # whatever else this box is already serving.
        if sudo nginx -t 2>/dev/null; then
            sudo systemctl reload nginx
            note "nginx reloaded"
        else
            sudo nginx -t || true
            die "nginx rejected the configuration; nothing was reloaded"
        fi
    fi
fi

# ---------------------------------------------------------------- summary ---
if [ "$WITH_NGINX" = 1 ]; then
    ADDRESS="http://${SERVER_NAME}"
else
    ADDRESS="http://$(hostname -I 2>/dev/null | awk '{print $1}'):${APP_PORT}"
fi

say "Done"
note "Open       ${ADDRESS}"
note "           the app finds its own server; nothing to type"
note ""
note "Ingest     ./infra/scripts/ingest.sh --profile prod"
note "Back up    ./infra/scripts/backup.sh"
note "Restore    ./infra/scripts/restore.sh <archive>"
note "Logs       docker compose logs -f app"

cat <<'WARNING'

  ┌──────────────────────────────────────────────────────────────────────┐
  │  THERE IS NO AUTHENTICATION.                                         │
  │                                                                      │
  │  Not on the API, not on the UI. Anything that can reach this host    │
  │  can read every transaction, every balance, and every statement you  │
  │  have ever imported — and can delete them.                           │
  │                                                                      │
  │  This is safe on a network you control and on nothing else. Do not   │
  │  port-forward it. Do not put it on a VPS. If you want it away from   │
  │  home, put it behind Tailscale or a WireGuard tunnel, which gives    │
  │  you real authentication instead of an address nobody has guessed.   │
  │                                                                      │
  │  docs/deploy.md says what a stopgap looks like if you need one now.  │
  └──────────────────────────────────────────────────────────────────────┘
WARNING
