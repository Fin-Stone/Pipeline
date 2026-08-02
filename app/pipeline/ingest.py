"""data/inbox -> content-addressed store -> parse -> validate -> ledger.

Each document is independent: a failure quarantines that document and the run
continues with the next one. Persistence for a document is a single database
transaction, so there is no such thing as a half-imported statement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import DOCUMENT_EXTENSIONS, Config
from ..domain.dedupe import assign_seq, dedupe_key, sha256_file
from ..domain.models import IngestOutcome, ParsedDocument
from ..domain.normalise import normalise_counterparty, normalise_description
from ..parsers import fingerprint as fingerprinting
from ..parsers import pdfio
from ..parsers.registry import AdapterRegistry, UnknownLayout, build_default_registry
from ..ports.notifier import SEVERITY_ERROR, SEVERITY_WARNING
from ..ports.parser import ParseError
from ..ports.repository import (
    STATUS_IMPORTED,
    STATUS_QUARANTINED,
    AccountRecord,
    BalanceRecord,
    DocumentRecord,
    TxnRecord,
)
from . import diagnostics, quarantine
from .validate import validate

log = logging.getLogger("finstone.ingest")

STATUS_DUPLICATE = "duplicate"
STATUS_QUARANTINED_OUT = "quarantined"


@dataclass(frozen=True, slots=True)
class IngestSummary:
    processed: int = 0
    imported: int = 0
    unverified: int = 0
    duplicates: int = 0
    quarantined: int = 0
    txns_inserted: int = 0
    outcomes: tuple[IngestOutcome, ...] = ()


def ingest_inbox(config: Config, context, repository, blob_store, notifier, registry: AdapterRegistry | None = None) -> IngestSummary:
    registry = registry or build_default_registry()
    inbox = config.inbox_dir
    outcomes = []

    files = [
        p for p in sorted(inbox.rglob("*"))
        if p.is_file() and p.suffix.lower() in DOCUMENT_EXTENSIONS
    ]
    for path in files:
        outcomes.append(ingest_file(config, context, path, repository, blob_store, notifier, registry))

    return IngestSummary(
        processed=len(outcomes),
        imported=sum(1 for o in outcomes if o.status == STATUS_IMPORTED),
        unverified=sum(1 for o in outcomes if o.status == "imported_unverified"),
        duplicates=sum(1 for o in outcomes if o.status == STATUS_DUPLICATE),
        quarantined=sum(1 for o in outcomes if o.status == STATUS_QUARANTINED_OUT),
        txns_inserted=sum(o.txns_inserted for o in outcomes),
        outcomes=tuple(outcomes),
    )


def ingest_file(config: Config, context, path: Path, repository, blob_store, notifier, registry: AdapterRegistry) -> IngestOutcome:
    digest = sha256_file(path)
    profile, relpath = _provenance(config, path)

    if repository.get_document_id(context, digest) is not None:
        return IngestOutcome(sha256=digest, path=str(path), status=STATUS_DUPLICATE)

    # Store the original before anything else can fail. The bytes are what
    # make a later reparse possible.
    storage_path = blob_store.put(path, digest)

    try:
        document = pdfio.load(path, password=config.pdf_password_for(_institution_hint(relpath)))
    except Exception as exc:
        return _quarantine(config, digest, path, profile, relpath, notifier,
                           "unreadable_document", f"cannot open {path.name}", error=exc)

    try:
        layout = fingerprinting.fingerprint_pdf(document)
    except Exception as exc:
        return _quarantine(config, digest, path, profile, relpath, notifier,
                           "fingerprint_failed", f"cannot fingerprint {path.name}", error=exc)

    try:
        adapter = registry.resolve(layout)
    except UnknownLayout as exc:
        # The designed outcome for a layout nobody has written an adapter for.
        # The fingerprint is recorded so registering it is a one-line change.
        return _quarantine(
            config, digest, path, profile, relpath, notifier,
            "unknown_layout",
            f"no adapter registered for layout {layout}",
            detail=fingerprinting.describe(document),
            error=exc,
        )

    try:
        parsed = adapter.parse(path, password=config.pdf_password_for(adapter.institution))
    except ParseError as exc:
        # Carry the adapter's own account of *where* it failed, plus the lines
        # around it, so the failure can be diagnosed from the report alone.
        context = dict(getattr(exc, "context", {}) or {})
        return _quarantine(
            config, digest, path, profile, relpath, notifier,
            "parse_failed", str(exc),
            detail={
                "adapter": adapter.name,
                "fingerprint": layout,
                "context": context,
                "neighbourhood": [
                    {"page": p, "y": round(y, 1), "text": t}
                    for p, y, t in diagnostics.build_neighbourhood(
                        document, context.get("page"), context.get("y")
                    ).lines
                ],
            },
            error=exc,
        )
    except Exception as exc:
        return _quarantine(config, digest, path, profile, relpath, notifier,
                           "parser_crashed", f"{adapter.name} raised {type(exc).__name__}",
                           detail={"adapter": adapter.name, "fingerprint": layout}, error=exc)

    result = validate(parsed, amount_ceiling_minor=config.amount_ceiling_minor)
    if not result.ok:
        return _quarantine(
            config, digest, path, profile, relpath, notifier,
            "validation_failed",
            f"{len(result.failures)} check(s) failed; the whole document was rejected",
            detail={
                "adapter": adapter.name,
                "fingerprint": layout,
                "failures": [
                    {"account": f.account, "check": f.check, "detail": f.detail}
                    for f in result.failures
                ],
                # The parsed rows themselves, so the report can point at the
                # transaction that matches the reconciliation difference.
                "accounts": _account_rows(parsed),
            },
        )

    record = DocumentRecord(
        sha256=digest,
        institution=parsed.institution,
        doc_type=parsed.doc_type,
        period_start=parsed.period_start,
        period_end=parsed.period_end,
        statement_date=parsed.statement_date,
        storage_path=storage_path,
        parse_status=result.status,
        source_profile=profile,
        source_relpath=relpath,
        parser_version=parsed.parser_version,
        layout_fingerprint=layout,
        fetched_at=datetime.now(timezone.utc),
    )
    balances, txns = _to_records(parsed)
    inserted = repository.insert_document(context, record, balances, txns)

    return IngestOutcome(
        sha256=digest,
        path=str(path),
        status=result.status,
        documents=1,
        accounts=inserted.accounts,
        txns_inserted=inserted.txns_inserted,
        txns_skipped=inserted.txns_skipped,
        detail={
            "adapter": adapter.name,
            "institution": parsed.institution,
            "unverified_accounts": list(result.unverified_accounts),
        },
    )


def _to_records(parsed: ParsedDocument) -> tuple[list[BalanceRecord], list[TxnRecord]]:
    balances: list[BalanceRecord] = []
    txns: list[TxnRecord] = []

    for account in parsed.accounts:
        key = AccountRecord(
            institution=parsed.institution,
            account_ref_masked=account.account_ref_masked,
            currency=account.currency,
            kind=account.kind,
            sub_account_label=account.sub_account_label,
        )
        balances.append(BalanceRecord(
            account_key=key,
            opening_balance_minor=account.opening_balance_minor,
            closing_balance_minor=account.closing_balance_minor,
        ))

        seqs = assign_seq(account.txns)
        for txn, seq in zip(account.txns, seqs, strict=True):
            description_norm = normalise_description(txn.description_raw)
            txns.append(TxnRecord(
                account_key=key,
                posted_date=txn.posted_date,
                value_date=txn.value_date,
                amount_minor=txn.amount_minor,
                currency=txn.currency,
                description_raw=txn.description_raw,
                description_norm=description_norm,
                counterparty_norm=normalise_counterparty(txn.description_raw),
                fx_amount_minor=txn.fx_amount_minor,
                fx_currency=txn.fx_currency,
                fx_rate=txn.fx_rate,
                seq=seq,
                dedupe_key=dedupe_key(
                    account.natural_key, txn.posted_date, txn.amount_minor, description_norm, seq
                ),
            ))

    return balances, txns


def _account_rows(parsed: ParsedDocument) -> dict:
    """Parsed transactions per account, for the reconciliation report."""
    rows = {}
    for account in parsed.accounts:
        name = (f"{account.account_ref_masked}/{account.sub_account_label}"
                if account.sub_account_label else account.account_ref_masked)
        rows[name] = [
            {
                "posted_date": t.posted_date.isoformat(),
                "description": t.description_raw,
                "amount_minor": t.amount_minor,
            }
            for t in account.txns
        ]
    return rows


def _provenance(config: Config, path: Path) -> tuple[str, str]:
    """Recover which profile a staged file came from, and its relative path."""
    try:
        relative = path.resolve().relative_to(config.inbox_dir.resolve())
    except ValueError:
        return "dummy", path.name
    parts = relative.parts
    if parts and parts[0] in ("dummy", "prod"):
        return parts[0], Path(*parts[1:]).as_posix() if len(parts) > 1 else path.name
    return "dummy", relative.as_posix()


def _institution_hint(relpath: str) -> str:
    """First path segment, used only to look up a configured PDF password."""
    head = Path(relpath).parts
    return head[0] if head else ""


def _quarantine(config, digest, path, profile, relpath, notifier, failure_class, message, *, detail=None, error=None) -> IngestOutcome:
    reason_path = quarantine.write_reason(
        config.quarantine_dir,
        digest,
        failure_class=failure_class,
        message=message,
        source_relpath=relpath,
        detail=detail,
        error=error,
    )
    severity = SEVERITY_WARNING if failure_class == "unknown_layout" else SEVERITY_ERROR
    notifier.notify(
        "Document quarantined",
        f"{relpath}: {message}",
        severity=severity,
        sha256=digest,
        reason=str(reason_path),
    )
    log.warning("quarantined %s (%s): %s", relpath, failure_class, message)
    return IngestOutcome(
        sha256=digest,
        path=str(path),
        status=STATUS_QUARANTINED_OUT,
        reason=failure_class,
        detail={"message": message, "reason_file": str(reason_path)},
    )
