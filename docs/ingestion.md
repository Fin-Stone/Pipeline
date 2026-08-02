# Phase 1 — Ingestion and Storage Design

**Status: design of record. Not yet implemented.**

This document is the specification the Phase 1 implementation will be built against. It is
written before the code so the design can be reviewed on its own terms. Anything marked
*planned* does not exist yet.

Scope is steps 1–5 of the build order in
[finance-pipeline-architecture.md](../finance-pipeline-architecture.md) §11: schema and
migrations, the content-addressed store and watched folder, one CSV adapter end to end, the
balance-reconciliation validator, and the PDF adapter with its fingerprint registry.

Out of scope: categorisation, beneficiary, recurrence detection, dashboards, IMAP fetch, and
every form of automated retrieval.

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

### Quarantine

A failure at any step writes `data/quarantine/<sha256>.reason.json` containing the failure
class, the adapter and fingerprint involved, expected versus actual balances where relevant,
and the traceback. The original is already in `data/store/`, so nothing is lost.

**One bad document never stops the run.** Every other file in the batch still imports.
Quarantine depth is a metric worth alerting on (architecture §8.2), not a silent state.

### `reparse`

Because originals are immutable and content-addressed, fixing an adapter and re-running it
over history is a first-class operation rather than a recovery scramble. `reparse --sha256 <hash>`
re-runs a single document from the store; the `parser_version` on `source_document` records
which adapter version produced the existing rows.

---

## 4. Fingerprinting and the adapter registry

One adapter per `(institution, doc_type, layout_version)`, routed by fingerprint, exactly as
described in architecture §2.3.

**CSV:** `sha1(normalised header row + delimiter + column count)`.

**PDF:** `sha1(normalise(page 1 header text) + column x-positions + producer metadata)`.
Column positions come from `pdfplumber`'s word-level x/y output, so a layout change that
moves a column is a different fingerprint.

### Unknown fingerprint is a loud, boring failure

This is the single most important behaviour in the routing layer. A statement whose layout
has changed must quarantine and notify — never fall through to a "best effort" or
"close enough" adapter. Silent corruption of the ledger is far worse than a failed import,
because a failed import is visible today and a corrupted ledger is discovered years later.

### Adding an adapter

Additive. No refactor, no changes to pipeline code.

1. Drop a redacted sample into `uploads/dummy/`.
2. Run `finstone fingerprint <path>` to print the fingerprint and whether anything currently
   matches it.
3. Write the adapter class in `app/parsers/csv/` or `app/parsers/pdf/` and register it
   against that fingerprint.
4. Add a small fixture to `tests/fixtures/` so the layout is covered by the test suite from
   birth.

**There are currently zero registered real-bank layouts.** Until redacted samples exist,
every real statement will quarantine on step 3 of `ingest` — which is the designed and
correct behaviour, not a bug.

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

```
opening_balance + Σ(credits) − Σ(debits) == closing_balance
```

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

### Account resolution

Accounts are upserted on `(institution, account_ref_masked, currency)`, derived from the
document header. **If an adapter cannot determine the account, the document quarantines** —
consistent with never guessing. Attaching transactions to the wrong account is
indistinguishable from correct behaviour until it is very expensive to unwind.

---

## 7. Data model

The schema is defined in [finance-pipeline-architecture.md](../finance-pipeline-architecture.md) §1
and is not restated here, to avoid the two drifting apart.

Phase 1 notes on top of it:

- The initial migration creates the **full** §1 model. Phase 1 populates only
  `source_document`, `account` and `txn`; `txn_enrichment`, `recurrence_series` and
  `txn_series_link` sit empty until the enrichment phase.
- `source_document` gains two Phase 1 columns not in §1: `source_profile` (`dummy` or `prod`)
  and `source_relpath`, both for staging provenance.
- `parse_status` is `TEXT` plus a `CHECK` constraint rather than a Postgres `ENUM`, because
  `ENUM` is not portable and portability is what the SQLite test run depends on.
- Timestamps are timezone-aware UTC.
- Money is `BIGINT` minor units with currency in its own column. Never a float, anywhere.

---

## 8. Planned CLI

*None of these exist yet.*

| Command | Purpose |
|---|---|
| `finstone stage --profile <dummy\|prod>` | Copy `uploads/<profile>/` into `data/inbox/`, recursively and idempotently |
| `finstone ingest` | Process everything in `data/inbox/` |
| `finstone run --profile <dummy\|prod>` | `stage` then `ingest` |
| `finstone fingerprint <path>` | Print a file's fingerprint and the adapter it routes to, if any |
| `finstone status` | Document count, transaction count, quarantine depth, unverified count |
| `finstone reparse --sha256 <hash>` | Re-parse one document from the immutable store |

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

Cases the suite must cover:

- Nested `uploads/dummy/<bank>/<year>/` subfolders are all discovered by the recursive walk.
- Running the pipeline twice produces identical document and transaction counts.
- A fixture with one row deleted fails reconciliation, quarantines, and writes **zero**
  transactions, with expected-versus-actual balances in `reason.json`.
- Two same-day identical amounts both survive, with `seq` 0 and 1.
- An unrecognised fingerprint quarantines rather than being guessed at.
- A balance-free CSV lands as `imported_unverified`, not as reconciled.
- Money parsing and round-tripping never touches a float.

## Related documents

- [finance-pipeline-architecture.md](../finance-pipeline-architecture.md) — design source of truth
- [development-rules.md](development-rules.md) — the two binding development rules
- [repo-structure.md](repo-structure.md) — repository layout
- [AGENTS.md](../AGENTS.md) — operating contract for agent-driven changes
