# Repository Structure and Delivery Model

## Why this repo is the main pipeline repo

This repository is the primary runtime for the finance pipeline. It should own the code, orchestration, data contracts, and deployment bootstrap needed to run the app in a self-hosted environment.

## Target repository layout

```text
repo-root/
  README.md
  AGENTS.md
  finance-pipeline-architecture.md
  bootstrap.ps1
  bootstrap.sh
  docs/
    repo-structure.md
    agent-doc-sync.md
    development-rules.md
    ingestion.md
  app/
    domain/
    ports/
    storage/
    pipeline/
    parsers/
    enrichers/
    orchestration/
    migrations/
  infra/
    compose/
    nginx/
    systemd/
    env/
    scripts/
  ui/
    pwa/
    dashboards/
  uploads/
    dummy/
    prod/
  data/
    inbox/
    store/
    quarantine/
  tests/
    fixtures/
```

## What each area should contain

### `app/`

Contains the actual finance pipeline code:

- deterministic ingest adapters
- statement parsers
- enrichment logic
- recurrence detection
- ledger and reconciliation logic
- the orchestrator-facing Python/TypeScript runtime

The subfolders split along the decoupling seams required by
[development-rules.md](development-rules.md):

| Folder | Contains |
|---|---|
| `domain/` | Models, money as minor units, date resolution, description normalisation, dedupe key derivation, recurrence, transfers, reconciliation, and the tenant context. No I/O. |
| `ports/` | `Protocol` definitions only — `LedgerRepository`, `BlobStore`, `DocumentSource`, `StatementAdapter`, `Notifier`. These are the seams. |
| `storage/` | The schema plus concrete repository and blob-store implementations. Pipeline code never imports these directly. |
| `pipeline/` | Stage, ingest, validate, quarantine, diagnostics, reconcile — the orchestrated flow. |
| `parsers/` | PDF I/O, fingerprinting, column bands, the adapter registry, and one package per institution. |
| `enrichers/` | Empty. Categorisation, recurrence and transfers landed in `domain/` and `pipeline/` instead, where the rest of the rules live; this folder is kept only as the home for the k-NN and model tiers of §3.1 when they arrive. |
| `orchestration/` | Empty. Schedulers and approvals arrive with automated retrieval; scheduling today is a systemd timer in `infra/systemd/`. |
| `migrations/` | Alembic environment and versioned migrations. |

Two boundaries are load-bearing and should not be blurred:

- **`app/parsers/pdfio.py` is the only module that imports `pdfplumber`.** Adapters build on
  its line and column model rather than each re-deriving how to turn positioned words into
  rows, which is also what makes replacing the PDF library a bounded change.
- **`app/storage/factory.py` is the only module that knows which class implements which
  port.** That is what makes the seams in `app/ports/` genuinely swappable rather than
  nominally so.

### `infra/`

Everything needed to run this on a machine that is not the developer's.

| Folder | Contains |
|---|---|
| `compose/` | The stack: API, UI, CLI container, and the Postgres overlay |
| `nginx/` | One reverse-proxy site file, serving UI and API on a single origin |
| `systemd/` | Unit templates — boot-time start, and the nightly backup timer |
| `scripts/` | `install.sh`, `restore.sh`, `backup.sh`, `ingest.sh` |
| `env/` | Reserved for per-environment overlays; `.env.example` at the root is the template today |

The two scripts that matter are `install.sh` and `restore.sh`, because
[development-rules.md](development-rules.md) Rule 3 asks for **one command to
install and one command to restore**. The other two exist to keep a guard from
being worked around: `ingest.sh` sets `FINSTONE_ALLOW_PROD=1` for exactly one
command, and `backup.sh` is what makes a restore have something to restore.

Deployment is documented in [deploy.md](deploy.md), which opens by saying there
is no authentication yet, because that is the first thing anyone putting this on
a network needs to know.

### `ui/`

Contains the user-facing surfaces:

- dashboard
- PWA for phone / TV view
- Grafana and alerting overlays

### `uploads/`

The human drop surface, organised however the operator finds convenient and walked
recursively by the `stage` step. Split into two profiles with materially different handling:

- `uploads/dummy/` holds redacted or synthetic statements and is safe for agents to read.
- `uploads/prod/` holds **real financial statements**. Agents must never read it — see
  [development-rules.md](development-rules.md) Rule 2. It is untracked and gitignored, and
  `stage --profile prod` refuses to run without `FINSTONE_ALLOW_PROD=1` in the environment.

Nothing is ever deleted from `uploads/`. It belongs to the operator, not the pipeline.

### `data/`

Contains local runtime data surfaces and the watch-folder conventions:

- `inbox/` for incoming documents
- `store/` for content-addressed immutable originals
- `quarantine/` for failed or suspect documents

`data/inbox/` is the machine surface: the single watched folder that is the pipeline's whole
interface. Keeping it distinct from `uploads/` means the prod/dummy distinction is enforced
at exactly one place — `stage` — instead of being smeared through every downstream
component. Later feeds such as IMAP fetch write into `data/inbox/` directly and never touch
`uploads/`.

### `tests/`

The test suite, plus `tests/fixtures/`: small synthetic documents committed to git so the
suite runs anywhere, including in CI with no operator data present. Fixtures are not a
substitute for `uploads/dummy/` and the two are not interchangeable.

## One-command startup philosophy

A user should be able to clone the repo and run one repository-root command that brings the primary stack online.

That command should handle:

1. environment setup
2. database startup
3. migration application
4. service startup order
5. health validation

That command is `bootstrap.ps1` on Windows or `bootstrap.sh` elsewhere, both thin wrappers
over `docker compose`. **There is deliberately no `Makefile`** — `make` is not installed on
the target host, and a documented command that does not run is worse than no command at all.

The container is the primary runtime: the application image pins `python:3.12-slim` because
the host carries only Python 3.14, where `pdfplumber` and `psycopg` wheel availability is
still patchy. Postgres runs from `pgvector/pgvector:pg17`, chosen now so the later
vector-search phase requires no image change.

The repo should not require multiple independent clones just to run the base application.

## Documentation continuity rule

Any change that modifies the repo shape, runtime boundaries, bootstrap flow, schema assumptions, or operational model must update the repository markdown files in the same change set.

The following docs are the update targets for such changes:

- [finance-pipeline-architecture.md](../finance-pipeline-architecture.md)
- [README.md](../README.md)
- [AGENTS.md](../AGENTS.md)
- [docs/repo-structure.md](repo-structure.md)
- [docs/development-rules.md](development-rules.md)
- [docs/ingestion.md](ingestion.md)
- [docs/api-contracts.md](api-contracts.md)
- [docs/roadmap.md](roadmap.md)

## Practical recommendation

Keep the repo monolithic from the delivery standpoint, even if some internal folders are independently versioned later. The main experience for operators and users should remain a single clone and a single startup command.
