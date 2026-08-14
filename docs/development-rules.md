# Development Rules

These rules are binding on every contributor to this repository, human or agent. They are
not style preferences — they exist because this system holds a financial ledger, and both
failure modes they guard against (a component that cannot be replaced, and real financial
data leaking into an agent's context) are expensive and hard to reverse.

If you read only one file in `docs/`, read this one. The other rule set you must know is in
[AGENTS.md](../AGENTS.md) (documentation stays in sync with implementation).

---

## Rule 1 — Decouple by default, up to a 20% performance ceiling

**Every implementation choice sits behind a seam unless the abstraction costs 20% or more
in performance.** A step that takes 1.0s and becomes 1.2s behind an interface is an
acceptable trade. The point is that no single vendor, library, or engine choice should be
load-bearing enough that replacing it means a rewrite.

The canonical example is the database. Switching Postgres to MySQL should be a
configuration change, not a project.

### The flagging obligation

**Any abstraction measured above 15% must be flagged explicitly in the pull request or task
summary, with a number.** Not "this is probably a bit slower" — an actual measurement.

The 15% flag and the 20% ceiling are deliberately different. The gap between them is the
zone where the trade is still worth making but the operator deserves to know about it.

| Measured cost | What to do |
|---|---|
| < 15% | Abstract it. No comment needed. |
| 15–20% | Abstract it, and **flag the number** in the change summary. |
| > 20% | Do not abstract by default. State the measurement, propose the coupled design, and let the operator decide. |

### How the ceiling gets measured

A claim about performance is not a measurement. When a seam plausibly sits near the
threshold, benchmark it:

- `scripts/bench_ingest.py` (planned) times N synthetic rows through the abstracted
  repository versus a direct, unabstracted implementation, and prints the delta as a
  percentage.
- Report the real number, not an estimate copied from a doc.
- If a seam exceeds 20%, the first remedy is a fast path **inside** the adapter — for
  example a Postgres `COPY` bulk-load path behind the same `LedgerRepository` interface —
  not deleting the seam.

Benchmarks should run against the volume this system actually sees. A personal finance
ledger is thousands of rows, not millions; an abstraction that costs 40% on a ten-million-row
insert and 3% on a five-thousand-row insert is a 3% abstraction here.

### What the first real measurement found

Worth recording, because none of it was the seam:

| | Before | After |
|---|---|---|
| Whole dashboard, 11 calls | 1,096 ms | 243 ms |
| A typical endpoint | 48–62 ms | 10–17 ms |
| `/review` | 536 ms | 80 ms |

Three causes, in order of size, and not one of them was the repository abstraction:

1. **A fresh database connection per request.** `build_repository` is lazy and free, but
   the first query on a new engine cost 27 ms of TCP and authentication against Postgres —
   against 8 ms for the query it was opened to run — and the pool was disposed at the end of
   every request. Three quarters of every response was connection setup. The engine is now
   shared for the life of the process; the tenant context is still resolved per request,
   which is the part that carries the isolation.
2. **Re-resolving the tenant three times per request.** `resolve_context` found-or-created
   the tenant, then the member, then read the role. On an existing install that is one join.
3. **Scanning 536 regexes against every row.** Nearly every rule is `^<escaped name>$` — a
   dictionary key wearing a regex costume, because that is what `operator_rule` writes. They
   are now indexed, and names are judged once each rather than once per row.

The lesson to carry forward: measure before optimising, and expect the answer to be
per-request overhead rather than the thing the code is shaped around. Rule 1's ceiling is
about abstraction cost, and abstraction was not the cost here.

### The Phase 1 seams

Each seam is a `Protocol` in `app/ports/`, chosen by environment variable, with the concrete
implementation in `app/storage/` or `app/parsers/`. Pipeline code imports the port and never
the implementation.

| Seam | Port | Phase 1 implementation | Cost to swap | Perf cost |
|---|---|---|---|---|
| Database engine | `LedgerRepository` | SQLAlchemy **Core** (not the ORM) | Change `DATABASE_URL` | ~10–20% on bulk insert vs. raw `psycopg` — **to be measured, not assumed** |
| Blob store | `BlobStore` | `LocalFsBlobStore`, content-addressed, sharded `store/ab/cd/<sha256>` | Write an S3/MinIO class | ~0% |
| Document source | `DocumentSource` | `LocalDirectorySource`, recursive | Write an IMAP or Paperless class | ~0% |
| Parser | `StatementAdapter` | CSV and PDF, routed by fingerprint | Register a class | ~0% — it *is* the design |
| Notifier | `Notifier` | `LogNotifier` | Write an ntfy or Gotify class | ~0% |
| Configuration | — | Environment variables only, `.env` from [../infra/env/](../infra/env/) | — | 0% |

SQLAlchemy Core rather than the ORM is itself an application of this rule: Core gives
dialect portability at close to raw-driver speed, while the ORM's identity map and unit of
work would cost more than they return for a write-mostly ingestion path.

### What is deliberately *not* abstracted

Equally important, so that nobody "fixes" these later in good faith:

- **Money as `BIGINT` minor units, with currency in a separate column.** This is a
  correctness invariant, not an implementation choice. There is no seam here and no float,
  ever, anywhere in the system.
- **`pgvector`.** Vector search arrives in a later phase. Adding a vector column now would
  break portability to SQLite — and therefore break the test suite that proves Rule 1 holds
  — for zero present benefit. When it lands, it goes behind its own port.
- **Postgres-only SQL.** Deduplication uses a portable *select existing keys → insert the
  complement* pattern with the unique index as a backstop, **not** `ON CONFLICT`. JSON
  columns use SQLAlchemy's `JSON` type, which maps to `JSONB` on Postgres and `TEXT` on
  SQLite. Writing dialect-specific SQL in shared code silently deletes the seam.

### How the seam is proven, not just claimed

An interface nobody has ever swapped is an assumption. The test suite runs the **same**
ingestion tests against two engines: SQLite by default, so tests need no running services,
and Postgres when `TEST_DATABASE_URL` is set. If a Postgres-ism leaks into pipeline code,
the SQLite run fails. That is the enforcement mechanism for Rule 1 — the table above is only
a description of intent.

### The measured cost, as of Phase 1

`python scripts/bench_ingest.py` produces these figures. They are recorded here so the next
person does not have to re-derive them, and re-measured whenever the storage layer changes.

| Measurement | Value |
|---|---|
| Repository vs. raw driver, 40 rows (one statement) | **+68%** (13.3 ms vs 7.9 ms) |
| Repository vs. raw driver, 5 000 rows (a decade backfilled) | **+120%** (99 ms vs 45 ms) |
| Per-document PDF parse | 185–221 ms |
| **Overall per-document impact** | **2.4%** |

**The rule is about overall impact, and overall impact is 2.4%.** In isolation the
repository looks expensive, but PDF parsing dominates per-document time by roughly forty to
one, so the seam costs about 5 ms on a document that takes 200 ms to read. Deleting it would
buy nothing an operator could perceive and would cost the portability the whole design rests
on.

The isolated ratio is recorded anyway, because it is the number that would start to matter
if bulk loading ever became the dominant path. If it does, the remedy is a bulk fast path
inside the Postgres adapter — behind the same `LedgerRepository` interface — not deleting
the seam.

### The only lever that can move the total

The same measurement that excuses the repository seam identifies where the time actually is:
**PDF text extraction is ~98% of per-document time.** Every optimisation aimed at storage,
hashing or dedupe is therefore competing for the remaining 2%, and the ceiling on all of them
together is smaller than the noise in a single parse.

Parsing is independent per document, so the one change that can plausibly clear the 20%
threshold in this rule is **bounded concurrency over documents**. Nothing else in the current
design can, and proposals to optimise the 2% should be measured against that before they are
taken seriously — several have been suggested, all of them plausible-sounding and none of
them able to matter.

It is not built, and this is not a claim that it is worth 20%: per this rule, a claim about
performance is not a measurement. What is recorded here is that it is the only candidate, so
it is the only one worth benchmarking first. Whoever does must keep quarantine and reporting
deterministic under concurrency — a run whose failure list depends on scheduling order is
worse than a slow one.

---

## Rule 2 — `uploads/prod/` is off-limits to agents

`uploads/prod/` holds real financial statements: real account numbers, real balances, real
counterparties. **No agent may read, list, search, copy, or otherwise open files under
`uploads/prod/`.** Not to "check the format", not to "verify a fix", not once.

Use `uploads/dummy/` instead. It exists for exactly this purpose and holds redacted or
synthetic documents that are safe to read. If a task appears to require reading real data,
that is the signal to stop and ask the operator, not to proceed carefully.

### Where test data actually lives

| Location | Contents | Agent access |
|---|---|---|
| `uploads/prod/` | Real statements, as the operator filed them | **Never.** Untracked, gitignored. |
| `data/store/` | The same bytes, content-addressed under their digest | **Never.** |
| `data/quarantine/<tenant>/files/` | Failed originals exported under recognisable names | **Never.** |
| `data/backups/` | Every original *and* every ledger row, in one archive | **Never.** |
| `uploads/dummy/` | Redacted or synthetic statements, operator-supplied | Yes |
| `tests/fixtures/` | Small synthetic files committed to git, built to exercise edge cases | Yes |

**Real statement bytes exist in four places, not one.** `uploads/prod/` is only where they
arrive. Ingestion copies every document into the content-addressed store,
`finstone quarantine --export` copies failed ones out again under names chosen to be
recognisable — which is the point of the command and also exactly what makes the folder
worth denying — and `finstone backup` puts all of them plus the whole ledger into a single
file. The rule is about the *contents*, so it follows them wherever they go.

An archive is the most concentrated of the four: one path, and everything is in it.

Everything an agent legitimately needs in order to debug a failure is already available
without any of these: `finstone quarantine` for which document, `finstone report --redact`
for why it failed, and `finstone doctor <path> --redact` for how it parsed.

`tests/fixtures/` and `uploads/dummy/` are not interchangeable. Fixtures are committed so
the test suite runs anywhere, including in CI with no operator data present. `uploads/dummy/`
is the operator's staging area for realistically-shaped documents and is not committed.

### Enforcement, in decreasing order of strength

1. **This document and [AGENTS.md](../AGENTS.md).** The written rule is what actually
   carries this, because it is the only layer that binds every agent regardless of tooling.
2. **`.claude/settings.json`** denies the file tools — read, search, glob, edit, write —
   against `./uploads/prod/**`, `./data/store/**`, `./data/quarantine/**/files/**` and
   `./data/backups/**`. This removes the easy accidental path.
3. **A runtime gate.** `stage --profile prod` refuses to run unless `FINSTONE_ALLOW_PROD=1`
   is set in the environment, so an operator cannot process real data by muscle memory.

**Be clear about the limit of layer 2.** Deny-by-path covers the file tools. An agent
running an arbitrary shell command can still read the directory, and no pattern list closes
that off completely. The tooling reduces accidents; it does not substitute for the rule.

### The other half of the rule: what gets *written down*

Rule 2 as written above keeps agents out of the real documents. It says nothing about what
happens to a value once a human has read one — and that is the half that actually failed.
See [handoff.md](../handoff.md): a Luhn-valid card number, an account number and a home
address were pasted out of real statements into layout fixtures, four days after an earlier
scrub had removed the same class of data. Nobody was careless with `uploads/prod/`. The
values simply arrived by a route nothing was watching.

Four layers now watch it, and they are listed here in the order they fire rather than by
strength, because the earliest one is the only one that *prevents* anything:

1. **`.githooks/pre-push`**, installed by `bootstrap`. Refuses to push personal data,
   scanning the whole range being pushed. `--no-verify` skips it; it is fast feedback.
2. **The `publish.yml` gate in the private staging repository.** This is the boundary.
   Nothing reaches the public repository without passing it, and it squashes the branch on
   the way through so a value that existed only in an intermediate commit is never
   published at all.
3. **`pr-checks / privacy`** on the public repository, for anything that arrived by another
   road — most realistically a contributor's pull request, which never passes the gate.
4. **`tests/test_no_personal_data.py`** in the suite, scanning the working tree.

All four call the same rule table, allowlist and skip list, in that one file. A second copy
would drift, and the tuning is not obvious: Luhn is what keeps the card rule usable, the OCR
glyph tables must be skipped, and there is deliberately no postcode rule because a bare
six-digit pattern collides with the minor-unit amounts this codebase is full of.

The scanner knows shapes, not ownership. It will not catch a real merchant name, a surname,
or a genuine transaction description — the pull request template asks a human about those.

### If the rule is broken

**Deleting the value in a follow-up commit does not remove it.** The commit that added it
still carries it, and `git log -S` still finds it. That is precisely why the earlier scrub
did not hold. The fix is to rewrite: `git commit --amend` or `git rebase -i`, then
force-push the feature branch, which carries no protection.

If it reached the public repository, the branch is not the problem — a public push is
permanent in ways deleting a branch does not undo. Stop and treat it as an incident.

Treat it as a data incident, not a mistake to quietly fix. Say so plainly in the task
summary, note which files were opened, and do not paste, summarise, or transcribe their
contents anywhere — including into commit messages, comments, test fixtures, or a
description of "what the format looks like".

---

---

## Rule 3 — Build for portability, and for more than one owner

The endgame is a system that installs cleanly on someone else's machine, and that can run
either self-hosted or hosted with per-tenant separation. Neither is being built now, but
both constrain decisions taken today, because some of them are cheap now and expensive
later.

### What this rules out immediately

- **No host-specific assumptions.** Paths come from configuration, not from a developer's
  layout. Nothing may assume a particular OS, an absolute path, or a tool that is not
  declared as a dependency. `make` is the standing example: it is not installed on the
  target host, so the entrypoint is `bootstrap.ps1` / `bootstrap.sh`.
- **One command to install.** Every added service or manual setup step is a tax on every
  future install. If a change cannot be brought up by the bootstrap script, it is not done.
- **One command to restore, through the same flow.** Installing is the easy half; the promise
  self-hosting actually makes is that the operator's data survives their hardware. A restore
  that needs a runbook is a restore nobody completes under stress, and a backup nobody has
  restored is not a backup. This is a product claim as much as an engineering one — it is
  most of what makes self-hosting defensible against a service that does it for you — so the
  restore path is exercised, not assumed.

  `finstone backup` and `finstone restore` are that command, and the archive is deliberately
  logical rather than an engine dump: moving from SQLite to Postgres **is** a restore, so the
  rare path is walked every time the ordinary one is. See [backups.md](backups.md).

### Where the two commands actually are

Both halves are built and both have been walked end to end on the real ledger.

```bash
./infra/scripts/install.sh --server-name finstone.lan
./infra/scripts/restore.sh <archive>
```

Install brings up Postgres with a password **generated on that machine**, nginx
serving the UI and API on one origin, a systemd unit for boot, and a nightly
backup timer — because a restore command is worth nothing without something to
restore, so the backup is a default rather than an option.

Re-running install is the upgrade path, which is why it never regenerates a
secret, drops a volume, or overwrites a file it did not write. Rotating the
Postgres password on an existing install would leave the volume holding the old
one and nothing able to connect: an "upgrade" that takes the ledger away.

**Verified rather than assumed**, which is the whole point of the rule: the
production ledger was backed up through the packaged command, restored into an
empty database through the packaged command, and compared — 235 documents, 14
accounts, 5,137 transactions, identical on both sides. See [deploy.md](deploy.md).

One thing packaging **cannot** fix and should not pretend to: there is no
authentication. Rule 3 makes the install portable, not safe to expose. The
deploy documentation opens with that rather than burying it.
- **No single-machine assumptions in the data layer.** The content-addressed store is
  already behind `BlobStore` precisely so a hosted deployment can put it on object storage
  without touching pipeline code.

### Multi-tenancy: structured for, not switched on

The destination is a **tenant with several members** — a family plan, where each member has
their own SSO login. The structure for that is in place. The behaviour is not: the system
runs as one tenant with one member until the pipeline is proven end to end.

The shape was built before it was needed because the expensive part is not the feature, it
is the retrofit. These three constraints are tenant-scoped **from the initial migration**:

| Constraint | Why it must be scoped |
|---|---|
| `source_document` unique on `(tenant_id, sha256)` | Two households can legitimately hold the same statement file; global uniqueness would silently treat the second import as a duplicate |
| `txn` unique on `(tenant_id, dedupe_key)` | Two households can have identical transactions on the same account reference |
| `account` unique on `(tenant_id, institution, account_ref_masked, sub_account_label, currency)` | Two households banking at the same institution would otherwise merge into one account |

Retrofitting any of these once a ledger has history means recomputing every dedupe key in
it. Doing it at migration `0001` cost one column.

### The rules that keep it working

- **`TenantContext` is a required argument, never a default.** Every `LedgerRepository`
  method takes it explicitly. Omitting it is a `TypeError` at the call site; an implicit
  default would make it a silent cross-tenant read. For a system holding several
  households' financial records, the noisy failure is the only acceptable one.
- **Tenant scope is not only a database concern.** Anything on disk that carries a
  document's *content* is scoped the same way: `data/quarantine/<tenant>/` for reason files
  and exported originals, `data/reports/<tenant>/` for run reports, `data/inbox/<profile>/`
  for staging. A reason file holds the real filename, the parsed rows, the balances and the
  account references — separating the ledger and then pooling that beside it would leave the
  separation decorative.

  Two things stay deliberately shared. `data/store/` is content-addressed, so identical bytes
  are one object by definition and a digest cannot be guessed without already holding the
  file; a hosted deployment moves it behind object storage with per-tenant prefixes, which is
  a `BlobStore` change and nothing else. `data/learned-layouts.json` holds PDF renderer
  identities and no customer data, and a layout proven by one household should help every
  household.
- **No repository method may touch a ledger table without filtering on `tenant_id`.**
  `tests/test_tenancy.py` asserts reads, writes and counts all stay inside their tenant, and
  `tests/test_migration.py` fails if any tenant-scoped table gains a globally unique
  constraint.
- **Authentication is the identity provider's job.** `member` stores the OIDC issuer and
  subject — the only globally stable identifier a provider gives you — plus an email that is
  explicitly *not* an identity, since it can be reassigned. **There is no password column and
  none may be added:** a credential this system never holds is one it can never leak. A test
  asserts the column does not exist.
- **Members are distinct from account owners.** `account.owner_member_id` is nullable, and
  `NULL` means shared across the tenant — a joint account. Ingestion always writes `NULL`,
  because a statement cannot know whose account it is and guessing would be worse than
  leaving it joint until someone says otherwise.

### What is deliberately deferred

Add now what is painful later; defer what is purely additive. Still to come, and none of it
requires touching existing rows: SSO login flow and session handling, per-account access
grants beyond the owner/shared split, role enforcement at an API boundary, and tenant
provisioning or invitations. Scheduled together in [roadmap.md](roadmap.md) as Alpha 4:
after alpha testing, and before anything is exposed beyond a trusted network.

The same principle governs the columns and tables that exist and are empty. A nullable
column on a table with history is free to add now and a backfill later;
`txn_enrichment.beneficiary` and the two recurrence tables are kept on that basis rather
than dropped and re-added, and `app/storage/schema.py` says beside each one what writes it
and what does not. **A column nobody populates looks exactly like a feature until somebody
queries it**, so the schema is expected to say which is which.

### Language and runtime

Python is a Phase 1 choice, not a permanent one. Should a rewrite in Go or .NET be worth it
later for speed or single-binary distribution, the ports in `app/ports/` are the boundary
that makes it a bounded piece of work rather than a rewrite: the domain rules (minor units,
dedupe keys, the reconciliation formula) are specified in this repository's documentation and
tests, not only in the Python that currently implements them.

Worth knowing before that trade is made: on the measurements above, **PDF text extraction is
roughly 98% of per-document time**, so the achievable speedup is bounded by what the
replacement's PDF library can do, not by the language.

---

## Applying these rules to a change

Before finishing any change, confirm:

1. Is every new external dependency — database, storage, transport, parser, notifier —
   reachable only through a port in `app/ports/`?
2. If any abstraction was measured above 15%, is the number stated in the change summary?
3. Did anything read from `uploads/prod/`?
4. Do the tests still pass on **both** SQLite and Postgres?
5. Does the change still install and run from the bootstrap script alone, with no manual
   step and no host-specific assumption?
6. Does it make a future tenant scope harder to add?
7. Are the documentation updates required by [AGENTS.md](../AGENTS.md) included in the same
   change set?

## Related documents

- [AGENTS.md](../AGENTS.md) — operating contract for agent-driven changes
- [finance-pipeline-architecture.md](../finance-pipeline-architecture.md) — design source of truth
- [ingestion.md](ingestion.md) — Phase 1 ingestion and storage design
- [repo-structure.md](repo-structure.md) — repository layout
- [agent-doc-sync.md](agent-doc-sync.md) — documentation synchronization contract
