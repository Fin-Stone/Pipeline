# Finstone Finance Pipeline

This repository is the main delivery repo for the self-hosted finance pipeline.

## Goal

Allow a user to clone the repo and bring the app up through a single root-level command.

## Primary design principle

Keep the system as one coherent runtime with one shared data model and one bootstrap path.

## Status

**Phase 1 — data ingestion and storage — is designed but not yet implemented.** The design
of record is [docs/ingestion.md](docs/ingestion.md); the implementation lands as a separate
change set against it. The repository currently contains documentation and layout only.

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

Two rules bind every contributor, human or agent. Both are stated in full in
[docs/development-rules.md](docs/development-rules.md) and summarised in
[AGENTS.md](AGENTS.md):

1. **Decouple by default, up to a 20% performance ceiling.** Implementation choices sit
   behind a seam unless the abstraction costs 20% or more. Anything measured above 15% must
   be flagged with a real number.
2. **`uploads/prod/` is off-limits to agents.** Real financial data. Use `uploads/dummy/`.

## Documentation

- [finance-pipeline-architecture.md](finance-pipeline-architecture.md) is the design and architecture source of truth.
- [docs/development-rules.md](docs/development-rules.md) states the two binding development rules.
- [docs/ingestion.md](docs/ingestion.md) is the Phase 1 ingestion and storage design.
- [docs/repo-structure.md](docs/repo-structure.md) explains the repository layout and startup strategy.
- [docs/agent-doc-sync.md](docs/agent-doc-sync.md) defines the documentation synchronization contract.
- [AGENTS.md](AGENTS.md) provides the operating contract for agent-driven changes.

## One-command uptake path

*Planned — ships with the Phase 1 implementation.*

The repo will expose a single root entrypoint that:

1. prepares environment and secrets templates
2. starts the services
3. applies migrations
4. verifies the health surface

That entrypoint will be `bootstrap.ps1` on Windows or `bootstrap.sh` elsewhere, both thin
wrappers over `docker compose`. **There is deliberately no `Makefile`** — `make` is not
installed on the target host, and a documented command that does not run is worse than no
command at all.
