"""The ledger persistence seam.

Any implementation must satisfy three properties the pipeline relies on:

1. `insert_document` is atomic — the document row, its accounts, its balances
   and all of its transactions land together or not at all. There is no such
   thing as a half-imported statement.
2. Re-inserting a document whose sha256 already exists **within the same
   tenant** is a no-op, and re-inserting a transaction whose dedupe_key already
   exists within that tenant is skipped.
3. **Every method is scoped by an explicit `TenantContext`.** No method may
   default it, infer it, or read across tenants. It is a required argument so
   that omitting it fails loudly at the call site rather than silently
   returning another household's records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from ..domain.tenancy import MemberIdentity, TenantContext

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

    # -- tenancy -------------------------------------------------------------

    def ensure_tenant(self, slug: str, name: str | None = None) -> int:
        """Return the tenant's id, creating it if absent."""

    def ensure_member(
        self,
        tenant_id: int,
        *,
        display_name: str,
        email: str | None = None,
        identity: MemberIdentity | None = None,
        role: str = "owner",
    ) -> int:
        """Return the member's id, creating it if absent."""

    def resolve_context(self, tenant_slug: str, member_email: str | None = None) -> TenantContext:
        """Resolve configuration into the context every other call requires.

        This is the single place a tenant is chosen. When SSO arrives it is
        replaced by resolution from an authenticated session, and nothing
        downstream changes.
        """

    def find_member_by_identity(self, identity: MemberIdentity) -> TenantContext | None:
        """Resolve an SSO identity to its member, or None if unknown."""

    # -- ledger --------------------------------------------------------------

    def get_document_id(self, context: TenantContext, sha256: str) -> int | None:
        """Return the id of a document already imported *for this tenant*."""

    def existing_dedupe_keys(self, context: TenantContext, keys: list[str]) -> set[str]:
        """Return the subset of `keys` already present for this tenant."""

    def insert_document(
        self,
        context: TenantContext,
        document: DocumentRecord,
        balances: list[BalanceRecord],
        txns: list[TxnRecord],
    ) -> InsertResult:
        """Persist a document and everything it carries, atomically."""

    def counts(self, context: TenantContext) -> StatusCounts:
        """Summary counts for `finstone status`, for this tenant only."""

    def close(self) -> None: ...
