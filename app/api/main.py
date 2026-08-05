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

from fastapi import Depends, FastAPI, HTTPException, Query

from ..config import PROFILE_PROD, Config, load_config
from ..domain.categories import DEFAULT_CATEGORIES, Rule, operator_rule, review_queue
from ..domain.recurrence import Occurrence, find_series
from ..storage.factory import build_repository

API_VERSION = "v1"
PREFIX = f"/api/{API_VERSION}"

app = FastAPI(
    title="Finstone",
    version=API_VERSION,
    summary="Self-hosted personal finance ledger",
)


def _config() -> Config:
    return load_config()


class _Session:
    """One request's repository and tenant.

    Opened and closed per request rather than held: the tenant is a property of
    the caller, and a long-lived handle bound to one tenant is how a hosted
    deployment leaks between households.
    """

    def __init__(self, config: Config, profile: str):
        self.config = config
        self.profile = profile
        self.repository = build_repository(config)
        self.context = self.repository.resolve_context(
            config.tenant_for(profile), config.member_email
        )

    def close(self) -> None:
        self.repository.close()


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
) -> dict:
    """The three axes every figure is derivable from. See §5.1(B)."""
    return {
        "since": since,
        "until": until,
        "account_ids": account_id,
        "categories": category,
    }


Filters = Annotated[dict, Depends(_filters)]


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


@app.get(f"{PREFIX}/summary", tags=["dashboard"])
def summary(handle: Session, filters: Filters) -> dict:
    """Spending by category, plus the per-month, per-week and per-day averages.

    The averages span **the same range as the totals**. Computing them over a
    different window is how two halves of one screen come to describe different
    periods — see §5.1(A).
    """
    rows = handle.repository.spending_summary(handle.context, **filters)
    total = sum(r["total_minor"] or 0 for r in rows)

    since, until = filters["since"], filters["until"]
    days = ((until - since).days + 1) if since and until else None

    return {
        "currency": "SGD",
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


@app.get(f"{PREFIX}/transactions", tags=["ledger"])
def transactions(
    handle: Session,
    filters: Filters,
    limit: Annotated[int, Query(le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Spending rows, newest first. Transfers are already excluded."""
    rows = handle.repository.list_spending(
        handle.context, limit=limit, offset=offset, **filters
    )
    return {"transactions": rows, "limit": limit, "offset": offset}


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
    rules = [
        Rule(pattern=r["pattern"], category=r["category"], weight=r["weight"], note=r["note"])
        for r in handle.repository.list_category_rules(handle.context)
    ]
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


@app.get(f"{PREFIX}/growth", tags=["dashboard"])
def growth(
    handle: Session,
    months: Annotated[int, Query(ge=1, le=60, description="Window, user setting")] = 6,
) -> dict:
    """Net worth over time.

    **Not implemented.** Growth is balance over time and must come from
    `statement_balance`, not from summing `txn`: summing transactions makes a
    transfer between the household's own accounts look like growth in one
    direction and loss in the other. The repository has no balance-history
    query yet, and returning a plausible-looking series computed the wrong way
    would be worse than returning nothing. See §5.1(A).
    """
    raise HTTPException(
        status_code=501,
        detail={
            "error": "not implemented",
            "reason": "growth must be derived from statement balances, not transaction sums",
            "window_months": months,
            # Serialised here: an exception detail is encoded as plain JSON and
            # never sees the response model's encoders.
            "since": (date.today() - timedelta(days=30 * months)).isoformat(),
        },
    )
