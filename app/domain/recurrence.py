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
from collections import Counter
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

#: The most a price may move and still be the same subscription, either way.
#: A tier change doubles a bill; it does not multiply it by eighty. Without a
#: bound, a single large payment landing a month after a small monthly series
#: was absorbed as a price rise from 10.00 to 800.00 — which is not a price
#: rise, it is a different payment.
MAX_PRICE_RATIO = 4.0

#: Payments needed before a short run is read as a continuation at a new price.
#: One is a coincidence with a plausible date; two show the cadence carried on.
MIN_TAIL = 2

#: How uneven a run's gaps must be before splitting it into concurrent plans is
#: even considered. A subscription billed every fortnight and two plans billed
#: monthly a fortnight apart produce the *same dates*; what tells them apart is
#: that two plans drift independently, so the merged gaps alternate, while one
#: subscription's do not vary at all. Below this the simpler reading stands.
SPLIT_MIN_SPREAD = 0.02

#: Payments each strand needs before a cluster is read as two commitments
#: rather than one irregular one. Above the three a single series needs,
#: deliberately: claiming there are *two* things hidden in one run is a larger
#: claim than finding one, and at the bare minimum it turned six coffees into
#: two monthly subscriptions.
SPLIT_MIN_STRAND = MIN_OCCURRENCES + 1

#: The most concurrent commitments one run may be read as. Two plans with one
#: provider at one price is a thing households actually have; three separate
#: commitments hiding in a single run of identical amounts is over-fitting, and
#: is what an irregular allowance paid forty times looks like from the inside.
SPLIT_MAX_STRANDS = 2

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


#: Names that are the *rail* the money travelled on, with no payee in them at
#: all. A direct debit reads `GIRO PAYMENTS / COLLECTIONS VIA GIRO` and a
#: standing instruction reads `FAST` — the bank simply does not say who was
#: paid, and no amount of normalising recovers a name that was never printed.
#:
#: Unlike a conduit these are not excluded, because what travels on them is
#: exactly what this page exists to show: insurance premiums, mortgage
#: instalments, the commitments a household forgets it has. They are grouped by
#: **amount** instead of by name — see `_group`.
RAILS = frozenset({
    "GIRO", "GIRO PAYMENTS / COLLECTIONS VIA GIRO", "GIRO PAYMENTS",
    "PAYMENTS / COLLECTIONS VIA GIRO", "COLLECTIONS VIA GIRO",
    "FAST", "FAST COLLECTION", "FAST PAYMENT", "FAST PAYMENT / RECEIPT",
    "INTERBANK GIRO", "STANDING INSTRUCTION", "DIRECT DEBIT",
})

#: Mechanisms, as the *raw* statement line opens. A **collection** is money a
#: biller pulled — a direct debit — and that is a fact about the row whatever
#: the bank printed after it.
#:
#: Needed because the payee code is not stable and normalisation cannot know
#: that. One monthly premium arrived eighteen times as
#: `GIRO Payments / Collections via GIRO PACS BNC-P/N…` and, on five of those
#: months, as `GIRO Payments / Collections via GIRO ACME LIFE PACS BNC-P/N…` —
#: the identical debit, printed two ways. Normalisation preserves the
#: difference, so one payee became two merchants and each fell below the three
#: occurrences a series needs. The mechanism is the same on all eighteen.
#:
#: Collections only, deliberately. `FAST Payment / Receipt` is money the
#: household *pushed* — a transfer, a card payment — and grouping those by
#: amount would gather unrelated round numbers into invented commitments.
RAIL_MECHANISMS = (
    "giro payments / collections via giro",
    "giro payments/collections via giro",
    "payments / collections via giro",
    "advice fast collection",
    "fast collection",
    "interbank giro",
)

#: How a series found by amount alone is labelled. Deliberately not a merchant
#: name: nothing here knows one, and inventing one would be the module telling
#: the operator something the statement never said.
UNNAMED = "Unnamed direct debit"


def is_rail(merchant_norm: str, description: str = "") -> bool:
    """Whether this row travelled on a rail rather than naming a payee.

    Two ways to be one, and the second is the one that matters.

    From the **name**: reference tokens are dropped first, because the same
    rail arrives both bare and with the bank's reference stuck to the end —
    `GIRO PAYMENTS / COLLECTIONS VIA GIRO` and the same followed by a reference
    are one mechanism and neither names anybody. Dropping only tokens carrying
    a digit keeps it narrow: `FAST PRU- INSURANCE PREMIUM` does name a payee
    and survives intact.

    From the **raw line**: a GIRO collection is a direct debit whatever code
    follows it. That has to be read from the raw text because normalisation has
    already removed the mechanism words, leaving only a biller code that the
    bank does not print consistently — which is how eighteen instances of one
    premium became two merchants and neither was a series.
    """
    return is_rail_name(merchant_norm) or _is_collection(description)


def is_rail_name(merchant_norm: str) -> bool:
    """Whether the counterparty *is* the mechanism, naming nobody at all.

    A name like this is never a useful grouping key — it lumps every direct
    debit in the ledger together whatever was being paid — so rows carrying one
    skip the merchant pass and go straight to the amount fallback.
    """
    words = [w for w in (merchant_norm or "").upper().split() if not any(c.isdigit() for c in w)]
    return bool(words) and " ".join(words) in RAILS


def _is_collection(description: str) -> bool:
    """Whether the raw line opens with a biller pulling money."""
    opening = " ".join((description or "").lower().split())
    return any(opening.startswith(mechanism) for mechanism in RAIL_MECHANISMS)


#: Periods a person would name, with what each tolerates. Month-end drift is
#: why the monthly window is wide enough to hold 28 through 33.
_PERIODS: tuple[tuple[str, int, int], ...] = (
    ("weekly", 7, 1),
    ("fortnightly", 14, 2),
    ("monthly", 30, 3),
    ("quarterly", 91, 5),
    ("yearly", 365, 7),
)

#: The periods a person may declare, for validating a manual mark. Taken from
#: the same table the detector snaps to, so the two can never drift apart and
#: offer the operator a period the rest of the module cannot express.
PERIOD_LABELS: tuple[str, ...] = tuple(label for label, _, _ in _PERIODS)

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
    #: The statement line as printed. Carried because the *mechanism* survives
    #: only here — see `is_rail`. Optional, so every existing caller and test
    #: keeps working; a row without it is judged on its name alone.
    description: str = ""


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


def _by_merchant(rows) -> dict[tuple[str, int | None], list[Occurrence]]:
    """The ordinary key: who the row was with."""
    grouped: dict[tuple[str, int | None], list[Occurrence]] = {}
    for occurrence in rows:
        grouped.setdefault((occurrence.merchant_norm, None), []).append(occurrence)
    return grouped


def _by_exact_amount(rows) -> dict[tuple[str, int | None], list[Occurrence]]:
    """The fallback key, for rows whose name settled nothing.

    Exact, not within a tolerance. A tolerance on a key nobody named would let
    two unrelated debits of similar size become one commitment.

    Labelled with whatever name the bank did print on the statements where it
    printed one — the same policy arrives named on one and bare on the next.
    """
    grouped: dict[int, list[Occurrence]] = {}
    for occurrence in rows:
        grouped.setdefault(abs(occurrence.amount_minor), []).append(occurrence)

    keyed: dict[tuple[str, int | None], list[Occurrence]] = {}
    for amount, group in grouped.items():
        keyed[(_best_name(group), amount)] = group
    return keyed


def _best_name(group) -> str:
    """The most presentable name the bank printed for this amount.

    A plain name beats a scheme code, then frequency decides. Both appear on
    one policy: thirteen statements say `PACS BNC-P/`, five say `ACME LIFE
    PACS BNC-P/`, and one says `Acme Life Insurance` — the last is the only
    one worth showing a person, and it is also the rarest, so frequency alone
    picks the code.

    `is_rail_name`, not `is_rail`: the question here is whether the
    *counterparty* names anybody, not how the money travelled. A GIRO
    collection that does print its insurer still knows who it paid.
    """
    names = Counter(
        o.merchant_norm for o in group if not is_rail_name(o.merchant_norm)
    )
    if not names:
        return UNNAMED
    plain = [name for name in names if name.replace(" ", "").isalpha()]
    ranked = plain or list(names)
    return max(ranked, key=lambda name: (names[name], name))


def _detect(groups) -> tuple[list[Series], dict]:
    """Run every group through amount clustering and concurrent-run splitting."""
    segments: list[Series] = []
    leftovers: dict[tuple[str, int | None], list[list[Occurrence]]] = {}
    # Sorted so the outcome never depends on the order rows arrived in. The
    # amount is `None` for a merchant group, so it cannot be compared directly.
    for key, rows in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        for cluster in _by_amount(rows):
            for run in _runs(cluster):
                found = _series_from(key[0], run)
                if found is not None:
                    segments.append(found)
                else:
                    leftovers.setdefault(key, []).append(run)
    return segments, leftovers


def find_series(occurrences, *, today: date | None = None) -> list[Series]:
    """Every repeating payment in the rows given.

    **By merchant first, and by amount only for what that leaves behind.**

    The name is the better key wherever the bank prints one, and the second
    pass exists because sometimes it does not. A direct debit reading only
    `GIRO Payments / Collections via GIRO` names nobody; worse, one that reads
    `… PACS BNC-P/N…` on thirteen statements and `… ACME LIFE PACS BNC-P/N…`
    on five names the same payee two ways, so one monthly premium became two
    merchants and neither reached the three occurrences a series needs.

    So rows that travelled on a collection rail and did not end up in any
    series are gathered again by exact amount. Strictly a fallback: replacing
    the merchant pass with it instead cost four insurers that *do* print their
    names, whose rows then dissolved into amount groups shared with unrelated
    payments — the monthly commitment halved and nobody had cancelled anything.

    Within a group: by amount, then into concurrent runs, because one provider
    can hold two subscriptions at different prices and two at the same one.
    """
    rows = [
        o for o in occurrences
        if o.merchant_norm and not is_conduit(o.merchant_norm)
    ]

    # A bare mechanism is not a merchant, so those rows skip the first pass
    # outright rather than lumping every direct debit under one name.
    named = [o for o in rows if not is_rail_name(o.merchant_norm)]
    segments, leftovers = _detect(_by_merchant(named))
    claimed = {txn_id for found in segments for txn_id in found.txn_ids}

    # The fallback reaches every unclaimed row sharing an amount with an
    # unclaimed *rail* row — not only the rail rows themselves. The same policy
    # arrives named on one statement and bare on the next, and the halves have
    # to find each other or neither reaches three occurrences.
    unclaimed = [o for o in rows if o.txn_id not in claimed]
    rail_amounts = {
        abs(o.amount_minor) for o in unclaimed
        if is_rail(o.merchant_norm, o.description)
    }
    stranded = [o for o in unclaimed if abs(o.amount_minor) in rail_amounts]
    if stranded:
        more, more_leftovers = _detect(_by_exact_amount(stranded))
        segments += more
        for key, runs in more_leftovers.items():
            leftovers.setdefault(key, []).extend(runs)

    series = _merge_price_changes(segments)
    series = _absorb_recent_changes(series, leftovers)
    return sorted(series, key=lambda s: (s.merchant_norm, s.amount_centre_minor))


def _chain(rows: list[Occurrence], days: int, slack: int) -> list[list[Occurrence]]:
    """Thread rows onto however many strands of one period it takes.

    First fit in date order: a row joins the first strand whose last payment
    sits a period behind it, and starts a new strand otherwise.
    """
    chains: list[list[Occurrence]] = []
    for row in rows:
        for chain in chains:
            if abs((row.posted_date - chain[-1].posted_date).days - days) <= slack:
                chain.append(row)
                break
        else:
            chains.append([row])
    return chains


def _runs(cluster: list[Occurrence]) -> list[list[Occurrence]]:
    """Split one amount's rows into the concurrent commitments they are.

    Two plans with the same provider at the same price, billed a fortnight
    apart, are one cluster whose gaps alternate 14 and 17 days. Averaged, that
    is 15, which lands squarely on "fortnightly" — so two monthly plans were
    reported as one fortnightly one, at half the true commitment, and the price
    cut on one of them was invisible because the other went on at the old price.

    **Deliberately narrow.** Splitting is only considered where the unsplit
    reading is *shorter* than monthly, and only into strands one named period
    longer. Everything else is left exactly as it was found.

    That is not timidity, it is the only version that holds up. Any series
    threaded at three times its period also covers every row — as three
    strands — so a rule permissive enough to find two monthly plans inside a
    fortnightly reading will just as happily find three quarterly commitments
    inside a monthly one. A first attempt did: a monthly allowance became three
    quarterly series, a coffee shop became two, and the page went from fifteen
    series to seventy-three.

    The narrow case is the only one that actually occurs. Two concurrent
    monthly plans look like one fortnightly series and nothing else does; two
    concurrent yearly plans would average six-monthly, which is not a period
    anybody names, so they are never detected as one thing in the first place.
    """
    rows = sorted(cluster, key=lambda r: (r.posted_date, r.txn_id))
    if len(rows) < SPLIT_MIN_STRAND * 2:
        return [rows]

    # **A last resort, and only for a run that otherwise describes nothing.**
    #
    # This is the whole safeguard, and it is load-bearing. Any run threaded at
    # three times its period also accounts for every row — as three strands, each
    # beautifully regular — so a rule that splits on "the strands fit better"
    # will split anything. It did: a monthly premium became three quarterly
    # ones, an allowance became three, and the page went from fifteen series to
    # seventy-three.
    #
    # A run that already yields a series is therefore left exactly as it is,
    # however much better some other reading might look. Only a run that yields
    # nothing at all is worth a second explanation — and two plans at one price
    # yield nothing, because their merged gaps alternate too widely to be any
    # period. That is the case this exists for and the only one it touches.
    if _series_from("", rows) is not None:
        return [rows]

    # Shortest period first, so the tightest explanation that accounts for
    # every row wins.
    for _, days, slack in _PERIODS:
        strands = [
            chain for chain in _chain(rows, days, slack)
            if len(chain) >= SPLIT_MIN_STRAND and _is_regular(chain)
        ]
        if (
            2 <= len(strands) <= SPLIT_MAX_STRANDS
            and sum(len(chain) for chain in strands) == len(rows)
        ):
            return strands
    return [rows]


def _spread(rows: list[Occurrence]) -> float:
    """How much a run's gaps vary, relative to their own average."""
    gaps = [
        (b.posted_date - a.posted_date).days
        for a, b in zip(rows, rows[1:])
    ]
    if not gaps or any(gap <= 0 for gap in gaps):
        return 1.0
    average = statistics.fmean(gaps)
    return (statistics.pstdev(gaps) / average) if average else 1.0


def _is_regular(rows: list[Occurrence]) -> bool:
    return _spread(rows) < MAX_SPREAD


def _absorb_recent_changes(series: list[Series], leftovers) -> list[Series]:
    """Attach a run too short to be a series to the one it continues.

    A subscription whose price changed last month has two payments at the new
    price, and three are needed to establish a series. Requiring three would
    mean a price change is only ever visible a quarter after it happened —
    which is precisely when it stops being worth telling anybody.

    The evidence is not the short run on its own; it is that an established
    series stops exactly where the short run starts, on the same cadence. That
    is a subscription that changed price, and it is the reading the operator
    was missing: one of two identical plans was reduced, and nothing showed it.
    """
    by_key: dict[str, list[Series]] = {}
    for found in series:
        by_key.setdefault(found.merchant_norm, []).append(found)

    absorbed: set[int] = set()
    for (merchant, _), runs in leftovers.items():
        for run in runs:
            if len(run) < MIN_TAIL:
                continue
            rows = sorted(run, key=lambda r: (r.posted_date, r.txn_id))
            if not _is_regular(rows):
                continue
            centre = _centre(rows)
            for candidate in by_key.get(merchant, ()):
                if id(candidate) in absorbed or candidate.last_seen >= rows[0].posted_date:
                    continue
                gap = (rows[0].posted_date - candidate.last_seen).days
                if abs(gap - candidate.period_days) > _tolerance(candidate.period_days):
                    continue
                was = candidate.amount_centre_minor
                if not was or not 1 / MAX_PRICE_RATIO <= centre / was <= MAX_PRICE_RATIO:
                    continue
                if round(centre) == was:
                    # Same price. Whatever this run is, it is not a change, and
                    # recording "400.00 -> 400.00" tells the operator nothing
                    # while looking like news.
                    continue
                absorbed.add(id(candidate))
                series[series.index(candidate)] = _extended(candidate, rows)
                break
    return series


def _extended(found: Series, rows: list[Occurrence]) -> Series:
    """One series, carried on at a new price by rows too few to stand alone."""
    centre = round(_centre(rows))
    return Series(
        merchant_norm=found.merchant_norm,
        amount_centre_minor=centre,
        amount_tolerance_minor=round(centre * AMOUNT_TOLERANCE),
        period_days=found.period_days,
        period_label=found.period_label,
        confidence=found.confidence,
        first_seen=found.first_seen,
        last_seen=rows[-1].posted_date,
        expected_next=rows[-1].posted_date + timedelta(days=found.period_days),
        txn_ids=found.txn_ids + tuple(r.txn_id for r in rows),
        price_changes=found.price_changes + (
            PriceChange(on=rows[0].posted_date,
                        from_minor=found.amount_centre_minor, to_minor=centre),
        ),
        total_paid_minor=found.total_paid_minor + sum(abs(r.amount_minor) for r in rows),
    )


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


def matching(
    occurrences, merchant_norm: str, amount_centre_minor: int,
) -> list[Occurrence]:
    """The rows a manual mark would gather, oldest first.

    The same merchant and an amount within the ordinary tolerance — the mark
    says "this repeats", not "this is the only one". Shown to the operator
    before anything is written, because a mark that silently swept up a
    neighbouring payment would be discovered as a wrong monthly total months
    later, with nothing on screen to explain it.
    """
    centre = abs(amount_centre_minor)
    tolerance = centre * AMOUNT_TOLERANCE
    return sorted(
        (
            row for row in occurrences
            if row.merchant_norm == merchant_norm
            and abs(abs(row.amount_minor) - centre) <= tolerance
        ),
        key=lambda r: (r.posted_date, r.txn_id),
    )


def declared_series(
    occurrences, merchant_norm: str, amount_centre_minor: int, period_label: str,
) -> Series | None:
    """A series because a person said so, on the period they said.

    The detector needs three occurrences and gaps that barely vary, and it is
    right to: without those, "regular" is a claim the data does not support.
    But an operator knows things the ledger cannot show — a policy paid yearly
    has two rows so far, a quarterly bill lands whenever the vendor gets round
    to invoicing — and refusing to record that is refusing the one source of
    information better than the rows.

    So the thresholds do not apply here, and **nothing else is relaxed**. The
    period is the operator's, not inferred. `confidence` still describes the
    gaps and nothing else: it reads 0.0 where there are not two of them to
    compare, which is honest and is usually exactly why a person had to say so.

    Returns `None` only when no row matches at all — a mark on a merchant whose
    rows have since been reparsed away has nothing to describe.
    """
    rows = matching(occurrences, merchant_norm, amount_centre_minor)
    if not rows:
        return None

    period_days = dict((label, days) for label, days, _ in _PERIODS)[period_label]
    gaps = [(b.posted_date - a.posted_date).days for a, b in zip(rows, rows[1:])]
    average = statistics.fmean(gaps) if gaps else 0.0
    spread = (statistics.pstdev(gaps) / average) if len(gaps) > 1 and average else 1.0
    centre = round(_centre(rows))
    last_seen = rows[-1].posted_date

    return Series(
        merchant_norm=merchant_norm,
        amount_centre_minor=centre,
        amount_tolerance_minor=round(centre * AMOUNT_TOLERANCE),
        period_days=period_days,
        period_label=period_label,
        confidence=round(max(0.0, 1.0 - spread / MAX_SPREAD), 4),
        first_seen=rows[0].posted_date,
        last_seen=last_seen,
        expected_next=last_seen + timedelta(days=period_days),
        txn_ids=tuple(r.txn_id for r in rows),
        total_paid_minor=sum(abs(r.amount_minor) for r in rows),
    )


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
