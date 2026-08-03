# Phase 1 — Ingestion and Storage

**Status: implemented.** The flow described here runs end to end. Trust Bank savings and
credit card statements and DBS consolidated statements import and reconcile; MariBank and
OCBC quarantine as unroutable until their adapters are written.

Scope is steps 1–5 of the build order in
[finance-pipeline-architecture.md](../finance-pipeline-architecture.md) §11: schema and
migrations, the content-addressed store and watched folder, one adapter end to end, the
balance-reconciliation validator, and the fingerprint registry.

**PDF-first, not CSV-first.** The build order anticipated a CSV adapter as the easiest
starting point, but every document the operator actually has is a PDF. The CSV path is
deferred until a CSV source exists; the extension allowlist already accepts one.

Out of scope: categorisation, beneficiary, recurrence detection, dashboards, IMAP fetch, and
every form of automated retrieval.

## What the corpus turned out to be

Findings from the 11 documents in `uploads/dummy`, all verified by inspection:

- **All are digital PDFs with real text layers.** No OCR tier is needed. One issuer stores
  its page objects in an `/ObjStm`, which makes a naive scan report zero fonts; it is not
  scanned.
- **Three are encrypted, and all three are owner-restricted rather than user-password
  protected** — they open with an empty password. Support for a configured password exists
  anyway (`FINSTONE_PDF_PASSWORD_<INSTITUTION>`) because e-statements delivered by email
  often do require one.
- **One document can carry several accounts.** A Trust savings statement contains one
  "pocket" per sub-account, each with its own opening and closing balance.
- **Cards and deposits reconcile differently**, and transaction dates carry no year.

---

## 1. The flow

```
uploads/<profile>/**            operator drop zone, recursive, human-organised
        │
        │  stage
        ▼
data/inbox/<profile>/<relpath>  the pipeline's watched folder
        │
        │  ingest
        ▼
sha256 ──▶ data/store/ab/cd/<sha256>     immutable original, never mutated
        │
        ├─▶ fingerprint ──▶ adapter registry ──▶ parse
        │                        │
        │                        └─ unknown layout ─┐
        │                                            │
        ├─▶ validate (balance reconciliation)        │
        │        │                                   │
        │        └─ fails ──────────────────────────┤
        │                                            ▼
        └─▶ persist (one transaction)      data/quarantine/<sha256>.reason.json
                                                     │
                                                     └─▶ notify, continue the run
```

Every stage is idempotent. Re-running the whole pipeline over the same files produces zero
new rows, which is what makes aggressive retries safe.

### Why `uploads/` and `data/inbox/` both exist

They serve different masters.

`uploads/` is the human surface. It is organised however the operator finds convenient —
by bank, by year, nested arbitrarily deep — and it is split into two profiles with very
different handling:

- `uploads/prod/` holds real statements. **Agents must never read it.** See
  [development-rules.md](development-rules.md) Rule 2.
- `uploads/dummy/` holds redacted or synthetic statements that are safe for agents and for
  development.

`data/inbox/` is the machine surface: the single watched folder that the architecture
document treats as the pipeline's entire interface (§2.1). Later feeds — IMAP attachment
fetch, automated retrieval — write directly into it and never touch `uploads/`.

Keeping them separate means the prod/dummy distinction is enforced at exactly one place,
`stage`, rather than smeared through every downstream component.

---

## 2. `stage`

Recursively walks `uploads/<profile>/` and copies files into
`data/inbox/<profile>/<relative path>`, preserving the operator's directory structure so a
human can still tell what a file is.

- **Idempotent.** A file whose sha256 already appears in `source_document` is skipped, not
  re-copied. Reorganising folders in `uploads/` does not cause re-imports.
- **Profile-gated.** `--profile prod` refuses to run unless `FINSTONE_ALLOW_PROD=1` is set
  in the environment. Real data should never be processed by muscle memory.
- **Provenance recorded.** `source_document.source_profile` and `source_document.source_relpath`
  capture where a document came from, so a row in the ledger can always be traced back to a
  file the operator recognises.

Nothing is deleted from `uploads/`. It is the operator's folder, not the pipeline's.

---

## 3. `ingest`

For each file in `data/inbox/`:

1. **Hash.** Compute sha256. If it already exists in `source_document`, this is a no-op —
   remove the inbox copy and move on.
2. **Store.** Copy the bytes to `data/store/<first 2>/<next 2>/<sha256>`. Content-addressed,
   immutable, never rewritten. The original bytes are kept forever so that a parser bugfix
   can be replayed over history without re-downloading anything.
3. **Fingerprint and route.** Compute the layout fingerprint, look it up in the adapter
   registry. **An unknown fingerprint quarantines the document and notifies. It never
   guesses.**
4. **Parse.** The adapter returns a `ParsedDocument`: institution, account reference, period
   start and end, opening and closing balance where the format carries them, and the rows.
5. **Validate.** See §5. A document that fails validation is rejected in full.
6. **Persist.** Document, account upsert, and all transactions are written in a **single
   database transaction**. Any failure rolls back the entire document. There is no such
   thing as a half-imported statement.

### Draining the inbox

**Once a document's bytes are in the content-addressed store, its inbox copy is removed.**
Without this the inbox grows without bound and every run reprocesses the entire history of
everything ever dropped into it — re-parsing, re-validating and re-alerting on documents
that were settled months ago.

Draining is only safe because the store holds the original: the inbox is a queue, not an
archive. `reparse` is how anything gets read a second time.

Ingestion is also **scoped to a profile**: `--profile prod` walks `data/inbox/prod/` and
nothing else. The prod/dummy boundary enforced at `stage` would be worth nothing if
ingestion ignored it.

### Quarantine

A failure at any step writes `data/quarantine/<sha256>.reason.json` containing the failure
class, the adapter and fingerprint involved, expected versus actual balances where relevant,
and the traceback. The original is already in `data/store/`, so nothing is lost.

**It also writes a `source_document` row with `parse_status='quarantined'`.** Recording the
failure in the ledger rather than only on disk is what stops the same document being
re-parsed and re-alerted on every subsequent run — a signal that fires every night is one
nobody reads. `stage` then skips the file too, because the ledger already knows its digest.

For a quarantined document, `institution` and `doc_type` come from the folder it was filed
in, not from its contents: with an unknown layout nothing has been read out of the document
at all. `finstone status` reports these separately from imported documents for that reason.

**One bad document never stops the run.** Every other file in the batch still imports.
Quarantine depth is a metric worth alerting on (architecture §8.2), not a silent state.

### Finding the document behind a failure

Two commands, because "why did it fail" and "which file is it" are different questions and
the answer to the first is deliberately redacted:

```
finstone quarantine            one line each: digest, failure class, check, source file
finstone quarantine --export   copy the originals out under openable names
finstone report                the full arithmetic of each failure
```

The listing prints the real source path, since it runs on the operator's own terminal against
their own data; `--redact` masks it for pasting elsewhere. Check names like
`balance_reconciliation` are this codebase's words rather than the document's and stay legible
either way.

`--export` writes each failed original to `data/quarantine/files/` as
`<first 8 of digest>-<source path, flattened>`, keeping the extension so the file opens. It
reads through the `BlobStore` port rather than from `uploads/`, because the store is the
immutable record of what actually failed — the upload may since have been re-downloaded,
renamed or moved, at which point it is no longer evidence.

It **syncs rather than accumulates**: an export whose reason file has gone is deleted, and
`reparse` removes a document's export along with its reason. A stale export is the same lie
as a stale `reason.json`, in a form someone can double-click.

Because that folder holds real statements under recognisable names, it is denied to agents
alongside `uploads/prod/` and `data/store/` — see
[development-rules.md](development-rules.md) Rule 2.

### `reparse`

Because originals are immutable and content-addressed, fixing an adapter and re-running it
over history is a first-class operation rather than a recovery scramble — and, once the
inbox is drained, the only way back to a document.

```
finstone reparse --quarantined      retry everything that failed
finstone reparse --sha256 <hash>    replay one document
```

Existing rows for the document are deleted first, so a reparse is a replacement rather than
a second import, and the stale `reason.json` is cleared so `finstone report` stops
describing a failure that has since been fixed. The stored bytes are never touched.

---

## 4. Routing to an adapter

One adapter per `(institution, doc_type, layout)`. An adapter declares a
**`LayoutSignature`** — the header lines that identify its format — and a document routes to
it when it contains all of them.

```python
SIGNATURE = LayoutSignature(
    producer="skia/pdf m",
    requires=(
        "trust bank singapore limited",
        "your savings account by trust statement is ready",
    ),
    page_size=(595, 842),
)
```

Matching is a **subset** test, not equality: extra lines are ignored. That is the whole
point, and it was arrived at the hard way.

### An unrecognised vendor is treated as a rename

Institutions rename the tool that renders their statements. DBS's creator went
`Quadient Group AG~Inspire` → `Quadient CXM AG~Inspire` → `Quadient~Inspire`;
Trust's producer went `Skia/PDF m80` → `m141`. Each time every header line still
matched and only the vendor string had moved — and 107 statements stopped
routing over it.

Two layers now absorb that:

1. **Producer and creator match on tokens**, not the whole string —
   `("quadient", "inspire")` survives all three names.
2. **Anything tokens miss is treated as a hypothesis.** If a document matches an
   adapter's required header lines and differs *only* in producer or creator,
   that adapter is tried, and accepted **only if the statement reconciles to
   the cent**. What was proven is written to `data/learned-layouts.json` so the
   next statement routes directly.

This is not the "never guess" rule being relaxed. Guessing is choosing without
evidence; this proposes and then verifies against an oracle the document
carries with it. The refusals are the point:

- **No balances, no healing.** A statement that would import as
  `imported_unverified` has nothing to verify against — exactly where a wrong
  adapter could pass unnoticed.
- **Two adapters that both reconcile is a refusal**, never a tiebreak.
- **Only the vendor string is relaxed.** The header lines that identify the
  format still have to match, and are never inferred.
- Healed documents are counted in the run summary and listed by
  `finstone learned`, with the document that proved each rule.

### Why not an exact fingerprint

Routing was originally an exact hash of the header band. It broke twice in production, both
times for reasons that had nothing to do with any layout:

1. **The producer version moved.** Trust renders through headless Chromium, and an upgrade
   took `Skia/PDF m80` to `Skia/PDF m141`. Producer version digits are now stripped.
2. **The customer moved house.** The header band excludes data by dropping any line
   containing a digit — which caught `Block 000`, `EXAMPLE AVENUE 2` and
   `Singapore 000000`, but not a street name with no number in it. A new address line
   appeared, and an otherwise identical statement quarantined.

The second one exposed something worse than brittleness: **every fingerprint in the corpus
contained the customer's name.** Layout identity depended on who the customer was and where
they lived, and that identity was stored in the ledger.

A signature fixes both by naming only what the *bank* says about its own format. A change of
address, a new marketing line, a renamed customer — none of them touch routing.

### What is still a loud failure

- **No adapter claims the document → quarantine.** Never a nearest match.
- **More than one claims it → error.** If two signatures both match, they are not distinct
  enough; picking a winner would be a guess. Fixing the signatures is the answer.

Writing a signature is a judgement call by whoever adds the adapter, and a wrong one is
caught by the balance check rather than silently importing. `finstone doctor <path> --redact`
prints exactly what each adapter required and did not find, plus the lines the document does
carry, so a new layout can be added from a pasted report.

### The fingerprint still exists

`fingerprint_pdf` is retained as a *record* — stored on `source_document.layout_fingerprint`
and printed in failure reports so a layout can be referred to precisely. It no longer decides
anything.

### Debugging a failure without the statement

Statements are financial documents. Asking for one in order to debug a parser is both a
privacy problem and a slow loop, so **every failure is reportable as text**:

```
finstone doctor <path> [--redact]   parse one document and explain the result
finstone report [--redact] [--out]  render every quarantined document
```

A run prints three lines and writes the detail to a file:

```
processed 74: 63 imported, 11 quarantined; 1091 transaction(s) inserted
  failures: 11 validation_failed
  report:   data/reports/20260802-230517-failures.txt
```

That file is **redacted by default** — the whole point is that it can be handed to someone
without handing over the statements. Per-document logging is off unless `-v` is passed:
every failure is already in its reason file and in the report, and two stderr lines per
failure buried the one number that mattered.

`doctor` touches no database. On success it prints every account, every parsed transaction
and the reconciliation arithmetic. On failure it prints the offending line, how that line
was split into columns, and the lines around it — enough to tell a column-band problem from
a value-reading problem at a glance.

For a reconciliation failure it prints the arithmetic rather than a verdict, because the
difference between expected and stated is usually exactly one transaction:

```
  opening balance                       100,000.00
  + sum of 3 parsed transactions           -242.41
  = expected closing                    100,000.00
  statement says closing                100,000.00
                                    --------------
  difference                               -120.00

  A parsed transaction matches the difference exactly:
    2026-01-20  Netflix subscription        -120.00
  That row is most likely counted twice, or carries the wrong sign.
```

`--redact` masks letters and keeps digits, because amounts are the evidence and merchant
names are not. Use it when pasting a failure from a real statement.

### Layout drift, and what absorbs it

Institutions change their statements. The design assumption is not that this can be
prevented, but that it must never fail *silently* — the balance check is what guarantees
that. Beyond it, drift falls into three tiers by how much work it costs:

| Kind of change | Cost | Example seen so far |
|---|---|---|
| Anything derivable from the document | **None** | A column moving; a savings statement rendering a different number of pockets; a statement running to more pages |
| Header wording, or the rendering tool | **One fingerprint to register** | A new marketing strapline in the header band |
| A genuinely different table structure | **Adapter code** | Trust adding a transaction-date column beside the posting date in 2025 |

Two rules keep as much as possible in the first tier:

- **Derive geometry from the document.** `header_bands` reads the column x-positions off the
  table's own header row, so a layout that shifts its columns costs nothing.
- **Keep volatile things out of the fingerprint.** Amounts and dates are excluded by the
  digit filter; the producer's *version* is stripped, because Trust renders through headless
  Chromium and a browser upgrade (`Skia/PDF m80` to `m141`) was otherwise enough to
  quarantine an unchanged statement.

### Two date columns

Trust statements up to 2023 print a single `Posting date`. From 2025 they print
**transaction date then posting date** — the purchase happened on the first, it hit the
account on the second.

The adapter reads whichever it finds, so one code path serves both:

- **The last date is always `posted_date`.** It is the one inside the statement period and
  the one reconciliation depends on.
- The first, where present, is `value_date`, resolved over a window widened ~95 days
  backwards. A purchase on 29 December posting on 2 January is normal, not an error.
- If the transaction date will not resolve, `value_date` is left null and a warning logged.
  Nothing in this phase reads it — not reconciliation, not the period check, not the dedupe
  key — so rejecting a document that otherwise reconciles to the cent would be
  disproportionate. The posting date stays strict.

### Descriptions that wrap

A long merchant name is printed on its own line above or below its row, about 6pt away,
against a row pitch of about 25pt. A line *above* belongs to the row that follows — that is
how foreign-currency rows print their merchant. A line *below*, within
`base.CONTINUATION_GAP`, belongs to the row it follows.

Getting this wrong corrupts `description_norm`, which feeds `dedupe_key`, so it is a
correctness issue and not only a cosmetic one.

### The CSV adapter contract

A redacted sample dropped into `uploads/dummy/` is most useful when it preserves:

| Element | Why it matters |
|---|---|
| The **exact header row**, unmodified | It is the fingerprint. Changing it changes routing. |
| The delimiter, quoting, and encoding | Affects parsing and the fingerprint. |
| Date format, verbatim | `03/04/2024` is ambiguous; the adapter must be told which convention this bank uses. |
| Sign convention | Whether debits are negative, or there are separate debit/credit columns. |
| Currency, and whether it is a column or implied | Money is always stored with an explicit currency. |
| Opening and closing balance lines, if present | Without them, reconciliation cannot run. See §5. |
| Header/footer metadata rows | Account reference and statement period usually live here. |

Amounts, dates, descriptions and account numbers can all be altered — the *structure* is
what the adapter is written against. Keeping at least one same-day duplicate amount and one
month boundary in the sample is worth doing, because those are the two cases most likely to
expose a bug.

---

## 5. Validation

**The statement carries its own checksum. Use it.** Architecture §2.4 is emphatic about this
and it is worth restating: this single check catches dropped rows, duplicated rows, sign
errors, misread OCR digits and column misalignment, and it is worth more than any amount of
parser cleverness.

### One formula, both statement types

`amount_minor` is **signed by its effect on the account balance as the statement presents
it**: money in is positive, money out is negative. A card purchase is negative; a payment to
the card is positive. Card statements are stored with `opening = −previous_outstanding` and
`closing = −current_outstanding`.

That collapses deposit and card statements into a single check:

```
opening_balance_minor + Σ(amount_minor) == closing_balance_minor
```

A savings statement reconciles as `100,000.00 + (−242.41) = 100,000.00`; a card statement
reconciles as `−4.24 + (−2.35) = −6.59`, which is the statement's own
`4.24 + 561.35 − 559.00 = 6.59` with the sign flipped. Adapters normalise into this
convention at their boundary, so nothing downstream needs to know which formula the
institution printed.

A useful side effect: a wrong sign inference produces a loud reconciliation failure rather
than silent corruption. That is why the convention can be applied to an issuer's edge cases
without having to be certain in advance.

If this does not reconcile **to the cent**, the entire document is rejected. Not the bad
row — the document. Partial imports produce a ledger that looks fine and is wrong.

Secondary checks:

- Row count against any "N transactions" line the statement declares.
- Date monotonicity.
- Every date within `[period_start, period_end]`.
- No amount above a configurable sanity ceiling.

### Formats without balances

Many CSV exports carry no opening or closing balance, so there is nothing to reconcile
against. Those documents import with `parse_status = 'imported_unverified'` and are counted
separately in `finstone status`.

This distinction is deliberate and must not be collapsed. An unverified import is not a
verified one; treating them alike would quietly discard the only end-to-end correctness
guarantee the system has. If a bank offers both a CSV without balances and a PDF with them,
the PDF is the better source despite being harder to parse.

---

## 6. Idempotency and deduplication

Two unique constraints carry the entire retry story.

**`source_document.sha256` is UNIQUE.** Re-importing the same file is a guaranteed no-op.

**`txn.dedupe_key` is UNIQUE**, computed as:

```
dedupe_key = sha256(account_id, posted_date, amount_minor, description_norm, seq)
```

`seq` is the index of the row within its `(account, posted_date, amount_minor, description_norm)`
group **as ordered in the source document**. This matters more than it looks:

- Re-importing the same document produces identical keys, so nothing duplicates.
- Two overlapping statements listing the same transactions in the same order produce
  identical keys, so the overlap collapses correctly.
- Two genuine $4.50 coffees on the same day get `seq` 0 and 1, so both survive. Real people
  do buy two coffees.

**Known limitation.** If an institution reorders same-key rows between two overlapping
statements, `seq` assignment can differ and a transaction may import twice. This is rare —
statement ordering is nearly always stable — and monthly reconciliation against the stated
closing balance (architecture §8.2) catches the resulting drift. The alternative designs
(assigning `seq` from what is already in the database) break idempotency outright, which is
a worse trade.

### Reading the transaction table

`app/parsers/tables.py` holds what the institutions genuinely share, written
only after all six layouts were measured:

| Layout | Dates | Amount columns | Direction from |
|---|---|---|---|
| Trust acc / cc | 1–2 | FCY + SGD | leading `+` |
| DBS acc | 1 | Withdrawal, Deposit, Balance | which column |
| DBS cc | 1 | Amount | `CR` suffix |
| MariBank acc | 1 | Outgoing, Incoming | which column |
| MariBank cc | 2 | Amount | explicit `-` |
| OCBC cc | 1 | Amount | `CR` suffix |

Shared: a header row fixes the columns; a line is a row when it carries a value
in a money column; descriptions wrap onto neighbouring lines.

**Cells are cut by two rules, because the two halves of these tables are
aligned differently.** Left of the money columns, a word joins the text column
whose left edge it sits at — descriptions are left-aligned and run long. At or
right of them, a word joins the money column whose *right* edge is nearest:
amounts are right-aligned independently of their headings, and a DBS balance
begins 8pt left of the word "Balance" while a withdrawal begins 30pt right of
"Withdrawal". One rule for both misfiles data.

Not shared, and left to adapters: how direction is read, which labels mean
opening and closing, and how a period or account reference is found.

### A statement's identity is its accounts and period

`source_document` carries two identities, and they answer different questions:

| | Identifies | Answers |
|---|---|---|
| `sha256` | the **file** | "have I seen these exact bytes?" |
| `statement_key` | the **statement** | "do I already hold this account's statement for this period?" |

`statement_key` hashes `(doc_type, period_start, period_end, sorted account references)`
and is unique per tenant. A bank issues one statement per account per period; that is the
identity, and the file carrying it is not. PDFs get re-downloaded, re-saved, renamed and
passed through tools that rewrite their metadata, and every one of those changes the bytes
without changing a single transaction.

**This matters most for shared accounts.** In a household where two members both have access
to a joint account, both uploading its statement is the normal case. Keyed only on bytes,
the second upload imported as a separate document whose every row then matched an existing
`dedupe_key` and was skipped — leaving a document with balances attached and no
transactions.

Three outcomes now:

- **Same bytes** → no-op, as before.
- **Same statement, different bytes** → recognised as already held. Reported as
  `same_statement`, no second document, no phantom balances.
- **Same statement, different transactions** → **quarantined** as `conflicting_statement`.
  That is a reissued or corrected statement, and choosing between two versions silently
  would be a guess. The report says how they differ: how many rows each holds, and how many
  are unique to each.

### Account resolution

Accounts are upserted on `(institution, account_ref_masked, sub_account_label, currency)`,
derived from the document header. **If an adapter cannot determine the account, the document
quarantines** — consistent with never guessing. Attaching transactions to the wrong account
is indistinguishable from correct behaviour until it is very expensive to unwind.

**`account_ref_masked` is the masked account number for a deposit account, and the card
*product* for a card.** Card numbers change when a card is reissued or replaced while the
account continues, so keying on the number would fork one account's history in two. The
product — the card brand — is what actually distinguishes two cards held at the same bank,
and it is stable for the life of the account. Every issuer in the corpus prints it:
`OCBC REWARDS CARD`, `MARI CREDIT CARD`, `LIVE FRESH DBS VISA PAYWAVE PLATINUM`. Trust
prints no card number at all and issues one product, so its adapter asserts the product name.

Note for later adapters: at least one issuer's card statement covers several cards in one
document, with a "grand total for all card accounts" line. The account model already handles
that — one `ParsedAccount` per card — but an adapter must not assume a single account.

---

## 7. Data model

The schema is defined in [finance-pipeline-architecture.md](../finance-pipeline-architecture.md) §1
and is not restated here, to avoid the two drifting apart.

Phase 1 additions on top of it:

- The initial migration creates the **full** §1 model. Phase 1 populates `source_document`,
  `account`, `txn` and `statement_balance`; `txn_enrichment`, `recurrence_series` and
  `txn_series_link` sit empty until the enrichment phase.
- **`statement_balance`** is a new table holding each account's stated opening and closing
  balance per document. Per-pocket balances need somewhere to live, and without them the
  monthly reconciliation in architecture §8.2 has nothing to compare against later.
- **`txn` gains `fx_amount_minor`, `fx_currency` and `fx_rate`.** Card statements bill in
  foreign currency across three printed lines — merchant, then date with both amounts, then
  the rate. `amount_minor` remains the settled amount, so reconciliation is unaffected.
- `source_document` gains `statement_date`, `layout_fingerprint`, `source_profile`
  (`dummy` or `prod`) and `source_relpath`.
- `account` gains `sub_account_label`, so one document's several pockets become several
  accounts.
- `parse_status` is `TEXT` plus a `CHECK` constraint rather than a Postgres `ENUM`, because
  `ENUM` is not portable and portability is what the SQLite test run depends on.
- Timestamps are timezone-aware UTC.
- Money is `BIGINT` minor units with currency in its own column. Never a float, anywhere.

`tests/test_migration.py` asserts that the migration and `app/storage/schema.py` agree,
column for column, so the schema the tests exercise is the schema an operator actually runs.

### The schema version is checked before anything runs

Every command that touches the ledger compares the database's Alembic revision
against the one the code was written for, and refuses to start if they differ:

```
error: the database schema is out of date.
  database is at : 0001_initial
  this code needs: 0003_statement_key
  run:  alembic upgrade head
```

Without it a mismatch surfaces as a driver error naming a missing column,
partway through a run, after files have already been staged. On a fresh install
it is the first thing anyone would hit.

**After pulling changes that add a migration, run `alembic upgrade head`.**

### Profiles are tenants

`uploads/dummy` and `uploads/prod` resolve to **different tenants** — `default-dummy` and
`default`. As far as production is concerned dummy documents do not exist: not in its
counts, not in its accounts, and not in its deduplication.

That last one is why. Sharing a tenant meant a real statement whose synthetic copy had
already been imported arrived with every row deduplicated away, leaving a document with
balances attached and no transactions. Four such documents existed in practice.

A tenant is precisely "a set of records that must never mix", which is exactly the
requirement, so the isolation reuses the mechanism that already exists and is already tested
rather than inventing a second one. Every command takes `--profile`, and that choice selects
the tenant.

### Tenancy

Every ledger table carries `tenant_id`, and the constraints that could collide between
households — `(tenant_id, sha256)`, `(tenant_id, dedupe_key)`, and the `account` identity —
are scoped by it. `tenant` and `member` tables exist, members carry an OIDC issuer and
subject for SSO, and `account.owner_member_id` distinguishes a personal account from a
joint one (`NULL` = shared across the tenant).

**The system runs single-tenant and single-member.** Migration `0001` seeds one `default`
tenant and one `owner` member, and `FINSTONE_TENANT` / `FINSTONE_MEMBER` resolve to them.
Multi-tenancy is structured for, not switched on: the shape exists because retrofitting it
onto a ledger with history would mean recomputing every dedupe key, while adding it at the
initial migration cost one column.

Every `LedgerRepository` method takes a `TenantContext` as a required argument, so ingestion
already runs under a tenant. Turning multi-tenancy on later changes how that context is
*resolved* — from configuration to an authenticated session — and nothing else. See
[development-rules.md](development-rules.md) Rule 3.

---

## 8. CLI

| Command | Purpose |
|---|---|
| `finstone stage --profile <dummy\|prod>` | Copy `uploads/<profile>/` into `data/inbox/`, recursively and idempotently |
| `finstone ingest [--profile <dummy\|prod>]` | Process `data/inbox/`, or just one profile's subtree |
| `finstone run --profile <dummy\|prod>` | `stage` then `ingest` |
| `finstone doctor <path> [--redact]` | Parse one document and explain the result; no database involved |
| `finstone report [--redact]` | Render every quarantined document as a readable report |
| `finstone fingerprint <path>` | Print a document's fingerprint and the adapter it routes to |
| `finstone adapters` | List registered layouts |
| `finstone reparse --quarantined \| --sha256 <hash>` | Replay from the immutable store after an adapter fix |
| `finstone status` | Document, account and transaction counts; quarantine depth; unverified count |

---

## 9. Runtime

The container is the primary runtime. This host has only Python 3.14, where `pdfplumber` and
`psycopg` wheel availability is still patchy, so the application image pins `python:3.12-slim`
and Postgres runs from `pgvector/pgvector:pg17` — chosen now so the later vector-search phase
needs no image change.

The entrypoint is `bootstrap.ps1` (Windows) or `bootstrap.sh`, both thin wrappers over
`docker compose`. **There is deliberately no `Makefile`:** `make` is not installed on this
host, and a documented command that does not run is worse than no command at all.

---

## 10. How this gets verified

The test suite runs the same ingestion tests against **both** SQLite (default, no services
required) and Postgres (when `TEST_DATABASE_URL` is set). That dual run is the proof that
Rule 1 in [development-rules.md](development-rules.md) actually holds rather than being
merely asserted.

Test documents come from two places, and they are not interchangeable:

- `tests/fixtures/make_pdf.py` writes minimal PDFs with no third-party dependency, giving
  deterministic inputs that run anywhere including CI.
- Adapter tests run against the operator's real documents in `uploads/dummy`, marked
  `requires_dummy` so they **skip cleanly when that folder is absent**. Those are the tests
  that prove the adapters read real statements; the fixtures prove the pipeline around them.

Cases covered:

- Nested `uploads/dummy/<bank>/<type>/` subfolders are all discovered by the recursive walk.
- Running the pipeline twice produces identical document and transaction counts.
- A statement with a wrong closing balance fails reconciliation, quarantines, and writes
  **zero** transactions, with expected-versus-actual in `reason.json`.
- One document quarantining does not stop the others in the batch importing.
- Two same-day identical amounts both survive, with `seq` 0 and 1.
- An unrecognised fingerprint quarantines rather than being guessed at.
- A balance-free document lands as `imported_unverified`, not as reconciled.
- A December-to-January period resolves year-less dates correctly, and an ambiguous one
  raises instead of guessing.
- Money parsing never touches a float, and sub-cent precision is refused rather than rounded.
- The migration and `app/storage/schema.py` agree column for column.
- `stage --profile prod` refuses to run without `FINSTONE_ALLOW_PROD=1`.

### Current results on the operator's corpus

`finstone run --profile dummy` over the 11 documents in `uploads/dummy`:

| Outcome | Count | Detail |
|---|---|---|
| Imported and reconciled | 4 | All Trust; 39 transactions across 4 accounts |
| Quarantined, `unknown_layout` | 7 | DBS ×2, MariBank ×4, OCBC ×1 — no adapter yet |

Every imported account reconciles exactly, including the three Trust pockets and both card
statements. A second run inserts nothing.

## Related documents

- [finance-pipeline-architecture.md](../finance-pipeline-architecture.md) — design source of truth
- [development-rules.md](development-rules.md) — the two binding development rules
- [repo-structure.md](repo-structure.md) — repository layout
- [AGENTS.md](../AGENTS.md) — operating contract for agent-driven changes
