#!/usr/bin/env bash
#
# Stage and ingest whatever is sitting in uploads/, on the box.
#
#     ./infra/scripts/ingest.sh                  the synthetic set
#     ./infra/scripts/ingest.sh --profile prod   real statements
#
# A wrapper over `docker compose run … finstone run` for one reason: the prod
# path needs FINSTONE_ALLOW_PROD=1, and a variable you have to remember is a
# variable somebody eventually exports in their shell profile — at which point
# the guard is gone. Here it is set for exactly one command and only when the
# profile asked for is prod.

# shellcheck source=infra/scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

PROFILE=dummy
while [ $# -gt 0 ]; do
    case "$1" in
        --profile) PROFILE=${2:?--profile needs a value}; shift 2 ;;
        *) break ;;
    esac
done

ENV_ARGS=()
if [ "$PROFILE" = prod ]; then
    ENV_ARGS=(-e FINSTONE_ALLOW_PROD=1)
    say "Ingesting real statements"
    note "uploads/prod is read-only to the container; originals are copied, never moved"
else
    say "Ingesting the synthetic set"
fi

compose --profile cli run --rm -T "${ENV_ARGS[@]}" cli \
    finstone run --profile "$PROFILE" "$@"

say "Where it went"
compose --profile cli run --rm -T cli finstone status --profile "$PROFILE"
