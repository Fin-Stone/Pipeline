#!/usr/bin/env sh
# Single-command bootstrap for the core stack (POSIX parity with bootstrap.ps1).
#
# Deliberately not a Makefile: `make` is not installed on the target host, and
# a documented command that does not run is worse than no command at all.

set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
COMPOSE="$ROOT/infra/compose/docker-compose.yml"
ENV_FILE="$ROOT/infra/env/.env"
ENV_EXAMPLE="$ROOT/infra/env/.env.example"

step() { printf '==> %s\n' "$1"; }
warn() { printf '!!  %s\n' "$1" >&2; }

if [ "${1:-}" = "--down" ]; then
    step 'Stopping the stack'
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE" down
    exit 0
fi

# 1. Environment
if [ ! -f "$ENV_FILE" ]; then
    step 'Creating infra/env/.env from the template'
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    warn 'POSTGRES_PASSWORD is empty. Edit infra/env/.env, then run this again.'
    exit 1
fi
if ! grep -Eq '^POSTGRES_PASSWORD=.+' "$ENV_FILE"; then
    warn 'POSTGRES_PASSWORD is not set in infra/env/.env.'
    exit 1
fi

# 2. Git hooks, when this is a checkout rather than an unpacked release.
#
# Rule 2 was prose on trust until the operator's own card ended up in a test
# fixture. The pre-push hook is what makes it fail in a second rather than in
# review, and wiring it here means nobody has to remember a second command.
if [ -d "$ROOT/.git" ] && [ -d "$ROOT/.githooks" ]; then
    step 'Installing git hooks'
    git -C "$ROOT" config core.hooksPath .githooks
fi

# 3. Runtime directories. uploads/prod is created but never read by agents.
step 'Preparing data and upload directories'
for dir in data/inbox data/store data/quarantine uploads/dummy uploads/prod; do
    mkdir -p "$ROOT/$dir"
done

# 4. Services, then migrations (the app service applies them on start).
step 'Starting Postgres and the pipeline runtime'
if [ "${1:-}" = "--rebuild" ]; then
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE" up -d --build
else
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE" up -d
fi

# 5. Health
step 'Verifying the pipeline is reachable'
docker compose --env-file "$ENV_FILE" -f "$COMPOSE" exec -T app finstone status

echo
step 'Ready'
echo "  Ingest the dummy documents:"
echo "    docker compose --env-file $ENV_FILE -f $COMPOSE exec app finstone run --profile dummy"
echo "  Explain a single document:"
echo "    docker compose --env-file $ENV_FILE -f $COMPOSE exec app finstone doctor <path>"
