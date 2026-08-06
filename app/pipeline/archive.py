"""Backup and restore, as a logical archive rather than an engine dump.

An untested backup is a rumour, and an engine-specific one is a rumour with a
prerequisite. `pg_dump` output restores into the same major version of the same
engine; this ledger is meant to survive the box it runs on, and the operator's
own migration path — SQLite on a laptop today, Postgres on a mini-PC tomorrow —
is the same operation as a restore. One mechanism serves both, which also means
the migration path is exercised every time a backup is tested.

**What is in it.** The whole database as rows, plus the content-addressed
originals, plus the learned-layout file. Everything else under `data/` is
regenerable: the inbox is transient, reports are rendered from reason files,
and quarantine is rebuilt by a reparse.

**What is not.** No encryption. That is deliberate rather than an omission:
the originals already sit in plaintext on the same disk, the architecture's
answer to theft is full-disk encryption, and a hand-rolled cipher here would
buy the *feeling* of protection over a real one. The archive is a plain file so
restic or Kopia can hold the encrypted off-site copy — see docs/backups.md.

**Money never becomes a float.** Numeric columns are written as decimal
strings and read back as `Decimal`. A JSON number here would silently round
the one thing the whole ledger exists to keep exact.
"""

from __future__ import annotations

import json
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from collections import Counter

from sqlalchemy import Date, DateTime, Numeric, create_engine, func, inspect, select

from ..domain.normalise import clean_raw
from ..storage import schema

#: Bumped only when an older archive can no longer be read. A restore refuses
#: a format it does not know rather than guessing at the bytes.
FORMAT = 1

MANIFEST = "manifest.json"
TABLES = "tables"
STORE = "store"
LEARNED = "learned-layouts.json"


class ArchiveError(RuntimeError):
    """The archive cannot be read, or cannot safely be restored where asked."""


@dataclass
class Manifest:
    format: int
    created_at: str
    schema_revision: str
    source_engine: str
    rows: dict[str, int] = field(default_factory=dict)
    blobs: int = 0
    blob_bytes: int = 0
    #: Digests referenced by a document that the store did not hold. Counted
    #: against distinct digests, never against documents: the store is
    #: content-addressed, so two documents with identical bytes — the same
    #: statement ingested into both the prod and dummy tenants, which is
    #: exactly what a dummy corpus is — are one blob and nothing is missing.
    missing_originals: int = 0
    #: `table.column` -> how many values held a control character that was
    #: replaced on the way in. Always empty for a ledger ingested by a build
    #: that cleans on the way in; non-empty means older rows carry extraction
    #: artifacts, which is worth saying out loud rather than fixing in silence.
    sanitised: dict[str, int] = field(default_factory=dict)
    has_learned_layouts: bool = False

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())


@dataclass
class RestoreResult:
    rows: dict[str, int]
    blobs: int
    schema_revision: str
    upgraded_to: str | None
    dry_run: bool = False


# --- encoding ---------------------------------------------------------------
# Driven by the declared column type, not by the Python value that came back.
# Guessing from the value is how a Decimal that happens to be whole ends up
# written as an int and read back as one.


def _encode(value, column, *, sanitised: Counter | None = None):
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = clean_raw(value)
        if cleaned != value:
            # Never silently. A backup that quietly alters what it saved is
            # worse than one that fails, so the count travels in the manifest
            # and the command prints it.
            if sanitised is not None:
                sanitised[f"{column.table.name}.{column.name}"] += 1
            value = cleaned
        return value
    if isinstance(column.type, Numeric):
        # str(Decimal) is exact and round-trips. float() would not.
        return str(value)
    if isinstance(column.type, DateTime):
        if not isinstance(value, datetime):
            return str(value)
        if column.type.timezone and value.tzinfo is None:
            # SQLite has no timezone type, so a value this application wrote as
            # aware UTC comes back naive. Written without an offset it would be
            # read on restore as whatever the target's session timezone is, and
            # every timestamp in the ledger would silently shift by that much on
            # any box not set to UTC. The application only ever writes UTC here,
            # so saying so is a restatement, not a guess.
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(column.type, Date):
        return value.isoformat() if isinstance(value, date) else str(value)
    return value


def _decode(value, column):
    if value is None:
        return None
    if isinstance(column.type, Numeric):
        return Decimal(value)
    if isinstance(column.type, DateTime):
        parsed = datetime.fromisoformat(value)
        if not column.type.timezone and parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    if isinstance(column.type, Date):
        return date.fromisoformat(value)
    return value


def _engine_name(url: str) -> str:
    return url.split(":", 1)[0].split("+", 1)[0]


def _revision(engine) -> str | None:
    from ..storage.factory import current_revision

    return current_revision(engine)


# --- backup -----------------------------------------------------------------


def create(
    database_url: str,
    store_dir: Path,
    target: Path,
    *,
    learned_path: Path | None = None,
    include_store: bool = True,
) -> Manifest:
    """Write every row, every original and the learned layouts to one archive.

    Streamed table by table into a staging directory and then tarred, rather
    than assembled in memory: the ledger is small today and this must not stop
    being true of a household that has been running it for a decade.
    """
    engine = create_engine(database_url)
    revision = _revision(engine)
    if revision is None:
        raise ArchiveError(
            "this database has no schema yet, so there is nothing to back up.\n"
            "  run:  alembic upgrade head"
        )

    manifest = Manifest(
        format=FORMAT,
        created_at=datetime.now(timezone.utc).isoformat(),
        schema_revision=revision,
        source_engine=_engine_name(database_url),
    )

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="finstone-backup-") as staging_root:
        staging = Path(staging_root)
        (staging / TABLES).mkdir()

        digests: set[str] = set()
        sanitised: Counter = Counter()
        with engine.connect() as conn:
            for table in schema.metadata.sorted_tables:
                path = staging / TABLES / f"{table.name}.jsonl"
                count = 0
                with path.open("w", encoding="utf-8") as handle:
                    for row in conn.execute(select(table)):
                        mapping = row._mapping
                        record = {
                            column.name: _encode(
                                mapping[column.name], column, sanitised=sanitised
                            )
                            for column in table.columns
                        }
                        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
                        count += 1
                        if table.name == "source_document":
                            digests.add(record["sha256"])
                manifest.rows[table.name] = count
        manifest.sanitised = dict(sanitised)

        if include_store:
            from ..storage.local_fs_blob import LocalFsBlobStore

            blob_store = LocalFsBlobStore(store_dir)
            (staging / STORE).mkdir()
            for digest in sorted(digests):
                if not blob_store.exists(digest):
                    # A document whose bytes are gone is a fact about this
                    # install, not a reason to refuse the backup — the rest of
                    # the ledger is still worth saving. It is counted so the
                    # operator is told rather than left to notice at a reparse.
                    manifest.missing_originals += 1
                    continue
                destination = staging / STORE / digest
                with blob_store.open(digest) as source, destination.open("wb") as out:
                    while chunk := source.read(1 << 20):
                        out.write(chunk)
                manifest.blobs += 1
                manifest.blob_bytes += destination.stat().st_size

        if learned_path and Path(learned_path).exists():
            (staging / LEARNED).write_bytes(Path(learned_path).read_bytes())
            manifest.has_learned_layouts = True

        (staging / MANIFEST).write_text(
            json.dumps(asdict(manifest), indent=2), encoding="utf-8"
        )

        # Written to a temporary name first: an interrupted backup must not
        # leave a truncated file at a path that looks like a good one.
        partial = target.with_name(target.name + ".partial")
        with tarfile.open(partial, "w:gz") as tar:
            for item in sorted(staging.iterdir()):
                tar.add(item, arcname=item.name)
        partial.replace(target)

    engine.dispose()
    return manifest


# --- reading ----------------------------------------------------------------


def read_manifest(archive: Path) -> Manifest:
    with tarfile.open(archive, "r:*") as tar:
        try:
            member = tar.extractfile(MANIFEST)
        except KeyError:
            member = None
        if member is None:
            raise ArchiveError(f"{archive} has no {MANIFEST}; it is not a finstone archive")
        payload = json.loads(member.read().decode("utf-8"))

    if payload.get("format") != FORMAT:
        raise ArchiveError(
            f"archive format {payload.get('format')!r}, this build reads {FORMAT}.\n"
            "  Restoring it would be a guess about the bytes. Use a build that matches."
        )
    known = {f for f in Manifest.__dataclass_fields__}
    return Manifest(**{k: v for k, v in payload.items() if k in known})


# --- restore ----------------------------------------------------------------


def _is_empty(engine) -> bool:
    """No ledger rows anywhere. The seeded default tenant does not count.

    A fresh `alembic upgrade head` seeds one tenant and one member so a
    single-user install works immediately. Treating that as "occupied" would
    make every ordinary restore need --force, which is how --force stops being
    read as a warning.
    """
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    with engine.connect() as conn:
        for table in schema.metadata.sorted_tables:
            if table.name not in present:
                continue
            count = conn.execute(select(func.count()).select_from(table)).scalar_one()
            if table.name == "tenant" and count <= 1:
                continue
            if table.name == "member" and count <= 1:
                continue
            if count:
                return False
    return True


def _alembic_config(database_url: str):
    from alembic.config import Config as AlembicConfig

    from ..config import REPO_ROOT

    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "app" / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    # Pin the run to the database being restored into rather than whatever
    # DATABASE_URL happens to be set to in this shell.
    config.attributes["url_set_by_caller"] = True
    return config


def _known_revision(revision: str) -> bool:
    from alembic.script import ScriptDirectory

    from ..config import REPO_ROOT

    script = ScriptDirectory(str(REPO_ROOT / "app" / "migrations"))
    try:
        return script.get_revision(revision) is not None
    except Exception:
        return False


def _reset_sequences(engine, conn) -> None:
    """Point each identity sequence past the ids just inserted.

    Rows are restored with their original ids so foreign keys still line up.
    On Postgres that leaves every sequence at 1, and the next insert collides
    with restored data — the ledger looks fine until the first new statement is
    ingested, which is the worst moment to find out. SQLite derives the next
    rowid from the maximum present and needs nothing.
    """
    if engine.dialect.name != "postgresql":
        return
    from sqlalchemy import text

    for table in schema.metadata.sorted_tables:
        if "id" not in table.c:
            continue
        conn.execute(text(f"""
            SELECT setval(
                pg_get_serial_sequence('{table.name}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table.name}), 1),
                (SELECT MAX(id) IS NOT NULL FROM {table.name})
            )
            WHERE pg_get_serial_sequence('{table.name}', 'id') IS NOT NULL
        """))


def restore(
    archive: Path,
    database_url: str,
    store_dir: Path,
    *,
    learned_path: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> RestoreResult:
    """Rebuild a ledger from an archive, on any engine.

    The target must be empty. An archive older than the code is restored at
    the revision it was written at and then migrated forward — which is exactly
    what those migrations are for, and the alternative is a backup that becomes
    unreadable at the moment it is needed.
    """
    from alembic import command

    manifest = read_manifest(archive)
    if not _known_revision(manifest.schema_revision):
        raise ArchiveError(
            f"the archive was written at schema revision {manifest.schema_revision},\n"
            "  which this build does not know. It is from a newer version.\n"
            "  Restore it with a build that has that migration."
        )

    engine = create_engine(database_url)
    if dry_run:
        engine.dispose()
        return RestoreResult(
            rows=dict(manifest.rows), blobs=manifest.blobs,
            schema_revision=manifest.schema_revision,
            upgraded_to=None, dry_run=True,
        )

    if not force and not _is_empty(engine):
        engine.dispose()
        raise ArchiveError(
            "refusing to restore over a ledger that already holds data.\n"
            "  A restore replaces everything; there is no merge.\n"
            "  Point DATABASE_URL at an empty database, or pass --force to "
            "discard what is there."
        )

    config = _alembic_config(database_url)

    # Dropped and rebuilt rather than deleted from: --force means "this ledger
    # is being replaced", and leftover schema at the wrong revision is exactly
    # what the archive's own revision is about to fix.
    schema.metadata.drop_all(engine)
    with engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))

    # Build the schema as it was when the archive was written, so the rows fit.
    command.upgrade(config, manifest.schema_revision)

    restored: dict[str, int] = {}
    with tarfile.open(archive, "r:*") as tar:
        with engine.begin() as conn:
            # The seeded tenant and member would collide with the archive's
            # own. The archive is the whole truth about this ledger.
            for table in reversed(schema.metadata.sorted_tables):
                conn.execute(table.delete())

            for table in schema.metadata.sorted_tables:
                try:
                    member = tar.extractfile(f"{TABLES}/{table.name}.jsonl")
                except KeyError:
                    member = None
                if member is None:
                    # A table added after the archive was written. The
                    # migrations run below are what fill it, if anything does.
                    restored[table.name] = 0
                    continue

                batch = []
                count = 0
                for line in member:
                    record = json.loads(line)
                    batch.append({
                        column.name: _decode(record.get(column.name), column)
                        for column in table.columns
                        if column.name in record
                    })
                    if len(batch) >= 1000:
                        conn.execute(table.insert(), batch)
                        count += len(batch)
                        batch = []
                if batch:
                    conn.execute(table.insert(), batch)
                    count += len(batch)
                restored[table.name] = count

            _reset_sequences(engine, conn)

        blobs = 0
        if store_dir is not None:
            from ..storage.local_fs_blob import LocalFsBlobStore

            blob_store = LocalFsBlobStore(store_dir)
            for entry in tar.getmembers():
                if not entry.name.startswith(f"{STORE}/") or not entry.isfile():
                    continue
                digest = entry.name.split("/", 1)[1]
                source = tar.extractfile(entry)
                if source is None:
                    continue
                with tempfile.NamedTemporaryFile(delete=False) as scratch:
                    while chunk := source.read(1 << 20):
                        scratch.write(chunk)
                    scratch_path = Path(scratch.name)
                # put() verifies nothing, but the store is content-addressed:
                # the digest is the filename, so a corrupted blob is found the
                # first time anything reparses from it rather than silently
                # standing in for the original.
                blob_store.put(scratch_path, digest)
                scratch_path.unlink(missing_ok=True)
                blobs += 1

        if manifest.has_learned_layouts and learned_path:
            try:
                member = tar.extractfile(LEARNED)
            except KeyError:
                member = None
            if member is not None:
                Path(learned_path).parent.mkdir(parents=True, exist_ok=True)
                Path(learned_path).write_bytes(member.read())

    # Forward to whatever this build expects. On a same-revision restore this
    # is a no-op; on an older archive it is the whole reason the restore works.
    upgraded_to = None
    head = _head_revision()
    if head and head != manifest.schema_revision:
        command.upgrade(config, "head")
        upgraded_to = head

    engine.dispose()
    return RestoreResult(
        rows=restored, blobs=blobs,
        schema_revision=manifest.schema_revision,
        upgraded_to=upgraded_to,
    )


def _head_revision() -> str | None:
    from ..storage.factory import head_revision

    return head_revision()
