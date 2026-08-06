"""The migration and the application schema must not drift apart.

app/storage/schema.py is what the code reads and writes through; the Alembic
migration is what actually shapes an operator's database. If they disagree,
tests pass against a schema nobody is running. This compares the two directly.
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text

from app.storage import schema

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _migrated_inspector(tmp_path):
    url = f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "app" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    # Pin the run to this throwaway database rather than whatever DATABASE_URL
    # happens to point at.
    config.attributes["url_set_by_caller"] = True
    command.upgrade(config, "head")
    return inspect(create_engine(url))


def _declared_inspector(tmp_path):
    url = f"sqlite:///{(tmp_path / 'declared.db').as_posix()}"
    engine = create_engine(url)
    schema.metadata.create_all(engine)
    return inspect(engine)


def test_migration_creates_every_declared_table(tmp_path):
    migrated = set(_migrated_inspector(tmp_path).get_table_names()) - {"alembic_version"}
    assert migrated == set(schema.metadata.tables)


def test_migration_columns_match_the_declared_schema(tmp_path):
    migrated = _migrated_inspector(tmp_path)
    declared = _declared_inspector(tmp_path)

    mismatches = {}
    for table in sorted(schema.metadata.tables):
        got = {c["name"]: str(c["type"]).upper() for c in migrated.get_columns(table)}
        want = {c["name"]: str(c["type"]).upper() for c in declared.get_columns(table)}
        if got != want:
            mismatches[table] = {"migration": got, "schema": want}
    assert not mismatches, f"migration and schema.py disagree: {mismatches}"


def _unique_columns(inspector, table):
    columns = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(table)}
    columns |= {tuple(i["column_names"]) for i in inspector.get_indexes(table) if i["unique"]}
    return columns


def test_unique_constraints_that_carry_idempotency_exist(tmp_path):
    """sha256 and dedupe_key uniqueness are what make every retry safe.

    Both are scoped by tenant: two households holding the same statement, or
    the same transaction on the same account reference, must not collide.
    """
    migrated = _migrated_inspector(tmp_path)

    assert ("tenant_id", "sha256") in _unique_columns(migrated, "source_document")
    assert ("tenant_id", "dedupe_key") in _unique_columns(migrated, "txn")
    assert (
        "tenant_id", "institution", "account_ref_masked", "sub_account_label", "currency",
    ) in _unique_columns(migrated, "account")


def test_no_ledger_uniqueness_escapes_the_tenant_scope(tmp_path):
    """A globally unique constraint on a ledger table is a cross-tenant
    collision waiting to happen. This catches one being added by accident."""
    from app.storage import schema

    migrated = _migrated_inspector(tmp_path)
    unscoped = {}
    for table in schema.TENANT_SCOPED_TABLES:
        for columns in _unique_columns(migrated, table.name):
            if "tenant_id" not in columns and columns != ("id",):
                unscoped.setdefault(table.name, []).append(columns)

    # statement_balance is keyed on (account_id, source_document_id); both
    # already belong to exactly one tenant, so it cannot collide across them.
    unscoped.pop("statement_balance", None)
    assert not unscoped, f"tenant-scoped tables carry global uniqueness: {unscoped}"


def test_seeds_the_default_tenant_and_member(tmp_path):
    """A fresh database must be immediately usable single-user."""
    from sqlalchemy import text

    url = f"sqlite:///{(tmp_path / 'seeded.db').as_posix()}"
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "app" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["url_set_by_caller"] = True
    command.upgrade(config, "head")

    with create_engine(url).connect() as conn:
        assert conn.execute(text("SELECT slug FROM tenant")).scalars().all() == ["default"]
        assert conn.execute(text("SELECT role FROM member")).scalars().all() == ["owner"]
        # No password column exists, and none should ever be added.
        columns = {c["name"] for c in inspect(create_engine(url)).get_columns("member")}
        assert not {c for c in columns if "password" in c or "secret" in c}


def test_migration_is_reversible(tmp_path):
    url = f"sqlite:///{(tmp_path / 'roundtrip.db').as_posix()}"
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "app" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    # Pin the run to this throwaway database rather than whatever DATABASE_URL
    # happens to point at.
    config.attributes["url_set_by_caller"] = True
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    remaining = set(inspect(create_engine(url)).get_table_names()) - {"alembic_version"}
    assert remaining == set()


class TestOnPostgres:
    """Every migration must run on the engine an operator actually deploys.

    These were SQLite-only, and that is exactly how `GROUP_CONCAT` — SQLite's
    spelling, with no Postgres equivalent of the same name — reached a real
    first start on Postgres and failed the API container in a restart loop. A
    migration is the one piece of code that runs against a live ledger with no
    chance to back out, so it is the last place a dialect assumption belongs.
    """

    @pytest.fixture
    def url(self):
        if not TEST_DATABASE_URL:
            pytest.skip("set TEST_DATABASE_URL to run the migrations on Postgres")
        engine = create_engine(TEST_DATABASE_URL)
        # A migration run owns the whole database, so it starts from nothing.
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        engine.dispose()
        return TEST_DATABASE_URL

    def _config(self, url):
        config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "app" / "migrations"))
        config.set_main_option("sqlalchemy.url", url)
        config.attributes["url_set_by_caller"] = True
        return config

    def test_every_migration_runs(self, url):
        command.upgrade(self._config(url), "head")
        tables = set(inspect(create_engine(url)).get_table_names()) - {"alembic_version"}
        assert tables == set(schema.metadata.tables)

    def test_the_backfills_run_with_data_present(self, url):
        """Empty-database runs miss half of what a migration does. 0003
        backfills a statement key from rows that must already exist, and a
        backfill is where dialect differences actually bite.
        """
        config = self._config(url)
        command.upgrade(config, "0002_dummy_tenant")

        engine = create_engine(url)
        with engine.begin() as conn:
            tenant_id = conn.execute(text("SELECT id FROM tenant WHERE slug='default'")).scalar_one()
            conn.execute(text("""
                INSERT INTO source_document
                    (tenant_id, sha256, institution, doc_type, period_start, period_end,
                     storage_path, parse_status, source_profile, source_relpath, fetched_at)
                VALUES (:t, :sha, 'Test', 'acc', '2026-06-01', '2026-06-30',
                        'x', 'imported', 'dummy', 'a.pdf', now())
            """), {"t": tenant_id, "sha": "a" * 64})
            document_id = conn.execute(text("SELECT id FROM source_document")).scalar_one()
            conn.execute(text("""
                INSERT INTO account
                    (tenant_id, institution, account_ref_masked, sub_account_label,
                     currency, kind)
                VALUES (:t, 'Test', '1234', '', 'SGD', 'deposit')
            """), {"t": tenant_id})
            account_id = conn.execute(text("SELECT id FROM account")).scalar_one()
            conn.execute(text("""
                INSERT INTO statement_balance
                    (tenant_id, source_document_id, account_id,
                     opening_balance_minor, closing_balance_minor)
                VALUES (:t, :d, :a, 0, 0)
            """), {"t": tenant_id, "d": document_id, "a": account_id})

        command.upgrade(config, "head")

        with engine.connect() as conn:
            key = conn.execute(text("SELECT statement_key FROM source_document")).scalar_one()
        assert key and len(key) == 64, "the backfill did not run"

    def test_migration_is_reversible(self, url):
        config = self._config(url)
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        remaining = set(inspect(create_engine(url)).get_table_names()) - {"alembic_version"}
        assert remaining == set()


class TestSchemaVersionGuard:
    """A database the code does not match must fail immediately and say what
    to do, not surface as a missing column partway through a run."""

    def test_an_unmigrated_database_is_refused(self, tmp_path):
        from app.storage.factory import SchemaOutOfDate, check_schema
        from app.storage.sqlalchemy_repo import SqlAlchemyLedgerRepository

        # create_schema builds the tables but records no revision, which is
        # exactly what a database restored or hand-built looks like.
        repo = SqlAlchemyLedgerRepository(f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
        repo.create_schema()
        with pytest.raises(SchemaOutOfDate, match="alembic upgrade head"):
            check_schema(repo)
        repo.close()

    def test_a_migrated_database_passes(self, tmp_path):
        from sqlalchemy import create_engine
        from app.storage.factory import check_schema
        from app.storage.sqlalchemy_repo import SqlAlchemyLedgerRepository

        url = f"sqlite:///{(tmp_path / 'y.db').as_posix()}"
        config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "app" / "migrations"))
        config.set_main_option("sqlalchemy.url", url)
        config.attributes["url_set_by_caller"] = True
        command.upgrade(config, "head")

        repo = SqlAlchemyLedgerRepository(url, engine=create_engine(url))
        check_schema(repo)   # must not raise
        repo.close()

    def test_the_message_names_both_revisions(self, tmp_path):
        from sqlalchemy import create_engine, text
        from app.storage.factory import SchemaOutOfDate, check_schema, head_revision
        from app.storage.sqlalchemy_repo import SqlAlchemyLedgerRepository

        url = f"sqlite:///{(tmp_path / 'z.db').as_posix()}"
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            conn.execute(text("INSERT INTO alembic_version VALUES ('0001_initial')"))

        repo = SqlAlchemyLedgerRepository(url, engine=engine)
        with pytest.raises(SchemaOutOfDate) as caught:
            check_schema(repo)
        message = str(caught.value)
        assert "0001_initial" in message and head_revision() in message
        repo.close()
