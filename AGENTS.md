# Agent Operating Guide

This repository is the main pipeline repo for the self-hosted finance pipeline. The default operating model is:

- one repo
- one bootstrap command
- one shared runtime and data model
- many adapters, one orchestrated pipeline

## Project intent

The repository exists to let a user clone the project and bring the platform up with a single entrypoint command: `./bootstrap.ps1` on Windows or `./bootstrap.sh` elsewhere, both thin wrappers over `docker compose`. There is deliberately no `Makefile` — see Agent expectations below.

The system architecture described in [finance-pipeline-architecture.md](finance-pipeline-architecture.md) is the source of truth for design intent.

## Development rules

Three rules are binding on every contributor, human or agent. All are stated in full in
[docs/development-rules.md](docs/development-rules.md); they are repeated here because an
agent that reads only this file must still follow them.

### Rule 1 — Decouple by default, up to a 20% performance ceiling

Every implementation choice sits behind a seam unless the abstraction costs 20% or more in
performance. 1.0s becoming 1.2s behind an interface is an acceptable trade. Switching
Postgres for MySQL should be a configuration change, not a project.

**Any abstraction measured above 15% must be flagged explicitly in the change summary, with
a real number.** Above 20%, do not abstract by default — state the measurement and let the
operator decide.

Two things are deliberately *not* abstracted, and must not be "fixed": money is always
`BIGINT` minor units with currency in a separate column (a correctness invariant, never a
float), and shared code stays free of dialect-specific SQL so the SQLite test run keeps the
database seam honest.

### Rule 2 — `uploads/prod/` is off-limits to agents

`uploads/prod/` holds real financial statements. **No agent may read, list, search, copy, or
otherwise open anything under it** — not to check a format, not to verify a fix, not once.

Use `uploads/dummy/` for redacted or synthetic documents, and `tests/fixtures/` for the
committed synthetic files the test suite runs against. If a task appears to require real
data, stop and ask the operator.

The same applies to real statement bytes wherever they end up, which is three places and not
one: `uploads/prod/`, the content-addressed store at `data/store/`, and any originals the
operator exports to `data/quarantine/files/`.

When a document fails, use `finstone quarantine` to see which documents failed, then
`finstone doctor <path>` or `finstone report` and paste the output. They are built so that no
statement ever has to be shared to diagnose a parser bug; `--redact` masks filenames,
descriptions and references while keeping the amounts and structure.

### Rule 3 — Build for portability, and for more than one owner

The system must stay installable on someone else's machine with one command, and must not
accumulate decisions that make per-tenant separation harder to add later. No host-specific
assumptions, no manual setup steps, and nothing outside `app/domain/dedupe.py` may compute
or assume the shape of a dedupe key.

The destination is a tenant with several members, each with their own SSO login. The
structure is in place — every ledger table carries `tenant_id`, uniqueness is tenant-scoped,
and `TenantContext` is a required argument on every repository method — but the system runs
as one tenant with one member until the pipeline is proven end to end.

Two things must not be undone: no repository method may touch a ledger table without
filtering on `tenant_id`, and no password column may be added to `member` (authentication
belongs to the identity provider). Both are asserted by tests.

## Expected repo structure

Use the following high-level shape:

```text
/
  AGENTS.md
  README.md
  finance-pipeline-architecture.md
  docs/
    repo-structure.md
    agent-doc-sync.md
    development-rules.md
    ingestion.md
  app/
    domain/          # models, money, normalisation, dedupe
    ports/           # protocol definitions — the decoupling seams
    storage/         # repository and blob-store adapters
    pipeline/
    parsers/
    enrichers/
    orchestration/
    migrations/
  infra/
    compose/
    systemd/
    env/
    scripts/
  ui/
    pwa/
    dashboards/
  uploads/
    dummy/           # redacted or synthetic documents — agent-readable
    prod/            # real documents — never read by agents, untracked
  data/
    inbox/
    store/
    quarantine/
  tests/
    fixtures/
```

`uploads/` is the human drop surface; `data/inbox/` remains the pipeline's watched folder.
The `stage` step copies between them, which is the single place the prod/dummy distinction
is enforced. See [docs/ingestion.md](docs/ingestion.md).

## Single-command delivery model

The repo should stay organized so that one command can bootstrap the core stack.

Recommended startup path:

1. create or validate environment variables from templates
2. start Postgres and supporting services
3. run schema migrations
4. bring up the orchestrator and dashboards
5. verify health checks and pipeline readiness

If an implementation cannot satisfy the one-command startup model, the repo should document the blocker in the root README and the architecture note.

## Documentation synchronization rule

When an agent changes any of the following areas, it must update the relevant markdown files in the same change set:

- architecture or design assumptions
- repo layout or service boundaries
- data model and schema meaning
- delivery/startup flow
- runbooks or operational behavior
- security or credential handling
- observability, alerting, or backup expectations

### Minimum doc-sync checklist

If a change affects architecture, data flow, repo shape, or delivery, update all applicable markdown files in this list:

- [finance-pipeline-architecture.md](finance-pipeline-architecture.md)
- [README.md](README.md)
- [docs/repo-structure.md](docs/repo-structure.md)
- [docs/agent-doc-sync.md](docs/agent-doc-sync.md)
- [docs/development-rules.md](docs/development-rules.md)
- [docs/ingestion.md](docs/ingestion.md)
- [docs/api-contracts.md](docs/api-contracts.md)
- [docs/roadmap.md](docs/roadmap.md) — when a change finishes something listed there, or
  moves it. A roadmap that still promises what shipped last week is worse than none.
- [docs/contributing.md](docs/contributing.md) — when the branch model, the checks that
  gate a change, the commit grammar, or the release procedure changes.

### Rule of thumb

If the change would make a future agent or operator misunderstand the runtime, the repo needs a doc update.

## The API contract is binding

[docs/api-contracts.md](docs/api-contracts.md) is the agreement between this
server and every client written against it. Two obligations follow, and neither
is optional:

- **Building a UI, or any other client? Read it first.** It states what a
  client may rely on — money as integer minor units, spending signed negative,
  transfers already excluded, unknowns refused rather than guessed. Do not infer
  those from the current implementation, and do not hardcode anything the
  contract says is tenant data.
- **Changing anything under `app/api/`? Update the contract in the same
  commit.** Not afterwards, and not in a follow-up.

The reason this is stricter than the rest of the doc-sync rule: under §5.2 a
self-hosted server runs a version the operator chose and upgrades on their
schedule. A route that quietly stops matching the document does not break a
build that someone can fix — it breaks somebody else's installation, remotely,
with no way for them to know why. `tests/test_api.py` asserts the promises
rather than the implementation for the same reason; a failure there means a
client somewhere is about to be lied to.

If a contract change is genuinely unavoidable, it is a **new API version**, not
an edit to the current one.

## Agent expectations

- Keep architecture, docs, and implementation aligned.
- Do not silently diverge the repo shape from the documented shape.
- Prefer small, deliberate updates over broad documentation churn.
- When in doubt, update the docs with the same commit that changes the system.
- Never document a command that does not run on the target host. There is deliberately no
  `Makefile`; the entrypoint is `bootstrap.ps1` or `bootstrap.sh`.

## Review prompt for future agents

Before finishing a change, answer these questions:

1. Did this change alter the repo shape, runtime behavior, data model, or deployment model?
2. If yes, which markdown files must be updated to reflect the change?
3. Are those markdown files included in the same change set?
4. Is every new external dependency reachable only through a port in `app/ports/`, and was
   any abstraction measured above 15% flagged with a number?
5. Did anything read from `uploads/prod/`?
6. Does every repository call still pass a `TenantContext`, and does every ledger query
   still filter on `tenant_id`?
7. Does the change still install and run from the bootstrap script alone?

If any answer is "no" to the second, third, sixth or seventh question, treat the change as
incomplete. If the answer to the fifth is "yes", treat it as a data incident and say so
plainly.
