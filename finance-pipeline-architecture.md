# Self-Hosted Finance Pipeline — Technical Architecture

Target host: Intel N95, 2×1 TB, Omada ER707, managed switches, Asus Lyra APs.
Constraint: all data on-prem; encrypted off-site backups permitted.
Credential root of trust: existing Vaultwarden.

> Assumption: "Trust" and "MariBank" place you in Singapore. That matters in two
> places — SGFinDex (§10) and the mobile-only bank problem (§6b).

---

## 1. The data model is the architecture

Everything else is replaceable. Get this wrong and every downstream phase inherits the damage.

**Separate immutable facts from mutable predictions.** Parsed transactions are ground
truth and never change. Categories, beneficiary, and frequency are *predictions* that get
recomputed whenever the rules or the model improve. Keeping them in different tables means
you can re-classify five years of history without risking the underlying ledger.

```
source_document
  id, sha256, institution, doc_type, period_start, period_end,
  fetched_at, fetch_method, storage_path, parser_version, parse_status

account
  id, institution, account_ref_masked, currency, kind (deposit/card/loan)

txn                                    -- immutable
  id, account_id, source_document_id,
  posted_date, value_date,
  amount_minor BIGINT, currency,        -- integers only, never float
  description_raw, description_norm,
  counterparty_norm,
  dedupe_key                            -- unique index

txn_enrichment                          -- mutable, versioned
  txn_id, category, subcategory,
  beneficiary,                          -- "for whom"
  confidence NUMERIC, source ENUM(rule|knn|llm|human),
  model_version, computed_at
  -- human rows always win; never overwritten by an automated pass

recurrence_series
  id, merchant_norm, amount_centre, amount_tolerance,
  period_days, period_label, confidence,
  expected_next, last_seen, member_count

txn_series_link  (txn_id, series_id)
```

Two details that prevent most of the pain later:

- **Money is `BIGINT` minor units.** No floats, ever. Currency is a separate column.
- **`dedupe_key = hash(account_id, posted_date, amount_minor, description_norm, seq)`**
  where `seq` is a counter for genuine same-day identical transactions (two $4.50 coffees
  do happen). Unique index on it. Combined with `source_document.sha256`, re-importing the
  same PDF is a guaranteed no-op — which is what makes every retry in the pipeline safe.

---

## 2. Phase 1 — Manual drop, deterministic parse

### 2.1 Ingest surface

One watched folder is the entire interface. Everything writes into it:

```
/srv/finance/inbox/          ← drop zone (Syncthing / SMB / Nextcloud)
/srv/finance/store/<sha256>  ← content-addressed originals, immutable
/srv/finance/quarantine/     ← failed parses + reason.json
```

Three feeds land there: manual downloads (phase 1), IMAP fetch from the `finance@` alias
(cheapest automation available — do this before touching a browser), and later the
automated retrieval tier.

**One addition in front of `inbox/`: a human staging surface, split by sensitivity.**

```
uploads/dummy/   ← redacted or synthetic documents, safe for agents to read
uploads/prod/    ← real statements, never read by agents, untracked
        │
        │  stage  — recursive, idempotent, preserves relative paths
        ▼
   inbox/        ← unchanged; still the pipeline's entire machine interface
```

`inbox/` stays exactly what it was, and later feeds write into it directly without touching
`uploads/`. The reason for the extra hop is that agent-driven development needs a folder of
real-shaped documents that is provably *not* the real one — and putting that boundary at a
single, explicit step means the prod/dummy distinction is enforced in one place instead of
being smeared through every downstream component. See
[docs/development-rules.md](docs/development-rules.md) and
[docs/ingestion.md](docs/ingestion.md).

**Optional but recommended: Paperless-ngx as the document-of-record layer.** It already
does consume-folder watching, OCRmyPDF, tagging, retention and full-text search, and
exposes an API the parser can poll. If you want to find "that DBS statement from March
2024" as a human, this is how. If you skip it, keep the content-addressed store — you
still need the original bytes to re-parse after a parser bugfix.

### 2.2 Parser preference order

Always take the most structured format the institution offers. This is the highest-leverage
decision in the whole build.

| Tier | Format | Tooling | Reliability |
|---|---|---|---|
| 1 | CSV / OFX / QFX / QIF / MT940 / CAMT.053 | `ofxtools`, `mt-940`, plain `csv` | Near-perfect |
| 2 | Digital PDF with text layer | `pdfplumber` (word-level x/y), `camelot` (lattice for ruled tables, stream for whitespace) | Good, layout-specific |
| 3 | Scanned PDF | `OCRmyPDF` to add a text layer → tier 2 | Fair, needs validation |
| 4 | Unknown / novel layout | `Docling` (IBM, self-hosted, layout-aware) or a VLM | Fallback only |

Multi-page tables are where general-purpose parsers break down: a table that starts
mid-page-1 and ends mid-page-3 with no repeated headers defeats most of them, which is
exactly why per-institution logic that knows the structure in advance wins.

### 2.3 Adapter registry + fingerprinting

One adapter per `(institution, doc_type, layout_version)`. Route by fingerprint:

```python
fingerprint = sha1(
    normalise(page1_header_text)
    + "|" + ",".join(f"{c:.0f}" for c in column_x_positions)
    + "|" + doc_producer_metadata
)
```

Unknown fingerprint → quarantine + notification. Never guess. A layout change should be a
loud, boring failure, not a silent corruption of the ledger.

### 2.4 The validation that matters most

**Statements carry their own checksum. Use it.**

```
opening_balance + Σ(credits) − Σ(debits) == closing_balance
```

If this doesn't reconcile to the cent, reject the *entire document* — don't import partial
rows. This single check catches dropped rows, duplicated rows, sign errors, misread digits
from OCR, and column misalignment. It is worth more than any amount of parser cleverness.

Secondary checks: row count vs. any "N transactions" line; date monotonicity; every date
inside `[period_start, period_end]`; no amount above a sanity ceiling.

---

## 3. Enrichment — category, beneficiary, frequency

### 3.1 Category and beneficiary: three stages, cheapest first

1. **Deterministic rules.** Regex/exact match on `description_norm`. Versioned in git.
   Covers the recurring 85–90% of volume. Fast, free, explainable, diffable.
2. **k-NN against your own labelled history.** Embed `description_norm`, store vectors in
   `pgvector` on the same Postgres. Nearest labelled neighbour, with distance as confidence.
   This is what makes the system get better the more you correct it.
3. **LLM for the residual only.** Batch weekly, not per-transaction. Constrained JSON output
   against your fixed category enum. Record `model_version` and prompt hash on the row so
   results are reproducible and re-runnable.

**"For whom" is just a second label with the same three stages** — and it's much easier than
category because cardinality is tiny (household members). The account or card is an
enormously strong prior. If you follow through on the per-category virtual-card idea from
the earlier plan, the card effectively *becomes* the label and this collapses to a lookup.

### 3.2 Frequency: don't use an LLM for this

Recurrence is a time-series problem with a clean deterministic solution. An LLM here is
slower, non-reproducible, and worse.

```
1. Group candidates: same merchant_norm, amount within ±7% of a running centre
2. Need n ≥ 3 occurrences
3. Compute inter-arrival deltas (days)
4. If  stdev(deltas) / mean(deltas) < 0.15  → it's a series
5. Snap mean to nearest canonical period, with tolerance:
     7 (±1) | 14 (±2) | 30/31 (±3, month-end aware) | 91 (±5) | 365 (±7)
6. Emit expected_next = last_seen + period_days
```

Handle weekend/holiday shifts (many direct debits land on the next business day) by
comparing on business-day distance, not calendar distance.

What this buys you, for free: **missed-payment alerts** (`expected_next` passed with no
match) and **price-increase alerts** (amount drifts outside tolerance on a known series) —
which is most of the practical value of tracking subscriptions at all.

### 3.2a Enrolment — the user knows things the dates cannot show

Detection alone can only find what has already happened three times. The operator knows on
the *first* payment that a subscription has started, knows which scattered rows were meant
to be one series, and knows why a payment was smaller than usual. Three interactions carry
that in.

**The governing rule: a declaration is an input to detection, never an output of it.** It is
the operator asserting something about their own money, so a re-run of the detector must
never overwrite, downgrade or silently drop it — the same guarantee §3.3 gives corrections
via `source='human'`. Everything below is stored beside the derived series, not inside it,
and survives a full rebuild.

**1. Declare a new recurring payment, from one occurrence.** After a single payment the user
marks it as recurring and states the period they expect. The system then *watches* rather
than concludes: each cycle it looks for a match, and reports when the expected pattern fails
to appear. A declared series therefore has a state — `awaiting confirmation` until enough
occurrences arrive to satisfy §3.2 on its own evidence, then `confirmed`. The alert that
matters early is **"you told me this repeats and it has not"**, which is exactly the case
detection cannot reach, because detection needs three occurrences and this has one.

**2. Enrol a missed series, retrospectively.** The user picks a period and selects at least
three transactions they say belong together, and the system derives the rule that would have
caught them: the merchant pattern, the amount centre and the tolerance wide enough to hold
what was chosen. Two things follow. The derived rule is shown back before it is saved,
because a rule inferred from three rows will also claim future rows and the user should see
what they are agreeing to. And if the selection cannot yield a coherent rule — the amounts
or intervals are too scattered — that is reported rather than forced, since a rule matching
everything is worse than no rule.

**3. Explain a break in the pattern.** A payment that is smaller, larger or absent is not
always a fault: promotions, annual discounts and payment holidays are ordinary. The user
pins a reason to the occurrence, and the system then *verifies* it — did the amount come in
lower as described, did it return to the centre afterwards, was it genuinely absent — rather
than accepting the explanation and suppressing the alert. An unverified explanation is worth
less than no explanation, because it teaches the user to trust a signal that stopped being
checked.

**On the aggregate ambition for (3).** Reporting that "others have seen discounts on similar
services" means data leaving one household and informing another, and that runs against the
tenant isolation everything else here is built on. It is buildable, and only on these terms:
**opt-in per user and off by default**; nothing shared beyond a merchant identity, a period
and a direction of change; no amounts, no dates, no account or member identifiers; and
aggregated with a floor — a minimum number of contributing households before any observation
is published — so a report can never describe one identifiable person's spending. Under
§5.2 this is also a *hosted-only* capability that cannot exist as a feature gap: a
self-hosted install has no cohort to aggregate over, so it must degrade to silence, not to a
missing button.

### 3.3 Human-in-the-loop review queue

Anything below a confidence threshold goes to a review queue. Corrections write to
`txn_enrichment` with `source='human'` and are permanently immune to automated overwrite.
They also become training data for stage 2. This feedback loop is the difference between a
system that plateaus at 70% and one that reaches 97%.

---

## 4. Storage

**Postgres**, single instance. Not SQLite — you want concurrent readers from the dashboard,
`pgvector`, and real migrations.

- Extensions: `pgvector`. Skip TimescaleDB; personal-scale volume doesn't justify it.
- Migrations in git (Alembic or Atlas). Never hand-edit schema on a box holding your ledger.
- Originals stay on disk, content-addressed, referenced by `sha256`. Never mutated.
- Sizing: a few hundred MB after decades. Both the DB and the document store fit
  comfortably; the 2×1 TB is overwhelmingly backup headroom, not capacity pressure.

---

## 5. Dashboard — phone, laptop, TV

The three targets have genuinely different requirements, and the TV is the one that breaks
the "just make it responsive" assumption: 10-foot viewing distance, no pointer, glanceable,
read-only.

| Option | Strength | Weakness on this build |
|---|---|---|
| **Grafana** | Free, Postgres datasource, kiosk mode, alerting you need anyway | Weak for ad-hoc financial slicing; utilitarian UX |
| **Metabase** | Best-in-class ad-hoc pivoting on tabular data | JVM footprint is heavy on an N95 |
| **Evidence.dev** | SQL → static site, instant load, git-versioned | Only the interactivity you hand-build |
| **Custom PWA** (SvelteKit/Next) | Only route to real 10-foot UI + home-screen install + offline | Most work |

**Recommendation: two surfaces, not one.**

- **Grafana** for *pipeline health* — run status, parse failures, quarantine depth,
  reconciliation drift, dead-man's-switch. You need it for §8 regardless, so it's free.
- **A small custom PWA** for the *finance UX*. PWA gets you home-screen install on the
  phone, offline read of cached data, and a `/tv` route with a large-type, auto-cycling,
  pointer-free layout. Serve it over Tailscale; a Chromecast/Fire Stick or a Pi in kiosk
  Chromium points at the LAN URL.

Design mobile-first → TV → desktop. Put the review/correction queue on phone and laptop
only; the TV is strictly read-only glanceable.

### 5.1 What the finance UX has to do — operator's brief

Recorded from the operator, to be built rather than re-derived later. The ordering is
theirs; the notes under each are what the data layer must provide for it.

**A. One consolidated view, across every account.** The landing surface is the whole
financial position, not a per-bank list. It carries:

- a graph of **financial growth over the last 6 months**, where 6 is a user setting and not
  a constant;
- **average spending per month, per week and per day**, over that same window.

Those averages must be computed over the *same* range as the graph, or the two halves of the
screen quietly describe different periods.

**B. Every metric is filterable and the range is movable.** Filter by one or more
institutions, by spending type, or by both at once. Extend, shorten, or type a custom date
range. This is the requirement that decides the query layer: every figure on the dashboard
has to be derivable from `(date range, set of accounts, set of categories)` rather than
precomputed for one window, so the aggregates cannot be materialised per-month and left at
that.

**C. Recurring payments get their own page**, showing what recurs weekly, monthly and
yearly. §3.2 already derives frequency without an LLM; this is its surface.

**D. The dashboard has to manage the reader's mental state**, on both the consolidated view
and the recurring page. Money growing well should read as calm; money depleting should read
as *slight* concern — enough to prompt a look, not enough to alarm. Concretely: a coloured
arrow for direction plus a percentage, because a percentage is what makes a number legible
at a glance without doing arithmetic.

Two things this brief demands of the layers beneath it, worth stating where they will be
read before the UI is written:

- **Growth is not the sum of transactions.** It is balance over time, so it comes from
  `statement_balance` and the reconciliation trail, not from summing `txn`. A transfer
  between two of the household's own accounts must not appear as growth in either
  direction.
- **Spending totals must exclude internal transfers**, or every credit-card payment inflates
  the monthly average by the size of the bill. `transfer_link` exists for this; any query
  behind a spending figure joins against it and drops both legs. A figure that ignores it is
  wrong in the direction that looks worst, which is exactly the direction (D) is sensitive
  to.

### 5.2 Client and server are separate, and the server is the user's choice

**The front end talks to the back end over an HTTP API and shares nothing else with it** —
no template rendering, no server-side session coupling, no direct database access from the
UI. The client is a first-class consumer of a documented API, and the same API is what any
future client uses.

**The hosting model is Bitwarden's.** A user signs in to *a server*, and which server that
is belongs to them: their own on-premise install, or a hosted instance run for them. The
client is configured with a server URL and is otherwise identical in both cases. This is a
promise about the product, not only about the code, and it constrains the build now.

What it rules out immediately:

- **No endpoint may be hardcoded in the client.** Server address is user-supplied
  configuration, entered at sign-in and stored per-profile, exactly as Bitwarden does it.
- **No feature may exist only on the hosted instance.** The moment one does, self-hosting
  becomes a degraded tier and the promise is broken. Hosted may differ in *operations* —
  backups, availability, support — never in capability.
- **The API is the contract, and it is versioned.** A self-hosted server will lag the hosted
  one, so a client must state the version it speaks and a server must be explicit when it
  cannot. Breaking a self-hoster's install with a client update is the failure mode this
  guards against.
- **Authentication is per-server.** Credentials, sessions and tokens belong to the server
  signed into and never travel between them. Combined with the OIDC issuer/subject already
  in `member` (Rule 3), the identity provider is a property of the deployment.

What it costs, stated plainly so it is not discovered later: every capability needs an API
surface before it has a UI, which is slower than rendering a page from the database. The
return is that self-hosted and hosted stay the same product, and that a second client — a
watch face, a CLI, someone else's — costs nothing extra to support.

The tenancy work in Rule 3 already assumes this endpoint: a hosted server is several
households on one deployment, and every ledger table is scoped from migration `0001`. What
5.2 adds is that the *client* must not care which of the two it is talking to.

---

## 6. Phase 2 — Automated retrieval

Split by channel. The constraints are completely different and conflating them is the main
way this phase fails.

### 6a. Email — do this first

Many institutions either attach the statement or send a "your statement is ready" notice.
An IMAP poller on the `finance@` alias that pulls attachments into `inbox/` is the cheapest
automation in the entire system and requires no credential handling at all. Exhaust this
before writing a single line of browser automation.

### 6b. Web portals — Playwright + Vaultwarden

Deterministic Playwright scripts, one per institution, in git.

**Credential retrieval.** Vaultwarden speaks the Bitwarden API, so `bw` (Bitwarden CLI)
works against it: unlock once at boot, hold `BW_SESSION` in memory, `bw get item <id>`
per run. `bw serve` gives a local REST endpoint if that's cleaner for the orchestrator.

**Be honest about what this costs you.** An unlocked vault session living on the same box
that runs the scrapers means compromise of that box is compromise of the vault. Unattended
automation and "credentials are never available in plaintext" are mutually exclusive —
pick knowingly. Mitigations, in order of value:

1. A **separate Vaultwarden account** for automation, containing *only* the scraper
   credentials. Your personal vault is never unlocked by the pipeline.
2. Unlock at boot from an operator-supplied passphrase or a hardware token — never a
   passphrase file on the same disk.
3. Read-only sub-user credentials at the institution, where offered. Several banks support
   a view-only secondary login; this is by far the strongest control available.

**MFA is the actual blocker**, and it varies by type:

| MFA type | Automatable? | Approach |
|---|---|---|
| TOTP | Yes — but be clear-eyed | Storing the seed next to the password collapses 2FA back to 1FA |
| SMS OTP | Partially | Requires the SIM/device in the loop (see §6c) |
| Push approval | No, by design | Pipeline pauses, notifies you, waits for the tap |
| Hardware key | No | Manual step, permanently |

Design the orchestrator around a **suspend-for-human-approval** primitive from day one.
Some institutions will always need a tap, and a pipeline that can't pause gracefully will
just fail nightly forever.

### 6c. Mobile-only banks (Trust, MariBank) — the hard part

**State the blocker up front: emulators do not work.** These apps use the Play Integrity
API. Waydroid, Redroid, BlueStacks and LDPlayer fail device attestation and the app simply
refuses to run — installing Play Services or microG does not reliably fix it, because the
environment isn't Google-certified. Rooting a real device and hiding it with
Magisk + Zygisk + Play Integrity Fix does work, but Google rotates detection every few
weeks, so the module needs constant updating — a stale module is the single most common
cause of "my banking app stopped working three weeks after rooting."

**The workable architecture is a device farm of one:**

```
N95 ──LAN──▶ [stock, unrooted Android phone]
             · permanently powered, wall-mounted or in a drawer
             · USB debugging on, `adb tcpip 5555`
             · isolated VLAN — this device holds banking credentials
             · Appium (UiAutomator2) or Maestro driving it
```

A cheap current-gen phone, never rooted, never updated to a ROM, passes integrity
naturally. That's the whole trick.

**Automation layer, by tier:**

- **Maestro** — YAML flows, far more tolerant of layout drift than Appium. Best default.
- **Appium/UiAutomator2** — fully deterministic, more brittle, better when you need
  precise control.
- **droidrun** (MIT, Python, drives real devices via ADB + accessibility tree, works with
  local models through Ollama) — the LLM-agent tier for exploration and self-healing, not
  for the nightly path. Benchmarks around 91% on AndroidWorld, but non-deterministic and
  slow; pair it with a deterministic framework rather than replacing one.

**Critical technique: don't scrape the transaction list off the screen.** Almost every
banking app has an in-app *export statement* / *share* action producing a PDF or CSV.
Automate to that button, then either share-to a Syncthing/Nextcloud folder on the device or
`adb pull` from `/sdcard/Download`. The file then enters the exact same Phase 1 parser you
already built and tested. Screen-scraping a list view is fragile, paginated, and gives you
no checksum to validate against.

**SMS OTP** on the same device: a companion app forwarding to your ntfy topic, or an
`adb shell content query` against the SMS provider (needs the default-SMS role granted).

**Worth deciding deliberately:** enabling ADB and automating a banking app very likely
breaches the institution's terms of service, and if fraud occurs the bank will point at it.
That's a real risk, not a formality — it's your call, but make it with open eyes.

---

## 7. Phase 3 — Self-healing scripts

Your instinct is the right architecture: **AI proposes, deterministic code executes, human
approves.** Never let the agent tier be the nightly path.

```
1. Nightly deterministic run (Playwright / Maestro)
       │
       ├─ success ──▶ document lands in inbox/ ──▶ Phase 1 parser
       │
       └─ failure ──▶ classify
                        ├─ transient (timeout, 5xx, network)
                        │     └─▶ retry w/ exponential backoff + jitter, then give up quietly
                        └─ structural (selector missing, unexpected screen)
                              └─▶ escalate

2. Agent tier attempts the goal once
       Web:     Skyvern (AGPL, self-host via Docker Compose, bring-your-own-LLM
                incl. Ollama; vision-based so it survives DOM changes) or browser-use
       Android: droidrun

3. Agent emits TWO artefacts:
       (a) the downloaded document — so tonight isn't lost
       (b) a PROPOSED deterministic script for tomorrow

4. Proposal opens a git branch/PR with:
       · diff vs. last known-good script
       · screenshot/video of the agent run
       · the new selectors it settled on

5. ntfy push → you review the diff on your phone → approve → merge

6. Merged script becomes the deterministic path. Agent tier sleeps again.
```

**Guardrails that are not optional:**

- **Read-only intent.** The agent's goal is always "navigate to statements and download."
  Never transfers, never settings changes. Enforce structurally where possible via a
  view-only sub-login, not just by prompt wording.
- **Never auto-merge.** An LLM that can silently rewrite the script that logs into your
  bank, without review, is the one failure mode with unbounded downside. Your
  notify-and-check design is correct — hold the line on it.
- **Golden fixtures.** Store a HAR / screenshot set per institution so parser and script
  assertions run offline in CI without touching the bank.
- **Rate discipline.** One login attempt per institution per day, jittered. Repeated
  automated logins are a fraud signal and will get accounts locked.
- **Cost ceiling.** Cap agent-tier invocations per week; a script failing in a loop that
  escalates nightly will quietly burn tokens.

---

## 8. Orchestration and plumbing

### 8.1 Orchestrator choice

| Option | Fit | Notes |
|---|---|---|
| **Windmill** | **Best fit** | AGPL; Python/TS scripts as first-class; schedules, retries, and a native *suspend-until-approved* primitive that maps exactly onto your human-review step |
| **n8n** | Good for the glue | Visual, huge node library (IMAP, HTTP, S3, ntfy); fair-code Sustainable Use License, free for internal personal use; weak on lineage/backfills |
| **Dagster / Prefect** | Powerful, heavy | Dagster's asset+partition model fits "one statement = one partition" beautifully; more to operate than this needs |
| **systemd timers + a Python CLI** | Genuinely viable | Fewest moving parts, lowest maintenance. Don't dismiss it |

Skip Airflow entirely. If you want retries, approvals and a UI without building them:
Windmill. If you want the smallest possible surface: systemd.

**No message broker.** Postgres as the queue (`SELECT … FOR UPDATE SKIP LOCKED`) is more
than sufficient at this volume. Adding Redis or RabbitMQ here is pure operational cost.

### 8.2 Resilience checklist

- **Idempotency everywhere**, keyed on `source_document.sha256` and `txn.dedupe_key`.
  Every rerun must be safe. This is what lets you retry aggressively without fear.
- **Quarantine, don't crash.** One unparseable document goes to `quarantine/` with a
  `reason.json` and a notification; the run continues for every other institution.
- **Dead-man's switch — the failure that actually hurts is silence.** A cron that stopped
  running four months ago is far worse than one that fails loudly. Self-host
  **Healthchecks** (open source) and ping it on every successful run, or a Grafana rule on
  "no successful run in N days." Build this before you build anything clever.
- **Alerting via ntfy** (self-hosted, push to phone, no account) or Gotify. Route: parse
  failures, unknown layouts, quarantine depth > 0, low-confidence backlog above threshold,
  missed recurring payment, reconciliation drift, pipeline silence.
- **Observability:** structured JSON logs → Loki; metrics → Prometheus; both into Grafana.
  If RAM gets tight on the N95, drop Loki first and keep files + Prometheus.
- **Reconciliation is the ultimate check.** Monthly, compare pipeline-derived closing
  balance per account against the statement's stated closing balance. Any drift means the
  pipeline is lying to you, and you want to know that month, not next year.

---

## 9. Data protection and backups

- **At rest:** LUKS, or ZFS native encryption if you go ZFS across the two 1 TB disks.
  Note the tradeoff: unattended reboot + encrypted volume means the key must live on the
  box, or you accept manual unlock after every power cut. Decide which; don't drift into it.
- **Postgres:** nightly `pg_dump`. WAL archiving only if you genuinely want PITR — at this
  data size, logical dumps are plenty.
- **Backup tool: restic or Kopia.** Both do client-side encryption, dedupe, and incremental
  snapshots. The off-site copy is ciphertext the provider cannot read, which is exactly what
  satisfies "on-prem data, encrypted off-site backups allowed."
- **3-2-1:** live on disk A → restic repo on disk B → restic repo off-site (B2 / Wasabi /
  S3). At a few hundred MB, off-site costs pennies per month.
- **Append-only off-site.** Use a B2 application key *without* delete permission, plus
  bucket lifecycle rules. Ransomware on the N95 must not be able to wipe the remote copy.
  This is the difference between a backup and the illusion of one.
- **Test restores quarterly**, into a scratch container, automated, with an alert if the
  restore fails. An untested backup is a rumour.
- **Vaultwarden's own backup is separate and critical** — it is the root of the entire
  automation tier. Back it up on a different schedule, to a different key.

---

## 10. Constraints to accept before starting

1. **The N95 has no usable GPU.** Local VLM/LLM inference is a batch-overnight path at
   best (single-digit tokens/sec on CPU for a 7B model). Design the LLM tiers as weekly
   batch jobs, not inline steps — or accept an external API call for the residual.
2. **Emulators are closed for Trust/MariBank.** Physical stock device or nothing.
3. **Unattended automation weakens your credential posture.** A vault that can unlock
   itself is a vault an attacker on that host can unlock. Separate automation account,
   read-only sub-logins where available.
4. **ToS and fraud liability.** Scraping and ADB-driving banking apps very likely breaches
   terms and may shift liability if something goes wrong.
5. **SGFinDex is the "correct" API answer and is not available to you.** It's real, it's
   Singpass-authenticated, and it has a published OpenAPI spec — but access requires being
   an onboarded participating application with PKI client assertions and a registered
   callback, not an individual. It also returns position/balance data rather than
   transaction-level detail, and the participating institutions are the incumbents
   (DBS/POSB, OCBC, UOB, Citi, HSBC, Maybank, StanChart, SGX CDP, plus insurers) — Trust
   and MariBank aren't among them. Worth knowing so you don't chase it.

---

## 11. Build order

| Step | Deliverable | Why here |
|---|---|---|
| 1 | Postgres schema + migrations | Everything depends on it |
| 2 | Content-addressed store + watched folder | The interface every feed writes to |
| 3 | One CSV adapter, end to end | Proves the pipeline with the easiest input |
| 4 | Balance-reconciliation validator | Build before more adapters, so every adapter is verified from birth |
| 5 | One PDF adapter + fingerprint registry | The real parsing problem, now with a safety net |
| 6 | Rules-based categorisation + review queue | Starts accumulating the labels stage 2 needs |
| 7 | Recurrence detector | Deterministic, high value, no dependencies |
| 8 | Grafana pipeline health + ntfy + dead-man's switch | Before automation, so failures are visible |
| 9 | restic 3-2-1 + first tested restore | Before the data becomes irreplaceable |
| 10 | PWA dashboard (phone → TV → desktop) | The payoff |
| 11 | IMAP auto-fetch | Cheapest automation, no credentials |
| 12 | Playwright + `bw` for web portals | Phase 2 proper |
| 13 | Physical Android device + Maestro export flows | The hard channel |
| 14 | k-NN categorisation on pgvector | Needs the labels from step 6 |
| 15 | Agent tier + PR-proposal loop | Phase 3; only worth it once 12–13 are breaking regularly |

Steps 1–10 are a complete, useful system with zero automation. If momentum dies there,
you've still replaced the subscription app and you own the data.
