#!/usr/bin/env bash
#
# One backup, and rotation. What finstone-backup.timer runs nightly, and what
# you run by hand before doing anything you might regret.
#
#     ./infra/scripts/backup.sh
#
#     FINSTONE_BACKUP_KEEP=14      how many to keep on this box
#     FINSTONE_BACKUP_ARGS=        passed through, e.g. --no-store
#
# The archive is engine-agnostic JSONL, not a pg_dump: it restores onto SQLite
# or Postgres, and onto a different machine entirely. See docs/backups.md.

# shellcheck source=infra/scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

KEEP=${FINSTONE_BACKUP_KEEP:-14}
EXTRA=${FINSTONE_BACKUP_ARGS:-}

HOST_DIR="$REPO_ROOT/data/backups"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
NAME="finstone-${STAMP}.tar.gz"

mkdir -p "$HOST_DIR"

# `run --rm` rather than `exec`, so this works whether or not the cli container
# happens to be up — a timer that depends on somebody having started something
# first is a timer that quietly stops backing up.
say "Backing up"
# shellcheck disable=SC2086  # EXTRA is deliberately word-split
compose --profile cli run --rm -T cli \
    finstone backup --out "/srv/finstone/data/backups/${NAME}" $EXTRA

[ -f "$HOST_DIR/$NAME" ] || die "the command reported success but wrote no archive"
note "$(du -h "$HOST_DIR/$NAME" | cut -f1)  $NAME"

# ------------------------------------------------------------- rotation ----
# Oldest first, by name — the stamp is ISO-8601 UTC, so lexical order is
# chronological order and no mtime is trusted. Only files this script's own
# pattern produced are ever considered for deletion.
say "Keeping the newest $KEEP"
mapfile -t archives < <(cd "$HOST_DIR" && ls -1 finstone-*.tar.gz 2>/dev/null | sort)
surplus=$(( ${#archives[@]} - KEEP ))
if [ "$surplus" -gt 0 ]; then
    for old in "${archives[@]:0:$surplus}"; do
        rm -f -- "$HOST_DIR/$old"
        note "removed $old"
    done
else
    note "${#archives[@]} on disk, nothing to remove"
fi

cat <<'OFFSITE'

  A copy on the same disk is not a backup — it survives a mistake, not a
  failure. Send these somewhere else, with a key that cannot delete:

      restic -r b2:your-bucket:finstone backup data/backups

  docs/backups.md explains why encryption belongs there and not here.
OFFSITE
