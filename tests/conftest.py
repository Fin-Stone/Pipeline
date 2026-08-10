"""Shared fixtures.

The important one is `repository`, which is parametrized over both engines.
Every pipeline test therefore runs twice: on SQLite, which needs no services,
and on Postgres when TEST_DATABASE_URL is set. That dual run is the
enforcement mechanism for Rule 1 of the development rules in README.md — if a
Postgres-ism leaks into shared code, the SQLite run fails; if a SQLite
assumption does, the Postgres run fails.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import Config
from app.storage.local_fs_blob import LocalFsBlobStore
from app.storage.sqlalchemy_repo import SqlAlchemyLedgerRepository

REPO_ROOT = Path(__file__).resolve().parent.parent
DUMMY_ROOT = REPO_ROOT / "uploads" / "dummy"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

_ENGINES = ["sqlite"] + (["postgres"] if TEST_DATABASE_URL else [])


def pytest_collection_modifyitems(config, items):
    """Skip adapter tests when the operator's dummy documents are absent.

    uploads/dummy is gitignored, so CI has no statements to parse. Those tests
    skip cleanly rather than failing.
    """
    if _has_dummy_pdfs():
        return
    skip = pytest.mark.skip(reason="no documents in uploads/dummy/")
    for item in items:
        if "requires_dummy" in item.keywords:
            item.add_marker(skip)


def _has_dummy_pdfs() -> bool:
    return DUMMY_ROOT.exists() and any(DUMMY_ROOT.rglob("*.pdf"))


@pytest.fixture(params=_ENGINES)
def repository(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite:///{(tmp_path / 'ledger.db').as_posix()}"
        repo = SqlAlchemyLedgerRepository(url)
        repo.create_schema()
        yield repo
        repo.close()
        return

    repo = SqlAlchemyLedgerRepository(TEST_DATABASE_URL)
    from app.storage import schema
    schema.metadata.drop_all(repo.engine)
    repo.create_schema()
    yield repo
    schema.metadata.drop_all(repo.engine)
    repo.close()


@pytest.fixture
def context(repository, config):
    """The tenant context every repository call requires.

    Single-tenant today, but resolved rather than assumed, so the tests
    exercise the same code path a multi-tenant deployment will.
    """
    return repository.resolve_context(config.tenant_slug, config.member_email)


@pytest.fixture
def other_context(repository):
    """A second tenant, for the isolation tests."""
    return repository.resolve_context("other-household", "someone@example.com")


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        database_url=f"sqlite:///{(tmp_path / 'ledger.db').as_posix()}",
        uploads_dir=tmp_path / "uploads",
        data_dir=tmp_path / "data",
        allow_prod=False,
        amount_ceiling_minor=10_000_000_00,
    )


@pytest.fixture
def blob_store(config) -> LocalFsBlobStore:
    return LocalFsBlobStore(config.store_dir)


class RecordingNotifier:
    def __init__(self):
        self.messages = []

    def notify(self, title, message, *, severity="info", **fields):
        self.messages.append((severity, title, message, fields))


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def dummy_root() -> Path:
    return DUMMY_ROOT
