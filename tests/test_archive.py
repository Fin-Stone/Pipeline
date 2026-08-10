"""Backup and restore.

An untested backup is a rumour, so these are mostly one test asked in different
ways: put a ledger in, take it out somewhere else, and check that what came out
is the same ledger — not the same row count, the same values.

The cross-engine cases are the ones that matter most. They are simultaneously
the disaster-recovery path and the SQLite-to-Postgres migration path, which is
the point of a logical archive over an engine dump: the migration is exercised
every time the backup is.
"""

from __future__ import annotations

import json
import os
import tarfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select

from app.domain.models import DEPOSIT
from app.pipeline import archive
from app.ports.repository import AccountRecord, BalanceRecord, DocumentRecord, TxnRecord
from app.storage import schema
from app.storage.local_fs_blob import LocalFsBlobStore
from app.storage.sqlalchemy_repo import SqlAlchemyLedgerRepository

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
SHA = "b" * 64
ORIGINAL = b"%PDF-1.4 not really a pdf, but it is these exact bytes that matter"


def _migrated(url: str) -> str:
    """A database at head, the way an operator's actually is."""
    from alembic import command

    config = archive._alembic_config(url)
    command.upgrade(config, "head")
    return url


def _sqlite_url(tmp_path, name="ledger.db") -> str:
    return f"sqlite:///{(tmp_path / name).as_posix()}"


def _seed(url: str, store_dir: Path) -> dict:
    """A small ledger with something of every kind in it.

    Includes a Numeric column and a fractional confidence deliberately: money
    and confidences are the two places a JSON float would quietly round.
    """
    repository = SqlAlchemyLedgerRepository(url)
    context = repository.resolve_context("default", "owner@localhost")

    account = AccountRecord(
        institution="Test", account_ref_masked="1234", sub_account_label="",
        currency="SGD", kind=DEPOSIT,
    )
    document = DocumentRecord(
        sha256=SHA, institution="Test", doc_type="acc",
        period_start=date(2026, 6, 1), period_end=date(2026, 6, 30),
        storage_path="x", parse_status="imported",
        source_profile="dummy", source_relpath="a.pdf",
        fetched_at=datetime.now(timezone.utc),
    )
    txns = [
        TxnRecord(
            account_key=account, posted_date=date(2026, 6, 3 + i), amount_minor=amount,
            currency="SGD", description_raw=f"row {i}", description_norm=f"row {i}",
            counterparty_norm=f"SHOP {i}", dedupe_key=f"k{i}", seq=i,
            # A rate with more precision than a float would hold exactly.
            fx_rate=Decimal("1.23456789") if i == 0 else None,
        )
        for i, amount in enumerate([-12345, 67890, -100])
    ]
    balances = [BalanceRecord(
        account_key=account, opening_balance_minor=0, closing_balance_minor=55445,
    )]
    repository.insert_document(context, document, balances, txns)

    repository.seed_categories(context, ["Grocery", "Dining"])
    repository.hide_txn(context, 1, note="a note")
    repository.mark_transfer(context, 2)
    repository.set_human_category(context, 3, "Grocery")
    repository.dismiss_recurrence(context, "NOT A SUBSCRIPTION", 1000)
    repository.mark_recurrence(context, "SOME INSURER", 27386, "yearly")

    LocalFsBlobStore(store_dir).put(_write_original(store_dir), SHA)

    counts = _snapshot(url)
    repository.close()
    return counts


def _write_original(store_dir: Path) -> Path:
    source = store_dir.parent / "original.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(ORIGINAL)
    return source


def _comparable(value):
    """One representation an instant has on both engines.

    SQLite has no timezone type and hands back naive datetimes; Postgres
    TIMESTAMPTZ hands back aware ones. The stored instant is identical, and
    comparing the Python values directly would report a difference that is a
    property of the engines rather than of the restore. Naive is read as UTC,
    which is the only thing this application ever writes.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return value


def _snapshot(url: str) -> dict:
    """Every row of every table, as comparable values.

    Compared table by table rather than by a count: a restore that dropped a
    column, rounded a decimal or lost a date would keep the counts intact.
    """
    engine = create_engine(url)
    out: dict = {}
    with engine.connect() as conn:
        for table in schema.metadata.sorted_tables:
            rows = []
            for row in conn.execute(select(table).order_by(*table.primary_key.columns)):
                rows.append({k: _comparable(v) for k, v in row._mapping.items()})
            out[table.name] = rows
    engine.dispose()
    return out


@pytest.fixture
def source(tmp_path):
    """A seeded SQLite ledger, plus its store, plus a snapshot of the truth."""
    url = _migrated(_sqlite_url(tmp_path))
    store = tmp_path / "store"
    before = _seed(url, store)
    return {"url": url, "store": store, "before": before}


class TestRoundTrip:
    def test_a_restored_ledger_holds_the_same_rows(self, source, tmp_path):
        """Value for value, not count for count."""
        target = tmp_path / "backup.tar.gz"
        archive.create(source["url"], source["store"], target)

        into = _migrated(_sqlite_url(tmp_path, "restored.db"))
        archive.restore(target, into, tmp_path / "restored-store")

        assert _snapshot(into) == source["before"]

    def test_the_originals_come_back_byte_for_byte(self, source, tmp_path):
        """Without these, a restored ledger cannot be reparsed — which is the
        one thing the immutable store exists to make possible."""
        target = tmp_path / "backup.tar.gz"
        archive.create(source["url"], source["store"], target)

        restored_store = tmp_path / "restored-store"
        archive.restore(target, _migrated(_sqlite_url(tmp_path, "r.db")), restored_store)

        with LocalFsBlobStore(restored_store).open(SHA) as handle:
            assert handle.read() == ORIGINAL

    def test_a_decimal_survives_as_a_decimal(self, source, tmp_path):
        """A JSON float would round it, and the same encoder carries money."""
        target = tmp_path / "backup.tar.gz"
        archive.create(source["url"], source["store"], target)

        into = _migrated(_sqlite_url(tmp_path, "restored.db"))
        archive.restore(target, into, tmp_path / "s")

        engine = create_engine(into)
        with engine.connect() as conn:
            rate = conn.execute(
                select(schema.txn.c.fx_rate).where(schema.txn.c.fx_rate.isnot(None))
            ).scalar_one()
        assert Decimal(str(rate)) == Decimal("1.23456789")

    def test_human_decisions_survive(self, source, tmp_path):
        """The rows nothing can regenerate: what a person hid, marked as a
        transfer, or categorised by hand. Everything else can be reparsed."""
        target = tmp_path / "backup.tar.gz"
        archive.create(source["url"], source["store"], target)

        into = _migrated(_sqlite_url(tmp_path, "restored.db"))
        archive.restore(target, into, tmp_path / "s")

        repository = SqlAlchemyLedgerRepository(into)
        context = repository.resolve_context("default", "owner@localhost")
        assert len(repository.list_hidden(context)) == 1
        assert repository.count_transfer_links(context) == 1
        # What repeats and what does not, where a person overruled the detector
        # in either direction. Neither is derivable from the rows — that is why
        # the tables exist — so a backup that dropped them would restore a
        # ledger quietly missing decisions nobody would think to check.
        assert len(repository.list_recurrence_dismissals(context)) == 1
        marks = repository.list_recurrence_marks(context)
        assert [m["merchant_norm"] for m in marks] == ["SOME INSURER"]
        assert marks[0]["period_label"] == "yearly"
        engine = create_engine(into)
        with engine.connect() as conn:
            sources = conn.execute(select(schema.txn_enrichment.c.source)).scalars().all()
        assert "human" in sources
        repository.close()


class TestItRefusesToDestroySilently:
    def test_it_will_not_restore_over_a_populated_ledger(self, source, tmp_path):
        """A restore replaces everything. Doing that to a live ledger because
        someone typed the wrong path is the accident worth a refusal."""
        target = tmp_path / "backup.tar.gz"
        archive.create(source["url"], source["store"], target)

        with pytest.raises(archive.ArchiveError, match="already holds data"):
            archive.restore(target, source["url"], tmp_path / "s")

        # And it really did leave it alone.
        assert _snapshot(source["url"]) == source["before"]

    def test_force_replaces_it(self, source, tmp_path):
        target = tmp_path / "backup.tar.gz"
        archive.create(source["url"], source["store"], target)

        archive.restore(target, source["url"], tmp_path / "s", force=True)

        assert _snapshot(source["url"]) == source["before"]

    def test_a_freshly_migrated_database_is_not_populated(self, source, tmp_path):
        """`alembic upgrade head` seeds one tenant and one member so a
        single-user install works immediately. If that counted as occupied,
        every ordinary restore would need --force — and a --force everyone
        types stops being read as a warning."""
        into = _migrated(_sqlite_url(tmp_path, "fresh.db"))
        target = tmp_path / "backup.tar.gz"
        archive.create(source["url"], source["store"], target)

        archive.restore(target, into, tmp_path / "s")  # no --force

    def test_a_dry_run_writes_nothing(self, source, tmp_path):
        target = tmp_path / "backup.tar.gz"
        archive.create(source["url"], source["store"], target)
        into = _migrated(_sqlite_url(tmp_path, "untouched.db"))
        before = _snapshot(into)

        result = archive.restore(target, into, tmp_path / "s", dry_run=True)

        assert result.dry_run is True
        assert _snapshot(into) == before

    def test_an_unreadable_format_is_refused_not_guessed(self, tmp_path):
        bogus = tmp_path / "bogus.tar.gz"
        payload = tmp_path / archive.MANIFEST
        payload.write_text(json.dumps({"format": 99}), encoding="utf-8")
        with tarfile.open(bogus, "w:gz") as tar:
            tar.add(payload, arcname=archive.MANIFEST)

        with pytest.raises(archive.ArchiveError, match="format"):
            archive.read_manifest(bogus)

    def test_something_that_is_not_an_archive_says_so(self, tmp_path):
        plain = tmp_path / "notes.tar.gz"
        with tarfile.open(plain, "w:gz") as tar:
            note = tmp_path / "note.txt"
            note.write_text("hello", encoding="utf-8")
            tar.add(note, arcname="note.txt")

        with pytest.raises(archive.ArchiveError, match="not a finstone archive"):
            archive.read_manifest(plain)


class TestWhatIsInIt:
    def test_no_store_skips_the_originals(self, source, tmp_path):
        target = tmp_path / "rows-only.tar.gz"
        manifest = archive.create(
            source["url"], source["store"], target, include_store=False
        )

        assert manifest.blobs == 0
        assert manifest.rows["txn"] == 3
        with tarfile.open(target) as tar:
            assert not [n for n in tar.getnames() if n.startswith("store/")]

    def test_the_manifest_records_the_schema_revision(self, source, tmp_path):
        """A restore has to build the schema the rows were shaped for before
        inserting them, so the revision travels with the data."""
        from app.storage.factory import head_revision

        target = tmp_path / "backup.tar.gz"
        archive.create(source["url"], source["store"], target)

        assert archive.read_manifest(target).schema_revision == head_revision()

    def test_shared_bytes_are_one_blob_and_nothing_is_missing(self, source, tmp_path):
        """The store is content-addressed, so the same statement ingested into
        two tenants — which is exactly what a dummy corpus is — is one blob for
        two documents. Counting blobs against documents reported that as seven
        lost originals on the first real backup, which is the kind of false
        alarm that teaches an operator to ignore the real one.
        """
        repository = SqlAlchemyLedgerRepository(source["url"])
        other = repository.resolve_context("default-dummy", "owner@localhost")
        repository.insert_document(
            other,
            DocumentRecord(
                sha256=SHA, institution="Test", doc_type="acc",
                period_start=date(2026, 6, 1), period_end=date(2026, 6, 30),
                storage_path="x", parse_status="imported",
                source_profile="dummy", source_relpath="a.pdf",
                fetched_at=datetime.now(timezone.utc),
            ),
            [], [],
        )
        repository.close()

        manifest = archive.create(
            source["url"], source["store"], tmp_path / "backup.tar.gz"
        )

        assert manifest.rows["source_document"] == 2
        assert manifest.blobs == 1
        assert manifest.missing_originals == 0

    def test_an_original_the_store_lost_is_counted(self, source, tmp_path):
        """A backup of a ledger whose bytes are gone is still worth taking —
        but the operator has to be told, not left to find out at a reparse."""
        empty_store = tmp_path / "nothing-here"
        manifest = archive.create(source["url"], empty_store, tmp_path / "b.tar.gz")

        assert manifest.blobs == 0
        assert manifest.missing_originals == 1

    def test_a_naive_timestamp_is_written_with_an_offset(self):
        """SQLite hands back naive datetimes for values this application wrote
        as aware UTC. Written without an offset, a restore would read them as
        the *target's* session timezone — shifting every timestamp in the
        ledger by hours on any box not set to UTC, with nothing to notice.
        """
        encoded = archive._encode(
            datetime(2026, 6, 3, 12, 0, 0), schema.source_document.c.fetched_at
        )
        assert encoded.endswith("+00:00")

    def test_backing_up_an_unmigrated_database_says_what_to_do(self, tmp_path):
        with pytest.raises(archive.ArchiveError, match="alembic upgrade head"):
            archive.create(_sqlite_url(tmp_path, "empty.db"), tmp_path / "s",
                           tmp_path / "out.tar.gz")


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL for the Postgres half")
class TestAcrossEngines:
    """The disaster-recovery path and the migration path are one path.

    This is what a logical archive buys over `pg_dump`: the operator moving a
    laptop's SQLite ledger onto a mini-PC running Postgres does it with the
    same two commands as someone restoring from a fire, so the rare one is
    exercised by the ordinary one.
    """

    @pytest.fixture
    def postgres(self):
        from sqlalchemy import text

        engine = create_engine(TEST_DATABASE_URL)
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        engine.dispose()
        return _migrated(TEST_DATABASE_URL)

    def test_sqlite_restores_into_postgres(self, source, postgres, tmp_path):
        target = tmp_path / "backup.tar.gz"
        archive.create(source["url"], source["store"], target)

        archive.restore(target, postgres, tmp_path / "pg-store")

        assert _snapshot(postgres) == source["before"]

    def test_the_sequences_move_past_the_restored_ids(self, source, postgres, tmp_path):
        """Rows keep their original ids so foreign keys still line up, which
        leaves every Postgres sequence at 1. The ledger then looks perfect
        until the next statement is ingested and collides — the worst possible
        moment to discover it.
        """
        target = tmp_path / "backup.tar.gz"
        archive.create(source["url"], source["store"], target)
        archive.restore(target, postgres, tmp_path / "pg-store")

        repository = SqlAlchemyLedgerRepository(postgres)
        context = repository.resolve_context("default", "owner@localhost")
        engine = create_engine(postgres)
        with engine.connect() as conn:
            before = conn.execute(select(func.max(schema.txn.c.id))).scalar_one()

        # The insert that used to fail.
        account = AccountRecord(
            institution="Test", account_ref_masked="1234", sub_account_label="",
            currency="SGD", kind=DEPOSIT,
        )
        repository.insert_document(
            context,
            DocumentRecord(
                sha256="c" * 64, institution="Test", doc_type="acc",
                period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
                storage_path="y", parse_status="imported",
                source_profile="dummy", source_relpath="b.pdf",
                fetched_at=datetime.now(timezone.utc),
            ),
            [],
            [TxnRecord(
                account_key=account, posted_date=date(2026, 7, 3), amount_minor=-500,
                currency="SGD", description_raw="new", description_norm="new",
                counterparty_norm="NEW", dedupe_key="new", seq=0,
            )],
        )

        with engine.connect() as conn:
            after = conn.execute(select(func.max(schema.txn.c.id))).scalar_one()
        assert after > before
        repository.close()

    def test_postgres_restores_back_into_sqlite(self, source, postgres, tmp_path):
        """Both directions, because "you can leave" is the property that makes
        self-hosting real."""
        seeded = tmp_path / "to-pg.tar.gz"
        archive.create(source["url"], source["store"], seeded)
        archive.restore(seeded, postgres, tmp_path / "pg-store")

        back = tmp_path / "from-pg.tar.gz"
        archive.create(postgres, tmp_path / "pg-store", back)
        into = _migrated(_sqlite_url(tmp_path, "back.db"))
        archive.restore(back, into, tmp_path / "back-store")

        assert _snapshot(into) == source["before"]
