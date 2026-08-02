"""The migration and the application schema must not drift apart.

app/storage/schema.py is what the code reads and writes through; the Alembic
migration is what actually shapes an operator's database. If they disagree,
tests pass against a schema nobody is running. This compares the two directly.
"""

from __future__ import annotations

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect

from app.storage import schema

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


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


def test_unique_constraints_that_carry_idempotency_exist(tmp_path):
    """sha256 and dedupe_key uniqueness are what make every retry safe."""
    migrated = _migrated_inspector(tmp_path)

    def unique_columns(table):
        columns = {tuple(u["column_names"]) for u in migrated.get_unique_constraints(table)}
        columns |= {tuple(i["column_names"]) for i in migrated.get_indexes(table) if i["unique"]}
        return columns

    assert ("sha256",) in unique_columns("source_document")
    assert ("dedupe_key",) in unique_columns("txn")
    assert ("institution", "account_ref_masked", "sub_account_label", "currency") in unique_columns("account")


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
