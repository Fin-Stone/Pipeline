"""Where concrete implementations are chosen.

This is the only module that knows which class implements which port. Swapping
Postgres for MySQL is a DATABASE_URL change; swapping local disk for S3 is one
line here.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from ..config import REPO_ROOT, Config
from ..ports.notifier import SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARNING
from .local_fs_blob import LocalFsBlobStore
from .sqlalchemy_repo import SqlAlchemyLedgerRepository

log = logging.getLogger("finstone")

_LEVELS = {SEVERITY_INFO: logging.INFO, SEVERITY_WARNING: logging.WARNING, SEVERITY_ERROR: logging.ERROR}


class LogNotifier:
    """Phase 1 notifier. Later phases route to ntfy or Gotify."""

    def notify(self, title: str, message: str, *, severity: str = SEVERITY_INFO, **fields) -> None:
        try:
            extra = " ".join(f"{k}={v}" for k, v in fields.items())
            log.log(_LEVELS.get(severity, logging.INFO), "%s: %s %s", title, message, extra)
        except Exception:  # pragma: no cover
            # A failure to notify must never fail the run that was trying to
            # report a problem.
            pass


class SchemaOutOfDate(RuntimeError):
    """The database does not match the schema this code expects."""


def head_revision() -> str | None:
    """The migration this code was written against."""
    try:
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory
    except ImportError:  # pragma: no cover - alembic is a hard dependency
        return None
    ini = REPO_ROOT / "alembic.ini"
    if not ini.exists():
        return None
    # Reading the script directory configures alembic's loggers, which then
    # narrate to stderr. This is a silent lookup, not a migration run.
    previous = logging.getLogger("alembic").level
    logging.getLogger("alembic").setLevel(logging.WARNING)
    try:
        script = ScriptDirectory.from_config(AlembicConfig(str(ini)))
        return script.get_current_head()
    finally:
        logging.getLogger("alembic").setLevel(previous)


def current_revision(engine) -> str | None:
    """The migration the database is actually at, or None if uninitialised."""
    inspector = inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return None
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()


def check_schema(repository) -> None:
    """Refuse to run against a database the code does not match.

    Without this the mismatch surfaces as a driver error partway through a
    run — after files have been staged and some documents processed — and the
    message names a missing column rather than the thing to do about it. On a
    fresh install it would be the first thing anyone hits.
    """
    expected = head_revision()
    if expected is None:
        return

    actual = current_revision(repository.engine)
    if actual == expected:
        return

    if actual is None:
        raise SchemaOutOfDate(
            "the database has no schema yet.\n"
            "  run:  alembic upgrade head"
        )
    raise SchemaOutOfDate(
        "the database schema is out of date.\n"
        f"  database is at : {actual}\n"
        f"  this code needs: {expected}\n"
        "  run:  alembic upgrade head"
    )


def build_repository(config: Config) -> SqlAlchemyLedgerRepository:
    return SqlAlchemyLedgerRepository(config.database_url)


def build_blob_store(config: Config) -> LocalFsBlobStore:
    return LocalFsBlobStore(config.store_dir)


def build_notifier(config: Config) -> LogNotifier:
    return LogNotifier()
