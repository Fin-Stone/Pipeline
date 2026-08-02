"""LedgerRepository on SQLAlchemy Core.

Core rather than the ORM: it gives dialect portability at close to raw-driver
speed, while the ORM's identity map and unit of work would cost more than they
return on a write-mostly ingestion path.

Everything here is portable SQL. Deduplication is a *select existing keys →
insert the complement* with the unique index as a backstop, deliberately not
`ON CONFLICT`, so SQLite and Postgres run identical code and the dual-engine
test run stays meaningful. See docs/development-rules.md Rule 1.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from ..ports.repository import (
    AccountRecord,
    BalanceRecord,
    DocumentRecord,
    InsertResult,
    StatusCounts,
    TxnRecord,
)
from . import schema

#: Chunk size for the "which of these keys already exist" query. Keeps the
#: parameter list well inside every driver's limit.
_KEY_CHUNK = 500


class SqlAlchemyLedgerRepository:
    """Works against any engine SQLAlchemy supports; tested on Postgres and SQLite."""

    def __init__(self, url: str, *, echo: bool = False, engine: Engine | None = None):
        self._engine = engine or create_engine(url, echo=echo, future=True)

    @property
    def engine(self) -> Engine:
        return self._engine

    def create_schema(self) -> None:
        schema.metadata.create_all(self._engine)

    def close(self) -> None:
        self._engine.dispose()

    # -- reads ---------------------------------------------------------------

    def get_document_id(self, sha256: str) -> int | None:
        stmt = select(schema.source_document.c.id).where(schema.source_document.c.sha256 == sha256)
        with self._engine.connect() as conn:
            return conn.execute(stmt).scalar_one_or_none()

    def existing_dedupe_keys(self, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        found: set[str] = set()
        with self._engine.connect() as conn:
            for start in range(0, len(keys), _KEY_CHUNK):
                chunk = keys[start:start + _KEY_CHUNK]
                stmt = select(schema.txn.c.dedupe_key).where(schema.txn.c.dedupe_key.in_(chunk))
                found.update(conn.execute(stmt).scalars())
        return found

    def counts(self) -> StatusCounts:
        with self._engine.connect() as conn:
            by_status = dict(
                conn.execute(
                    select(schema.source_document.c.parse_status, func.count())
                    .group_by(schema.source_document.c.parse_status)
                ).all()
            )
            by_institution = dict(
                conn.execute(
                    select(schema.source_document.c.institution, func.count())
                    .group_by(schema.source_document.c.institution)
                ).all()
            )
            accounts = conn.execute(select(func.count()).select_from(schema.account)).scalar_one()
            txns = conn.execute(select(func.count()).select_from(schema.txn)).scalar_one()
        return StatusCounts(
            documents=by_status.get("imported", 0) + by_status.get("imported_unverified", 0),
            documents_unverified=by_status.get("imported_unverified", 0),
            documents_quarantined=by_status.get("quarantined", 0),
            accounts=accounts,
            txns=txns,
            by_institution=by_institution,
        )

    # -- writes --------------------------------------------------------------

    def insert_document(
        self,
        document: DocumentRecord,
        balances: list[BalanceRecord],
        txns: list[TxnRecord],
    ) -> InsertResult:
        """Persist a document and everything it carries, atomically.

        One transaction covers the document row, every account upsert, the
        per-account balances and all transactions. A failure anywhere rolls
        back the whole document — there is no such thing as a half-imported
        statement.
        """
        fetched_at = document.fetched_at or datetime.now(timezone.utc)

        with self._engine.begin() as conn:
            existing = conn.execute(
                select(schema.source_document.c.id)
                .where(schema.source_document.c.sha256 == document.sha256)
            ).scalar_one_or_none()
            if existing is not None:
                return InsertResult(document_id=existing)

            document_id = conn.execute(
                schema.source_document.insert().values(
                    sha256=document.sha256,
                    institution=document.institution,
                    doc_type=document.doc_type,
                    period_start=document.period_start,
                    period_end=document.period_end,
                    statement_date=document.statement_date,
                    fetched_at=fetched_at,
                    fetch_method=document.fetch_method,
                    storage_path=document.storage_path,
                    parser_version=document.parser_version,
                    layout_fingerprint=document.layout_fingerprint,
                    parse_status=document.parse_status,
                    source_profile=document.source_profile,
                    source_relpath=document.source_relpath,
                )
            ).inserted_primary_key[0]

            account_ids: dict[tuple, int] = {}
            for record in balances:
                account_ids[_account_key(record.account_key)] = self._upsert_account(conn, record.account_key)
            for record in txns:
                key = _account_key(record.account_key)
                if key not in account_ids:
                    account_ids[key] = self._upsert_account(conn, record.account_key)

            for record in balances:
                conn.execute(
                    schema.statement_balance.insert().values(
                        account_id=account_ids[_account_key(record.account_key)],
                        source_document_id=document_id,
                        opening_balance_minor=record.opening_balance_minor,
                        closing_balance_minor=record.closing_balance_minor,
                    )
                )

            inserted, skipped = self._insert_txns(conn, document_id, account_ids, txns)

        return InsertResult(
            document_id=document_id,
            accounts=len(account_ids),
            txns_inserted=inserted,
            txns_skipped=skipped,
        )

    def _upsert_account(self, conn, record: AccountRecord) -> int:
        where = (
            (schema.account.c.institution == record.institution)
            & (schema.account.c.account_ref_masked == record.account_ref_masked)
            & (schema.account.c.sub_account_label == (record.sub_account_label or ""))
            & (schema.account.c.currency == record.currency)
        )
        found = conn.execute(select(schema.account.c.id).where(where)).scalar_one_or_none()
        if found is not None:
            return found
        return conn.execute(
            schema.account.insert().values(
                institution=record.institution,
                account_ref_masked=record.account_ref_masked,
                sub_account_label=record.sub_account_label or "",
                currency=record.currency,
                kind=record.kind,
            )
        ).inserted_primary_key[0]

    def _insert_txns(self, conn, document_id: int, account_ids: dict, txns: list[TxnRecord]) -> tuple[int, int]:
        if not txns:
            return 0, 0

        keys = [t.dedupe_key for t in txns]
        already: set[str] = set()
        for start in range(0, len(keys), _KEY_CHUNK):
            chunk = keys[start:start + _KEY_CHUNK]
            already.update(
                conn.execute(
                    select(schema.txn.c.dedupe_key).where(schema.txn.c.dedupe_key.in_(chunk))
                ).scalars()
            )

        rows = []
        seen_in_batch: set[str] = set()
        for t in txns:
            if t.dedupe_key in already or t.dedupe_key in seen_in_batch:
                continue
            seen_in_batch.add(t.dedupe_key)
            rows.append({
                "account_id": account_ids[_account_key(t.account_key)],
                "source_document_id": document_id,
                "posted_date": t.posted_date,
                "value_date": t.value_date,
                "amount_minor": t.amount_minor,
                "currency": t.currency,
                "fx_amount_minor": t.fx_amount_minor,
                "fx_currency": t.fx_currency,
                "fx_rate": t.fx_rate,
                "description_raw": t.description_raw,
                "description_norm": t.description_norm,
                "counterparty_norm": t.counterparty_norm,
                "seq": t.seq,
                "dedupe_key": t.dedupe_key,
            })

        skipped = len(txns) - len(rows)
        if not rows:
            return 0, skipped

        try:
            conn.execute(schema.txn.insert(), rows)
        except IntegrityError:
            # Backstop for a concurrent writer having inserted the same key
            # between the check above and this insert. Falling back to one row
            # at a time keeps the rest of the document importable.
            inserted = 0
            for row in rows:
                savepoint = conn.begin_nested()
                try:
                    conn.execute(schema.txn.insert(), [row])
                    savepoint.commit()
                    inserted += 1
                except IntegrityError:
                    savepoint.rollback()
                    skipped += 1
            return inserted, skipped

        return len(rows), skipped


def _account_key(record: AccountRecord) -> tuple:
    return (
        record.institution,
        record.account_ref_masked,
        record.sub_account_label or "",
        record.currency,
    )
