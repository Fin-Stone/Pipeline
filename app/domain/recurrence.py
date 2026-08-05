"""Finding the payments that repeat.

Recurrence is a time-series question with a clean deterministic answer, so it
gets one. An LLM here would be slower, unreproducible and worse, and would put a
model between the operator and a fact that is plainly derivable from dates.

The shape of a series, from the architecture:

    same merchant, amount within a tolerance of a running centre
    at least three occurrences
    inter-arrival gaps whose spread is small next to their average
    an average that lands on a period a human would name

What it buys beyond a list of subscriptions is the two alerts that make tracking
them worth anything: a **missed payment**, when `expected_next` passes with no
match, and a **price rise**, when the amount drifts outside the tolerance of a
series already known.

**Transfers must be excluded before anything reaches here.** A credit-card
payment repeats monthly against the same counterparty and would be read as a
subscription, while being neither spending nor a merchant. The caller drops
anything carrying a `transfer_link`; this module takes what it is given.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta

#: How far an amount may sit from its series' centre and still belong to it.
#: Subscriptions creep with tax and tier changes without becoming a new thing.
AMOUNT_TOLERANCE = 0.07

#: The most a run of gaps may vary, relative to its own average, before it is
#: better described as "sometimes" than as a period.
MAX_SPREAD = 0.15

#: Fewer than three occurrences is a coincidence, not a series: any two dates
#: have an interval, and it is always perfectly regular.
MIN_OCCURRENCES = 3

#: Periods a person would name, with what each tolerates. Month-end drift is
#: why the monthly window is wide enough to hold 28 through 33.
_PERIODS: tuple[tuple[str, int, int], ...] = (
    ("weekly", 7, 1),
    ("fortnightly", 14, 2),
    ("monthly", 30, 3),
    ("quarterly", 91, 5),
    ("yearly", 365, 7),
)


@dataclass(frozen=True, slots=True)
class Occurrence:
    txn_id: int
    posted_date: date
    amount_minor: int
    merchant_norm: str


@dataclass(frozen=True, slots=True)
class Series:
    merchant_norm: str
    amount_centre_minor: int
    amount_tolerance_minor: int
    period_days: int
    period_label: str
    confidence: float
    last_seen: date
    expected_next: date
    txn_ids: tuple[int, ...]

    @property
    def occurrences(self) -> int:
        return len(self.txn_ids)

    def is_overdue(self, today: date) -> bool:
        """Past due with nothing matching it — the missed-payment signal.

        Given the period's own tolerance, so a direct debit that lands a day
        late is not reported as missing.
        """
        return today > self.expected_next + timedelta(days=_tolerance(self.period_days))


def _tolerance(period_days: int) -> int:
    for _, days, slack in _PERIODS:
        if days == period_days:
            return slack
    return 0


def find_series(occurrences, *, today: date | None = None) -> list[Series]:
    """Every repeating payment in the rows given.

    Grouped by merchant, then by amount within a merchant: one shop can hold
    two subscriptions at different prices, and averaging them together would
    describe neither.
    """
    by_merchant: dict[str, list[Occurrence]] = {}
    for occurrence in occurrences:
        if occurrence.merchant_norm:
            by_merchant.setdefault(occurrence.merchant_norm, []).append(occurrence)

    series: list[Series] = []
    for merchant, rows in sorted(by_merchant.items()):
        for cluster in _by_amount(rows):
            found = _series_from(merchant, cluster)
            if found is not None:
                series.append(found)

    return sorted(series, key=lambda s: (s.merchant_norm, s.amount_centre_minor))


def _by_amount(rows: list[Occurrence]) -> list[list[Occurrence]]:
    """Split one merchant's rows into runs of comparable amount.

    Walking in amount order and starting a new cluster whenever the next row
    sits outside the tolerance of the one running. Two subscriptions to the
    same shop separate cleanly; a price that crept stays one series.
    """
    clusters: list[list[Occurrence]] = []
    for row in sorted(rows, key=lambda r: (abs(r.amount_minor), r.txn_id)):
        if clusters:
            centre = _centre(clusters[-1])
            if centre and abs(abs(row.amount_minor) - centre) <= centre * AMOUNT_TOLERANCE:
                clusters[-1].append(row)
                continue
        clusters.append([row])
    return clusters


def _centre(cluster: list[Occurrence]) -> float:
    return statistics.fmean(abs(r.amount_minor) for r in cluster) if cluster else 0.0


def _series_from(merchant: str, cluster: list[Occurrence]) -> Series | None:
    if len(cluster) < MIN_OCCURRENCES:
        return None

    rows = sorted(cluster, key=lambda r: (r.posted_date, r.txn_id))
    gaps = [
        (b.posted_date - a.posted_date).days
        for a, b in zip(rows, rows[1:])
    ]
    if not gaps or any(gap <= 0 for gap in gaps):
        # Same-day repeats are not a period. Two coffees on one morning would
        # otherwise read as a series with an interval of zero.
        return None

    average = statistics.fmean(gaps)
    spread = (statistics.pstdev(gaps) / average) if average else 1.0
    if spread >= MAX_SPREAD:
        return None

    period = _canonical(average)
    if period is None:
        return None

    label, period_days = period
    centre = round(_centre(rows))
    last_seen = rows[-1].posted_date

    return Series(
        merchant_norm=merchant,
        amount_centre_minor=centre,
        amount_tolerance_minor=round(centre * AMOUNT_TOLERANCE),
        period_days=period_days,
        period_label=label,
        # Tight gaps read as high confidence, and it is a description of the
        # evidence rather than a probability: 1.0 means the gaps never varied.
        confidence=round(max(0.0, 1.0 - spread / MAX_SPREAD), 4),
        last_seen=last_seen,
        expected_next=last_seen + timedelta(days=period_days),
        txn_ids=tuple(r.txn_id for r in rows),
    )


def _canonical(average: float) -> tuple[str, int] | None:
    """The named period an average gap lands on, if it lands on one.

    A run of gaps that is regular but matches nothing a person would call a
    period is not reported. Something arriving every 47 days reliably is real,
    but calling it a subscription would be the model talking, not the data.
    """
    for label, days, slack in _PERIODS:
        if abs(average - days) <= slack:
            return label, days
    return None


def price_rises(series: Series, recent: list[Occurrence]) -> list[Occurrence]:
    """Occurrences that have drifted outside a known series' tolerance.

    The other half of what recurrence detection is for. A subscription whose
    price moved is worth surfacing precisely because nothing else will: it is
    still paid, still on time, and still invisible.
    """
    centre = series.amount_centre_minor
    return [
        row for row in recent
        if row.merchant_norm == series.merchant_norm
        and abs(abs(row.amount_minor) - centre) > series.amount_tolerance_minor
    ]
