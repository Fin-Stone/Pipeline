# API contracts

**This document is the contract.** A client is written against what is written
here, and a self-hosted server promises it. A route that drifts from this
document breaks somebody's install rather than somebody's build.

> **If you are building a UI or any other client, read this first.**
> **If you are changing `app/api/`, change this in the same commit.**
> That obligation is in [../AGENTS.md](../AGENTS.md) and is not optional.

Implementation: [`app/api/main.py`](../app/api/main.py).
Tests asserting these promises: [`tests/test_api.py`](../tests/test_api.py).
Run it: `pip install -e .[api]` then `uvicorn app.api.main:app`.
Interactive schema: `/docs`, machine-readable at `/openapi.json`.

---

## The promises

These hold across every endpoint. They are the parts a client may rely on
without checking.

**1. Money is always integer minor units, and never a float.** Every monetary
field ends in `_minor` and is a whole number of cents. A JSON float would
reintroduce exactly the error the ledger exists to avoid, so no endpoint emits
one — not for a total, not for an average, not for a rounded display value.
Formatting is the client's job.

**2. Amounts are signed by effect on the account.** Spending is **negative**,
money in is positive, and card balances are stored negated so one formula
covers both. The API does not flip signs for presentation.

**3. Every figure is derivable from `(date range, accounts, categories)`.**
There are no precomputed per-month aggregates, because §5.1(B) requires the
dashboard to re-range and re-filter on demand. Any endpoint that reports
numbers accepts the filter parameters below.

**4. Transfers are already excluded from every spending figure.** A movement
between the household's own accounts is not expenditure; including it would
overstate spending by the size of every credit-card payment. A client does not
need to filter them out and must not try.

**5. The version is in the path.** `/api/v1/...`. A self-hosted server lags the
hosted one, so a client checks `GET /health` and decides whether it can talk to
this server at all. This is what stops a client release breaking a self-hoster
who has not upgraded — see architecture §5.2.

**6. Unknowns are refused, never guessed.** An unknown profile is `400`, an
unknown category is `422` *with the list of known ones*, and a figure that
cannot be computed correctly is `501` with the reason. A plausible number
computed the wrong way is worse than no number.

---

## Filter parameters

Accepted by `/summary` and `/transactions`. All optional; omitted means
unbounded.

| Parameter | Type | Notes |
|---|---|---|
| `profile` | `dummy` \| `prod` | Which tenant's ledger. Defaults to `prod`. |
| `since` | date | Inclusive start, `YYYY-MM-DD` |
| `until` | date | Inclusive end |
| `account_id` | int | Repeatable: `?account_id=1&account_id=2` |
| `category` | string | Repeatable |

---

## Endpoints

### `GET /api/v1/health`

```json
{ "status": "ok", "api_version": "v1" }
```

Call before anything else. `api_version` is how a client decides it can speak
to this server.

### `GET /api/v1/accounts`

Every account in the tenant: `id`, `institution`, `account_ref_masked`,
`sub_account_label`, `currency`, `kind` (`deposit` | `card`).

### `GET /api/v1/categories`

The tenant's taxonomy: `id`, `name`, `position`. Seeded with the default set on
first call and editable thereafter — a household's budgeting structure is its
own, so **a client must render whatever it is given and never hardcode this
list.**

### `GET /api/v1/summary`

The consolidated view behind §5.1(A).

```json
{
  "currency": "SGD",
  "range": { "since": "2026-01-01", "until": "2026-01-31", "days": 31 },
  "total_minor": -123456,
  "by_category": [{ "category": "Grocery", "rows": 42, "total_minor": -50000 }],
  "average_minor": { "per_day": -3982, "per_week": -27874, "per_month": -119460 }
}
```

**The averages span the same range as the totals.** Computing them over a
different window is how two halves of one screen come to describe different
periods, which §5.1(A) exists to prevent.

`average_minor` fields are **`null` when the range is open-ended**, because an
average over an unbounded period is not a small number — it is not a number. A
client must render that as "—" and not as zero.

`category` is `null` for rows nothing has categorised yet. That bucket is
meant to be visible: a large one means the rules are behind.

### `GET /api/v1/transactions`

Spending rows, newest first, with `limit` (≤1000, default 200) and `offset`.
Each row carries `id`, `posted_date`, `amount_minor`, `currency`,
`counterparty_norm`, `account_id`, `institution`, `account_ref_masked`,
`category`, `source`.

`source` is `rule` \| `knn` \| `llm` \| `human`. A client should show which,
because §5.1 asks users to be able to tell what was decided automatically from
what they corrected.

### `GET /api/v1/recurring`

Behind the recurring-payments page, §5.1(C).

```json
{
  "currency": "SGD",
  "monthly_commitment_minor": -190540,
  "series": [ { "merchant": "...", "period_label": "monthly", "..." : "..." } ],
  "due_soon": [], "overdue": [], "lapsed": []
}
```

Each series carries `amount_centre_minor` (what it costs **now**),
`monthly_equivalent_minor` (normalised so a yearly and a monthly commitment
compare), `occurrences`, `total_paid_minor`, `first_seen`, `last_seen`,
`expected_next`, `confidence`, and `price_changes`.

**`lapsed` is separate from `overdue` deliberately.** A cancelled subscription
and a skipped payment want opposite reactions: one should go quiet, the other
should be raised. A client that merges them fills the page with the corpses of
old subscriptions.

`price_changes` is what serves "trend subscription pricing": each entry is
`{on, from_minor, to_minor}`. A price rise is a fact about a subscription, not
a new subscription.

### `GET /api/v1/review`

What still needs a person, **ranked by what deciding it is worth**, not
alphabetically and not by frequency.

```json
{ "outstanding": 632, "value_at_stake_minor": 40700731,
  "items": [{ "counterparty": "...", "occurrences": 36, "total_minor": 8287349 }] }
```

Show `occurrences` and `total_minor` next to each name. That is the difference
between a chore and an obvious call.

### `POST /api/v1/review/decide?counterparty=&category=`

Settles one counterparty for good. `201` on success.

```json
{ "counterparty": "...", "category": "Furnishing", "created": true, "applied": false }
```

- Stored as a **rule**, so it covers past and future rows together.
- Weighted above anything imported or seeded: deciding by hand ends the
  argument rather than adding a vote to it.
- Deciding the same thing twice is **not an error** — `created` is `false`.
- `422` with `{"error": "unknown category", "known": [...]}` for a category the
  tenant does not have.

**`applied` is always `false`.** Writing decisions through the ledger is a
separate pass so a run of decisions costs one write rather than one each. Until
then `/summary` will not reflect them. A client working through a queue should
decide freely and apply once at the end.

### `GET /api/v1/transfers`

`{ "linked": 206 }` — how many movements between the household's own accounts
have been paired. Exposed so a client can *show* that spending figures exclude
them rather than merely assert it.

### `GET /api/v1/growth` — **501, not implemented**

Returns `501` with a reason, deliberately.

Growth is **balance over time** and must come from `statement_balance`, not
from summing `txn`. Summing transactions makes a transfer between the
household's own accounts look like growth in one direction and a loss in the
other. The repository has no balance-history query yet, and a plausible-looking
series computed the wrong way would be worse than nothing — it is the figure
§5.1(D) attaches an emotional signal to.

**This is the one endpoint the dashboard needs and does not have.** Building it
means a balance-history query over `statement_balance`, not a change here.

---

## Not yet in the contract

Named so that their absence is a decision rather than an oversight.

| Missing | Why it matters |
|---|---|
| `GET /growth` | Above. The headline figure of §5.1(A). |
| Applying decisions | `/review/decide` records; nothing writes it through. CLI does this today. |
| Correcting a *categorised* row | Only unmatched counterparties are reviewable, so a wrong category is invisible. |
| Row-level `source='human'` | The pass refuses to overwrite it, but nothing writes it. |
| Editing the taxonomy | `/categories` reads only. Renaming must also rewrite enrichments. |
| Authentication | There is none. See §5.2: per-server, OIDC, no password ever stored. **Do not deploy this beyond a trusted network until it exists.** |
