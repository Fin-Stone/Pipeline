"""finstone command line.

argparse rather than a CLI framework: one fewer dependency for a surface this
small.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import PROFILE_DUMMY, PROFILES, ConfigError, load_config
from .parsers import fingerprint as fingerprinting
from .parsers import pdfio
from .parsers.registry import build_default_registry
from .pipeline import quarantine
from .pipeline.ingest import ingest_inbox, reparse
from .pipeline.stage import stage
from .storage.factory import (
    SchemaOutOfDate,
    build_blob_store,
    build_notifier,
    build_repository,
    check_schema,
)


def _configure_logging(verbose: bool) -> None:
    """Quiet by default.

    A run over a few dozen statements used to emit two stderr lines per
    failure, burying the one number that matters. Every failure is already
    recorded in its reason file and rendered into the run's report, so the
    log adds nothing at the terminal. `-v` brings it back.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("finstone").setLevel(logging.DEBUG if verbose else logging.ERROR + 1)
    # Reading the migration head to check the schema version makes alembic
    # narrate its plugin setup. Migrations are run through the `alembic`
    # command, which has its own logging config; nothing alembic says belongs
    # in this tool's output.
    logging.getLogger("alembic").setLevel(logging.WARNING)


def cmd_stage(args) -> int:
    config = load_config()
    repository = build_repository(config)
    try:
        check_schema(repository)
        context = repository.resolve_context(config.tenant_for(args.profile), config.member_email)
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
        check_schema(repository)
        context = repository.resolve_context(config.tenant_for(args.profile), config.member_email)
        summary = ingest_inbox(
            config, context, repository, build_blob_store(config), build_notifier(config),
            profile=args.profile,
        )
    finally:
        repository.close()
    _print_summary(summary, config, args.profile)
    return 0


def cmd_run(args) -> int:
    config = load_config()
    repository = build_repository(config)
    try:
        check_schema(repository)
        context = repository.resolve_context(config.tenant_for(args.profile), config.member_email)
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
    _print_summary(summary, config, args.profile)
    return 0


def _print_summary(summary, config=None, profile=PROFILE_DUMMY) -> None:
    """Three lines: what happened, what failed, where the detail is.

    Anything longer gets skimmed. The per-document detail lives in the report
    file, which is the thing worth sending on.
    """
    if summary.processed == 0:
        print("nothing to ingest: the inbox is empty for this profile")
        return

    parts = [f"{summary.imported} imported"]
    if summary.unverified:
        parts.append(f"{summary.unverified} unverified")
    if summary.duplicates:
        parts.append(f"{summary.duplicates} already imported")
    if summary.quarantined:
        parts.append(f"{summary.quarantined} quarantined")
    print(f"processed {summary.processed}: {', '.join(parts)}; "
          f"{summary.txns_inserted} transaction(s) inserted")

    if summary.healed:
        print(f"  healed:   {summary.healed} document(s) matched a renamed vendor string "
              f"and reconciled; see `finstone learned`")

    if not summary.quarantined:
        return

    reasons = Counter(o.reason or "unknown" for o in summary.outcomes if o.status == "quarantined")
    print("  failures: " + ", ".join(f"{count} {reason}" for reason, count in reasons.most_common()))

    if config is not None:
        written = _write_run_report(config, summary, profile)
        if written is not None:
            print(f"  report:   {written}")


def _write_run_report(config, summary, profile) -> Path | None:
    """Write this run's failures to a file, redacted and ready to send.

    Redacted by default: the whole point is that it can be handed to someone
    without handing over the statements.
    """
    from .pipeline import diagnostics
    from .pipeline.quarantine import REASON_SUFFIX

    digests = [o.sha256 for o in summary.outcomes if o.status == "quarantined"]
    quarantine_dir = config.quarantine_dir_for(profile)
    blocks = []
    for digest in digests:
        reason_file = quarantine_dir / f"{digest}{REASON_SUFFIX}"
        if not reason_file.exists():
            continue
        payload = json.loads(reason_file.read_text(encoding="utf-8"))
        blocks.append(diagnostics.render_reason(payload, redact=True))

    if not blocks:
        return None

    reports_dir = config.reports_dir_for(profile)
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = reports_dir / f"{stamp}-failures.txt"
    header = (
        f"finstone failure report  {datetime.now().isoformat(timespec='seconds')}\n"
        f"{len(blocks)} of {summary.processed} documents failed.\n"
        "Redacted: letters masked, digits kept. Safe to send as-is.\n\n"
    )
    target.write_text(header + "\n\n".join(blocks) + "\n", encoding="utf-8")
    return target


def cmd_fingerprint(args) -> int:
    """Show how a document is identified, and which adapter claims it."""
    from .pipeline import diagnostics

    path = Path(args.path)
    config = load_config()
    document = pdfio.load(path, password=config.pdf_password_for(path.parent.name))
    registry = build_default_registry(config.learned_rules_path)

    described = fingerprinting.describe(document)
    if args.redact:
        # Header lines are lifted straight off page 1 and routinely contain the
        # customer's name and address, which is exactly why they no longer
        # decide routing.
        described["label_lines"] = [
            diagnostics.describe(line, True) for line in described["label_lines"]
        ]
    try:
        described["routes_to"] = registry.resolve(document).name
    except Exception as exc:
        described["routes_to"] = None
        described["why_not"] = str(exc)
    described["candidates"] = registry.explain(document)

    print(json.dumps(described, indent=2, ensure_ascii=False))
    if described["routes_to"] is None:
        print(
            "\nNo adapter claims this document. `candidates` lists what each one "
            "required and did not find.",
            file=sys.stderr,
        )
    return 0


def cmd_status(args) -> int:
    config = load_config()
    repository = build_repository(config)
    try:
        check_schema(repository)
        context = repository.resolve_context(config.tenant_for(args.profile), config.member_email)
        counts = repository.counts(context)
    finally:
        repository.close()
    print(f"profile              {args.profile}")
    print(f"tenant               {config.tenant_for(args.profile)}")
    print(f"documents            {counts.documents}")
    print(f"  unverified         {counts.documents_unverified}")
    print(f"accounts             {counts.accounts}")
    print(f"transactions         {counts.txns}")
    print(f"quarantined          {counts.documents_quarantined}")
    print(f"quarantine reports   {quarantine.depth(config.quarantine_dir_for(args.profile))}")
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
        print("\n  finstone quarantine              which files they are")
        print("  finstone report                  why each one failed")
        print("  finstone reparse --quarantined   retry after adding an adapter")
    return 0


def cmd_reparse(args) -> int:
    """Replay documents from the immutable store after an adapter fix."""
    config = load_config()
    repository = build_repository(config)
    try:
        check_schema(repository)
        context = repository.resolve_context(config.tenant_for(args.profile), config.member_email)
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
    _print_summary(summary, config, args.profile)
    return 0


def cmd_learned(args) -> int:
    """List vendor strings the pipeline proved belong to an adapter."""
    from .parsers.learned import LearnedRules

    config = load_config()
    learned = LearnedRules.load(config.learned_rules_path)
    if not learned:
        print("nothing learned yet")
        return 0

    print(f"{len(learned)} learned vendor string(s)  ({config.learned_rules_path})\n")
    for vendor in learned.vendors:
        print(f"{vendor.adapter}")
        print(f"  producer  {vendor.producer or '(none)'}")
        print(f"  creator   {vendor.creator or '(none)'}")
        print(f"  learned   {vendor.learned_at}")
        evidence = vendor.evidence or {}
        print(f"  proved by {evidence.get('proved_by_sha256', '?')[:16]}  "
              f"({evidence.get('accounts_reconciled', '?')} account(s) reconciled, "
              f"{evidence.get('transactions', '?')} transactions)")
        print()
    return 0


def cmd_adapters(args) -> int:
    """List what each adapter claims, so routing is inspectable."""
    for registration in build_default_registry().registrations():
        signature = registration.signature
        print(f"{registration.adapter.name}@{registration.adapter.version}")
        print(f"  producer   {signature.producer or '(any)'}")
        size = signature.page_size
        print(f"  page size  {f'{size[0]:.0f}x{size[1]:.0f}' if size else '(any)'}")
        print("  requires   " + "\n             ".join(signature.requires))
        print()
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
    registry = build_default_registry(config.learned_rules_path)

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
        adapter = registry.resolve(document)
    except Exception as exc:
        print("adapter     NONE\n")
        print(diagnostics.RULE)
        print("UNROUTABLE")
        print(diagnostics.RULE)
        print(f"{exc}\n")
        print("This document would be quarantined. Each adapter below shows what it")
        print("required and did not find:\n")
        for candidate in registry.explain(document):
            print(f"  {candidate['adapter']}")
            print(f"    expects producer  {candidate['expects_producer']}  "
                  f"(this document: {fingerprinting.normalise_producer(document.producer) or '(none)'})")
            for missing in candidate["missing"]:
                print(f"    missing line      {diagnostics.describe(missing, args.redact)}")
        # Lifted straight off page 1, so routinely containing the customer's
        # name and address — which is exactly why they no longer decide
        # routing, and why they are redacted here.
        print("\nHeader lines this document actually carries:")
        for label in fingerprinting.describe(document)["label_lines"]:
            print(f"    {diagnostics.describe(label, args.redact)}")
        print("\nTo add an adapter, pick the lines above that are the *bank's* words —")
        print("never the customer's — and declare them as its LayoutSignature.")
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


def _warn_about_unscoped(config) -> None:
    """Point at reason files written before quarantine was split by tenant.

    They sit loose at the root, and nothing in them says which tenant they
    belong to — the payload only started recording the profile at the split. So
    they are not silently adopted into a tenant that may not own them.

    Nothing is lost by discarding them: a reason file is derived state. The
    ledger holds the authoritative quarantined status and the store holds the
    bytes, so `reparse` rebuilds the reason in the right place.
    """
    from .pipeline.quarantine import REASON_SUFFIX

    loose = sorted(config.quarantine_dir.glob(f"*{REASON_SUFFIX}"))
    if not loose:
        return
    # The listing above went to stdout; without this the note lands before it.
    sys.stdout.flush()
    print(
        f"\n  note: {len(loose)} reason file(s) at {config.quarantine_dir} predate the split\n"
        "  by tenant and are not listed above. Rebuild them where they belong with\n"
        "  `finstone reparse --quarantined --profile <profile>`, then delete them.",
        file=sys.stderr,
    )


def _why_it_failed(payload: dict, redact: bool) -> str:
    """The shortest true answer to "what went wrong", for one line of a table."""
    from .pipeline import diagnostics

    checks = [
        failure["check"]
        for failure in payload.get("detail", {}).get("failures", [])
        if failure.get("check")
    ]
    if checks:
        # Check names are this codebase's own words, never the document's, so
        # they stay legible under --redact. Several accounts can fail the same
        # one; fromkeys dedupes without losing the order they were found in.
        return ",".join(dict.fromkeys(checks))

    if payload.get("failure_class") in ("unknown_layout", "ambiguous_layout"):
        # No check ran, because nothing was read out of the document at all.
        # The class already says that; the message only restates it.
        return ""

    return diagnostics.describe(
        textwrap.shorten(payload.get("message") or "", 44, placeholder="..."), redact,
    )


def cmd_quarantine(args) -> int:
    """List what is quarantined, and optionally copy the originals out.

    `finstone report` explains *why* each document failed. This answers the
    question that comes first — which files they are — because the report a run
    writes is redacted by design, and the store names originals by digest. Both
    are correct, and between them an operator could not find the file to open.
    """
    from .pipeline import diagnostics
    from .pipeline.quarantine import REASON_SUFFIX

    config = load_config()
    quarantine_dir = config.quarantine_dir_for(args.profile)
    reasons = sorted(quarantine_dir.glob(f"*{REASON_SUFFIX}"))
    if not reasons:
        print(f"quarantine is empty for profile {args.profile}")
        _warn_about_unscoped(config)
        return 0

    header = ("sha256", "class", "check", "source")
    rows = []
    for reason_file in reasons:
        payload = json.loads(reason_file.read_text(encoding="utf-8"))
        rows.append((
            payload.get("sha256", "")[:8],
            payload.get("failure_class") or "",
            _why_it_failed(payload, args.redact),
            diagnostics.describe(payload.get("source_relpath") or "", args.redact),
        ))

    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(3)]

    def line(row):
        return "  ".join(row[i].ljust(widths[i]) for i in range(3)) + "  " + row[3]

    print(f"{len(rows)} quarantined   profile {args.profile}   {quarantine_dir}\n")
    print(line(header))
    for row in rows:
        print(line(row))

    if not args.export:
        print("\n  finstone quarantine --export   copy the originals out to open them")
        print("  finstone report                why each one failed")
        _warn_about_unscoped(config)
        return 0

    files_dir = config.quarantine_files_dir_for(args.profile)
    written = quarantine.export_originals(quarantine_dir, files_dir, build_blob_store(config))
    print(f"\n{len(written)} original(s) -> {files_dir}")
    for path in written:
        print(f"  {diagnostics.describe(path.name, args.redact)}")
    if len(written) < len(rows):
        print(f"  {len(rows) - len(written)} not exported: no original in the store")
    _warn_about_unscoped(config)
    return 0


def cmd_report(args) -> int:
    """Render every quarantined document as a readable, pasteable report."""
    from .pipeline import diagnostics
    from .pipeline.quarantine import REASON_SUFFIX

    config = load_config()
    reasons = sorted(config.quarantine_dir_for(args.profile).glob(f"*{REASON_SUFFIX}"))
    if not reasons:
        print(f"quarantine is empty for profile {args.profile}")
        _warn_about_unscoped(config)
        return 0

    blocks = [
        diagnostics.render_reason(
            json.loads(reason_file.read_text(encoding="utf-8")), redact=args.redact
        )
        for reason_file in reasons
    ]

    separator = "\n\n"
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(separator.join(blocks) + "\n", encoding="utf-8")
        print(f"{len(blocks)} failure(s) written to {target}")
        return 0

    print(separator.join(blocks))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finstone", description="Self-hosted finance pipeline")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("stage", help="copy uploads/<profile> into data/inbox")
    p.add_argument("--profile", choices=PROFILES, default=PROFILE_DUMMY)
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser("ingest", help="process data/inbox for one profile")
    p.add_argument(
        "--profile", choices=PROFILES, default=PROFILE_DUMMY,
        help="which profile's inbox to process; also selects its tenant",
    )
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("run", help="stage then ingest")
    p.add_argument("--profile", choices=PROFILES, default=PROFILE_DUMMY)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("fingerprint", help="show how a document is identified and routed")
    p.add_argument("path")
    p.add_argument("--redact", action="store_true", help="mask header lines before printing")
    p.set_defaults(func=cmd_fingerprint)

    p = sub.add_parser("status", help="ledger and quarantine counts for one profile")
    p.add_argument("--profile", choices=PROFILES, default=PROFILE_DUMMY)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "reparse",
        help="replay documents from the store after an adapter fix",
    )
    p.add_argument("--profile", choices=PROFILES, default=PROFILE_DUMMY)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--sha256", help="one document")
    group.add_argument("--quarantined", action="store_true", help="every quarantined document")
    p.set_defaults(func=cmd_reparse)

    p = sub.add_parser("learned", help="vendor strings proven to belong to an adapter")
    p.set_defaults(func=cmd_learned)

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

    p = sub.add_parser("quarantine", help="list quarantined documents and which files they are")
    p.add_argument("--profile", choices=PROFILES, default=PROFILE_DUMMY)
    p.add_argument(
        "--export", action="store_true",
        help="copy the originals out of the store under openable names",
    )
    p.add_argument("--redact", action="store_true", help="mask filenames, for pasting elsewhere")
    p.set_defaults(func=cmd_quarantine)

    p = sub.add_parser("report", help="render every quarantined document as a readable report")
    p.add_argument("--profile", choices=PROFILES, default=PROFILE_DUMMY)
    p.add_argument("--redact", action="store_true", help="mask descriptions and references")
    p.add_argument("--out", help="write to a file instead of stdout")
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
    except SchemaOutOfDate as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
