"""finstone command line.

argparse rather than a CLI framework: one fewer dependency for a surface this
small.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import PROFILE_DUMMY, PROFILES, ConfigError, load_config
from .parsers import fingerprint as fingerprinting
from .parsers import pdfio
from .parsers.registry import build_default_registry
from .pipeline import quarantine
from .pipeline.ingest import ingest_inbox, reparse
from .pipeline.stage import stage
from .storage.factory import build_blob_store, build_notifier, build_repository


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def cmd_stage(args) -> int:
    config = load_config()
    repository = build_repository(config)
    try:
        context = repository.resolve_context(config.tenant_slug, config.member_email)
        result = stage(config, args.profile, repository=repository, context=context)
    finally:
        repository.close()
    print(
        f"staged {result.staged} new file(s) from uploads/{result.profile} "
        f"({result.discovered} discovered, {result.already_known} already imported, "
        f"{result.already_staged} already in inbox)"
    )
    return 0


def cmd_ingest(args) -> int:
    config = load_config()
    repository = build_repository(config)
    try:
        context = repository.resolve_context(config.tenant_slug, config.member_email)
        summary = ingest_inbox(
            config, context, repository, build_blob_store(config), build_notifier(config),
            profile=args.profile,
        )
    finally:
        repository.close()
    _print_summary(summary)
    return 0


def cmd_run(args) -> int:
    config = load_config()
    repository = build_repository(config)
    try:
        context = repository.resolve_context(config.tenant_slug, config.member_email)
        staged = stage(config, args.profile, repository=repository, context=context)
        print(
            f"staged {staged.staged} new file(s) from uploads/{staged.profile} "
            f"({staged.discovered} discovered, {staged.already_known} already imported, "
            f"{staged.already_staged} already in inbox)"
        )
        summary = ingest_inbox(
            config, context, repository, build_blob_store(config), build_notifier(config),
            profile=args.profile,
        )
    finally:
        repository.close()
    _print_summary(summary)
    return 0


def _print_summary(summary) -> None:
    if summary.processed == 0:
        print("nothing to ingest: the inbox is empty for this profile")
        return
    print(
        f"processed {summary.processed}: {summary.imported} imported, "
        f"{summary.unverified} imported-unverified, {summary.duplicates} already imported, "
        f"{summary.quarantined} quarantined; {summary.txns_inserted} transaction(s) inserted"
    )
    for outcome in summary.outcomes:
        if outcome.status == "quarantined":
            note = " (already known)" if outcome.reason == "already_quarantined" else f"  ({outcome.reason})"
            print(f"  quarantined  {Path(outcome.path).name}{note}")


def cmd_fingerprint(args) -> int:
    path = Path(args.path)
    config = load_config()
    document = pdfio.load(path, password=config.pdf_password_for(path.parent.name))
    described = fingerprinting.describe(document)
    registry = build_default_registry()
    known = registry.known_fingerprints()
    described["registered_adapter"] = known.get(described["fingerprint"])
    print(json.dumps(described, indent=2, ensure_ascii=False))
    if described["registered_adapter"] is None:
        print(
            "\nNo adapter is registered for this layout. Add the fingerprint above to the "
            "FINGERPRINTS list of the matching adapter.",
            file=sys.stderr,
        )
    return 0


def cmd_status(args) -> int:
    config = load_config()
    repository = build_repository(config)
    try:
        context = repository.resolve_context(config.tenant_slug, config.member_email)
        counts = repository.counts(context)
    finally:
        repository.close()
    print(f"tenant               {config.tenant_slug}")
    print(f"documents            {counts.documents}")
    print(f"  unverified         {counts.documents_unverified}")
    print(f"accounts             {counts.accounts}")
    print(f"transactions         {counts.txns}")
    print(f"quarantined          {counts.documents_quarantined}")
    print(f"quarantine reports   {quarantine.depth(config.quarantine_dir)}")
    if counts.by_institution:
        print("\nimported by institution:")
        for name, count in sorted(counts.by_institution.items()):
            print(f"  {name:<24} {count}")
    if counts.quarantined_by_institution:
        # Grouped by the folder the file was in, not by anything parsed out of
        # it — for an unknown layout nothing was read from the document at all.
        print("\nquarantined by folder (nothing was parsed from these):")
        for name, count in sorted(counts.quarantined_by_institution.items()):
            print(f"  {name:<24} {count}")
        print("\n  finstone report                  why each one failed")
        print("  finstone reparse --quarantined   retry after adding an adapter")
    return 0


def cmd_reparse(args) -> int:
    """Replay documents from the immutable store after an adapter fix."""
    config = load_config()
    repository = build_repository(config)
    try:
        context = repository.resolve_context(config.tenant_slug, config.member_email)
        summary = reparse(
            config, context, repository, build_blob_store(config), build_notifier(config),
            sha256=args.sha256, quarantined_only=args.quarantined,
        )
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        repository.close()
    if summary.processed == 0:
        print("nothing to reparse")
        return 0
    _print_summary(summary)
    return 0


def cmd_adapters(args) -> int:
    for fp, name in build_default_registry().known_fingerprints().items():
        print(f"{fp}  {name}")
    return 0


def cmd_doctor(args) -> int:
    """Parse one document and explain exactly what happened, touching no database.

    The fastest debugging loop: point it at a statement that failed, paste the
    output. Nothing here needs the PDF itself to interpret.
    """
    from .domain.dedupe import sha256_file
    from .pipeline import diagnostics
    from .pipeline.validate import validate
    from .ports.parser import ParseError

    path = Path(args.path)
    if not path.exists():
        print(f"error: {path} does not exist", file=sys.stderr)
        return 2

    config = load_config()
    digest = sha256_file(path)
    registry = build_default_registry()

    try:
        document = pdfio.load(path, password=config.pdf_password_for(path.parent.name))
    except Exception as exc:
        print(f"cannot open {path.name}: {exc}", file=sys.stderr)
        return 1

    layout = fingerprinting.fingerprint_pdf(document)
    # Filenames carry the institution and the statement month, so they are
    # document-derived text and must honour --redact like everything else.
    print(f"file        {diagnostics.describe(path.name, args.redact)}")
    print(f"sha256      {digest}")
    print(f"pages       {len(document.pages)}")
    print(f"producer    {document.producer or '(none)'}")
    print(f"layout      {layout}")

    try:
        adapter = registry.resolve(layout)
    except Exception:
        print("adapter     NONE REGISTERED\n")
        print(diagnostics.RULE)
        print("UNKNOWN LAYOUT")
        print(diagnostics.RULE)
        print("This layout has no adapter, so the document would be quarantined.")
        print("To add one, put this fingerprint in the adapter's FINGERPRINTS list:\n")
        print(f"    {layout}\n")
        # These are lines lifted straight off page 1. They usually contain only
        # static layout labels, but a name or address line without digits in it
        # would be included verbatim, so they are redacted like any other
        # document text.
        print("Header lines the fingerprint was computed from:")
        for label in fingerprinting.describe(document)["label_lines"]:
            print(f"    {diagnostics.describe(label, args.redact)}")
        return 1

    print(f"adapter     {adapter.name}@{adapter.version}\n")

    try:
        parsed = adapter.parse(path, password=config.pdf_password_for(adapter.institution))
    except ParseError as exc:
        context = dict(getattr(exc, "context", {}) or {})
        print(diagnostics.parse_failure_report(
            filename=path.name,
            sha256=digest,
            message=str(exc),
            adapter=f"{adapter.name}@{adapter.version}",
            fingerprint=layout,
            context=context,
            neighbourhood=diagnostics.build_neighbourhood(
                document, context.get("page"), context.get("y")
            ),
            redact=args.redact,
        ))
        return 1

    result = validate(parsed, amount_ceiling_minor=config.amount_ceiling_minor)
    _print_parsed(parsed, redact=args.redact)

    if not result.ok:
        from .pipeline.ingest import _account_rows
        print()
        print(diagnostics.reconciliation_report(
            filename=path.name,
            sha256=digest,
            adapter=f"{adapter.name}@{adapter.version}",
            failures=[{"account": f.account, "check": f.check, "detail": f.detail} for f in result.failures],
            accounts=_account_rows(parsed),
            redact=args.redact,
        ))
        return 1

    print(f"\nRESULT: {result.status.upper()} — every check passed.")
    return 0


def _print_parsed(parsed, *, redact: bool) -> None:
    from .pipeline.diagnostics import THIN, _describe, _money

    print(f"period      {parsed.period_start} .. {parsed.period_end}")
    print(f"statement   {parsed.statement_date or '(none)'}")
    print(f"accounts    {len(parsed.accounts)}")
    for account in parsed.accounts:
        name = f"{account.account_ref_masked}" + (f" / {account.sub_account_label}" if account.sub_account_label else "")
        total = sum(t.amount_minor for t in account.txns)
        print()
        print(THIN)
        print(f"{_describe(name, redact)}   [{account.kind}, {account.currency}]")
        print(THIN)
        print(f"  opening {_money(account.opening_balance_minor)}")
        for txn in account.txns:
            fx = ""
            if txn.fx_amount_minor is not None:
                fx = f"   ({txn.fx_amount_minor / 100:,.2f} {txn.fx_currency} @ {txn.fx_rate})"
            print(f"  {txn.posted_date}  {_describe(txn.description_raw, redact)[:44]:<44}{_money(txn.amount_minor)}{fx}")
        print(f"  {'sum of ' + str(len(account.txns)) + ' rows':<56}{_money(total)}")
        print(f"  closing {_money(account.closing_balance_minor)}")
        if account.has_balances:
            ok = account.opening_balance_minor + total == account.closing_balance_minor
            print(f"  reconciles: {'YES' if ok else 'NO'}")
        else:
            print("  reconciles: N/A (statement states no balances)")


def cmd_report(args) -> int:
    """Render every quarantined document as a readable, pasteable report."""
    from .pipeline import diagnostics
    from .pipeline.quarantine import REASON_SUFFIX

    config = load_config()
    reasons = sorted(config.quarantine_dir.glob(f"*{REASON_SUFFIX}"))
    if not reasons:
        print("quarantine is empty")
        return 0

    for reason_file in reasons:
        payload = json.loads(reason_file.read_text(encoding="utf-8"))
        detail = payload.get("detail", {})
        name = payload.get("source_relpath") or reason_file.name
        failure_class = payload.get("failure_class")

        if failure_class == "validation_failed":
            print(diagnostics.reconciliation_report(
                filename=name,
                sha256=payload["sha256"],
                adapter=detail.get("adapter"),
                failures=detail.get("failures", []),
                accounts=detail.get("accounts"),
                redact=args.redact,
            ))
        elif failure_class == "unknown_layout":
            print(diagnostics.RULE)
            print("UNKNOWN LAYOUT")
            print(diagnostics.RULE)
            print(f"file        {diagnostics.describe(name, args.redact)}")
            print(f"layout      {detail.get('fingerprint')}")
            print(f"producer    {detail.get('producer') or '(none)'}")
            print("\nNo adapter is registered for this layout. Add the fingerprint above")
            print("to the matching adapter's FINGERPRINTS list.")
            print(diagnostics.RULE)
        else:
            context = detail.get("context", {})
            neighbourhood = diagnostics.Neighbourhood(
                lines=tuple((n["page"], n["y"], n["text"]) for n in detail.get("neighbourhood", [])),
                highlight_y=context.get("y"),
            )
            print(diagnostics.parse_failure_report(
                filename=name,
                sha256=payload["sha256"],
                message=payload.get("message", ""),
                adapter=detail.get("adapter"),
                fingerprint=detail.get("fingerprint"),
                context=context,
                neighbourhood=neighbourhood,
                redact=args.redact,
            ))
        print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finstone", description="Self-hosted finance pipeline")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("stage", help="copy uploads/<profile> into data/inbox")
    p.add_argument("--profile", choices=PROFILES, default=PROFILE_DUMMY)
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser("ingest", help="process data/inbox")
    p.add_argument(
        "--profile", choices=PROFILES, default=None,
        help="restrict to one profile's inbox subtree; omit to process all of it",
    )
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("run", help="stage then ingest")
    p.add_argument("--profile", choices=PROFILES, default=PROFILE_DUMMY)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("fingerprint", help="print a document's layout fingerprint")
    p.add_argument("path")
    p.set_defaults(func=cmd_fingerprint)

    p = sub.add_parser("status", help="ledger and quarantine counts")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "reparse",
        help="replay documents from the store after an adapter fix",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--sha256", help="one document")
    group.add_argument("--quarantined", action="store_true", help="every quarantined document")
    p.set_defaults(func=cmd_reparse)

    p = sub.add_parser("adapters", help="list registered layouts")
    p.set_defaults(func=cmd_adapters)

    p = sub.add_parser(
        "doctor",
        help="parse one document and explain the result; the debugging entry point",
    )
    p.add_argument("path")
    p.add_argument(
        "--redact", action="store_true",
        help="mask descriptions and references, keeping amounts and structure, "
             "so a failure on a real statement is safe to paste",
    )
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("report", help="render every quarantined document as a readable report")
    p.add_argument("--redact", action="store_true", help="mask descriptions and references")
    p.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
