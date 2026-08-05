"""Net worth over time, from what the statements declared.

**Balance, never a sum of transactions.** Summing movements would make a
transfer between the household's own accounts look like growth on one side and
loss on the other, and would count a card the wrong way round. Every figure
here comes from a closing balance a statement stated about itself — the same
number the reconciliation check already proved to the cent.

The one real problem is that accounts do not close on the same day. A DBS
statement ends on the 31st, a card cycle on the 20th, and asking "what was I
worth on the 30th of June" means combining balances declared at different
moments. Each account therefore **carries forward its last declared balance**
until it declares another. That is the honest reading: between statements, the
last thing the bank said is the last thing anyone knows.

A consequence worth stating rather than hiding: a point is only as current as
its stalest account. `accounts_known` travels with every point so a client can
say how much of the picture is actually in view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class Declared:
    """One closing balance, as a statement stated it."""

    period_end: date
    account_id: int
    closing_balance_minor: int


@dataclass(frozen=True, slots=True)
class Point:
    on: date
    total_minor: int
    #: How many accounts had declared anything by this date. A rising count
    #: means earlier points are not comparable with later ones.
    accounts_known: int


def net_worth(declared, *, every: str = "month") -> list[Point]:
    """Net worth at each point a statement closed.

    `every` is `month` or `statement`. Monthly collapses a month's statements
    into one figure at its last closing date, which is what a chart wants;
    `statement` keeps every declaration, which is what an audit wants.
    """
    rows = sorted(declared, key=lambda d: (d.period_end, d.account_id))
    if not rows:
        return []

    latest: dict[int, int] = {}
    points: list[Point] = []

    for period_end, group in _by_date(rows):
        for row in group:
            # Later statements for one account replace earlier ones rather than
            # adding to them: a balance is a position, not a movement.
            latest[row.account_id] = row.closing_balance_minor
        points.append(Point(
            on=period_end,
            total_minor=sum(latest.values()),
            accounts_known=len(latest),
        ))

    if every == "statement":
        return points

    # One point per month, the last in it: within a month the earlier figures
    # are the same picture with fewer accounts updated, and plotting all of
    # them draws a sawtooth that is an artefact of statement timing.
    by_month: dict[tuple[int, int], Point] = {}
    for point in points:
        by_month[(point.on.year, point.on.month)] = point
    return [by_month[key] for key in sorted(by_month)]


def _by_date(rows):
    current: list = []
    current_date = None
    for row in rows:
        if current_date is not None and row.period_end != current_date:
            yield current_date, current
            current = []
        current_date = row.period_end
        current.append(row)
    if current:
        yield current_date, current


def change(points: list[Point]) -> dict:
    """How the position moved across the window.

    The percentage is against the opening figure and is **undefined when that
    is zero or negative** — a household that began the window in debt has no
    meaningful "percent growth", and inventing one would put a confident number
    on the screen §5.1(D) attaches a feeling to.
    """
    if len(points) < 2:
        return {"from_minor": None, "to_minor": None, "change_minor": None, "percent": None}

    first, last = points[0].total_minor, points[-1].total_minor
    movement = last - first
    return {
        "from_minor": first,
        "to_minor": last,
        "change_minor": movement,
        "percent": round(movement / first, 4) if first > 0 else None,
    }
