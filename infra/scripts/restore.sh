#!/usr/bin/env bash
#
# Rebuild this install's ledger from an archive. One command — Rule 3, which
# asks for one command to install *and one to restore*, because a restore path
# nobody has walked is not a restore path.
#
#     ./infra/scripts/restore.sh data/backups/finstone-20260806T…Z.tar.gz
#     ./infra/scripts/restore.sh <archive> --dry-run   say what it holds
#     ./infra/scripts/restore.sh <archive> --force     over a ledger with rows
#
# The archive may live anywhere on this machine; it does not have to be under
# data/. It may also have come from a different engine or a different box —
# that is the whole reason the archive is logical rather than a pg_dump.

# shellcheck source=infra/scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

[ $# -ge 1 ] || die "usage: restore.sh <archive> [--dry-run|--force]"

# Checked before canonicalising, not after. `readlink -f` on a path whose
# parent does not exist fails, and under `set -e` that ends the script with no
# message at all — the operator mistypes a path and gets silence.
[ -f "$1" ] || die "no such archive: $1"
ARCHIVE=$(readlink -f "$1"); shift

DRY_RUN=0
for arg in "$@"; do [ "$arg" = "--dry-run" ] && DRY_RUN=1; done

# The container already has data/ mounted. An archive from anywhere else is
# bind-mounted read-only for the one command, rather than copied into the
# ledger's own directory where it would then be picked up by rotation.
case "$ARCHIVE" in
    "$REPO_ROOT/data/"*)
        IN_CONTAINER="/srv/finstone/data/${ARCHIVE#"$REPO_ROOT/data/"}"
        MOUNT=()
        ;;
    *)
        IN_CONTAINER="/tmp/restore.tar.gz"
        MOUNT=(-v "${ARCHIVE}:/tmp/restore.tar.gz:ro")
        ;;
esac

say "Reading the archive"
compose --profile cli run --rm -T "${MOUNT[@]}" cli \
    finstone restore "$IN_CONTAINER" --dry-run

if [ "$DRY_RUN" = 1 ]; then
    note "nothing was written"
    exit 0
fi

# The API is stopped for the duration. A restore replaces every row, and a
# dashboard reading through the middle of that shows figures that were never
# true of any ledger — which is worse than being down for the minute it takes.
say "Stopping the app while the ledger is replaced"
compose stop app

restore_status=0
say "Restoring"
compose --profile cli run --rm -T "${MOUNT[@]}" cli \
    finstone restore "$IN_CONTAINER" "$@" || restore_status=$?

# Brought back either way. A failed restore that also leaves the stack down has
# turned one problem into two, and the operator needs the dashboard to see what
# state they are actually in.
say "Starting the app"
compose start app

[ "$restore_status" = 0 ] || die "the restore failed; the stack is back up on whatever is in the database"

say "Done"
note "Check a number you recognise before trusting it:"
note "  docker compose --profile cli run --rm cli finstone status --profile prod"
