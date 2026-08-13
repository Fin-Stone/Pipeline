# API contracts

**This document is the contract.** A client is written against what is written
here, and a self-hosted server promises it. A route that drifts from this
document breaks somebody's install rather than somebody's build.

> **If you are building a UI or any other client, read this first.**
> **If you are changing `app/api/`, change this in the same commit.**

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

**1a. The currency is the ledger's, and is never assumed.** Every response that
reports money carries `currency`, read from the accounts in scope — not
declared by the server and not compiled into the client. It is `null` only on
an empty ledger, where there are no figures for a currency to describe.

Where the accounts in scope are held in **more than one** currency, the
response is `501` with the list and the reason, because adding SGD to USD gives
a number that is not money in any currency:

```json
{ "detail": { "error": "mixed currencies", "currencies": ["SGD", "USD"],
              "reason": "…Filter to one currency with account_id." } }
```

The refusal comes with the way through it — `account_id` narrows the scope — so
a household with one foreign account does not lose the dashboard. Per-currency
breakdowns are the real answer and are not built.

This was `SGD`, unconditionally, in four endpoints. Correct for the household
this was built for and wrong for every other one, in the way that looks like it
works: the numbers were right and only the symbol lied.

**2. Amounts are signed by effect on the account.** Spending is **negative**,
money in is positive, and card balances are stored negated so one formula
covers both. The API does not flip signs for presentation.

**2a. Every action a person can take is reversible, and can be found again
later.** Not a courtesy — a design constraint on this API. Anything that changes
a figure must have both an inverse route *and* a listing route, because an undo
nobody can reach is not an undo:

| Action | Inverse | Where it can be found |
|---|---|---|
| `POST /hidden` | `DELETE /hidden/{txn_id}` | `GET /hidden` |
| `POST /transfers/mark` | `DELETE /transfers/mark/{txn_id}` | `GET /transfers/marked` |
| `POST /paybacks` | `DELETE /paybacks/{expense_txn_id}` | `GET /paybacks` |
| `POST /transactions/{id}/category` | `DELETE …/category`, or post another | `GET /transactions` |
| `POST /categories` | `DELETE /categories/{name}` | `GET /categories` |
| `POST /review/decide` | `DELETE /review/decide?counterparty=` | `GET /rules` |
| `POST /rules` | `DELETE /rules/{id}` | `GET /rules?origin=all` |
| `POST /documents` | `DELETE /documents/{sha256}` | `GET /documents` |
| `POST /documents/scan` | `DELETE /documents/{sha256}`, per document | `GET /documents` |
| `POST /recurring/dismiss` | `DELETE /recurring/dismiss` | `GET /recurring/dismissed` |
| `POST /recurring/mark` | `DELETE /recurring/mark` | `GET /recurring/marked` |
| `POST /transfers/rematch?apply=true` | run it again with the old window | `GET /transfers` |

**A new endpoint that changes a figure must add a row to that table.** Marking a
transfer had the inverse and not the listing for a while: the row vanished from
every screen, the totals moved, and nothing in any client could name it again.
It was undoable in principle and unreachable in practice, which is the failure
mode this rule exists to catch.

`POST /documents/reparse` is deliberately not in the table, and is the one
exception worth naming: it replaces one reading of a document's own bytes with
another, so there is nothing to invert — the way back is to fix the adapter and
run it again. What the rule's spirit does demand of it is that it never costs
the operator a decision, and it does not; see the endpoint.

Where the reverse would destroy something — deleting a category that
transactions are filed under — the API **refuses and says how much is in the
way**, rather than cascading. A cascade is the one thing a person cannot undo.

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

**Money arriving on a credit card is never income**, under any `direction`.
Card balances are stored negated, so a positive row on a card means the debt
went down — never that the household got richer. There are three ways that
happens and none of them is earnings: the bill being paid from one of the
household's own accounts, a merchant refunding a purchase, and cashback or
points. The first is a transfer; the other two reverse spending that is already
counted. Left in, an unmatched bill payment reads as a month's salary.

Such a row is **excluded from income, not subtracted from spending.** A refund
belongs against the charge it reverses, and `POST /paybacks` is how a person
says which charge that is; inferring it from a date would move money out of a
category on a guess. A card credit that has not been paired or attributed
therefore appears in no total, and `out + in` still equals `net`.

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

**A server carrying a client returns the client here instead**, as `text/html`
with `Cache-Control: no-store`, and serves it for every other unmatched path so
a refresh deep in the app is not a 404. The official image does; a `pip install`
does not. A client must therefore not treat this route as a capability check —
`GET /api/v1/health` is that, and is the only route that answers the question
"can I talk to this server".

Paths under `/api/`, plus `/docs`, `/redoc` and `/openapi.json`, **always belong
to the server**. An unknown one is a JSON `404`, never the page — a client handed
HTML with a `200` on it would parse the page it is running in as a ledger.

### `GET /api/v1/health`

```json
{ "status": "ok", "api_version": "v1" }
```

Call before anything else. `api_version` is how a client decides it can speak
to this server.

### `POST /api/v1/documents`

**`multipart/form-data`, repeatable field `files`.** The only endpoint in this
API that does not take its input as query parameters, because there is no other
way to carry a file. `201`.

```json
{ "accepted": 2, "rejected": [{ "filename": "notes.txt", "reason": "not a statement — expected a PDF, OFX, CSV or MT940" }],
  "documents": [{ "filename": "june.pdf", "status": "imported",
                  "sha256": "…", "transactions": 84, "reason": null }],
  "imported": 1, "duplicates": 0, "quarantined": 1, "transactions": 84,
  "transfers": { "added": 2, "removed": 0, "linked": 211 } }
```

- **Every file is reported on its own.** A batch of twelve with one unparseable
  statement in it must not read as twelve failures.
- **A duplicate is a normal outcome, not an error.** Re-uploading what is
  already imported is what somebody does when they are not sure whether they
  did. So is `quarantined`: a layout with no adapter yet is set aside with a
  readable `reason` rather than guessed at.
- **Refused before anything is written**: not a statement by its leading bytes
  (an extension is a claim, the magic number is what it *is*), empty, or over
  **32 MB**. Path separators in a filename are stripped, not rejected — the
  operator did not choose the name their bank generated.
- Files land in the **inbox**, not in `uploads/`. That folder is the operator's
  own and is mounted read-only for exactly that reason. Nothing is lost: the
  original bytes go to the content-addressed store, where the durable copy has
  always lived.
- Ingestion drains the whole inbox for that profile, not only what this request
  carried — the inbox is a queue, and a file left there by a failed earlier run
  should not need a second mechanism to pick it up.
- `transfers` reports the re-pairing that runs afterwards, and is `null` when
  nothing was imported. An upload changes spending figures in two ways and only
  one of them is the new rows.
- `FINSTONE_ALLOW_PROD` is **not** consulted here and is not being evaded. It
  guards *reading `uploads/prod`* — a folder automation must never walk on its
  own initiative. A file handed over in a request is the deliberate act that
  flag exists to require.

### `GET /api/v1/documents?parse_status=&limit=`

`{ "total": 246, "documents": [...] }` — what is in the ledger. The listing half
of rule 2a: an upload moves every figure on the dashboard, so there has to be
somewhere to see what was added and take one back out.

**A quarantined document carries `reason`**, in the words the pipeline used:

```json
{ "failure_class": "unknown_layout", "message": "no adapter claims this layout",
  "quarantined_at": "2026-08-13T02:11:07Z", "detail": { ... } }
```

The traceback is deliberately not in it — that belongs in `finstone report`,
which is the whole record and is meant to be pasted to somebody. A client that
had to know which fields to ignore would eventually show the wrong one.

Imported documents carry no `reason` key at all, rather than a null one.

### `POST /api/v1/documents/scan`

Stage and import whatever is sitting in the uploads folder for this profile.

```json
{ "discovered": 246, "staged": 3, "already_imported": 243, "processed": 3,
  "imported": 3, "duplicates": 0, "quarantined": 0, "transactions": 191,
  "transfers": { "added": 2, "removed": 0, "linked": 209 } }
```

For the folder of statements too large to drag into a browser, which until this
existed meant `docker compose run … finstone run` — a shell, on a box, to do
the ordinary thing.

- **Safe to repeat, and meant to be.** Documents already in the ledger are
  recognised by digest and skipped, so this is a sync rather than an import.
- **`uploads/` is never written to.** Files are copied out of it. They belong
  to the operator, not to the pipeline.
- **`409` for the prod profile without `FINSTONE_ALLOW_PROD`**, with the
  variable named in the message. This endpoint cannot waive that guard: an API
  that could switch it off would be the way around it.

### `POST /api/v1/documents/reparse?sha256=&quarantined_only=`

Read the stored originals again, after an adapter fix.

```json
{ "processed": 1, "imported": 1, "unverified": 0, "quarantined": 0,
  "transactions": 82, "decisions": { "restored": 14, "dropped": 0 },
  "documents": [{ "sha256": "…", "status": "imported", "reason": null }],
  "transfers": { "added": 0, "removed": 0, "linked": 209 } }
```

Give a digest **or** ask for the quarantined ones; both together is `422`, and
so is neither — reparsing the whole ledger is not offered from here. An unknown
digest is `404`. Nothing is re-downloaded: the bytes are in the
content-addressed store and have never been touched.

**Not an inverse, and rule 2a does not ask it to be.** A reparse does not undo
anything — it replaces one reading of the same bytes with another, and the way
back is to fix the adapter and run it again.

**What rule 2a does require is that it never costs a decision.** Hand-set
categories, hidden rows, manual transfer marks and paybacks are captured by
`dedupe_key` before the rows are replaced and reattached afterwards; the key is
derived from what the statement says and is stable across a reparse, which is
what it exists for. `decisions.dropped` counts the ones whose row the new
reading changed too much to recognise — a category is never moved onto a
neighbouring transaction to avoid the loss, because wrong and silent is worse
than gone and counted. **A client should surface a non-zero `dropped`.**

### `GET /api/v1/reconciliation`

Whether the ledger still agrees with the balances the banks declared.

```json
{ "clean": false, "currency": "SGD",
  "drifts": [{ "account_id": 3, "account": "OCBC 1234/360", "kind": "movement",
               "since": "2025-05-28", "until": "2025-06-28",
               "declared_minor": -4500, "observed_minor": -9000,
               "difference_minor": -4500 }] }
```

Validation at import proves one statement consistent with itself, at the moment
it was parsed. This is the half that can only be asked afterwards: whether the
ledger still agrees once overlapping documents have been deduplicated into it.
Architecture §8.2 calls it the ultimate check and puts a deadline on it — drift
is something you want to know that month, not next year.

- `clean` is the field a monitor watches. Everything else is for the person who
  then opens two statements.
- `kind` is `continuity` — one statement's closing balance disagreeing with the
  next one's opening, usually a statement nobody has imported yet — or
  `movement`, the ledger disagreeing with the banks about what happened in
  between. The second is what catches a transaction imported twice.
- Both claims are given, never only the difference: the difference alone does
  not say which side to go and look at.
- **It never corrects anything.** A ledger that adjusts itself to match a
  number it cannot explain has stopped being a record.

### `DELETE /api/v1/documents/{sha256}`

`{ "sha256": "…", "deleted": true, "transfers": { "linked": 209, "removed": 2 } }`

The inverse of an upload, and what makes uploading safe to try. It takes the
document's transactions with it — that is the point — along with the paybacks,
hidden marks, categories and transfer links that pointed at those rows. Leaving
those would break foreign keys and, worse, leave decisions attached to rows that
no longer exist.

**The original file is kept.** It is content-addressed and immutable, and the
design's oldest promise is that the bytes a bank sent are never thrown away.
Re-uploading the same statement restores it — though the decisions do not come
back, and a client should say so before deleting rather than after.

A digest that is not here is `deleted: false`, not `404` — the same shape as
unhiding twice.

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
`expected_next`, `confidence`, `price_changes`, `marked_by`, and `category` with
`decided_by`.

**`marked_by` is `operator` when a person said this repeats** and `null` when
the detector found it. The distinction is what a client needs to offer the right
inverse — un-marking somebody's own mark, rather than dismissing it — and it is
also the honest label for the period, which on a marked series is an assertion
rather than a measurement.

**`category` is `null` when nothing has decided yet**, which is a case a client
should offer to settle rather than hide. `decided_by` is `operator` or
`imported`, so a person can see whether the filing was theirs.

**`grouped_by` says whether there is anything to decide.** `merchant` is the
normal case. `amount` means the rows were gathered by amount because the bank
printed no payee, and `merchant` is then a label this server invented rather
than a counterparty any row holds — so `category` and `decided_by` are always
`null` and **a client must not offer to decide it.** A rule written against an
invented label matches no transaction, while the series would read the rule back
and show itself as filed: settled on screen, untouched in the ledger. Those rows
are categorised individually with `POST /transactions/{id}/category`.

**Correcting it is `POST /review/decide` with the same merchant name.** There is
no separate route to categorise a series, deliberately: a series *is* a
merchant, a merchant's category is one decision, and two ways to set one thing
is how they come to disagree. Correcting a subscription filed wrongly therefore
also fixes every other transaction from that merchant, and takes effect
immediately.

**`lapsed` is separate from `overdue` deliberately.** A cancelled subscription
and a skipped payment want opposite reactions: one should go quiet, the other
should be raised. A client that merges them fills the page with the corpses of
old subscriptions.

`price_changes` is what serves "trend subscription pricing": each entry is
`{on, from_minor, to_minor}`. A price rise is a fact about a subscription, not
a new subscription — and so is a **cut**, which a client should not draw as bad
news: it is the evidence that a plan change actually took effect.

A change needs only **two** payments at the new price, not the three a series
needs. Requiring three would mean a price change is visible a quarter after it
happened, which is when it stops being worth telling anybody. The evidence is
not the short run on its own; it is that an established series stops exactly
where it starts, on the same cadence.

**Two commitments at the same price with one provider are two series.** Billed
a fortnight apart, their merged gaps alternate and average to "fortnightly", so
they were reported as one subscription at half the true commitment. Splitting is
a last resort applied only to a run that yields no series at all — anything
threaded at three times its period also covers every row, so a looser rule turns
one monthly premium into three quarterly ones.

**A direct debit the bank never named is grouped by its exact amount** and
labelled `Unnamed direct debit`, or with whatever name the bank did print on the
statements where it printed one. `GIRO PAYMENTS / COLLECTIONS VIA GIRO` names
nobody, and seven insurance premiums were invisible because the same policy
appeared under two or three such names and each fragment fell below three
occurrences.

### `POST /api/v1/recurring/dismiss?merchant=&amount_centre_minor=&note=`

Says a detected series is not a subscription. `201`.

```json
{ "merchant": "...", "amount_centre_minor": 2300, "dismissed": true }
```

- **Because detection is a guess.** Three payments of the same amount a year
  apart are usually a premium and are sometimes three people settling up after
  three holidays. The rules that find the real ones are the same rules that
  occasionally find these, so the answer is a cheap way to say no rather than a
  stricter detector.
- Keyed on the **name and amount the operator was shown**, not on transaction
  ids — a reparse replaces those. The detection pass stays a pure function of
  the ledger and goes on producing the series; this removes it from the reading
  only, so `DELETE` restores exactly what was there.
- A dismissed series leaves `series`, `due_soon`, `overdue`, `lapsed` **and**
  `monthly_commitment_minor`. `dismissed_count` says how many are held back.
- **Nothing about the transactions changes.** A dismissal is a fact about the
  reading, not about the rows.
- A series whose price later moves reappears, deliberately: the commitment that
  was dismissed is not the one now on the statement.
- Saying it twice is not an error — `dismissed` is `false`.

### `DELETE /api/v1/recurring/dismiss?merchant=&amount_centre_minor=`

Puts it back. `restored` is `false` when there was nothing to put back, so a
second click does what the first one did.

### `GET /api/v1/recurring/dismissed`

Everything said not to be a subscription, with `merchant_norm`,
`amount_centre_minor`, `note` and `dismissed_at`. Contract rule 2a: without it
the series would vanish from every screen with nothing to name it by.

### `POST /api/v1/recurring/mark?merchant=&amount_centre_minor=&period=&note=`

Says something **is** a subscription, on a period the detector cannot infer.
`201`.

```json
{ "merchant": "...", "amount_centre_minor": 24000, "period": "yearly", "marked": true }
```

- **Because the detector's bar is right and still leaves real commitments
  invisible.** It needs three occurrences and gaps that barely vary; a yearly
  premium has two rows after two years, a quarterly bill invoiced whenever the
  vendor remembers never qualifies, and a plan taken out last month has one row.
  Loosening the detector to reach those is the wrong trade — it is what turned
  one monthly premium into three quarterly ones — and the person paying it knew
  the answer all along.
- `period` is one of `weekly`, `fortnightly`, `monthly`, `quarterly`, `yearly`.
  Anything else is `422`. **It is not second-guessed:** two rows a year apart
  marked `monthly` stay monthly, because correcting the period to the one the
  dates imply would make the route useless for the irregular billing it exists
  for.
- Keyed on the **name and amount**, exactly as a dismissal is, so it survives a
  reparse. Detection stays a pure function of the ledger; a mark is a second
  reading laid over it.
- **A marked series replaces whatever the detector made of the same rows.** Both
  readings at once would count one commitment twice, and
  `monthly_commitment_minor` is the figure the page exists to state.
- `confidence` on a marked series still describes **the gaps and nothing else**,
  and reads `0` where there are not two of them to compare. It is not raised to
  reflect that a person is sure: that would dress an assertion up as the
  strongest evidence available.
- Marking again on a **different period corrects it** rather than creating a
  second subscription. Saying the same thing twice is not an error — `marked` is
  `false`.
- **Nothing about the transactions changes.**

### `GET /api/v1/recurring/candidates?txn_id=&period=`

What marking that row would gather, **before anything is written**.

```json
{ "merchant": "...", "amount_centre_minor": 24000, "period": "yearly",
  "expected_gap_days": 365, "monthly_equivalent_minor": 2282,
  "expected_next": "2026-06-15", "gap_days": [365],
  "matches": [{ "txn_id": 1, "posted_date": "2024-06-15", "amount_minor": -24000 }] }
```

- **A mark reaches the merchant, not the charge that was clicked** — every row
  with the same name and an amount within the ordinary tolerance. Those are not
  the same thing, and this is the only place the difference is visible; a mark
  that quietly swept up a neighbouring payment surfaces months later as a
  monthly total nobody can account for.
- `gap_days` against `expected_gap_days` is the useful comparison: it is how a
  person notices they picked `monthly` for something billed yearly. A client
  should **flag a mismatch and not block it** — irregular billing is what the
  mark is for.
- `404` when the row is not a recurrence candidate, which includes a transfer
  leg or a hidden row. Answering "no such row" about one visibly on screen would
  be a lie nobody can act on.
- Writes nothing.

### `DELETE /api/v1/recurring/mark?merchant=&amount_centre_minor=`

Takes the mark back. `unmarked` is `false` when there was nothing to take back.
Un-marking has to be as cheap as marking: a page somebody is afraid to touch is
one that stays wrong.

### `GET /api/v1/recurring/marked`

Everything said to be a subscription, with `merchant_norm`,
`amount_centre_minor`, `period_label`, `note` and `marked_at`. Contract rule 2a:
a mark whose rows a reparse later renamed stops appearing on the recurring page,
and without this there would be nothing anywhere to say it still existed.

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
{ "counterparty": "...", "category": "Furnishing", "created": true, "applied": 412 }
```

- Stored as a **rule**, so it covers past and future rows together.
- Weighted above anything imported or seeded: deciding by hand ends the
  argument rather than adding a vote to it.
- Deciding the same thing twice is **not an error** — `created` is `false`.
- **Deciding it differently replaces the earlier decision**, and `replaced` says
  what it overrode so a client can offer to put it back. A decision is a
  statement about a merchant, and changing one's mind replaces it rather than
  casting a second vote: two operator rules on one name carry equal weight and
  equal specificity, so they tie, the merchant becomes *contested*, and it falls
  back to uncategorised. Correcting a wrong category used to make it worse than
  leaving it, silently.
- `422` with `{"error": "unknown category", "known": [...]}` for a category the
  tenant does not have.

**`applied` is the number of rows the rules now account for**, and the pass runs
before this returns. A decision is true everywhere the moment it is made:
`/summary`, `/transactions`, `/trend` and the category filter all move with it.

It used to be `false` always, with applying deferred to a separate pass so a run
of decisions cost one write rather than one each. That saved real work on a
batch import and cost the operator the truth on every other day: the review
queue reads *rules* and the spending list reads the ledger, so a merchant left
the queue and stayed uncategorised on the expenses page — and got categorised
again by hand. A person clicking a button is owed the consequence of it.

### `DELETE /api/v1/review/decide?counterparty=`

Takes a decision back, keyed on the name it was made about rather than on a rule
id. A client working the queue decided about a *counterparty*; asking it to
remember an id it was never shown would put the undo out of reach of the screen
that needs it.

```json
{ "counterparty": "SOME SHOP", "removed": 1,
  "was": [{ "pattern": "^SOME SHOP$", "category": "Grocery" }], "applied": 409 }
```

- Matches the name **case-insensitively**.
- **Only the operator's own rules.** An imported one was nobody's decision and
  is not what this route promised to reverse — `DELETE /rules/{id}` is for when
  one of those really is the thing in the way.
- Removing nothing is **not an error**; `removed` is `0`, so a second click does
  what the first one did.
- `applied` is a row count, symmetric with deciding: taking a decision back has
  to reach the ledger too, or the rows it categorised keep wearing a category no
  rule stands behind any more.

### `GET /api/v1/rules?origin=&q=&limit=`

The rules deciding this ledger, **newest first**, and who put each one there.

```json
{ "total": 3, "rules": [{
  "id": 812, "pattern": "^SOME SHOP$", "category": "Grocery",
  "weight": 100, "note": "decided by operator", "created_at": "2026-08-06T…",
  "origin": "operator", "counterparty": "SOME SHOP", "transactions": 47 }] }
```

- `origin` is `operator` (the default), `imported`, or `all`. It defaults to
  `operator` because that is the answer to the question anybody arrives with —
  *what have I decided?* The imported set is large and was nobody's decision.
- `origin: "operator"` means **exactly what `POST /review/decide` writes**: the
  operator weight *and* the operator note. Weight alone is not enough — an
  import is free to propose one.
- `counterparty` is the plain name where the pattern is one escaped literal,
  which every decision is. **Show it, not the pattern**; a person looking at
  `^IKEA\-RESTAURANT$` is looking at the implementation of their own decision.
  `null` for a real expression, and then `pattern` is all there is.
- `transactions` is how many rows that name accounts for today, so removing a
  rule can be a considered act. `null` — never `0` — where there is no plain
  name to count against: "not counted" is a different claim from "covers
  nothing".
- Sorted by insertion order, not by weight. The rule somebody wants is nearly
  always the one they just wrote, and weight order buries it among everything
  else at 100.

### `POST /api/v1/rules?pattern=&category=&weight=&note=`

`201`. Puts a rule back — the inverse of removing one, and the only thing that
can restore an imported rule. Takes the pattern **verbatim** rather than a
counterparty: what is being undone is a rule, and rebuilding one from a name
would not reproduce anything that was a real expression.

```json
{ "id": 813, "pattern": "^SOME SHOP$", "category": "Grocery",
  "created": true, "applied": false }
```

- `422` for a pattern that is not a valid expression, rather than storing
  something that throws on the next categorisation pass.
- `422` with the known categories for a category the tenant does not have.
- Adding one that already exists is **not an error**; `created` is `false`.
- **`id` is a new id.** The row is a new row, and an engine may reuse the one
  just freed. A client that deleted a rule and put it back must read this rather
  than carry on with the id it was holding.

### `DELETE /api/v1/rules/{rule_id}`

```json
{ "rule_id": 812, "deleted": true, "applied": false,
  "was": { "pattern": "^SOME SHOP$", "category": "Grocery",
           "weight": 100, "note": "decided by operator" } }
```

**`was` is the point.** A rule id means nothing once the row is gone, so the
response carries everything `POST /rules` needs to put it back. Without that,
undo would be a promise the client could not keep.

Deleting something already gone is `deleted: false` with `was: null`, not `404`
— the same shape as unhiding twice.

### `GET /api/v1/transfers`

How many movements between the household's own accounts have been paired, and
the rule that paired them. Exposed so a client can *show* that spending figures
exclude them rather than merely assert it — and because a rule nobody can read
is a rule nobody can fix.

```json
{ "linked": 209, "manual": 3,
  "window": { "min_days": 0, "max_days": 4, "named_days": 30, "card_days": 21 },
  "defaults": { "min_days": 0, "max_days": 4, "named_days": 30, "card_days": 21 } }
```

`defaults` is sent alongside so a client can show which numbers the operator
changed without hardcoding the defaults and drifting from the server.

### `POST /api/v1/transfers/rematch?min_days=&max_days=&named_days=&card_days=&apply=&save=`

Pairs the transfers again and says **what would change**.

```json
{ "window": { "min_days": 0, "max_days": 4, "named_days": 30, "card_days": 21 },
  "found": 210, "rows_excluded": 420, "value_minor": 36165752,
  "by_evidence": { "pays a card": 85, "amount and date": 83,
                   "names the other account": 42, "names the card it pays": 2 },
  "added": 24, "removed": 20, "unchanged": 186, "manual": 3,
  "ambiguous": [{ "txn_id": 4102, "amount_minor": -50000,
                  "posted_date": "2026-03-04", "candidate_txn_ids": [4110, 4119] }],
  "applied": false, "saved": false }
```

- **Reports by default. `apply=true` writes.** The pass changes what the ledger
  *means* — a linked pair stops counting as spending — and a client should be
  able to show that before it is true.
- **`added` and `removed` are the point, and are never netted.** "210 links"
  says nothing about whether to apply it; `removed` is what the operator gives
  up, and a single net number would hide it.
- **`manual` marks are never touched.** They are the operator's own claim and
  are not regenerable, so a re-run leaves them alone.
- **A window per kind of evidence**, because the window has to widen with the
  strength of the claim. `max_days` is amount and date alone — the weakest
  thing two rows can say, so the tightest bound. `named_days` is one leg naming
  the other's account number, which is near-proof, and covers `names the card
  it pays` as well. `card_days` is a deposit account paying a card, which is a
  transfer by construction: the purchases the card made are already spending,
  so counting the payment doubles the bill. Omitted values keep whatever the
  tenant already chose.
- **`names the card it pays` is a payment quoting a card number.** A card
  account is keyed by its product, never its number, because numbers change on
  reissue — so the numbers an account has been known by are kept beside it, and
  a payment to *any* of them settles that card. It is distinct from `pays a
  card`, which is structural and cannot tell two cards apart when both are paid
  in the same month. Numbers arrive from card statements as they are imported;
  a ledger imported before this existed gets them on the next `finstone
  reparse`.
- **Pairs are settled closest first**, across the whole ledger, before any
  looser fit is considered — never one row at a time in date order. The latter
  lets whichever row comes first take a counterpart that a later row answers
  exactly, and the displaced row then takes somebody else's. `days_apart`
  on a link is therefore the best available fit, not merely an admissible one.
- `min_days` is the smallest gap allowed, on every kind. A value beyond every
  window is `422` rather than a matcher that silently pairs nothing.
- **`save=true` requires `apply=true`.** A window remembered from a preview
  would leave the stored rule and the ledger disagreeing about what was
  decided. A value equal to the default is *not* stored — storing it would pin
  the install to today's default forever.

**The window is stored against the ledger, not the machine.** How far apart two
banks book a transfer is a fact about the banks: it belongs in a backup, and it
must survive a move to another box. `.env` would lose it to the one operation
whose whole purpose is to preserve what a person decided.

**This also runs by itself after every import**, and after a document is
deleted. A statement arriving can complete a pair that was waiting for it — a
card payment whose other leg had not been imported yet — and leaving that until
somebody remembers to re-run is how a ledger quietly overstates spending.

### `POST /api/v1/transfers/mark?txn_id=&counterpart_id=`

`201 { "txn_id": 16964, "counterpart_id": null, "marked": true }`.

For transfers the matcher could not prove: a counterpart at a bank this ledger
does not hold, a partial payment, or two candidates that fitted equally well.
`counterpart_id` is optional — a one-sided mark is the operator asserting where
the money went where nothing here can corroborate it.

Marked links carry `origin: "manual"` and **survive a re-run of the matcher**,
which rebuilds only what it found itself. `marked` is `false` if the row is
already part of a link.

### `GET /api/v1/transfers/marked`

```json
{ "marked": [{ "id": 4, "out_txn_id": 16964, "in_txn_id": null,
               "amount_minor": 10000, "txn_amount_minor": -10000,
               "evidence": "operator", "linked_at": "...",
               "posted_date": "2026-07-30", "counterparty_norm": "...",
               "institution": "DBS" }],
  "count": 1, "total_minor": -10000 }
```

**Manual marks only.** The matcher's own links are regenerable and are not
anybody's decision, so offering to undo one would promise something the next
run takes straight back.

This exists so a client can offer a way back. `DELETE` had been available from
the start and nothing could list what there was to delete — the row simply
disappeared from every screen and the totals moved. Undoable in principle,
unreachable in practice.

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

### `DELETE /api/v1/categories/{name}`

`{ "deleted": true }`, `404` if there is no such category.

**`409` while anything still references it**, with the counts in the message.
It does not cascade: deleting a category that transactions are filed under would
either orphan them or silently re-file them, and neither is something a person
can undo. Re-filing first is the operator's decision, and the refusal says how
much there is to re-file.

This endpoint is why adding a category is a safe thing to try.

### `GET /api/v1/categories/{name}/usage`

```json
{ "name": "Gifts", "exists": true, "rules": 2, "transactions": 47 }
```

So a client can grey out a delete and say why, rather than offering it and
returning a 409.

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

Each of these is scheduled in [roadmap.md](roadmap.md); the phase is named so
the absence has a date attached rather than only a reason.

| Missing | Why it matters | When |
|---|---|---|
| Authentication | There is none. See §5.2: per-server, OIDC, no password ever stored. **Do not deploy this beyond a trusted network until it exists.** | Alpha 4 |
| Renaming or merging a category | `POST` adds; neither rename nor merge exists. A rule references a category by id so a rename cannot orphan it, but a merge must rewrite every enrichment naming both. | Alpha 2 |
| A category weight or budget | Categories carry `position` and nothing else, so no endpoint can say a month was over or under. | Alpha 2 |
| Reviewing what is *already* categorised | `/review` lists only unmatched counterparties, so a wrong rule among the existing set is invisible until someone happens to see the row. | Alpha 2 |
| A series state, and alerts on it | `detected` / `declared` / `watching` / `confirmed` (§3.2a). Dismiss and mark exist; nothing alerts, and nothing can explain a break in a pattern. | Alpha 2 |
| A per-currency breakdown | Mixed-currency scopes are refused rather than summed — see promise 1a. The honest total is one figure per currency, and no endpoint emits that shape. | Alpha 4 |
| Splitting one payback across two charges | A link consumes the whole inflow. The schema carries an amount so this can be added without a migration; the unallocated remainder would then have to keep counting as income. | Beta 2 |

One entry that used to be here is gone: **surviving a reparse**. Hand-set
categories, hidden rows, manual transfer marks and paybacks are now carried
across by `dedupe_key`, and what cannot be carried is counted and reported
rather than dropped in silence. See `POST /documents/reparse`.
