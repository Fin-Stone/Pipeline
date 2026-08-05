# Finstone Finance Pipeline

This repository is the main delivery repo for the self-hosted finance pipeline.

## Goal

Allow a user to clone the repo and bring the app up through a single root-level command.

## Primary design principle

Keep the system as one coherent runtime with one shared data model and one bootstrap path.

## Status

**Phase 1 — data ingestion and storage — is implemented.** Statements dropped into
`uploads/` are staged, hashed into an immutable content-addressed store, routed to an adapter
by layout fingerprint, validated against the statement's own opening and closing balances,
and written to Postgres. Anything that fails is quarantined with a readable reason and the
run continues.

Trust Bank savings and credit card statements parse and reconcile end to end. DBS, MariBank
and OCBC quarantine as unknown layouts until their adapters are written — the designed
behaviour, not a gap. See [docs/ingestion.md](docs/ingestion.md).

```
finstone run --profile dummy      stage and ingest
finstone status                   ledger and quarantine counts
finstone doctor <path>            parse one document and explain the result
finstone quarantine               which documents failed, and which files they are
finstone report                   why every quarantined document failed
```

Every command that touches the ledger takes `--profile dummy|prod`. It selects the uploads
tree, the tenant, and that tenant's quarantine — real and synthetic documents share none of
the three.

## Running the whole thing

```bash
docker compose up
```

UI on http://localhost:8080, API on http://localhost:8000. Nothing to configure
first: the database is SQLite inside `data/`, migrations run on start, and the UI asks
for a server address rather than having one baked in. See
[infra/compose/README.md](infra/compose/README.md) for the CLI container, Postgres, and
what is mounted read-only.

## API

The UI talks to the backend over HTTP and shares nothing else with it. The server address is
the user's choice — their own install or a hosted one — as Bitwarden does it.

```
pip install -e .[api]
uvicorn app.api.main:app        # /docs for the interactive schema
```

**[docs/api-contracts.md](docs/api-contracts.md) is the contract.** Read it before writing a
client; update it in the same commit as any change under `app/api/`. It is not a description
of the implementation — it is what a self-hosted server promises, and drifting from it breaks
somebody else's installation rather than your build.

## Repo shape

- `app/` contains the pipeline core logic
- `infra/` contains deployment, container, and service setup
- `ui/` contains the user-facing dashboard / PWA layer
- `uploads/` is the human drop surface for statement documents
- `data/` contains runtime storage surfaces such as inbox, store, and quarantine
- `tests/` contains the test suite and its committed synthetic fixtures

### `uploads/` versus `data/inbox/`

These are two different surfaces and the distinction matters.

`uploads/` is for humans. It is organised however you find convenient — by bank, by year,
nested as deep as you like — and it is split into two profiles:

| Folder | Contents | Agent access |
|---|---|---|
| `uploads/dummy/` | Redacted or synthetic statements | Yes |
| `uploads/prod/` | **Real financial statements** | **Never.** Untracked and gitignored. |

`data/inbox/` is for the machine: the single watched folder that is the pipeline's entire
interface. The `stage` step copies from `uploads/` into it, and later feeds — IMAP fetch,
automated retrieval — write into it directly.

## Development rules

Three rules bind every contributor, human or agent. All are stated in full in
[docs/development-rules.md](docs/development-rules.md) and summarised in
[AGENTS.md](AGENTS.md):

1. **Decouple by default, up to a 20% performance ceiling.** Implementation choices sit
   behind a seam unless the abstraction costs 20% or more. Anything measured above 15% must
   be flagged with a real number. Measured as of Phase 1: the database seam is **2.4% of
   per-document time**, because PDF parsing dominates by roughly forty to one.
2. **`uploads/prod/` is off-limits to agents.** Real financial data. Use `uploads/dummy/`.
3. **Build for portability, and for more than one owner.** One command to install and no
   host-specific assumptions. The schema is tenant-scoped and carries `tenant`/`member`
   tables with SSO identity fields, but runs single-tenant and single-member until the
   pipeline is proven end to end.

## Documentation

- [finance-pipeline-architecture.md](finance-pipeline-architecture.md) is the design and architecture source of truth.
- [docs/development-rules.md](docs/development-rules.md) states the three binding development rules.
- [docs/ingestion.md](docs/ingestion.md) is the Phase 1 ingestion and storage design of record.
- [docs/repo-structure.md](docs/repo-structure.md) explains the repository layout and startup strategy.
- [docs/agent-doc-sync.md](docs/agent-doc-sync.md) defines the documentation synchronization contract.
- [AGENTS.md](AGENTS.md) provides the operating contract for agent-driven changes.

## One-command uptake path

```
./bootstrap.ps1        # Windows
./bootstrap.sh         # everywhere else
```

The entrypoint prepares `infra/env/.env` from the template, creates the runtime
directories, starts Postgres and the pipeline runtime, applies migrations, and verifies the
health surface. **There is deliberately no `Makefile`** — `make` is not installed on the
target host, and a documented command that does not run is worse than no command at all.

To work on the pipeline directly instead of in the container:

```
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/python -m pytest              # SQLite; set TEST_DATABASE_URL to add Postgres
.venv/Scripts/python -m app.cli run --profile dummy
```

Note that the container pins Python 3.12: `pdfplumber` and `psycopg` wheel availability on
3.14 is still patchy, and the parser is the part of this system least worth debugging
against a moving toolchain.
