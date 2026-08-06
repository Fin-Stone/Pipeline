"""The HTTP surface the UI talks to.

**Read [docs/api-contracts.md](../../docs/api-contracts.md) before changing
anything here, and update it in the same change.** The contract is what a
client is written against and what a self-hosted server promises; a route that
drifts from it breaks somebody's install rather than somebody's build.

Design, from architecture §5.2:

- **Every figure is derivable from `(date range, accounts, categories)`.** The
  dashboard has to filter and re-range on demand, so there are no precomputed
  per-month aggregates to go stale.
- **Money is integer minor units in every field, with the currency beside it.**
  No floats cross this boundary — a JSON float would reintroduce exactly the
  error the ledger is built to avoid.
- **Amounts are returned as the ledger stores them**, signed by effect on the
  account. Spending is negative. Presentation is the client's business.
- **The version is in the path.** A self-hosted server lags the hosted one, and
  a client must be able to tell what it is talking to.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

import os

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from ..config import PROFILE_PROD, Config, load_config
from ..domain.categories import (
    DEFAULT_CATEGORIES,
    Rule,
    RuleSet,
    operator_rule,
    review_queue,
)
from ..domain.networth import Declared, change, net_worth
from ..domain.recurrence import Occurrence, find_series
from ..storage.factory import build_repository
from ..storage.sqlalchemy_repo import choose_bucket, rolling_window, trend_centre

API_VERSION = "v1"
PREFIX = f"/api/{API_VERSION}"

app = FastAPI(
    title="Finstone",
    version=API_VERSION,
    summary="Self-hosted personal finance ledger",
)

#: The client is served from somewhere else by design — it is a separate
#: application pointed at whichever server the user chose — so every browser
#: call is cross-origin and without this none of them arrive.
#:
#: The default is permissive, and that is an honest default rather than a lax
#: one: **there is no authentication yet**, so the API is open to anything that
#: can reach it and CORS restricts only browsers, not the `curl` next to them.
#: It buys nothing until auth exists, and pretending otherwise would be worse.
#: Narrow it with FINSTONE_CORS_ORIGINS once there is something to protect.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.environ.get("FINSTONE_CORS_ORIGINS", "*").split(",")
        if origin.strip()
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _config() -> Config:
    return load_config()


#: One repository per database URL, for the life of the process.
#:
#: **The engine is shared; the tenant is not.** Building a repository is lazy
#: and free, but the first query on a new engine pays a full connect — 27ms of
#: TCP and authentication against Postgres, against 8ms for the query it was
#: opened to run. Disposing it at the end of every request threw that away and
#: paid it again on the next one, so three quarters of every response was
#: connection setup.
#:
#: This is safe because a repository is not bound to a tenant. Every method
#: takes a `TenantContext` and filters on it — the tenant-isolation tests exist
#: to keep it that way — so what must stay per-request is the *context*, and it
#: does. Sharing the pool underneath changes nothing about who can see what.
_REPOSITORIES: dict[str, object] = {}


def _repository_for(config: Config):
    repository = _REPOSITORIES.get(config.database_url)
    if repository is None:
        repository = build_repository(config)
        _REPOSITORIES[config.database_url] = repository
    return repository


def reset_repositories() -> None:
    """Drop every pooled engine. For tests, which build a database per case."""
    for repository in _REPOSITORIES.values():
        repository.close()
    _REPOSITORIES.clear()


class _Session:
    """One request's tenant, over a shared connection pool."""

    def __init__(self, config: Config, profile: str):
        self.config = config
        self.profile = profile
        self.repository = _repository_for(config)
        self.context = self.repository.resolve_context(
            config.tenant_for(profile), config.member_email
        )

    def close(self) -> None:
        # Deliberately not disposing: the pool outlives the request. Connections
        # are returned to it by the context managers around each query.
        pass


def session(
    profile: Annotated[str, Query(description="Which tenant's ledger")] = PROFILE_PROD,
    config: Config = Depends(_config),
):
    try:
        handle = _Session(config, profile)
    except Exception as exc:  # unknown profile is the only expected case
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        yield handle
    finally:
        handle.close()


Session = Annotated[_Session, Depends(session)]


def _filters(
    since: Annotated[date | None, Query(description="Inclusive start")] = None,
    until: Annotated[date | None, Query(description="Inclusive end")] = None,
    account_id: Annotated[list[int] | None, Query(description="Repeatable")] = None,
    category: Annotated[list[str] | None, Query(description="Repeatable")] = None,
    exclude_txn_id: Annotated[
        list[int] | None,
        Query(description="Repeatable. Hidden for this request only."),
    ] = None,
    q: Annotated[
        str | None,
        Query(description="Free text. Searches the whole ledger, ignoring the date range."),
    ] = None,
) -> dict:
    """The axes every figure is derivable from. See §5.1(B).

    `exclude_txn_id` is what makes session-level hiding real rather than
    cosmetic. A client that merely dropped rows from a list would leave the
    totals and the trend describing a different set of transactions from the
    one on screen — so the exclusion is passed to the server, which owns the
    arithmetic, and every figure moves together.

    **A search drops the date range**, here rather than in each endpoint, so
    `/summary`, `/trend` and `/transactions` cannot disagree about what is on
    screen. Somebody searching for a merchant is searching precisely because
    they do not know which month it was in; silently confining that to the last
    six would return nothing and look like an answer.
    """
    searching = bool(q and q.strip())
    return {
        "since": None if searching else since,
        "until": None if searching else until,
        "account_ids": account_id,
        "categories": category,
        "exclude_txn_ids": exclude_txn_id,
        "q": q.strip() if searching else None,
    }


Filters = Annotated[dict, Depends(_filters)]


@app.get("/", tags=["meta"])
def root() -> dict:
    """Say what this is, to whoever opened the address in a browser.

    A bare 404 here is technically correct and useless: the first thing an
    operator does with a new self-hosted service is visit its root, and telling
    them nothing is how a working install looks broken.
    """
    return {
        "service": "finstone",
        "api_version": API_VERSION,
        "api_root": PREFIX,
        "docs": "/docs",
        "openapi": "/openapi.json",
        "note": "This is the API. The UI is a separate application that connects to it.",
    }


@app.get(f"{PREFIX}/health", tags=["meta"])
def health() -> dict:
    """Liveness, and what this server speaks.

    A client uses `api_version` to decide whether it can talk to this server at
    all, which is what stops a client update breaking a self-hoster who has not
    upgraded.
    """
    return {"status": "ok", "api_version": API_VERSION}


@app.get(f"{PREFIX}/accounts", tags=["ledger"])
def accounts(handle: Session) -> dict:
    return {"accounts": handle.repository.list_accounts(handle.context)}


@app.get(f"{PREFIX}/categories", tags=["categorisation"])
def categories(handle: Session) -> dict:
    """The tenant's taxonomy, seeded on first use and editable thereafter."""
    handle.repository.seed_categories(handle.context, DEFAULT_CATEGORIES)
    return {"categories": handle.repository.list_categories(handle.context)}


Direction = Annotated[
    str,
    Query(
        pattern="^(out|in|net)$",
        description="out = spending, in = money received, net = both together",
    ),
]


@app.get(f"{PREFIX}/summary", tags=["dashboard"])
def summary(handle: Session, filters: Filters, direction: Direction = "out") -> dict:
    """Spending by category, plus the per-month, per-week and per-day averages.

    The averages span **the same range as the totals**. Computing them over a
    different window is how two halves of one screen come to describe different
    periods — see §5.1(A).
    """
    rows = handle.repository.spending_summary(
        handle.context, direction=direction, **filters
    )
    total = sum(r["total_minor"] or 0 for r in rows)

    since, until = filters["since"], filters["until"]
    days = ((until - since).days + 1) if since and until else None

    return {
        "currency": "SGD",
        "direction": direction,
        # Echoed so a client can say "these figures describe a search", and
        # so a null range is explained rather than looking like a bug.
        "q": filters["q"],
        "range": {"since": since, "until": until, "days": days},
        "total_minor": total,
        "by_category": sorted(
            (
                {
                    "category": r["category"],
                    "rows": r["rows"],
                    "total_minor": r["total_minor"] or 0,
                }
                for r in rows
            ),
            key=lambda r: r["total_minor"],
        ),
        # Null rather than zero where the range is open-ended: an average over
        # an unbounded period is not a small number, it is not a number.
        "average_minor": {
            "per_day": round(total / days) if days else None,
            "per_week": round(total / days * 7) if days else None,
            "per_month": round(total / days * 30) if days else None,
        },
    }


@app.get(f"{PREFIX}/trend", tags=["dashboard"])
def trend(
    handle: Session,
    filters: Filters,
    bucket: Annotated[
        str, Query(pattern="^(auto|day|week|month)$", description="Bar width")
    ] = "auto",
    rolling: Annotated[
        int, Query(ge=0, le=24, description="Trailing buckets; 0 chooses by width")
    ] = 0,
) -> dict:
    """Money per period, for the bar chart.

    Bar width follows the range rather than a fixed count: days at a fortnight
    or less, weeks up to a year, months beyond. A year of daily bars is
    unreadable and a fortnight of monthly ones is a single block, so the
    granularity is a property of the question being asked.

    `direction` is deliberately absent: every point carries out, in and net, so
    a client switching the chart between them redraws rather than re-fetches
    and cannot end up comparing two differently bucketed series.

    Filters are the same as everywhere else, so the chart can be narrowed to a
    bank or a category without a second endpoint.
    """
    since, until = filters["since"], filters["until"]
    days = ((until - since).days + 1) if since and until else None
    chosen = choose_bucket(days) if bucket == "auto" else bucket

    # Resolved here rather than read back off the points: an empty range has no
    # points to read, and a short one reaches only a partial window. Either way
    # a client still has to label the line, and inferring the rule from the data
    # is how the label comes to disagree with the line above it.
    window = rolling or rolling_window(chosen)
    points = handle.repository.spending_trend(
        handle.context, bucket=chosen, rolling=window, **filters
    )
    return {
        "currency": "SGD",
        "bucket": chosen,
        "rolling_window": window,
        "q": filters["q"],
        "range": {"since": since, "until": until, "days": days},
        # Each point carries out, in and net, so one call serves the spending
        # chart, the income chart and the net one without three chances for the
        # filters to drift apart.
        "points": points,
        # Mean and median both, because the gap between them is the
        # information: a mean well away from the median is being carried by a
        # few large one-offs.
        "centre": trend_centre(points),
    }


@app.get(f"{PREFIX}/hidden", tags=["dashboard"])
def hidden(handle: Session) -> dict:
    """What the operator has taken out of the picture, and what it comes to.

    The total is returned with the list on purpose. A dashboard that quietly
    omits things is worth less than one that says what it omitted, and the
    only way to keep hiding honest is to make the size of it visible.
    """
    rows = handle.repository.list_hidden(handle.context)
    return {
        "hidden": rows,
        "count": len(rows),
        "total_minor": sum(r["amount_minor"] for r in rows),
    }


@app.post(f"{PREFIX}/hidden", tags=["dashboard"], status_code=201)
def hide(handle: Session, txn_id: int, note: str = "") -> dict:
    """Hide a transaction from every figure, until it is unhidden.

    Nothing about the transaction changes: this is a claim about what should
    count, held beside the ledger like every other judgement.
    """
    return {"txn_id": txn_id, "hidden": handle.repository.hide_txn(handle.context, txn_id, note)}


@app.delete(f"{PREFIX}/hidden/{{txn_id}}", tags=["dashboard"])
def unhide(handle: Session, txn_id: int) -> dict:
    return {"txn_id": txn_id, "restored": handle.repository.unhide_txn(handle.context, txn_id)}


@app.get(f"{PREFIX}/transactions", tags=["ledger"])
def transactions(
    handle: Session,
    filters: Filters,
    direction: Direction = "out",
    limit: Annotated[int, Query(le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Transactions, newest first. Transfers and hidden rows already excluded.

    Each row carries `amount_minor` as the statement stated it and
    `effective_amount_minor` after anything paid back for it. Show both: a
    figure a client derived itself cannot be audited against the total above it.
    """
    rows = handle.repository.list_spending(
        handle.context, direction=direction, limit=limit, offset=offset, **filters
    )
    return {"transactions": rows, "limit": limit, "offset": offset, "q": filters["q"]}


@app.get(f"{PREFIX}/recurring", tags=["dashboard"])
def recurring(
    handle: Session,
    due_within: Annotated[int, Query(description="Days ahead to flag")] = 14,
) -> dict:
    """Detected series, with what is due and what was missed.

    Lapsed series are reported separately rather than as overdue: a cancelled
    subscription and a skipped payment want opposite reactions.
    """
    rows = handle.repository.list_recurrence_candidates(handle.context)
    series = find_series(
        Occurrence(
            txn_id=r["id"],
            posted_date=r["posted_date"],
            amount_minor=r["amount_minor"],
            merchant_norm=r["counterparty_norm"],
        )
        for r in rows
    )
    today = date.today()
    live = [s for s in series if not s.is_lapsed(today)]

    def _out(s):
        return {
            "merchant": s.merchant_norm,
            "amount_centre_minor": s.amount_centre_minor,
            "monthly_equivalent_minor": s.monthly_equivalent_minor,
            "period_label": s.period_label,
            "occurrences": s.occurrences,
            "total_paid_minor": s.total_paid_minor,
            "first_seen": s.first_seen,
            "last_seen": s.last_seen,
            "expected_next": s.expected_next,
            "confidence": s.confidence,
            "price_changes": [
                {"on": c.on, "from_minor": c.from_minor, "to_minor": c.to_minor}
                for c in s.price_changes
            ],
        }

    return {
        "currency": "SGD",
        "monthly_commitment_minor": sum(s.monthly_equivalent_minor for s in live),
        "series": [_out(s) for s in live],
        "due_soon": [_out(s) for s in live if s.is_due_within(today, due_within)],
        "overdue": [_out(s) for s in live if s.is_overdue(today)],
        "lapsed": [_out(s) for s in series if s.is_lapsed(today)],
    }


@app.get(f"{PREFIX}/review", tags=["categorisation"])
def review(handle: Session, limit: Annotated[int, Query(le=500)] = 50) -> dict:
    """What still needs a person, ranked by what deciding it is worth."""
    rules = RuleSet(
        Rule(pattern=r["pattern"], category=r["category"], weight=r["weight"], note=r["note"])
        for r in handle.repository.list_category_rules(handle.context)
    )
    targets = handle.repository.list_categorisation_targets(handle.context)
    queue = review_queue(
        ((t["counterparty_norm"], t["amount_minor"]) for t in targets), rules
    )
    return {
        "outstanding": len(queue),
        "value_at_stake_minor": sum(i.total_minor for i in queue),
        "items": [
            {
                "counterparty": i.counterparty,
                "occurrences": i.occurrences,
                "total_minor": i.total_minor,
            }
            for i in queue[:limit]
        ],
    }


@app.post(f"{PREFIX}/review/decide", tags=["categorisation"], status_code=201)
def decide(handle: Session, counterparty: str, category: str) -> dict:
    """Settle one counterparty, for good.

    Stored as a rule rather than a row edit, so it covers the past and the
    future together, and weighted above anything imported: deciding by hand
    ends the argument rather than adding a vote to it.
    """
    handle.repository.seed_categories(handle.context, DEFAULT_CATEGORIES)
    known = {c["name"].lower(): c["name"] for c in handle.repository.list_categories(handle.context)}
    chosen = known.get(category.lower())
    if chosen is None:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown category", "known": sorted(known.values())},
        )
    added = handle.repository.add_category_rules(
        handle.context, [operator_rule(counterparty, chosen)]
    )
    return {
        "counterparty": counterparty,
        "category": chosen,
        "created": bool(added),
        # The ledger is not rewritten here: applying is a separate, explicit
        # pass so a run of decisions costs one write rather than one each.
        "applied": False,
    }


@app.get(f"{PREFIX}/transfers", tags=["ledger"])
def transfers(handle: Session) -> dict:
    """Movements between the household's own accounts.

    Exposed because their absence from every spending figure is a claim the
    client should be able to show its user rather than merely assert.
    """
    return {"linked": handle.repository.count_transfer_links(handle.context)}


@app.post(f"{PREFIX}/transfers/mark", tags=["ledger"], status_code=201)
def mark_transfer(
    handle: Session,
    txn_id: int,
    counterpart_id: Annotated[int | None, Query(description="The other leg, if held here")] = None,
) -> dict:
    """Say a row is a transfer when the matcher could not prove it.

    The automatic pass pairs only what it can prove — equal magnitude, opposite
    signs, a few days apart, across two accounts. A counterpart at a bank this
    ledger does not hold, a partial payment, or two equally good candidates all
    fall outside that and are all ordinary.

    Marked links carry `origin: manual` and survive a re-run of the matcher,
    which rebuilds only what it found itself.
    """
    marked = handle.repository.mark_transfer(handle.context, txn_id, counterpart_id)
    return {"txn_id": txn_id, "counterpart_id": counterpart_id, "marked": marked}


@app.delete(f"{PREFIX}/transfers/mark/{{txn_id}}", tags=["ledger"])
def unmark_transfer(handle: Session, txn_id: int) -> dict:
    """Undo an operator's mark. The matcher's own links are untouched."""
    return {"txn_id": txn_id, "unmarked": handle.repository.unmark_transfer(handle.context, txn_id)}


# --- paybacks ---------------------------------------------------------------
# One person pays for a group and the others settle up. Without this the ledger
# reads a $300 dinner and $270 of income from nowhere, when $30 was spent and
# nothing was earned. Distinct from a transfer, which excludes both legs — here
# the charge was real, just smaller than the statement says.


@app.get(f"{PREFIX}/paybacks", tags=["ledger"])
def paybacks(handle: Session, expense_txn_id: Annotated[int | None, Query()] = None) -> dict:
    """What has come back, and against which charge."""
    rows = handle.repository.list_paybacks(handle.context, expense_txn_id)
    return {
        "paybacks": rows,
        "count": len(rows),
        "total_minor": sum(r["amount_minor"] for r in rows),
    }


@app.get(f"{PREFIX}/paybacks/candidates", tags=["ledger"])
def payback_candidates(
    handle: Session,
    expense_txn_id: int,
    q: Annotated[str | None, Query(description="Free text over the inflows")] = None,
    days: Annotated[
        int | None, Query(ge=1, le=3650, description="Only within this many days of the charge"),
    ] = None,
    limit: Annotated[int, Query(le=500)] = 100,
) -> dict:
    """Inflows that could be somebody settling this charge.

    Nearest the charge's own date first, because a payback usually follows
    within days. Anything already spoken for — settling another charge, part of
    a transfer, hidden — is not offered at all: linking it would be refused, and
    offering a choice that cannot be taken is worse than not offering it.
    """
    try:
        rows = handle.repository.list_payback_candidates(
            handle.context, expense_txn_id, q=q, within_days=days, limit=limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"candidates": rows, "count": len(rows)}


@app.post(f"{PREFIX}/paybacks", tags=["ledger"], status_code=201)
def link_paybacks(
    handle: Session,
    expense_txn_id: int,
    income_txn_id: Annotated[list[int], Query(description="Repeatable")],
    note: str = "",
) -> dict:
    """Say these inflows settle part of this charge.

    Repeatable `income_txn_id`, so linking five people is one round trip and
    either all of it lands or none of it does. A partial success here would
    leave a charge discounted by an amount the operator never chose.
    """
    try:
        result = handle.repository.link_paybacks(
            handle.context, expense_txn_id, income_txn_id, note=note,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # 422 rather than 400: the request was well formed, the ledger says no.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"expense_txn_id": expense_txn_id, **result}


@app.delete(f"{PREFIX}/paybacks/{{expense_txn_id}}", tags=["ledger"])
def unlink_paybacks(
    handle: Session,
    expense_txn_id: int,
    income_txn_id: Annotated[
        int | None, Query(description="Just this one; omit to unlink them all"),
    ] = None,
) -> dict:
    """Undo a payback. The charge goes back to what the statement said."""
    removed = handle.repository.unlink_paybacks(handle.context, expense_txn_id, income_txn_id)
    return {"expense_txn_id": expense_txn_id, "unlinked": removed}


@app.get(f"{PREFIX}/growth", tags=["dashboard"])
def growth(
    handle: Session,
    months: Annotated[int, Query(ge=1, le=120, description="Window, user setting")] = 6,
    every: Annotated[str, Query(pattern="^(month|statement)$")] = "month",
) -> dict:
    """Net worth over time, from the balances the statements declared.

    Not a sum of transactions: that would read a transfer between the
    household's own accounts as growth on one side and loss on the other, and
    would count a card the wrong way round. Card balances are stored negated,
    so a debt subtracts without this needing to know which is which.

    `accounts_known` on each point is how many accounts had declared anything
    by then. It rises as history fills in, and a client should say so rather
    than let an early point look like a real dip.
    """
    since = date.today() - timedelta(days=31 * months)
    rows = handle.repository.balance_history(handle.context, since=since)
    points = net_worth(
        [
            Declared(
                period_end=r["period_end"],
                account_id=r["account_id"],
                closing_balance_minor=r["closing_balance_minor"],
            )
            for r in rows
        ],
        every=every,
    )
    return {
        "currency": "SGD",
        "window_months": months,
        "since": since,
        "points": [
            {"on": p.on, "total_minor": p.total_minor, "accounts_known": p.accounts_known}
            for p in points
        ],
        # The arrow and the percentage §5.1(D) asks for. `percent` is null where
        # the window opened at zero or in debt, because there is no honest
        # percentage of that and a number here carries a feeling.
        "change": change(points),
    }


@app.post(f"{PREFIX}/categories", tags=["categorisation"], status_code=201)
def add_category(handle: Session, name: str) -> dict:
    """Add a category. The taxonomy is the tenant's to shape."""
    cleaned = " ".join(name.split())
    if not cleaned:
        raise HTTPException(status_code=422, detail={"error": "name is required"})
    existing = {c["name"].lower() for c in handle.repository.list_categories(handle.context)}
    if cleaned.lower() in existing:
        raise HTTPException(status_code=409, detail={"error": "category already exists"})
    handle.repository.add_category(handle.context, cleaned)
    return {"name": cleaned, "created": True}


@app.post(f"{PREFIX}/transactions/{{txn_id}}/category", tags=["categorisation"])
def set_category(handle: Session, txn_id: int, category: str) -> dict:
    """Correct one transaction's category, by hand.

    Written as `source='human'`, which the rule pass is built never to
    overwrite: the whole value of correcting something is that it stays
    corrected, and a pass that could undo it would make every correction
    provisional.

    This is the row-level counterpart to `/review/decide`. That settles a
    counterparty and every transaction with it; this settles one transaction
    where the merchant is right in general and wrong here.
    """
    known = {c["name"].lower(): c["name"] for c in handle.repository.list_categories(handle.context)}
    chosen = known.get(category.lower())
    if chosen is None:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown category", "known": sorted(known.values())},
        )
    handle.repository.set_human_category(handle.context, txn_id, chosen)
    return {"txn_id": txn_id, "category": chosen, "source": "human"}


@app.delete(f"{PREFIX}/transactions/{{txn_id}}/category", tags=["categorisation"])
def clear_category(handle: Session, txn_id: int) -> dict:
    """Undo a correction, letting the rules decide again."""
    return {
        "txn_id": txn_id,
        "cleared": handle.repository.clear_human_category(handle.context, txn_id),
    }
