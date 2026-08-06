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

A JSON **string** breaks this promise just as badly as a float, and is the more
likely way to break it: Postgres widens `SUM(bigint)` to `numeric` so it cannot
overflow, which arrives as a `Decimal` and serialises as a quoted string. The
same ledger then answers `/summary` with a number on SQLite and a string on
Postgres. Every money sum is cast back to an integer in SQL. A client is
entitled to `typeof === "number"` on every `_minor` field.

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
| `direction` | `out` \| `in` \| `net` | Which side of zero. Defaults to `out`. |
| `q` | string | Free text. **Drops the date range.** See below. |

**`q` searches the whole ledger and ignores `since`/`until`.** Somebody
searching for a merchant is searching precisely because they do not know which
month it was in; silently confining that to the requested window would return
nothing and look like an answer. Account and category filters still apply —
those are explicit choices the user can see.

The drop happens once, in the shared filter dependency, so `/summary`, `/trend`
and `/transactions` cannot disagree about what is on screen. The response
reports `range.since: null` and echoes `q`, so a client can explain the null
averages rather than look broken. Blank or whitespace-only is not a search.

Matched case-insensitively against **both** `counterparty_norm` and
`description_raw`: normalisation strips references and mechanism words, so the
text a user remembers seeing on the statement often survives only in the raw
description. `%` and `_` are literal characters, not wildcards.

**`direction` selects a side, it does not change the sign convention.** `out`
keeps only rows below zero and `in` only rows above; `net` keeps both and
therefore sums to what the household actually kept. Amounts stay signed as
stored in every case, so an `in` total is positive and an `out` total is
negative, and a `net` total may be either. A client must not take absolute
values to make them agree — a household that spent more than it earned needs to
see the minus.

Whatever `direction` is passed, transfers between the household's own accounts
and persistently hidden rows are excluded first. `net` means net of the outside
world, not net of every row in the ledger.

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
  "direction": "out",
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

`direction` is echoed back so a client rendering two of these side by side can
tell which is which without tracking it separately. Under `direction=net`,
`by_category` holds a category's *net* position, which may be either sign — a
category that received a refund larger than its spending is a positive row, and
that is the truth about it, not an error to be filtered out.

### `GET /api/v1/transactions`

Rows on the requested side of zero, newest first, with `limit` (≤1000, default
200) and `offset`.

Each row carries **two amounts**: `amount_minor` as the statement stated it, and
`effective_amount_minor` after anything paid back for it, with
`paid_back_minor` between them. They are equal for almost every row. Show both
where they differ — a client that displays only the second is showing a figure
that disagrees with the bank statement and cannot be audited against it, and a
client that computes the second itself is doing arithmetic on money this API
promised to do.

`description_raw` is what the statement printed, beside the normalised
`counterparty_norm`.
Each row carries `id`, `posted_date`, `amount_minor`, `currency`,
`counterparty_norm`, `account_id`, `institution`, `account_ref_masked`,
`category`, `source`.

`source` is `rule` \| `knn` \| `llm` \| `human`. A client should show which,
because §5.1 asks users to be able to tell what was decided automatically from
what they corrected.

### `GET /api/v1/trend`

Money per period, for the bar chart. Takes every filter above except
`direction`, plus:

| Parameter | Type | Notes |
|---|---|---|
| `bucket` | `auto` (default) \| `day` \| `week` \| `month` | Bar width |
| `rolling` | int | Trailing-average window, in buckets. Omit for the default. |

```json
{ "currency": "SGD", "bucket": "month", "rolling_window": 3,
  "range": { "since": "2025-08-06", "until": "2026-08-05", "days": 365 },
  "points": [{
    "period": "2026-07-01", "rows": 412,
    "out_minor": -499012, "in_minor": 1240433, "net_minor": 741421,
    "total_minor": -499012,
    "rolling": { "out_minor": -1183441, "in_minor": 1215502, "net_minor": 32061 },
    "rolling_of": 3
  }] }
```

**A point carries all three directions, not one.** `direction` is deliberately
not a trend parameter: spending, income and net are the same periods measured
three ways, and a client switching between them should not have to re-fetch and
risk drawing two series bucketed differently. `total_minor` is `out_minor` and
exists only so clients written against the older shape keep working; new code
should read the named field.

**Bar width follows the range, not a fixed count.** `auto` chooses days at 14
days or fewer, weeks below a year, months beyond — a year of daily bars is
unreadable and a fortnight of monthly ones is a single block. `period` is the
first day of the bucket; weeks start Monday.

Periods with no activity are **absent** rather than zero. A client drawing a
continuous axis fills the gaps itself.

#### The rolling average

`rolling` is a **trailing** mean over the last `rolling_window` buckets,
inclusive of the point itself. Trailing, not centred: a centred window needs
periods that have not happened yet, and the question the line answers is whether
the household is heading up or down *now*.

`rolling_window` defaults to the width that spans roughly a season at each
bucket size — 7 days, 4 weeks, 3 months — and is echoed in the response so a
client can label the line without duplicating the rule.

**`rolling_of` is how many buckets that point's average actually covered.** The
earliest points cannot see a full window, so their average is over fewer periods
and is not comparable with the rest. A client must distinguish them — fading the
bar, or starting the line where `rolling_of` reaches `rolling_window`. Silently
drawing a one-bucket "average" alongside a settled one invents a trend that is
just the series starting.

`centre` also accompanies the points, describing `out_minor` only:

```json
"centre": { "mean_minor": -2228763, "median_minor": -2197123, "buckets": 12 }
```

**Both are given because the gap between them is the information.** A
household's spending is not symmetric — one renovation drags a mean somewhere
no ordinary month has been, while the median keeps describing a typical period.
Where they diverge sharply, a few large one-offs are carrying the average, and a
client may usefully say so.

`centre` describes the whole range as one number and so cannot show direction.
It answers "what is a typical month"; `rolling` answers "is this month worse
than the last few". Draw `rolling` as the line on the chart and use `centre` for
the sentence underneath, not the other way round.

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

### `POST /api/v1/transfers/mark?txn_id=&counterpart_id=`

`201 { "txn_id": 16964, "counterpart_id": null, "marked": true }`.

For transfers the matcher could not prove: a counterpart at a bank this ledger
does not hold, a partial payment, or two candidates that fitted equally well.
`counterpart_id` is optional — a one-sided mark is the operator asserting where
the money went where nothing here can corroborate it.

Marked links carry `origin: "manual"` and **survive a re-run of the matcher**,
which rebuilds only what it found itself. `marked` is `false` if the row is
already part of a link.

### `DELETE /api/v1/transfers/mark/{txn_id}`

`{ "unmarked": true }`. Removes an operator's mark only; the matcher's own
links are untouched.

---

## Paybacks

One person pays for a group and the others settle up afterwards. Recorded
faithfully, that reads as two lies: $300 of Dining and $270 of income from
nowhere, when the household spent $30 and earned nothing.

**A payback is not a transfer, and the difference is the whole point.** A
transfer moves money between the household's own accounts, so neither leg is
real activity and both are excluded. Here the charge *was* real — just smaller
than the statement says. So a payback link **reduces the charge** and removes
the inflow from income. The signs already do the arithmetic:
`amount_minor + Σ paybacks`.

The charge itself stays in every figure. Excluding it, as a transfer would,
would erase the household's own share along with everyone else's.

**A payback settles one charge and no other**, enforced in the schema. One
person's $60 discounting two dinners would take $120 off spending on the
strength of $60. A link consumes the whole inflow; splitting one transfer across
two charges is not supported.

### `GET /api/v1/paybacks?expense_txn_id=`

```json
{ "paybacks": [{ "id": 3, "expense_txn_id": 812, "income_txn_id": 940,
                 "amount_minor": 6000, "note": "", "linked_at": "...",
                 "posted_date": "2026-06-04", "counterparty_norm": "...",
                 "institution": "DBS" }],
  "count": 1, "total_minor": 6000 }
```

Omit `expense_txn_id` for every link in the tenant.

### `GET /api/v1/paybacks/candidates?expense_txn_id=&q=&days=&limit=`

Inflows that could be somebody settling this charge, **nearest the charge's own
date first** — a payback usually follows within days, and the user is scanning
for it rather than reading a ledger. `days` bounds the window either side; `q`
searches as above.

Anything already spoken for — settling another charge, part of a transfer,
hidden — **is not offered at all**. Linking it would be refused, and offering a
choice that cannot be taken is worse than not offering it.

404 if the charge is not in this ledger.

### `POST /api/v1/paybacks?expense_txn_id=&income_txn_id=&income_txn_id=`

`income_txn_id` is repeatable, so linking five people is one round trip and
either all of it lands or none of it does. A partial success would leave a
charge discounted by an amount nobody chose.

```json
{ "expense_txn_id": 812, "linked": 4,
  "paid_back_minor": 24000, "effective_amount_minor": -6000 }
```

**422, with a reason naming the numbers**, when the ledger says no:

| Refused | Why |
|---|---|
| More back than was spent | A charge cannot become income. Being paid back *exactly* is fine — you fronted it and were not eating. |
| The inflow already settles another charge | One payback, one charge |
| The charge is money in, or a payback is money out | The shapes are not interchangeable |
| Either side is a transfer leg or hidden | Money moved to oneself was never anyone's repayment |

404 when a transaction is not in this ledger.

### `DELETE /api/v1/paybacks/{expense_txn_id}?income_txn_id=`

Unlinks one payback, or every payback on the charge when `income_txn_id` is
omitted. The charge goes back to what the statement said.

### What paybacks do not change

`/review`, `/recurring` and categorisation all keep reading the **raw** amount.
A $300 dinner is a Dining charge whoever ended up paying for it, and a rule is
about the merchant, not about who paid you back. The adjustment belongs to
figures that total money, and to nothing that asks what a transaction was.

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
| Surviving a reparse | `reparse` deletes and rewrites a document's rows with new ids, so hidden rows, hand-set categories, manual transfer marks and paybacks are **silently discarded**. Re-matching them by `dedupe_key` — which is stable across a reparse and exists for exactly this — is the fix and is not built. |
| Splitting one payback across two charges | A link consumes the whole inflow. The schema carries an amount so this can be added without a migration; the unallocated remainder would then have to keep counting as income. |
| Renaming or merging a category | `POST` adds; neither rename nor merge exists, and both must rewrite the enrichments that named the old one. |
| A category weight or budget | Categories carry `position` and nothing else, so no endpoint can say a month was over or under. |
| Reviewing what is *already* categorised | `/review` lists only unmatched counterparties, so a wrong rule among the 398 is invisible until someone happens to see the row. |
| Authentication | There is none. See §5.2: per-server, OIDC, no password ever stored. **Do not deploy this beyond a trusted network until it exists.** |
