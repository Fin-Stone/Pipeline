# Backup and restore

An untested backup is a rumour. Everything below is written so that testing it
is one command, because a restore path nobody has walked is not a restore path.

```bash
finstone backup                       # -> data/backups/finstone-<timestamp>.tar.gz
finstone restore <archive>            # into whatever DATABASE_URL points at
finstone restore <archive> --dry-run  # say what it holds, write nothing
```

## What is worth backing up, and what is not

| Under `data/` | In the archive | Why |
|---|---|---|
| the database | **yes** | Human decisions live only here — see below |
| `store/` | **yes** | The immutable originals. Content-addressed, deduplicated |
| `learned-layouts.json` | **yes** | Operator state; cheap to carry, annoying to rebuild |
| `inbox/` | no | Transient. Files are copied out of it and into the store |
| `reports/` | no | Rendered from the reason files on demand |
| `quarantine/` | no | Rebuilt by `finstone reparse --quarantined` |

**The ledger's rows are mostly regenerable and its decisions are not.** Every
transaction, balance and account can be reconstructed by reparsing the store.
What cannot be reconstructed is what a person decided: the category rules, the
hand-corrected categories, the rows marked as internal transfers, the rows
hidden from the dashboard. Those exist in exactly one place. That asymmetry is
the reason this command exists at a higher priority than it looks.

## It is a logical archive, not an engine dump

Rows as JSONL, not `pg_dump` output. This costs a little size and buys two
things:

- **It restores onto a different engine.** SQLite on a laptop today, Postgres
  on a mini-PC tomorrow, back again if that box dies. Moving engines *is* a
  restore, so the migration path and the disaster path are one path and the
  rare one is exercised by the ordinary one.
- **It does not depend on a server version.** A `pg_dump` from 17 restoring
  into 16 is its own adventure, at the worst possible moment.

Money is written as decimal strings and read back as `Decimal`; a JSON number
would round the one thing the ledger exists to keep exact.

Timestamps are written with an explicit UTC offset even when the source engine
handed them back naive. Without that, restoring into Postgres reads them in the
*session* timezone and every timestamp in the ledger shifts by however far the
box is from UTC, with nothing to notice.

## Restoring an older archive

The archive records the schema revision it was written at. A restore builds the
schema *as it was*, inserts the rows, then runs the remaining migrations
forward — which is what those migrations are for. A backup that stops being
readable after a schema change is not a backup.

An archive from a **newer** build is refused rather than guessed at.

## What a restore refuses to do

It will not restore over a ledger that already holds rows. There is no merge:
a restore replaces everything, and doing that to a live ledger because someone
typed the wrong path is the accident worth a refusal. `--force` overrides it and
says plainly that it discards what is there.

A freshly migrated database is not "populated" — `alembic upgrade head` seeds
one tenant and one member so a single-user install works immediately. If that
counted, every ordinary restore would need `--force`, and a `--force` everyone
types stops being read as a warning.

## Encryption is deliberately not here

The archive is a plain file. That is a decision, not an omission:

- The originals already sit in plaintext on the same disk, and the answer to a
  stolen machine is full-disk encryption (LUKS or ZFS native), not a second
  cipher inside one file on it.
- A hand-rolled encryption step here would buy the *feeling* of protection over
  a real one, and it is the kind of code whose bugs surface only when it is
  needed.

The off-site copy is where encryption belongs, and a tool that already does it
well should do it:

```bash
finstone backup --out /tmp/finstone.tar.gz
restic -r b2:your-bucket:finstone backup /tmp/finstone.tar.gz
```

Use a B2 application key **without** delete permission plus bucket lifecycle
rules, so ransomware on the box cannot wipe the remote copy. That is the
difference between a backup and the illusion of one.

## Testing a restore

Quarterly, into a scratch database, and check a number you recognise:

```bash
finstone backup --out /tmp/test.tar.gz
DATABASE_URL=sqlite:////tmp/scratch.db alembic upgrade head
DATABASE_URL=sqlite:////tmp/scratch.db finstone restore /tmp/test.tar.gz
DATABASE_URL=sqlite:////tmp/scratch.db finstone status --profile prod
```

The document, account and transaction counts should match the live ledger. The
command also compares every table's restored row count against the manifest and
fails loudly if they disagree.

## Moving from SQLite to Postgres

The same two commands. This is the path the project itself took.

```bash
finstone backup --out data/backups/to-postgres.tar.gz

# Bring up Postgres and migrate it to head
POSTGRES_PASSWORD=... docker compose \
    -f docker-compose.yml -f infra/compose/postgres.yml up -d

# Restore into it
DATABASE_URL='postgresql+psycopg://finstone:...@127.0.0.1:5432/finstone' \
    finstone restore data/backups/to-postgres.tar.gz
```

Rows keep their original ids so foreign keys still line up, which leaves every
Postgres sequence at 1 — the restore moves them past the restored data. Without
that the ledger looks perfect until the next statement is ingested and collides,
which is the worst possible moment to find out.

## Two things a real backup found

Both were latent for as long as the ledger stayed on one engine, and both are
recorded here because they are the argument for testing restores at all:

- **A NUL byte in a transaction description.** A merchant's apostrophe came out
  of PDF extraction as `0x00`. SQLite stores it happily; Postgres refuses text
  containing NUL outright. The ledger was portable right up until the day it had
  to move. Descriptions are now cleaned on the way in, and `backup` reports any
  value it had to sanitise rather than altering it silently.
- **Totals arriving as strings.** Postgres widens `SUM(bigint)` to `numeric` so
  it cannot overflow, and that comes back as a `Decimal` and serialises to a
  JSON string. The same ledger answered `/summary` with a number on one engine
  and a string on the other. Every money sum is now cast in SQL.
