"""Human-readable failure reports.

The design goal is specific: **a failure must be diagnosable from a pasted
terminal report or a screenshot, without anyone sending the statement.**
Statements are financial documents; asking for one in order to debug a parser
is both a privacy problem and a slow feedback loop.

So a report carries the things needed to fix an adapter — the offending line,
how it was split into columns, its neighbours for context, and the exact
reconciliation arithmetic — and nothing that requires opening the PDF.

Amounts are shown because reconciliation cannot be debugged without them.
`redact=True` masks descriptions and account references while keeping every
number and the layout structure intact, for pasting a failure from a real
statement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RULE = "=" * 78
THIN = "-" * 78


def _money(minor: int | None, *, width: int = 14) -> str:
    if minor is None:
        return "—".rjust(width)
    return f"{minor / 100:,.2f}".rjust(width)


def _mask(text: str) -> str:
    """Keep shape, drop content: letters to x, digits kept (they are the evidence)."""
    return re.sub(r"[A-Za-z]", "x", text or "")


def _describe(text: str, redact: bool) -> str:
    return _mask(text) if redact else text


@dataclass(frozen=True, slots=True)
class Neighbourhood:
    """Lines around a failure, so column drift is visible in the report."""

    lines: tuple[tuple[int, float, str], ...]
    highlight_y: float | None = None


def parse_failure_report(
    *,
    filename: str,
    sha256: str,
    message: str,
    adapter: str | None = None,
    fingerprint: str | None = None,
    context: dict | None = None,
    neighbourhood: Neighbourhood | None = None,
    redact: bool = False,
) -> str:
    context = context or {}
    out = [RULE, "PARSE FAILURE", RULE]
    out.append(f"file        {_describe(filename, redact)}")
    out.append(f"sha256      {sha256}")
    if adapter:
        out.append(f"adapter     {adapter}")
    if fingerprint:
        out.append(f"layout      {fingerprint}")
    out.append(f"message     {message}")

    if context.get("failed_on"):
        out.append(f"failed on   {context['failed_on']}")

    if context.get("raw_line") is not None:
        out += ["", THIN, f"OFFENDING LINE   page {context.get('page', '?')}, y={context.get('y', '?')}", THIN]
        out.append(f"  raw      {_describe(context['raw_line'], redact)}")
        columns = context.get("columns") or {}
        if columns:
            out.append("  columns  " + "".join(
                f"{name}={_describe(value, redact)!r}  " for name, value in columns.items()
            ))
            out.append("")
            out.append("  If a value is in the wrong column above, the column bands are")
            out.append("  misaligned for this layout. If the columns look right, the")
            out.append("  problem is in how that value is read.")

    if neighbourhood and neighbourhood.lines:
        out += ["", THIN, "SURROUNDING LINES", THIN]
        for page, y, text in neighbourhood.lines:
            marker = " <== here" if neighbourhood.highlight_y is not None and abs(y - neighbourhood.highlight_y) < 0.5 else ""
            out.append(f"  p{page} y={y:7.1f} | {_describe(text, redact)[:88]}{marker}")

    out += ["", "Paste this whole block to report the failure. It contains no page images", "and no data beyond the lines shown.", RULE]
    return "\n".join(out)


def reconciliation_report(
    *,
    filename: str,
    sha256: str,
    adapter: str | None,
    failures: list[dict],
    accounts: dict | None = None,
    redact: bool = False,
) -> str:
    """Explain a reconciliation failure as arithmetic, not as a boolean.

    The difference between expected and stated is the single most useful number
    for debugging: it is very often exactly one transaction, which points
    straight at a row that was missed, duplicated or given the wrong sign.
    """
    out = [RULE, "RECONCILIATION FAILURE", RULE]
    out.append(f"file        {_describe(filename, redact)}")
    out.append(f"sha256      {sha256}")
    if adapter:
        out.append(f"adapter     {adapter}")
    out.append("")
    out.append("The whole document was rejected. No transactions were written.")

    for failure in failures:
        detail = failure.get("detail", {})
        out += ["", THIN, f"ACCOUNT  {_describe(failure.get('account', '?'), redact)}", THIN]

        if failure.get("check") != "balance_reconciliation":
            out.append(f"  check failed: {failure.get('check')}")
            for key, value in detail.items():
                out.append(f"    {key}: {value}")
            continue

        opening = detail.get("opening_minor")
        total = detail.get("transactions_sum_minor")
        expected = detail.get("expected_closing_minor")
        stated = detail.get("stated_closing_minor")
        difference = detail.get("difference_minor")

        label_width = 34
        out.append(f"  {'opening balance':<{label_width}}{_money(opening)}")
        out.append(f"  {'+ sum of ' + str(detail.get('txn_count', 0)) + ' parsed transactions':<{label_width}}{_money(total)}")
        out.append(f"  {'= expected closing':<{label_width}}{_money(expected)}")
        out.append(f"  {'statement says closing':<{label_width}}{_money(stated)}")
        out.append(f"  {'':<{label_width}}{'-' * 14}")
        out.append(f"  {'difference':<{label_width}}{_money(difference)}")
        out.append("")
        out += _hints(difference, accounts, failure.get("account"), redact)

    out += ["", "Paste this whole block to report the failure.", RULE]
    return "\n".join(out)


def _hints(difference, accounts, account_name, redact) -> list[str]:
    if not difference:
        return ["  The totals agree; the failure is in another check."]

    hints = []
    rows = (accounts or {}).get(account_name) or []
    matches = [r for r in rows if abs(r.get("amount_minor", 0)) == abs(difference)]

    if matches:
        hints.append("  A parsed transaction matches the difference exactly:")
        for row in matches[:5]:
            hints.append(
                f"    {row.get('posted_date')}  "
                f"{_describe(str(row.get('description', ''))[:40], redact):<40} "
                f"{_money(row.get('amount_minor'))}"
            )
        hints.append("  That row is most likely counted twice, or carries the wrong sign.")
    elif difference % 2 == 0 and any(abs(r.get("amount_minor", 0)) == abs(difference) // 2 for r in rows):
        hints.append("  Half the difference matches a parsed row, which is the signature of")
        hints.append("  a sign error: a debit read as a credit is wrong by twice its value.")
    else:
        hints.append("  No parsed row matches the difference, so a row was probably missed")
        hints.append("  entirely — check for a transaction split across lines, or one that")
        hints.append("  fell outside the transaction table's column bands.")

    return hints


def build_neighbourhood(document, page: int | None, y: float | None, *, span: int = 6) -> Neighbourhood:
    """Lines around a position, as context for the report."""
    if page is None or y is None:
        return Neighbourhood(lines=())
    flat = [(line.page_number, line.top, line.text) for line in document.lines()]
    index = next((i for i, (p, t, _) in enumerate(flat) if p == page and abs(t - y) < 0.5), None)
    if index is None:
        return Neighbourhood(lines=())
    window = flat[max(0, index - span):index + span + 1]
    return Neighbourhood(lines=tuple(window), highlight_y=y)
