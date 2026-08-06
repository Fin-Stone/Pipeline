# Install

One container. It carries the dashboard and the API on a single port, so there
is no second service to run and nothing to configure to make the two halves talk
to each other.

```bash
docker run -d \
  --name finstone \
  --restart unless-stopped \
  -p 8000:8000 \
  -v finstone-data:/srv/finstone/data \
  -v "$PWD/statements":/srv/finstone/uploads:ro \
  ghcr.io/fin-stone/finstone:latest
```

Then open `http://<your-box>:8000`.

> **The image publishes from CI on the first push to `main`.** Until that has
> run once, `docker pull` will 404 and you want
> [build it yourself](#appendix-build-it-yourself), which is the same thing a
> few minutes slower and is what everything below was tested against.

---

## Before you start: there is no authentication

Not on the API, not on the dashboard, not anywhere. **Anything that can reach
this container can read every transaction, balance and statement you import, and
can delete them.**

That is a known gap and the next real piece of work. Until it exists, the network
is the access control:

| | |
|---|---|
| **Fine** | A home LAN you control. Tailscale or WireGuard from outside it |
| **Not fine** | A forwarded port. A VPS. Anything with a public address |

Do not skip this and come back to it. [deploy.md](deploy.md#a-stopgap-if-you-need-one-now)
has a stopgap and an honest account of what it does and does not buy.

---

## Step 1 — Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
```

Log out and back in, then check it took:

```bash
docker run --rm hello-world
```

If that needs `sudo`, the group change has not applied to your shell yet.

## Step 2 — A folder to live in

```bash
mkdir -p ~/finstone/statements && cd ~/finstone
```

`statements/` is where you will drop PDFs. It is mounted **read-only**: the
pipeline copies originals into its own content-addressed store and never writes
to your folder.

## Step 3 — Get the compose file

`docker run` from the top works, but compose is easier to live with — upgrades,
restarts and the CLI are all one command shorter.

```bash
curl -fsSLO https://raw.githubusercontent.com/Fin-Stone/Pipeline/main/infra/deploy/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/Fin-Stone/Pipeline/main/infra/deploy/.env.example -o .env
```

Open `.env`. Nothing in it is required; the defaults are a working install. The
two worth a look now:

```ini
FINSTONE_PORT=8000
FINSTONE_UPLOADS=./statements
```

## Step 4 — Start it

```bash
docker compose up -d
docker compose logs -f app
```

You are waiting for `Application startup complete`. The first start also runs
the database migrations — that happens on every start, deliberately, so an
upgraded image comes up instead of failing with a message nobody is watching
for.

Check it:

```bash
curl -s localhost:8000/api/v1/health
# {"status":"ok","api_version":"v1"}
```

## Step 5 — Open it

`http://<your-box>:8000` — from the box itself, a laptop, or your phone.

There is **nothing to configure on first load**. The app asks which server to
talk to only when it cannot work it out; served from the same container as the
API, it uses the address you typed into the browser. The setup screen is still
there behind **Change**, for pointing this client at a different server.

The dashboard will be empty. That is next.

## Step 6 — Put statements in

Copy PDFs into `~/finstone/statements/` — over `scp`, Samba, Syncthing, whatever
you already use. Subfolders are fine; the pipeline walks the tree.

```bash
cp ~/Downloads/*.pdf ~/finstone/statements/
```

## Step 7 — Ingest them

```bash
docker compose run --rm -e FINSTONE_ALLOW_PROD=1 app \
    finstone run --profile prod
```

`FINSTONE_ALLOW_PROD=1` is required and is deliberately not in any config file.
Processing real financial data is an explicit act each time, not a setting
somebody turns on once and forgets.

Then see where it went:

```bash
docker compose run --rm app finstone status --profile prod
```

```
documents            235
accounts             14
transactions         5137
quarantined          0
```

**Quarantined is not a failure.** A statement layout with no adapter yet is set
aside with a readable reason and the run continues, rather than guessing at
numbers. Ask why:

```bash
docker compose run --rm app finstone report --profile prod --redact
```

Trust Bank savings and credit-card statements parse and reconcile end to end
today. DBS, MariBank and OCBC quarantine as unknown layouts until their adapters
are written — see [ingestion.md](ingestion.md).

## Step 8 — Tell it what things are

Refresh the dashboard and there will be numbers. Most rows will be `Others`,
because nothing has been told what your merchants are yet.

The **Review** tab ranks what is left by what deciding it is worth — the largest
sixty unplaced names carried 88% of the unaccounted money on the corpus this was
built against, so working down that list is short. Every decision is one click
and every one of them can be taken back, from the same screen.

Then write them through the ledger:

```bash
docker compose run --rm app finstone categorise --profile prod --apply
```

Decisions are recorded when you make them and applied in one pass, so working
through a queue costs one write rather than one per click.

## Step 9 — Back it up

Do this before you have data you would miss, not after.

```bash
docker compose run --rm app finstone backup
```

The reason is not the transactions — those can be rebuilt by reparsing the
originals. It is **the decisions**: every category rule, every hand-set category,
every row you marked as a transfer, hid, or settled against a payback exists in
exactly one place.

The archive lands in the data volume. To keep copies on the host, add a mount:

```yaml
    volumes:
      - finstone-data:/srv/finstone/data
      - ./backups:/srv/finstone/data/backups
```

Restoring is one command, onto any engine, on any machine:

```bash
docker compose run --rm app finstone restore /srv/finstone/data/backups/<file>.tar.gz
```

That is the same command whether you are recovering from a dead disk or moving
from SQLite to Postgres — the rare path is walked every time the ordinary one
is. See [backups.md](backups.md).

---

## Upgrading

```bash
docker compose pull && docker compose up -d
```

Migrations run on start. Your data is in a named volume and is not touched by
replacing the container.

Pin a version in `.env` once you have data you care about, so an upgrade is
something you chose rather than something that happened:

```ini
FINSTONE_TAG=v0.2.0
```

## A name instead of a port

Optional, and worth it if you will use this often.

```bash
sudo apt install nginx
```

Put the container on loopback so the proxy is the only way in — `.env`:

```ini
FINSTONE_BIND=127.0.0.1:
```

```bash
docker compose up -d
```

Then take [infra/nginx/finstone.conf](../infra/nginx/finstone.conf), substitute
the name and port, and enable it:

```bash
sed -e 's|SERVER_NAME|finstone.lan|' -e 's|APP_PORT|8000|' finstone.conf \
    | sudo tee /etc/nginx/sites-available/finstone
sudo ln -sfn /etc/nginx/sites-available/finstone /etc/nginx/sites-enabled/finstone
sudo nginx -t && sudo systemctl reload nginx
```

TLS, boot-time start and a nightly backup timer are in
[deploy.md](deploy.md). If you are installing from a clone, one script does all
of it.

## Postgres instead of SQLite

SQLite is genuinely fine for a household ledger and needs no password to look
after. Postgres is worth it above a few thousand transactions, or if you want
the dashboard snappy on a slow box.

Uncomment the `db` service in `docker-compose.yml`, set `POSTGRES_PASSWORD` in
`.env`, and set `DATABASE_URL` on the app. Then move your data across — which is
a backup and a restore, the same two commands as a disaster recovery:

```bash
docker compose run --rm app finstone backup --out /srv/finstone/data/to-postgres.tar.gz
docker compose up -d                       # now on Postgres, empty
docker compose run --rm app finstone restore /srv/finstone/data/to-postgres.tar.gz
```

Check a number you recognise before trusting it.

---

## When it will not start

| What you see | What it is |
|---|---|
| `permission denied … docker.sock` | Not in the `docker` group, or you have not logged out since being added |
| `port is already allocated` | Something else has 8000. Change `FINSTONE_PORT` in `.env` |
| `manifest unknown` on pull | The image has not been published yet — [build it yourself](#appendix-build-it-yourself) |
| The container restarts in a loop | `docker compose logs app`. Almost always a migration; the schema guard refuses to run at the wrong revision on purpose |
| The page loads but says it cannot reach the server | You are on a different address from the one the container answers on. Use **Change** and enter the address in your browser's bar, including `http://` |
| Everything is `Others` | Nothing has been categorised yet — Step 8 |
| Everything quarantined | No adapter for those layouts yet. `finstone report` says which |

Logs, always:

```bash
docker compose logs -f app
```

---

## Appendix: build it yourself

Also the path to take if you want to change anything.

```bash
git clone https://github.com/Fin-Stone/Pipeline.git finstone
cd finstone
docker compose up -d --build
```

That builds the same single image from source — the client and the server, one
container — and mounts `./data` and `./uploads` from the checkout instead of
using named volumes.

On a box you intend to keep, use the installer instead. It adds Postgres with a
password generated on that machine, nginx, a systemd unit for boot, and a
nightly backup:

```bash
./infra/scripts/install.sh --server-name finstone.lan
```

[deploy.md](deploy.md) covers what it does and how to undo each part.
