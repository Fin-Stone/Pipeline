# Sourced by the other scripts here. Not runnable on its own.
#
# One place decides where the repository is and which Compose files describe
# the stack, because the alternative is three scripts that agree until one of
# them is edited.

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
ENV_FILE="$REPO_ROOT/.env"

# Which engine this install runs on, read from .env rather than guessed.
#
# It would be easy to infer it — a POSTGRES_PASSWORD is set, so it must be
# Postgres — but an install whose shape is deduced is one you cannot read off
# the box at 2am. It is written down instead.
finstone_engine() {
    if [ -f "$ENV_FILE" ] && grep -qE '^FINSTONE_ENGINE=postgres[[:space:]]*$' "$ENV_FILE"; then
        echo postgres
    else
        echo sqlite
    fi
}

compose_files() {
    local files=(-f "$REPO_ROOT/docker-compose.yml")
    if [ "$(finstone_engine)" = postgres ]; then
        files+=(-f "$REPO_ROOT/infra/compose/postgres.yml")
    fi
    printf '%s\n' "${files[@]}"
}

compose() {
    local files=()
    mapfile -t files < <(compose_files)
    (cd "$REPO_ROOT" && docker compose "${files[@]}" "$@")
}

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

require_docker() {
    command -v docker >/dev/null 2>&1 \
        || die "docker is not installed. https://docs.docker.com/engine/install/ubuntu/"
    # v2 as a subcommand, not the old docker-compose binary. The stack uses
    # `include:`, which v1 has never understood, so a v1 box fails with a
    # message about YAML rather than about its version.
    docker compose version >/dev/null 2>&1 \
        || die "docker compose v2 is not available. Install the docker-compose-plugin package."
    docker info >/dev/null 2>&1 \
        || die "cannot talk to the Docker daemon. Is it running, and are you in the docker group?
  sudo usermod -aG docker \$USER   then log out and back in"
}
