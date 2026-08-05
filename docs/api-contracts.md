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
| `exclude_txn_id` | int | Repeatable. Hidden for this request only. |

**`exclude_txn_id` is what makes session-level hiding real.** A client that
merely dropped rows from a list would leave the totals, averages and trend
describing a different set of transactions from the one on screen. The server
owns the arithmetic, so the exclusion is passed to it and every figure moves
together. Rows hidden persistently via `/hidden` are excluded automatically and
need not be repeated here.

---

## Endpoints

### `GET /`

Not versioned, because it is what an operator hits before they know what the
server speaks.

```json
{ "service": "finstone", "api_version": "v1", "api_root": "/api/v1",
  "docs": "/docs", "openapi": "/openapi.json",
  "note": "This is the API. The UI is a separate application that connects to it." }
```

A bare 404 here is technically correct and useless: visiting the root is the
first thing anyone does with a new self-hosted service, and answering nothing is
how a working install looks broken.

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

### `GET /api/v1/trend`

Spending per period, for the bar chart. Takes every filter above, plus
`bucket` = `auto` (default) | `day` | `week` | `month`.

```json
{ "currency": "SGD", "bucket": "week",
  "range": { "since": "2026-05-07", "until": "2026-08-04", "days": 90 },
  "points": [{ "period": "2026-05-04", "total_minor": -112590, "rows": 29 }] }
```

**Bar width follows the range, not a fixed count.** `auto` chooses days at 14
days or fewer, weeks below a year, months beyond — a year of daily bars is
unreadable and a fortnight of monthly ones is a single block. `period` is the
first day of the bucket; weeks start Monday.

Periods with no spending are **absent** rather than zero. A client drawing a
continuous axis fills the gaps itself.

### `GET /api/v1/hidden`

```json
{ "hidden": [{ "id": 16964, "posted_date": "2026-07-30", "amount_minor": -10000,
               "counterparty_norm": "...", "institution": "DBS",
               "note": "", "hidden_at": "..." }],
  "count": 1, "total_minor": -10000 }
```

**`total_minor` is part of the contract, not a convenience.** A dashboard that
quietly omits things is worth less than one that says what it omitted, so a
client must show the size of what is hidden somewhere the user will meet it.

### `POST /api/v1/hidden?txn_id=&note=`

`201`, `{ "txn_id": 16964, "hidden": true }`. `hidden` is `false` if it already
was — not an error.

Hiding excludes a row from **every** figure: totals, averages, trend and
transaction lists. Nothing about the transaction changes; this is a claim about
what should count, held beside the ledger and reversible.

### `DELETE /api/v1/hidden/{txn_id}`

`{ "txn_id": 16964, "restored": true }`. `restored` is `false` if it was not
hidden.

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

### `GET /api/v1/growth`

Net worth over time. Takes `months` (1–120, default 6) and `every` =
`month` | `statement`.

```json
{ "currency": "SGD", "window_months": 12, "since": "2025-08-08",
  "points": [{ "on": "2026-07-31", "total_minor": 5837770, "accounts_known": 12 }],
  "change": { "from_minor": 13128011, "to_minor": 5837770,
              "change_minor": -7290241, "percent": -0.5553 } }
```

**Never a sum of transactions.** Balances come from `statement_balance`, which
the reconciliation check already proved to the cent. Summing movements would
read a transfer between the household's own accounts as growth on one side and
loss on the other, and would count a card the wrong way round.

**`accounts_known` is part of the contract.** Accounts do not close on the same
day, so each carries its last declared balance forward until it declares
another — a point is only as current as its stalest account. A client must be
able to say when an early point covers fewer accounts, rather than let it look
like a real dip.

**`change.percent` is `null` when the window opened at zero or in debt.** There
is no honest percentage of a negative position, and this is the figure §5.1(D)
attaches a feeling to. Render the absolute movement instead, never a fabricated
rate.

### `POST /api/v1/categories?name=`

`201 { "name": "Gifts", "created": true }`. `409` if it already exists, `422`
if the name is blank. The taxonomy is the tenant's to shape.

### `POST /api/v1/transactions/{txn_id}/category?category=`

Corrects one transaction. Written as `source: "human"`, which no automated pass
will overwrite — the row-level counterpart to `/review/decide`, which settles a
counterparty and everything with it. `422` with the known categories if the
name is not one.

### `DELETE /api/v1/transactions/{txn_id}/category`

`{ "cleared": true }` — drops the correction so the next rule pass decides again.

---

## Not yet in the contract

Named so that their absence is a decision rather than an oversight.

| Missing | Why it matters |
|---|---|
| Applying decisions | `/review/decide` records; nothing writes it through. CLI does this today. |
| Renaming or merging a category | `POST` adds; neither rename nor merge exists, and both must rewrite the enrichments that named the old one. |
| Reviewing what is *already* categorised | `/review` lists only unmatched counterparties, so a wrong rule among the 398 is invisible until someone happens to see the row. |
| Authentication | There is none. See §5.2: per-server, OIDC, no password ever stored. **Do not deploy this beyond a trusted network until it exists.** |
