"""data/inbox -> content-addressed store -> parse -> validate -> ledger.

Each document is independent: a failure quarantines that document and the run
continues with the next one. Persistence for a document is a single database
transaction, so there is no such thing as a half-imported statement.

Three properties that are easy to get wrong and matter a lot:

- **The inbox is drained.** Once a document's bytes are safely in the
  content-addressed store, its inbox copy is removed. Otherwise the inbox
  grows without bound and every run reprocesses the entire history of
  everything ever dropped in.
- **Ingestion is scoped to a profile.** `--profile prod` must never touch
  `data/inbox/dummy/`. The prod/dummy boundary is worth exactly nothing if it
  is enforced at `stage` and then ignored here.
- **Failures are recorded, not just reported.** A quarantined document gets a
  `source_document` row with `parse_status='quarantined'`, so it is not
  re-parsed and re-alerted on every subsequent run. Replaying it after an
  adapter fix is what `reparse` is for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import DOCUMENT_EXTENSIONS, Config
from ..domain.dedupe import assign_seq, dedupe_key, sha256_file, statement_key
from ..domain.models import IngestOutcome, ParsedDocument
from ..domain.normalise import normalise_counterparty, normalise_description
from ..parsers import fingerprint as fingerprinting
from ..parsers import pdfio
from ..parsers.registry import (
    AdapterRegistry,
    AmbiguousLayout,
    LayoutError,
    UnknownLayout,
    build_default_registry,
)
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
    #: Documents accepted by proving an unrecognised vendor string was a
    #: rename. Surfaced because an automatic adaptation should be reviewable,
    #: not invisible.
    healed: int = 0
    outcomes: tuple[IngestOutcome, ...] = ()


def ingest_inbox(
    config: Config,
    context,
    repository,
    blob_store,
    notifier,
    registry: AdapterRegistry | None = None,
    profile: str | None = None,
) -> IngestSummary:
    """Process the inbox, or just one profile's subtree of it.

    `profile` scopes the walk to `data/inbox/<profile>/`. Passing it is what
    keeps a prod run from reprocessing dummy documents, and vice versa — the
    boundary enforced at `stage` has to be honoured here too or it means
    nothing.
    """
    registry = registry or build_default_registry(config.learned_rules_path)
    root = config.inbox_dir / profile if profile else config.inbox_dir
    outcomes = []

    files = [
        p for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix.lower() in DOCUMENT_EXTENSIONS
    ] if root.exists() else []
    for path in files:
        outcomes.append(ingest_file(config, context, path, repository, blob_store, notifier, registry))

    _prune_empty_dirs(config.inbox_dir)

    return IngestSummary(
        processed=len(outcomes),
        imported=sum(1 for o in outcomes if o.status == STATUS_IMPORTED),
        unverified=sum(1 for o in outcomes if o.status == "imported_unverified"),
        duplicates=sum(1 for o in outcomes if o.status == STATUS_DUPLICATE),
        quarantined=sum(1 for o in outcomes if o.status == STATUS_QUARANTINED_OUT),
        txns_inserted=sum(o.txns_inserted for o in outcomes),
        healed=sum(1 for o in outcomes if o.detail.get("healed_vendor")),
        outcomes=tuple(outcomes),
    )


def ingest_file(
    config: Config, context, path: Path, repository, blob_store, notifier,
    registry: AdapterRegistry, *, drain: bool = True,
    provenance: tuple[str, str] | None = None,
) -> IngestOutcome:
    """Ingest one file.

    `provenance` overrides the (profile, relative path) that would otherwise be
    derived from the file's location. A reparse reads from the content-addressed
    store, where the filename is a digest and the directory says nothing, so it
    passes the provenance recorded when the document was first staged.
    """
    digest = sha256_file(path)
    profile, relpath = provenance or _provenance(config, path)

    known = repository.get_document_status(context, digest)
    if known is not None:
        # Already seen — imported or quarantined. Either way, re-running the
        # same parse would produce the same result and the same alert, so the
        # inbox copy is dropped and the document is left as it stands.
        # `reparse` is how a fixed adapter replays it from the store.
        if drain:
            _drain(path)
        status = STATUS_QUARANTINED_OUT if known == STATUS_QUARANTINED else STATUS_DUPLICATE
        return IngestOutcome(
            sha256=digest, path=str(path), status=status,
            reason="already_quarantined" if known == STATUS_QUARANTINED else None,
        )

    # Store the original before anything else can fail. The bytes are what
    # make a later reparse possible, and what make draining the inbox safe.
    storage_path = blob_store.put(path, digest)

    def fail(failure_class: str, message: str, **kwargs) -> IngestOutcome:
        outcome = _quarantine(
            config, context, repository, digest, path, profile, relpath, notifier,
            failure_class, message, storage_path=storage_path, **kwargs
        )
        if drain:
            _drain(path)
        return outcome

    try:
        document = pdfio.load(path, password=config.pdf_password_for(_institution_hint(relpath)))
    except Exception as exc:
        return fail("unreadable_document", f"cannot open {path.name}", error=exc)

    try:
        layout = fingerprinting.fingerprint_pdf(document)
    except Exception as exc:
        return fail("fingerprint_failed", f"cannot fingerprint {path.name}", error=exc)

    healed_from = None
    try:
        adapter = registry.resolve(document)
    except UnknownLayout as exc:
        # An unrecognised vendor string is treated as a rename to be proven
        # or rejected, not as an unknown format. See _attempt_heal.
        healed = _attempt_heal(config, document, path, registry, notifier)
        if healed is None:
            return fail(
                "unknown_layout", str(exc),
                detail={**fingerprinting.describe(document),
                        "candidates": registry.explain(document)},
                error=exc,
            )
        adapter, healed_from = healed
    except LayoutError as exc:
        # The designed outcome for a document no adapter claims — or one that
        # several claim, which means the signatures are wrong and picking a
        # winner would be a guess. `explain` records which required line each
        # adapter was missing, which is the actionable half of the failure.
        return fail(
            "ambiguous_layout" if isinstance(exc, AmbiguousLayout) else "unknown_layout",
            str(exc),
            detail={**fingerprinting.describe(document), "candidates": registry.explain(document)},
            error=exc,
        )

    try:
        parsed = adapter.parse(path, password=config.pdf_password_for(adapter.institution))
    except ParseError as exc:
        # Carry the adapter's own account of *where* it failed, plus the lines
        # around it, so the failure can be diagnosed from the report alone.
        # Named `where` rather than `context`, which is the tenant.
        where = dict(getattr(exc, "context", {}) or {})
        return fail(
            "parse_failed", str(exc),
            detail={
                "adapter": adapter.name,
                "fingerprint": layout,
                "context": where,
                "neighbourhood": [
                    {"page": p, "y": round(y, 1), "text": t}
                    for p, y, t in diagnostics.build_neighbourhood(
                        document, where.get("page"), where.get("y")
                    ).lines
                ],
            },
            error=exc,
        )
    except Exception as exc:
        return fail(
            "parser_crashed", f"{adapter.name} raised {type(exc).__name__}",
            detail={"adapter": adapter.name, "fingerprint": layout}, error=exc,
        )

    result = validate(parsed, amount_ceiling_minor=config.amount_ceiling_minor)
    if not result.ok:
        return fail(
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
            institution=parsed.institution,
            doc_type=parsed.doc_type,
            period_start=parsed.period_start,
            period_end=parsed.period_end,
        )

    # Identity is the statement — one per account per period — not the file.
    # A re-downloaded, re-saved or renamed PDF is the same statement, and in a
    # household where two members both upload a shared account's statement it
    # must be recognised as one rather than importing a second document whose
    # every row then deduplicates away.
    key = statement_key(
        parsed.doc_type, parsed.period_start, parsed.period_end,
        [a.account_ref_masked for a in parsed.accounts],
    )
    balances, txns = _to_records(parsed)

    existing = repository.find_by_statement_key(context, key)
    if existing is not None:
        already = repository.dedupe_keys_for_document(context, existing["id"])
        arriving = {t.dedupe_key for t in txns}
        if already == arriving:
            # The same statement arriving again. Nothing to add, and nothing
            # wrong: this is the expected outcome for a shared account.
            if drain:
                _drain(path)
            return IngestOutcome(
                sha256=digest, path=str(path), status=STATUS_DUPLICATE,
                reason="same_statement",
                detail={"already_held_as": existing["source_relpath"]},
            )
        # Same account and period, different contents. That is a reissued or
        # corrected statement, or a parse that disagrees with itself — either
        # way something the operator should see rather than have resolved
        # silently in one direction.
        return fail(
            "conflicting_statement",
            "another document already holds this account's statement for this period, "
            "with different transactions",
            detail={
                "adapter": adapter.name,
                "already_held_as": existing["source_relpath"],
                "already_held_sha256": existing["sha256"],
                "transactions_here": len(arriving),
                "transactions_there": len(already),
                "only_here": len(arriving - already),
                "only_there": len(already - arriving),
            },
            institution=parsed.institution,
            doc_type=parsed.doc_type,
            period_start=parsed.period_start,
            period_end=parsed.period_end,
        )

    record = DocumentRecord(
        sha256=digest,
        statement_key=key,
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
    inserted = repository.insert_document(context, record, balances, txns)

    # The bytes are in the store and the rows are in the ledger; the inbox
    # copy has done its job.
    if drain:
        _drain(path)

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
            **({"healed_vendor": healed_from} if healed_from else {}),
        },
    )


def reparse(
    config: Config, context, repository, blob_store, notifier,
    registry: AdapterRegistry | None = None,
    *, sha256: str | None = None, quarantined_only: bool = False,
) -> IngestSummary:
    """Replay documents from the immutable store after an adapter fix.

    This is why originals are content-addressed and never mutated: a parser
    bugfix can be applied to years of history without re-downloading anything.
    Existing rows for the document are deleted first, so a reparse is a
    replacement rather than a second import.
    """
    registry = registry or build_default_registry(config.learned_rules_path)

    if sha256:
        targets = [d for d in repository.list_documents(context) if d["sha256"] == sha256]
        if not targets:
            raise LookupError(f"no document with sha256 {sha256} for this tenant")
    elif quarantined_only:
        targets = repository.list_documents(context, parse_status=STATUS_QUARANTINED)
    else:
        targets = repository.list_documents(context)

    outcomes = []
    for target in targets:
        stored = Path(target["storage_path"])
        if not stored.exists():
            log.error("original missing from the store for %s", target["sha256"])
            outcomes.append(IngestOutcome(
                sha256=target["sha256"], path=str(stored),
                status=STATUS_QUARANTINED_OUT, reason="original_missing",
            ))
            continue

        repository.delete_document(context, target["sha256"])
        quarantine.clear_reason(config.quarantine_dir, target["sha256"])
        # drain=False: the file being read *is* the stored original, and the
        # store is immutable.
        #
        # The provenance is carried over from the row just deleted. Without it
        # the document would be re-recorded against its own digest, because
        # that is its filename in the store — losing the operator's folder and
        # making `status` group failures by hash instead of by institution.
        outcomes.append(ingest_file(
            config, context, stored, repository, blob_store, notifier, registry, drain=False,
            provenance=(target["source_profile"], target["source_relpath"]),
        ))

    return IngestSummary(
        processed=len(outcomes),
        imported=sum(1 for o in outcomes if o.status == STATUS_IMPORTED),
        unverified=sum(1 for o in outcomes if o.status == "imported_unverified"),
        duplicates=sum(1 for o in outcomes if o.status == STATUS_DUPLICATE),
        quarantined=sum(1 for o in outcomes if o.status == STATUS_QUARANTINED_OUT),
        txns_inserted=sum(o.txns_inserted for o in outcomes),
        healed=sum(1 for o in outcomes if o.detail.get("healed_vendor")),
        outcomes=tuple(outcomes),
    )


def _drain(path: Path) -> None:
    """Remove a processed file from the inbox.

    Only ever called once the bytes are safely in the content-addressed store.
    Without this the inbox grows without bound and every run reprocesses
    everything ever dropped into it.
    """
    try:
        path.unlink()
    except OSError as exc:  # pragma: no cover - a locked file should not fail the run
        log.warning("could not remove %s from the inbox: %s", path, exc)


def _prune_empty_dirs(root: Path) -> None:
    """Tidy the directory skeleton the drained files leave behind."""
    if not root.exists():
        return
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            next(directory.iterdir())
        except StopIteration:
            directory.rmdir()
        except OSError:  # pragma: no cover
            pass


def _attempt_heal(config, document, path, registry, notifier):
    """Test whether an unrecognised vendor string is a rename.

    Institutions rename the tool that renders their statements. DBS's went
    "Quadient Group AG~Inspire" to "Quadient CXM AG~Inspire" to
    "Quadient~Inspire"; Trust's went "Skia/PDF m80" to "m141". Each time, every
    header line identifying the format still matched and only the vendor
    string had moved.

    So when a document matches an adapter's required header lines and differs
    *only* in producer or creator, that adapter is a hypothesis. It is tested
    by parsing the document and requiring the result to reconcile to the cent
    against the balances the statement states for itself.

    This is not the "never guess" rule being relaxed. Guessing is choosing
    without evidence; this proposes and then verifies against an oracle the
    document carries with it, and refuses in every case where the oracle is
    absent or ambiguous:

    - **No balances, no healing.** A statement that would import as
      `imported_unverified` has nothing to verify against, so it is never
      healed — that is exactly where a wrong adapter could pass unnoticed.
    - **Two adapters that both reconcile is a refusal**, not a tiebreak.
    - What was accepted is recorded, with the document that proved it, so the
      decision can be audited rather than taken on trust.
    """
    candidates = registry.rename_candidates(document)
    if not candidates:
        return None

    accepted = []
    for registration in candidates:
        adapter = registration.adapter
        try:
            parsed = adapter.parse(path, password=config.pdf_password_for(adapter.institution))
        except Exception:
            continue   # not this format; that is the hypothesis failing, not an error
        result = validate(parsed, amount_ceiling_minor=config.amount_ceiling_minor)
        # Every account must reconcile. `unverified_accounts` means at least
        # one had no balances to check, which is not proof.
        if result.ok and not result.unverified_accounts and parsed.accounts:
            accepted.append((adapter, parsed))

    if len(accepted) != 1:
        if accepted:
            log.warning(
                "refusing to heal %s: %d adapters reconcile, which is ambiguous",
                path.name, len(accepted),
            )
        return None

    adapter, parsed = accepted[0]
    producer = fingerprinting.normalise_producer(document.producer)
    creator = fingerprinting.normalise_producer(document.creator)
    registry.learned.record(
        adapter.name, producer, creator,
        evidence={
            "proved_by_sha256": sha256_file(path),
            "accounts_reconciled": len(parsed.accounts),
            "transactions": parsed.txn_count,
        },
    )
    notifier.notify(
        "Layout healed",
        f"{adapter.name} accepted a renamed vendor string "
        f"(producer={producer!r}, creator={creator!r}); the statement reconciles",
        severity=SEVERITY_WARNING,
    )
    return adapter, {"producer": producer, "creator": creator}


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
    """First path segment — how the operator filed it, not what was parsed."""
    head = Path(relpath).parts
    return head[0] if head else ""


def _doc_type_hint(relpath: str) -> str:
    """Second path segment, by the <bank>/<type>/ convention."""
    parts = Path(relpath).parts
    return parts[1] if len(parts) > 2 else "unknown"


def _quarantine(
    config, context, repository, digest, path, profile, relpath, notifier,
    failure_class, message, *, detail=None, error=None, storage_path=None,
    institution=None, doc_type=None, period_start=None, period_end=None,
) -> IngestOutcome:
    reason_path = quarantine.write_reason(
        config.quarantine_dir,
        digest,
        failure_class=failure_class,
        message=message,
        source_relpath=relpath,
        detail=detail,
        error=error,
    )

    # Record the failure in the ledger, not just on disk. Without this the
    # document is re-parsed and re-alerted on every subsequent run, which
    # turns a real signal into noise and eventually into something nobody
    # reads. `reparse` is how a fixed adapter replays it.
    if storage_path is not None:
        try:
            repository.insert_document(
                context,
                DocumentRecord(
                    sha256=digest,
                    # Provenance, not parsed content: for an unknown layout
                    # nothing has been read out of the document at all. The
                    # 'quarantined' status makes that unambiguous.
                    institution=institution or _institution_hint(relpath) or "unknown",
                    doc_type=doc_type or _doc_type_hint(relpath),
                    period_start=period_start,
                    period_end=period_end,
                    storage_path=storage_path,
                    parse_status=STATUS_QUARANTINED,
                    source_profile=profile,
                    source_relpath=relpath,
                    layout_fingerprint=(detail or {}).get("fingerprint"),
                    fetched_at=datetime.now(timezone.utc),
                ),
                [], [],
            )
        except Exception as exc:  # pragma: no cover - never let bookkeeping fail the run
            log.warning("could not record quarantine for %s: %s", relpath, exc)
    severity = SEVERITY_WARNING if failure_class == "unknown_layout" else SEVERITY_ERROR
    notifier.notify(
        "Document quarantined",
        f"{relpath}: {message}",
        severity=severity,
        sha256=digest,
        reason=str(reason_path),
    )
    log.debug("quarantined %s (%s): %s", relpath, failure_class, message)
    return IngestOutcome(
        sha256=digest,
        path=str(path),
        status=STATUS_QUARANTINED_OUT,
        reason=failure_class,
        detail={"message": message, "reason_file": str(reason_path)},
    )
