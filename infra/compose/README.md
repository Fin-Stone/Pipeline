# Compose stack, from source

**Installing rather than developing? [../../docs/install.md](../../docs/install.md)**
— one container from a published image, no clone. This page is the build-from-
source stack, which is what you want if you are changing anything.

```bash
docker compose up -d --build     # from the repository root
```

→ http://localhost:8000 — the dashboard, with the API at `/api/v1` and the
interactive schema at `/docs`.

Nothing needs configuring first. The database is SQLite inside `data/`, and
migrations run on every start.

## One image, one container

The app image carries the built client and serves it beside the API. That is
what makes `docker run` enough, and it removes CORS from the default path
entirely: the browser talks to one origin because there only is one.

It also means there is nothing to type on first load. The client reads the
address the page came from — at runtime, from `window.location`, not baked in at
build time — so §5.2 still holds and **Change** still points it anywhere.

| Variable | Default | |
|---|---|---|
| `FINSTONE_PORT` | 8000 | |
| `FINSTONE_BIND` | *(empty)* | `127.0.0.1:` puts it behind a proxy only |
| `FINSTONE_IMAGE` | `ghcr.io/fin-stone/finstone:latest` | What `up` without `--build` pulls |

## Running the CLI

The pipeline commands run in their own container, kept apart from the app so a
long ingest cannot take the dashboard down with it.

```bash
docker compose --profile cli run --rm cli finstone status --profile prod
docker compose --profile cli run --rm -e FINSTONE_ALLOW_PROD=1 cli finstone run --profile prod
```

`FINSTONE_ALLOW_PROD` is deliberately absent from the compose file. Processing
real statements must be a deliberate act each time — Rule 2. `infra/scripts/ingest.sh`
wraps this so the variable is set for exactly one command.

## Postgres instead of SQLite

An explicit overlay rather than a profile, because Compose interpolates every
service whether or not its profile is selected: a required password in the main
file would make the plain `docker compose up` fail before it starts.

```bash
cp .env.example .env      # set POSTGRES_PASSWORD
docker compose -f docker-compose.yml -f infra/compose/postgres.yml up -d
```

The image is pgvector so the later k-NN phase needs no image change.

Postgres publishes on **127.0.0.1 only**. Nothing off the machine needs it, and
the reason it is published at all is so the dual-engine test run can reach the
real engine from the host:

```bash
docker compose -f docker-compose.yml -f infra/compose/postgres.yml \
    exec db psql -U finstone -d postgres -c "CREATE DATABASE finstone_test;"
TEST_DATABASE_URL='postgresql+psycopg://finstone:...@127.0.0.1:5432/finstone_test' pytest
```

That run is what keeps engine-specific SQL out of shared code. It is not
optional decoration — it is how `GROUP_CONCAT` in a migration, and money totals
arriving as strings, were both found.

## Moving existing data onto Postgres

Same two commands as a disaster restore, which is the point:

```bash
finstone backup --out data/backups/to-postgres.tar.gz
DATABASE_URL='postgresql+psycopg://finstone:...@127.0.0.1:5432/finstone' \
    finstone restore data/backups/to-postgres.tar.gz
```

See [../../docs/backups.md](../../docs/backups.md).

## What is mounted, and how

| Path | Mode | Why |
|---|---|---|
| `data/` | read-write | The ledger, the content-addressed store, quarantine |
| `uploads/` | **read-only** | The pipeline copies out of it and must never write to the operator's own folder |

`.dockerignore` keeps `uploads/`, `data/` and the categorisation payloads out of
the build context entirely. Anything in a build context is baked into an image
layer and travels wherever that image goes.

## Which interface the port lands on

`FINSTONE_BIND` is empty by default, so 8000 is published on every interface —
what a laptop wants, because the phone on the sofa has to be able to reach it.

A server install sets it to `127.0.0.1:` so nothing but the reverse proxy can
reach the container. `infra/scripts/install.sh` does it for you.

## Before exposing this to a network

**There is no authentication.** Not on the API, not on the dashboard. CORS
defaults to permissive and that is honest rather than lax — it restricts browsers
and not the `curl` beside them, so it buys nothing until there is something to
protect. Keep the stack on a trusted network, or behind Tailscale, until auth
exists.

[../../docs/deploy.md](../../docs/deploy.md) says what a stopgap looks like and,
more importantly, what it does not buy.

## Verified

Both paths have been built and run: SQLite by default, and Postgres via the
overlay with the full production ledger restored into it and every dashboard
figure compared against the SQLite original. They match.

The first real Postgres start is also what surfaced two bugs that configuration
validation could never catch — a SQLite-only function in a migration, and money
totals arriving as strings — so treat "it validates" and "it runs" as genuinely
different claims.
