# Scripts

Everything an operator does to a running install. Bash, targeting Ubuntu.

```bash
./infra/scripts/install.sh      # install, and the upgrade path — safe to repeat
./infra/scripts/ingest.sh       # stage and ingest what is in uploads/
./infra/scripts/backup.sh       # one backup, and rotation
./infra/scripts/restore.sh      # the whole ledger back, from any archive
```

`_common.sh` is sourced, not run. It decides where the repository is and which
Compose files describe this stack, so three scripts cannot drift apart on
either question.

See **[../../docs/deploy.md](../../docs/deploy.md)** for the whole story,
including the authentication gap you should read before exposing any of this.

## Why these exist rather than a page of commands to copy

Two of them are Rule 3 — one command to install, one to restore — and the
other two are guard rails around the ways this can go quietly wrong:

- **`ingest.sh`** sets `FINSTONE_ALLOW_PROD=1` for exactly one command. A
  variable you have to remember is a variable somebody eventually exports in
  their shell profile, and then Rule 2's guard is gone.
- **`restore.sh`** stops the API before replacing rows and starts it again
  afterwards, whether the restore worked or not. A dashboard read through the
  middle of a restore shows figures that were never true of any ledger.
- **`backup.sh`** rotates by name, not by mtime — the stamp is ISO-8601 UTC, so
  lexical order is chronological order — and only ever considers files matching
  the pattern it writes itself.

## Repeatability

`install.sh` is the upgrade path, so it is written to be run again and again.
It never regenerates a secret, never drops a volume, and never overwrites a
file it did not write. Rotating the Postgres password on an existing install
would leave the database volume holding the old one and nothing able to
connect — an upgrade that takes the ledger away.
