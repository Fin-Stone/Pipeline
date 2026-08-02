"""Where concrete implementations are chosen.

This is the only module that knows which class implements which port. Swapping
Postgres for MySQL is a DATABASE_URL change; swapping local disk for S3 is one
line here.
"""

from __future__ import annotations

import logging

from ..config import Config
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


def build_repository(config: Config) -> SqlAlchemyLedgerRepository:
    return SqlAlchemyLedgerRepository(config.database_url)


def build_blob_store(config: Config) -> LocalFsBlobStore:
    return LocalFsBlobStore(config.store_dir)


def build_notifier(config: Config) -> LogNotifier:
    return LogNotifier()
