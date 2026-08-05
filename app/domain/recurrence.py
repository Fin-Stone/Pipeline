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

#: Periods that may pass before a series reads as cancelled rather than late.
LAPSED_AFTER_PERIODS = 2

#: How far apart two runs of the same merchant and period may sit and still be
#: one subscription that changed price. One skipped cycle is forgiven; a gap of
#: years is a service taken up again, which is a different thing.
MERGE_GAP_PERIODS = 2.0

#: Names that are a *route* money took, not the party it went to. An e-wallet
#: top-up, a marketplace order and an ATM withdrawal are all real spending, but
#: the name on the row says how it was paid rather than what for.
#:
#: They are excluded from recurrence specifically, and only there. A wallet
#: topped up every month looks exactly like a subscription and is the most
#: regular thing in the ledger, so it would be found early and believed —
#: while telling the operator nothing they can cancel, which is what the
#: recurring page is for. The spending itself still counts everywhere else.
#:
#: A real subscription bought *through* one of these arrives under the
#: service's own name, so nothing genuine is lost by the exclusion.
CONDUITS = frozenset({
    "PAYLAH", "PAYNOW", "GRABPAY", "SHOPEEPAY", "FAVEPAY", "ALIPAY", "WECHATPAY",
    "LAZADA", "SHOPEE", "AMAZON", "QOO10", "CAROUSELL",
    "ATM", "CASH WITHDRAWAL", "CASH", "NETS", "EZ-LINK", "EZLINK",
})


def is_conduit(merchant_norm: str) -> bool:
    """Whether a name describes the route rather than the counterparty."""
    if not merchant_norm:
        return True
    words = merchant_norm.upper().split()
    return bool(words) and (words[0] in CONDUITS or merchant_norm.upper() in CONDUITS)


#: Periods a person would name, with what each tolerates. Month-end drift is
#: why the monthly window is wide enough to hold 28 through 33.
_PERIODS: tuple[tuple[str, int, int], ...] = (
    ("weekly", 7, 1),
    ("fortnightly", 14, 2),
    ("monthly", 30, 3),
    ("quarterly", 91, 5),
    ("yearly", 365, 7),
)

#: How many times a period falls in a month, for comparing unlike commitments.
_PER_MONTH = {
    "weekly": 52 / 12,
    "fortnightly": 26 / 12,
    "monthly": 1.0,
    "quarterly": 1 / 3,
    "yearly": 1 / 12,
}


@dataclass(frozen=True, slots=True)
class Occurrence:
    txn_id: int
    posted_date: date
    amount_minor: int
    merchant_norm: str


@dataclass(frozen=True, slots=True)
class PriceChange:
    """A subscription's price moving, which is a fact about it rather than a
    new subscription."""

    on: date
    from_minor: int
    to_minor: int


@dataclass(frozen=True, slots=True)
class Series:
    merchant_norm: str
    #: What it costs *now*. A series that has changed price keeps the latest,
    #: because "what am I paying" is the question the overview answers.
    amount_centre_minor: int
    amount_tolerance_minor: int
    period_days: int
    period_label: str
    confidence: float
    first_seen: date
    last_seen: date
    expected_next: date
    txn_ids: tuple[int, ...]
    #: Every time the price moved, oldest first. Empty for a steady series.
    price_changes: tuple[PriceChange, ...] = ()
    #: Summed from the occurrences themselves, not from the centre, so a series
    #: that changed price still totals what was actually paid.
    total_paid_minor: int = 0

    @property
    def occurrences(self) -> int:
        return len(self.txn_ids)

    @property
    def monthly_equivalent_minor(self) -> int:
        """What this costs per month, whatever period it actually runs on.

        A yearly 120 and a monthly 10 are the same commitment, and a total that
        adds the printed amounts says otherwise.

        Scaled by the *named* period rather than by its length in days. A month
        is not 30 days, so dividing by days makes a yearly 120 come out at 9.86
        and a monthly 10 at 10.00 — two figures for one commitment, in a number
        whose only job is to let the two be compared.
        """
        return round(self.amount_centre_minor * _PER_MONTH[self.period_label])

    def is_overdue(self, today: date) -> bool:
        """Past due with nothing matching it — the missed-payment signal.

        Given the period's own tolerance, so a direct debit landing a day late
        is not reported as missing. A series far enough past due to be lapsed
        is no longer overdue: it is finished, and saying so is the point.
        """
        due = self.expected_next + timedelta(days=_tolerance(self.period_days))
        return today > due and not self.is_lapsed(today)

    def is_lapsed(self, today: date) -> bool:
        """Missed enough cycles to read as cancelled rather than late.

        A cancelled subscription and a skipped payment want opposite
        reactions: one should go quiet, the other should be raised. Without
        the distinction the recurring page fills with the corpses of things
        the operator stopped paying for years ago, each still nagging.
        """
        return today > self.expected_next + timedelta(days=self.period_days * LAPSED_AFTER_PERIODS)

    def is_due_within(self, today: date, days: int) -> bool:
        """The forward view: what is coming, not only what was missed."""
        return today <= self.expected_next <= today + timedelta(days=days)


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
        if occurrence.merchant_norm and not is_conduit(occurrence.merchant_norm):
            by_merchant.setdefault(occurrence.merchant_norm, []).append(occurrence)

    segments: list[Series] = []
    for merchant, rows in sorted(by_merchant.items()):
        for cluster in _by_amount(rows):
            found = _series_from(merchant, cluster)
            if found is not None:
                segments.append(found)

    series = _merge_price_changes(segments)
    return sorted(series, key=lambda s: (s.merchant_norm, s.amount_centre_minor))


def _merge_price_changes(segments: list[Series]) -> list[Series]:
    """Rejoin runs that a price rise split apart.

    Amount clustering keeps a series together while its price creeps, but a
    rise past the tolerance — 9.99 to 12.99 is 30% — starts a new cluster. Left
    alone that reports one subscription as two: the overview double-counts, the
    list shows a service twice, the abandoned half looks permanently overdue,
    and the price trend is invisible because the rise is modelled as two
    unrelated things rather than one thing that changed.

    Two runs of the same merchant and period, where one ends about where the
    other begins, are therefore one subscription with a price change at the
    join. A price rise is a fact about a subscription, not a new subscription.
    """
    grouped: dict[tuple[str, int], list[Series]] = {}
    for segment in segments:
        grouped.setdefault((segment.merchant_norm, segment.period_days), []).append(segment)

    merged: list[Series] = []
    for group in grouped.values():
        run: list[Series] = []
        for segment in sorted(group, key=lambda s: s.first_seen):
            if run and _adjoins(run[-1], segment):
                run.append(segment)
                continue
            if run:
                merged.append(_joined(run))
            run = [segment]
        if run:
            merged.append(_joined(run))
    return merged


def _adjoins(earlier: Series, later: Series) -> bool:
    """Whether one run picks up about where the other left off."""
    if later.first_seen <= earlier.last_seen:
        return False  # overlapping runs are two live subscriptions, not one
    gap = (later.first_seen - earlier.last_seen).days
    return gap <= earlier.period_days * MERGE_GAP_PERIODS


def _joined(run: list[Series]) -> Series:
    if len(run) == 1:
        return run[0]

    last = run[-1]
    changes = tuple(
        PriceChange(on=b.first_seen, from_minor=a.amount_centre_minor,
                    to_minor=b.amount_centre_minor)
        for a, b in zip(run, run[1:])
    )
    return Series(
        merchant_norm=last.merchant_norm,
        amount_centre_minor=last.amount_centre_minor,
        amount_tolerance_minor=last.amount_tolerance_minor,
        period_days=last.period_days,
        period_label=last.period_label,
        # The weakest run's regularity, not an average of them: a merged series
        # is only as trustworthy as its least regular stretch.
        confidence=min(segment.confidence for segment in run),
        first_seen=run[0].first_seen,
        last_seen=last.last_seen,
        expected_next=last.expected_next,
        txn_ids=tuple(i for segment in run for i in segment.txn_ids),
        price_changes=changes,
        total_paid_minor=sum(segment.total_paid_minor for segment in run),
    )


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
        first_seen=rows[0].posted_date,
        last_seen=last_seen,
        expected_next=last_seen + timedelta(days=period_days),
        txn_ids=tuple(r.txn_id for r in rows),
        total_paid_minor=sum(abs(r.amount_minor) for r in rows),
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
