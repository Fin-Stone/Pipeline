# Compose stack

```bash
docker compose up            # from the repository root
```

- **UI** → http://localhost:8080
- **API** → http://localhost:8000 (schema at `/docs`)

On first load the UI asks for a server address. Enter `http://localhost:8000`,
or the machine's LAN address if you are opening it from a phone.

Nothing needs configuring first. The database is SQLite inside `data/`,
migrations run on every start, and no API address is baked into the UI image —
the same image works against any server, which is the point of §5.2.

## Ports

| Variable | Default |
|---|---|
| `FINSTONE_UI_PORT` | 8080 |
| `FINSTONE_API_PORT` | 8000 |

## Running the CLI

The pipeline commands run in their own container, kept apart from the API so a
long ingest cannot take the dashboard down with it.

```bash
docker compose --profile cli up -d cli
docker compose exec cli finstone status --profile prod
docker compose exec -e FINSTONE_ALLOW_PROD=1 cli finstone run --profile prod
```

`FINSTONE_ALLOW_PROD` is deliberately absent from the compose file. Processing
real statements must be a deliberate act each time — Rule 2.

## Postgres instead of SQLite

An explicit overlay rather than a profile, because Compose interpolates every
service whether or not its profile is selected: a required password in the main
file would make the plain `docker compose up` fail before it starts.

```bash
POSTGRES_PASSWORD=... docker compose \
    -f docker-compose.yml -f infra/compose/postgres.yml up
```

The image is pgvector so the later k-NN phase needs no image change.

## What is mounted, and how

| Path | Mode | Why |
|---|---|---|
| `data/` | read-write | The ledger, the content-addressed store, quarantine |
| `uploads/` | **read-only** | The pipeline copies out of it and must never write to the operator's own folder |

`.dockerignore` keeps `uploads/`, `data/` and the categorisation payloads out of
the build context entirely. Anything in a build context is baked into an image
layer and travels wherever that image goes.

## Before exposing this to a network

**There is no authentication.** Not on the API, not on the UI. CORS defaults to
permissive and that is honest rather than lax — it restricts browsers and not
the `curl` beside them, so it buys nothing until there is something to protect.
Keep the stack on a trusted network, or behind Tailscale, until auth exists.

## Unverified

The compose configuration validates (`docker compose config`) for both the
default and Postgres paths. **The images have never been built and the stack has
never been started** — no Docker daemon was available when this was written.
Expect the first `docker compose up` to surface build or runtime problems that
configuration validation cannot catch.
