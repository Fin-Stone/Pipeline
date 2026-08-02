"""The ledger persistence seam.

Any implementation must satisfy two properties the pipeline relies on:

1. `insert_document` is atomic — the document row, its accounts, its balances
   and all of its transactions land together or not at all. There is no such
   thing as a half-imported statement.
2. Re-inserting a document whose sha256 already exists is a no-op, and
   re-inserting a transaction whose dedupe_key already exists is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

# parse_status values. Portable TEXT + CHECK rather than a Postgres ENUM.
STATUS_IMPORTED = "imported"
STATUS_IMPORTED_UNVERIFIED = "imported_unverified"
STATUS_QUARANTINED = "quarantined"
PARSE_STATUSES = (STATUS_IMPORTED, STATUS_IMPORTED_UNVERIFIED, STATUS_QUARANTINED)


@dataclass(frozen=True, slots=True)
class AccountRecord:
    institution: str
    account_ref_masked: str
    currency: str
    kind: str
    sub_account_label: str | None = None


@dataclass(frozen=True, slots=True)
class TxnRecord:
    account_key: AccountRecord
    posted_date: date
    amount_minor: int
    currency: str
    description_raw: str
    description_norm: str
    counterparty_norm: str
    dedupe_key: str
    seq: int
    value_date: date | None = None
    fx_amount_minor: int | None = None
    fx_currency: str | None = None
    fx_rate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BalanceRecord:
    account_key: AccountRecord
    opening_balance_minor: int | None
    closing_balance_minor: int | None


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    sha256: str
    institution: str
    doc_type: str
    period_start: date | None
    period_end: date | None
    storage_path: str
    parse_status: str
    source_profile: str
    source_relpath: str
    parser_version: str | None = None
    layout_fingerprint: str | None = None
    statement_date: date | None = None
    fetched_at: datetime | None = None
    fetch_method: str = "manual_upload"


@dataclass(frozen=True, slots=True)
class InsertResult:
    document_id: int
    accounts: int = 0
    txns_inserted: int = 0
    txns_skipped: int = 0


@dataclass(frozen=True, slots=True)
class StatusCounts:
    documents: int = 0
    documents_unverified: int = 0
    documents_quarantined: int = 0
    accounts: int = 0
    txns: int = 0
    by_institution: dict[str, int] = field(default_factory=dict)


@runtime_checkable
class LedgerRepository(Protocol):
    def create_schema(self) -> None:
        """Create tables if absent. Used by tests; production uses migrations."""

    def get_document_id(self, sha256: str) -> int | None:
        """Return the id of an already-imported document, or None."""

    def existing_dedupe_keys(self, keys: list[str]) -> set[str]:
        """Return the subset of `keys` already present in the ledger."""

    def insert_document(
        self,
        document: DocumentRecord,
        balances: list[BalanceRecord],
        txns: list[TxnRecord],
    ) -> InsertResult:
        """Persist a document and everything it carries, atomically."""

    def counts(self) -> StatusCounts:
        """Summary counts for `finstone status`."""

    def close(self) -> None: ...
