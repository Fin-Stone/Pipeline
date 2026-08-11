"""Date parsing, including the year-less dates statements are full of.

Trust prints transaction dates as "01 Jun" with no year. The year has to come
from the statement period, and getting it wrong at a December/January boundary
silently files transactions twelve months away from where they belong. This
module resolves it once, and raises rather than guessing when it cannot.
"""

from __future__ import annotations

import re
from datetime import date

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DAY_MONTH_YEAR = re.compile(r"^\s*(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})\s*$")
#: "01/12/2021". Day first — see parse_numeric_date.
_NUMERIC = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\s*$")
_DAY_MONTH = re.compile(r"^\s*(\d{1,2})\s+([A-Za-z]{3,9})\.?\s*$")
#: "20/01" — day and month with no year at all. OCBC prints card transaction
#: dates this way. Day first, for the reason `parse_numeric_date` gives.
_DAY_MONTH_NUMERIC = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})\s*$")


class DateParseError(ValueError):
    """Raised when a date cannot be read, or when its year is ambiguous."""


def _month(name: str) -> int:
    key = name.strip().lower()[:4]
    if key in _MONTHS:
        return _MONTHS[key]
    key = key[:3]
    if key in _MONTHS:
        return _MONTHS[key]
    raise DateParseError(f"unknown month name {name!r}")


def _day_and_month(text: str) -> tuple[int, int] | None:
    """Day and month from a year-less date, in either form statements print.

    "01 Jun" and "20/01" are the same problem — the year is missing and has to
    come from the statement period — so they resolve through one path rather
    than each institution inventing its own.
    """
    m = _DAY_MONTH.match(text)
    if m:
        return int(m.group(1)), _month(m.group(2))
    m = _DAY_MONTH_NUMERIC.match(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def parse_full_date(text: str) -> date:
    """Parse "3 Aug 2025" / "31 July 2025"."""
    m = _DAY_MONTH_YEAR.match(text)
    if not m:
        raise DateParseError(f"cannot read {text!r} as a full date")
    day, month_name, year = m.groups()
    try:
        return date(int(year), _month(month_name), int(day))
    except ValueError as exc:
        raise DateParseError(f"{text!r} is not a real date") from exc


def parse_numeric_date(text: str) -> date:
    """Parse "01/12/2021", day first.

    Day-first because every institution in this corpus is Singaporean and
    prints DD/MM/YYYY. Ambiguity is *not* resolved by falling back to
    month-first: "01/12" quietly becoming 12 January would be invisible and
    wrong, and a wrong date in a ledger is worse than a rejected document. An
    impossible day raises.
    """
    match = _NUMERIC.match(text)
    if not match:
        raise DateParseError(f"cannot read {text!r} as a numeric date")
    day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise DateParseError(
            f"{text!r} is not a real date read day-first; month-first is not attempted "
            "because a silently swapped day and month is invisible"
        ) from exc


def resolve_period_date(text: str, period_start: date, period_end: date) -> date:
    """Resolve a year-less "01 Jun" against the statement period it belongs to.

    Each candidate year between the period's start and end is tried, and the
    result must land inside the period. Exactly one match is required:

    - zero matches means the row is outside the period the statement claims to
      cover, which is a parse or validation problem either way;
    - more than one match means the period spans the same day twice, so the
      year genuinely cannot be determined from the document.

    Both cases raise, so the document quarantines instead of importing a
    transaction into the wrong year.
    """
    if period_start > period_end:
        raise DateParseError(f"period start {period_start} is after end {period_end}")

    if _NUMERIC.match(text):
        return parse_numeric_date(text)

    parts = _day_and_month(text)
    if parts is None:
        return parse_full_date(text)
    day, month = parts

    candidates = []
    for year in range(period_start.year, period_end.year + 1):
        try:
            candidate = date(year, month, int(day))
        except ValueError:
            continue  # e.g. 29 Feb in a non-leap year
        if period_start <= candidate <= period_end:
            candidates.append(candidate)

    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise DateParseError(
            f"{text!r} does not fall inside the statement period {period_start}..{period_end}"
        )
    raise DateParseError(
        f"{text!r} is ambiguous within {period_start}..{period_end}: {candidates}"
    )


#: How far before a statement period a transaction date may legitimately fall.
#: Card statements routinely show a purchase made in the previous month that
#: posted in this one. Generous enough for any posting lag, and far short of a
#: year, so the "exactly one candidate" guarantee below still holds.
DEFAULT_LOOKBACK_DAYS = 95

#: How far *after* a period a posting date may legitimately fall.
#:
#: A statement can carry a row it posted after the period closed and still
#: count in the closing balance: OCBC credits a 360 account's month-end
#: interest with the last day of the month as its value date and the next day
#: as its posting date, inside the balance the statement carries forward.
#:
#: Days, not months, and deliberately small. This widens the window a year-less
#: date is resolved against, so it has to stay far short of a year for the
#: "exactly one candidate" rule to keep meaning what it says — and a settlement
#: that lands weeks after its period is a misread row, not a posting lag.
DEFAULT_LOOKAHEAD_DAYS = 5


def resolve_near_period(
    text: str,
    period_start: date,
    period_end: date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    lookahead_days: int = 0,
) -> date:
    """Resolve a year-less date that may fall just outside the period.

    Card statements print a transaction date alongside the posting date, and
    the transaction routinely happened in the previous month — "29 Dec" on a
    January statement is normal, not an error. That is what `lookback_days` is
    for, and it is the common case.

    `lookahead_days` widens the other end, for the rarer statement that posts a
    row after its own period closed while still counting it in the closing
    balance. The two are separate because a layout usually needs one or the
    other: a card's *transaction* date runs early, a deposit account's month-end
    *posting* date can run late, and widening both ends by both amounts would
    accept dates neither layout ever prints.

    The same single-match rule applies, over the widened window: a margin under
    a year cannot make a day-and-month ambiguous.
    """
    from datetime import timedelta

    return resolve_period_date(
        text,
        period_start - timedelta(days=lookback_days),
        period_end + timedelta(days=lookahead_days),
    )


def parse_period(text: str) -> tuple[date, date]:
    """Parse "1 Jul 2025 - 31 Jul 2025" into its two endpoints."""
    parts = re.split(r"\s+[-–—]\s+", text.strip())
    if len(parts) != 2:
        raise DateParseError(f"cannot read {text!r} as a period")
    return parse_full_date(parts[0]), parse_full_date(parts[1])
