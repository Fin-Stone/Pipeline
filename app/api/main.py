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
from pathlib import Path
from typing import Annotated

import dataclasses
import os
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import PROFILE_PROD, Config, load_config
from ..domain.categories import (
    DEFAULT_CATEGORIES,
    Rule,
    RuleSet,
    operator_rule,
    review_queue,
    rule_origin,
)
from ..domain.networth import Declared, change, net_worth
from ..domain.recurrence import Occurrence, find_series
from ..domain.transfers import Window
from ..pipeline.transfers import preview, realign, save_window, window_for
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


#: Where a built client lives, when this image carries one.
#:
#: Carrying it is what makes **one container** enough — the shape a self-hoster
#: expects, and the difference between `docker run` and a compose file with
#: three services in it. Nothing here depends on it: with no build present this
#: is an API and says so, which is what a `pip install` and every test gets.
WEB_ROOT = Path(os.environ.get("FINSTONE_WEB_ROOT", "/srv/finstone/web"))


def _serving_client() -> bool:
    return (WEB_ROOT / "index.html").is_file()


class _Assets(StaticFiles):
    """Hashed filenames, so they can be cached hard.

    Same policy as the nginx image serves, because the two are alternatives and
    a returning browser must not get a different answer depending on which one
    is in front of it.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


if (WEB_ROOT / "assets").is_dir():
    app.mount("/assets", _Assets(directory=WEB_ROOT / "assets"), name="assets")


def _api_root() -> dict:
    return {
        "service": "finstone",
        "api_version": API_VERSION,
        "api_root": PREFIX,
        "docs": "/docs",
        "openapi": "/openapi.json",
        "note": "This is the API. The UI is a separate application that connects to it.",
    }


@app.get("/", tags=["meta"])
def root():
    """Say what this is, to whoever opened the address in a browser.

    A bare 404 here is technically correct and useless: the first thing an
    operator does with a new self-hosted service is visit its root, and telling
    them nothing is how a working install looks broken.

    When the image carries a client, this *is* the client. When it does not,
    the JSON above is the honest answer.
    """
    if _serving_client():
        return _index()
    return _api_root()


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


# ------------------------------------------------------------------ documents ---
#: The largest statement this will take. Bank PDFs are hundreds of kilobytes;
#: a year of them bundled is a few megabytes. Well above anything real and far
#: below what would let one request fill the disk.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

#: Refused before anything is written. An extension is a claim; these are what
#: the file actually is. A `.pdf` that is not a PDF has either gone wrong on
#: the way here or is not a statement, and either way the parser is the wrong
#: place to find out.
_MAGIC = {
    b"%PDF": ".pdf",
    b"OFXH": ".ofx",
    b"<OFX": ".ofx",
    b"<?xm": ".xml",
}


def _accepted_name(name: str) -> str:
    """A filename safe to write, keeping enough of the original to recognise.

    Path separators and `..` are stripped rather than rejected: the operator
    did not choose the name their bank generated, and refusing an upload over
    a character they cannot see is a worse answer than saving it as something
    sensible.
    """
    stem = Path(name or "").name.replace("\\", "_")
    cleaned = "".join(c for c in stem if c.isalnum() or c in "-_. ").strip(". ")
    return cleaned or "upload.pdf"


def _looks_like_a_document(head: bytes, name: str) -> bool:
    if any(head.startswith(magic) for magic in _MAGIC):
        return True
    # Text formats with no magic number of their own. Judged by extension,
    # because that is genuinely all there is to go on for a CSV.
    return Path(name).suffix.lower() in {".csv", ".qif", ".sta", ".mt940"}


@app.post(f"{PREFIX}/documents", tags=["ledger"], status_code=201)
async def upload_documents(
    handle: Session,
    files: Annotated[list[UploadFile], File(description="Statement files")],
) -> dict:
    """Take statements over HTTP and put them through the pipeline.

    The alternative was `scp` and a shell, which is a fine answer for the
    person who built this and a poor one for the person living with it.

    Files land in the inbox rather than in `uploads/`. `uploads/` is the
    operator's own folder and is mounted read-only for exactly that reason —
    the pipeline copies out of it and never writes to it. Nothing is lost: the
    original bytes go into the content-addressed store, which is where the
    durable copy has always lived.

    That also settles `FINSTONE_ALLOW_PROD`, which is not checked here and is
    not being evaded. It guards *reading `uploads/prod`* — a folder of real
    statements that automation must never walk on its own initiative. A file
    handed over in a request is the deliberate act the flag exists to require,
    and it is not in that folder.

    **The transfer matcher runs afterwards.** A statement arriving can complete
    a pair that has been waiting for it — a card payment whose other leg was
    never imported — and leaving that until somebody remembers to re-run is
    how the ledger ends up quietly overstating spending.

    Every file is reported on its own. One unparseable statement in a batch of
    twelve must not look like twelve failures, and a duplicate is a normal
    outcome rather than an error: re-uploading what is already imported is what
    somebody does when they are not sure whether they did.

    Ingestion drains the whole inbox for this profile rather than only what
    arrived here. The inbox is a queue, and a file left in it by a run that
    failed halfway should not need a second mechanism to pick it up.
    """
    from ..pipeline.ingest import ingest_inbox
    from ..pipeline.transfers import realign
    from ..storage.factory import build_blob_store, build_notifier

    config = handle.config
    inbox = config.inbox_dir / handle.profile / "uploaded"
    inbox.mkdir(parents=True, exist_ok=True)

    accepted, rejected = [], []
    for upload in files:
        name = _accepted_name(upload.filename or "")
        body = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(body) > MAX_UPLOAD_BYTES:
            rejected.append({"filename": name, "reason": "larger than 32 MB"})
            continue
        if not body:
            rejected.append({"filename": name, "reason": "empty"})
            continue
        if not _looks_like_a_document(body[:4], name):
            rejected.append({
                "filename": name,
                "reason": "not a statement — expected a PDF, OFX, CSV or MT940",
            })
            continue
        # Written under a name that cannot collide with a concurrent upload of
        # the same statement. Duplicate *content* is caught by the digest a
        # moment later, which is the check that matters.
        target = inbox / name
        if target.exists():
            target = inbox / f"{Path(name).stem}-{uuid4().hex[:8]}{Path(name).suffix}"
        target.write_bytes(body)
        accepted.append({"filename": name, "path": target})

    summary = None
    if accepted:
        summary = ingest_inbox(
            config, handle.context, handle.repository,
            build_blob_store(config), build_notifier(config),
            profile=handle.profile,
        )

    # Matched back by path, so a batch reports per file rather than in total.
    outcomes = {str(o.path): o for o in (summary.outcomes if summary else ())}
    imported = []
    for item in accepted:
        outcome = outcomes.get(str(item["path"]))
        imported.append({
            "filename": item["filename"],
            "status": outcome.status if outcome else "pending",
            "sha256": outcome.sha256 if outcome else None,
            "transactions": outcome.txns_inserted if outcome else 0,
            # Why it was set aside, in the words the pipeline used. A
            # quarantined statement is not a failure of this endpoint and the
            # operator needs the reason, not a status code.
            "reason": outcome.reason if outcome else None,
        })

    repaired = None
    if summary and (summary.imported or summary.unverified):
        outcome = realign(handle.repository, handle.context)
        repaired = {
            "added": len(outcome.added),
            "removed": len(outcome.removed),
            "linked": len(outcome.result.links),
        }

    return {
        "accepted": len(accepted),
        "rejected": rejected,
        "documents": imported,
        "imported": summary.imported if summary else 0,
        "duplicates": summary.duplicates if summary else 0,
        "quarantined": summary.quarantined if summary else 0,
        "transactions": summary.txns_inserted if summary else 0,
        # What re-pairing did, because an upload changes spending figures in
        # two ways and only one of them is the new rows.
        "transfers": repaired,
    }


@app.get(f"{PREFIX}/documents", tags=["ledger"])
def documents(
    handle: Session,
    parse_status: str | None = None,
    limit: Annotated[int, Query(le=1000)] = 200,
) -> dict:
    """What has been imported — the listing half of contract rule 2a.

    An upload adds transactions to every figure on the dashboard. Without a way
    to see what is in the ledger and take one back out, that is a one-way door.
    """
    rows = handle.repository.list_documents(handle.context, parse_status)
    return {"total": len(rows), "documents": rows[:limit]}


@app.delete(f"{PREFIX}/documents/{{sha256}}", tags=["ledger"])
def delete_document(handle: Session, sha256: str) -> dict:
    """Remove a document and everything it brought with it.

    The inverse of an upload, and the reason an upload is safe to try. It takes
    the transactions with it — that is the point — along with the paybacks,
    hidden marks, categories and transfer links that pointed at them, because
    leaving those behind would break the foreign keys and, worse, leave
    decisions attached to rows that no longer exist.

    **The original file is kept.** It is content-addressed and immutable, and
    the whole design says the bytes a bank sent are the one thing never thrown
    away. Re-uploading the same statement restores it.

    `deleted: false` for a digest that is not here, rather than a 404 — the
    same shape as unhiding twice.
    """
    removed = handle.repository.delete_document(handle.context, sha256)
    repaired = None
    if removed:
        # The rows that anchored a pair are gone, so the pair must go too.
        outcome = realign(handle.repository, handle.context)
        repaired = {"linked": len(outcome.result.links), "removed": len(outcome.removed)}
    return {"sha256": sha256, "deleted": bool(removed), "transfers": repaired}


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


def _literal_name(pattern: str) -> str | None:
    """The plain name a pattern matches, if it matches exactly one.

    `None` for a real expression, and also for a pattern that no longer
    compiles: a single unusable rule left by some past import must not be able
    to take down the listing that exists to let somebody remove it.
    """
    try:
        return Rule(pattern=pattern, category="").literal
    except ValueError:
        return None


def _decisions_for(handle: Session, counterparty: str) -> list[dict]:
    """The operator's own rules that settle exactly this counterparty.

    Matched on the name rather than on the pattern, because a client that made
    a decision knows what it decided about and should not have to reconstruct
    the escaping to take it back. Case-insensitive for the same reason.
    """
    wanted = (counterparty or "").strip().upper()
    if not wanted:
        return []
    return [
        r for r in handle.repository.list_category_rules(handle.context)
        if rule_origin(r["weight"], r["note"]) == "operator"
        and _literal_name(r["pattern"]) == wanted
    ]


@app.delete(f"{PREFIX}/review/decide", tags=["categorisation"])
def undecide(handle: Session, counterparty: str) -> dict:
    """Take back a decision, in the same words it was made in.

    The inverse of `POST /review/decide`, keyed on the counterparty rather than
    on a rule id: a client working through the queue decided about a *name*,
    and asking it to remember an id it was never shown would put the undo out
    of reach of the screen that needs it.

    Only the operator's own rules are in scope. An imported one was nobody's
    decision, is not what this route promised to reverse, and has `DELETE
    /rules/{id}` for when it really is the thing in the way.

    Removing nothing is not an error — `removed` is `0` — so a second click
    does what the first one did.
    """
    doomed = _decisions_for(handle, counterparty)
    for rule in doomed:
        handle.repository.delete_category_rule(handle.context, rule["id"])
    return {
        "counterparty": counterparty,
        "removed": len(doomed),
        # What it used to say, so the client can put it back verbatim.
        "was": [{"pattern": r["pattern"], "category": r["category"]} for r in doomed],
        # Symmetric with deciding: neither writes through the ledger, so a
        # queue can be worked and reworked for one pass at the end.
        "applied": False,
    }


@app.get(f"{PREFIX}/rules", tags=["categorisation"])
def rules(
    handle: Session,
    origin: Annotated[str, Query(pattern="^(operator|imported|all)$")] = "operator",
    q: str | None = None,
    limit: Annotated[int, Query(le=500)] = 100,
) -> dict:
    """The rules deciding this ledger, and who put each one there.

    Defaults to `operator` because that is the answer to the question anybody
    actually arrives with — *what have I decided?* The imported set is large,
    was nobody's decision, and reads as noise beside a handful of deliberate
    ones.

    Newest first. A rule somebody wants to find is nearly always the one they
    just wrote, and weight order buries it among everything else at 100.

    `transactions` is how many rows a rule's name accounts for today, so
    removing one can be a considered act rather than a guess. It is counted
    only for rules that match a single literal name — anything with real regex
    in it would need a scan, and none of what this route is for has any.
    """
    stored = handle.repository.list_category_rules(handle.context)
    wanted = [
        (r, rule_origin(r["weight"], r["note"]), _literal_name(r["pattern"]))
        for r in stored
    ]
    if origin != "all":
        wanted = [w for w in wanted if w[1] == origin]
    if q:
        needle = q.strip().upper()
        wanted = [
            w for w in wanted
            if needle in w[0]["pattern"].upper() or needle in w[0]["category"].upper()
        ]

    total = len(wanted)
    # By id, which is insertion order. `created_at` would say the same thing
    # and is naive on one engine and aware on the other, so sorting on it is a
    # portability hazard for no gain.
    wanted.sort(key=lambda w: w[0]["id"], reverse=True)
    shown = wanted[:limit]
    counts = handle.repository.counterparty_row_counts(
        handle.context, [literal for _, _, literal in shown if literal]
    )
    return {
        "total": total,
        "rules": [
            {
                "id": r["id"],
                "pattern": r["pattern"],
                "category": r["category"],
                "weight": r["weight"],
                "note": r["note"],
                "created_at": r["created_at"],
                "origin": origin_of,
                # The plain name, where there is one. A client showing
                # `^IKEA\-RESTAURANT$` to a person is showing them the
                # implementation of their own decision.
                "counterparty": literal,
                "transactions": counts.get(literal) if literal else None,
            }
            for r, origin_of, literal in shown
        ],
    }


@app.post(f"{PREFIX}/rules", tags=["categorisation"], status_code=201)
def add_rule(
    handle: Session,
    pattern: str,
    category: str,
    weight: int = 0,
    note: str = "",
) -> dict:
    """Put a rule back — the inverse of removing one.

    Exists so that `DELETE /rules/{id}` is not a one-way door for the imported
    set, which nothing in the API could otherwise restore. Deliberately takes
    the pattern verbatim rather than a counterparty: what is being undone is a
    rule, and rebuilding one from a name would not reproduce a real expression.

    `422` for a pattern that is not a valid expression, rather than storing
    something that will throw on the next categorisation pass.
    """
    handle.repository.seed_categories(handle.context, DEFAULT_CATEGORIES)
    known = {c["name"].lower(): c["name"] for c in handle.repository.list_categories(handle.context)}
    chosen = known.get(category.lower())
    if chosen is None:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown category", "known": sorted(known.values())},
        )
    try:
        Rule(pattern=pattern, category=chosen)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc)}) from exc

    added = handle.repository.add_category_rules(
        handle.context, [(pattern, chosen, weight, note)]
    )
    restored = next(
        (r for r in handle.repository.list_category_rules(handle.context)
         if r["pattern"] == pattern and r["category"] == chosen),
        None,
    )
    return {
        # The row is a new row, and its id is whatever the engine assigned —
        # SQLite reuses one just freed. Named here so a client that deleted a
        # rule and put it back is not left holding an id nothing answers to.
        "id": restored["id"] if restored else None,
        "pattern": pattern,
        "category": chosen,
        "created": bool(added),
        "applied": False,
    }


@app.delete(f"{PREFIX}/rules/{{rule_id}}", tags=["categorisation"])
def delete_rule(handle: Session, rule_id: int) -> dict:
    """Remove one rule, and say what it was.

    `was` is the whole point: a rule id means nothing once the row is gone, so
    the response carries everything `POST /rules` needs to put it back. Without
    that, undo would be a promise the client could not keep.

    Deleting something already gone is `deleted: false`, not `404` — the same
    shape as unhiding twice.
    """
    found = next(
        (r for r in handle.repository.list_category_rules(handle.context)
         if r["id"] == rule_id),
        None,
    )
    if found is None:
        return {"rule_id": rule_id, "deleted": False, "was": None, "applied": False}
    handle.repository.delete_category_rule(handle.context, rule_id)
    return {
        "rule_id": rule_id,
        "deleted": True,
        "was": {
            "pattern": found["pattern"], "category": found["category"],
            "weight": found["weight"], "note": found["note"],
        },
        "applied": False,
    }


def _window_json(w: Window) -> dict:
    return {
        "min_days": w.min_days, "max_days": w.max_days,
        "named_days": w.named_days, "card_days": w.card_days,
    }


def _realignment_json(outcome) -> dict:
    return {
        "window": _window_json(outcome.window),
        "found": len(outcome.result.links),
        "by_evidence": outcome.by_evidence,
        "rows_excluded": len(outcome.result.linked_txn_ids),
        "value_minor": outcome.value_minor,
        # The diff, which is the actual decision. "206 links" says nothing
        # about whether to apply; "9 new, 2 gone" is the whole question.
        "added": len(outcome.added),
        "removed": len(outcome.removed),
        "unchanged": outcome.unchanged,
        "manual": outcome.manual,
        "ambiguous": [
            {
                "txn_id": a.txn_id,
                "amount_minor": a.amount_minor,
                "posted_date": a.posted_date,
                "candidate_txn_ids": list(a.candidate_txn_ids),
            }
            for a in outcome.result.ambiguous
        ],
        "applied": outcome.applied,
    }


@app.get(f"{PREFIX}/transfers", tags=["ledger"])
def transfers(handle: Session) -> dict:
    """Movements between the household's own accounts, and the rule in force.

    Exposed because their absence from every spending figure is a claim the
    client should be able to show its user rather than merely assert — and
    because a window nobody can read is a window nobody can fix.
    """
    return {
        "linked": handle.repository.count_transfer_links(handle.context),
        "manual": len(handle.repository.list_manual_transfers(handle.context)),
        "window": _window_json(window_for(handle.repository, handle.context)),
        "defaults": _window_json(Window()),
    }


@app.post(f"{PREFIX}/transfers/rematch", tags=["ledger"])
def rematch_transfers(
    handle: Session,
    min_days: Annotated[int | None, Query(ge=0, le=365)] = None,
    max_days: Annotated[int | None, Query(ge=0, le=365)] = None,
    named_days: Annotated[int | None, Query(ge=0, le=365)] = None,
    card_days: Annotated[int | None, Query(ge=0, le=365)] = None,
    apply: bool = False,
    save: bool = False,
) -> dict:
    """Pair the transfers again, and say what would change.

    **Reports by default and writes only with `apply=true`**, because the pass
    changes what the ledger *means* — a linked pair stops counting as spending —
    and a client should be able to show that before it is true.

    The window widens with the strength of the evidence, so there is one per
    kind. `max_days` covers amount and date alone, which is the weakest claim
    two rows can make; `named_days` covers one leg naming the other's account
    number, which is near-proof; `card_days` covers a deposit account paying a
    card, which is a transfer by construction whatever the dates say. Omitted
    values keep whatever this tenant already chose.

    `save=true` remembers the window. It is stored against the *ledger*, not the
    machine, so it survives a backup and a move to another box: how far apart
    two banks book a transfer is a fact about the banks.
    """
    window = window_for(handle.repository, handle.context)
    overrides = {
        field: value
        for field, value in (
            ("min_days", min_days), ("max_days", max_days),
            ("named_days", named_days), ("card_days", card_days),
        )
        if value is not None
    }
    try:
        window = dataclasses.replace(window, **overrides)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc)}) from exc

    outcome = (realign if apply else preview)(handle.repository, handle.context, window)
    if apply and save:
        save_window(handle.repository, handle.context, window)
    return {**_realignment_json(outcome), "saved": bool(apply and save)}


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


@app.get(f"{PREFIX}/transfers/marked", tags=["ledger"])
def marked_transfers(handle: Session) -> dict:
    """Rows a person said were transfers, so they can say otherwise later.

    Only manual marks. The matcher's own links are regenerable and are not
    decisions anybody has to walk back — but a manual mark takes a row out of
    every figure on one click, and a client that cannot list them cannot offer
    a way back. An action with no route back is not a feature.
    """
    rows = handle.repository.list_manual_transfers(handle.context)
    return {
        "marked": rows,
        "count": len(rows),
        "total_minor": sum(r["txn_amount_minor"] for r in rows),
    }


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


@app.delete(f"{PREFIX}/categories/{{name}}", tags=["categorisation"])
def delete_category(handle: Session, name: str) -> dict:
    """Remove a category nothing is using.

    Exists so that adding one is reversible. It refuses while any rule or
    transaction still references it, rather than cascading: deleting a category
    that rows are filed under would either orphan them or silently re-file them,
    and neither is something a person can undo.

    Re-filing first is the operator's call, and the refusal says how much there
    is to re-file.
    """
    try:
        removed = handle.repository.delete_category(handle.context, name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"no category {name!r}")
    return {"name": name, "deleted": True}


@app.get(f"{PREFIX}/categories/{{name}}/usage", tags=["categorisation"])
def category_usage(handle: Session, name: str) -> dict:
    """What references a category, so a client can say whether it can go."""
    return {"name": name, **handle.repository.category_usage(handle.context, name)}


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


# --------------------------------------------------------------- the client ---
# Everything below must stay at the bottom of this file. Starlette matches
# routes in the order they were added, and the catch-all here would otherwise
# swallow every API route declared after it.


def _index() -> FileResponse:
    """The single page, never cached.

    The assets beside it are content-hashed and cached for a year; this file
    names them. Cache it and an upgraded container keeps serving the previous
    build to a returning browser, which is how a self-hoster ends up running
    two versions at once and reporting bugs from neither.
    """
    return FileResponse(
        WEB_ROOT / "index.html",
        headers={"Cache-Control": "no-store"},
    )


#: Paths that belong to the server whatever else is being served.
#:
#: Without this, a mistyped API call would come back as HTML with a 200 on it,
#: and a client would parse the page it is running in as a ledger.
_SERVER_PREFIXES = ("api/", "docs", "redoc", "openapi.json")


@app.get("/{path:path}", include_in_schema=False)
def client(path: str):
    """Any other path is the app itself — or an honest 404.

    A single-page app owns its own routing, so a refresh on any screen but the
    first has to return the page rather than a 404. That is the whole of this
    route.
    """
    if path.startswith(_SERVER_PREFIXES):
        raise HTTPException(status_code=404, detail=f"no such endpoint: /{path}")
    if not _serving_client():
        # An API-only install. Saying so beats returning the SPA's 404 screen
        # for something that was never going to be a screen.
        raise HTTPException(
            status_code=404,
            detail=f"no such endpoint: /{path}. This server carries no client; "
                   f"the API is at {PREFIX}.",
        )
    return _index()
