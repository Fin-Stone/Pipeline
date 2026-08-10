# Running this on your own box

**Just want it running? [install.md](install.md) is the step-by-step.** One
container, ten minutes, no clone.

This page is the rest: a machine you intend to keep. It is written against the
one it was built for — a small Intel N95 mini-PC running Ubuntu Server, on a
home network, reached through nginx — and adds the things a permanent install
wants and a first try does not: Postgres, a name on port 80, a systemd unit, a
nightly backup, and TLS. Nothing here is specific to that hardware; it is named
because every number below was measured on it rather than guessed.

---

## Read this first

**There is no authentication.** Not on the API, not on the UI, not anywhere.
Anything that can open `http://your-box` can read every transaction, every
balance and every statement you have imported, and can delete them.

That is a known gap, not an oversight — it is the next real piece of work. Until
it exists, the network *is* the access control:

| | |
|---|---|
| **Fine** | A home LAN you control, or a Tailscale / WireGuard tunnel |
| **Not fine** | A port forwarded from your router. A VPS. Anything with a public address |

If you want to reach it away from home, use a tunnel. Tailscale gives you real
authentication — device identity, revocable — which is a different thing from an
address nobody has guessed yet. [A stopgap](#a-stopgap-if-you-need-one-now) is
below if you need something today, along with an honest account of what it does
and does not buy.

---

## What you need

- Ubuntu 22.04 or 24.04, amd64
- Docker Engine and the Compose plugin
  ([install](https://docs.docker.com/engine/install/ubuntu/)), and your user in
  the `docker` group
- nginx, if you want the reverse proxy: `sudo apt install nginx`
- About 4 GB of disk for images, plus room for the ledger and its backups

```bash
sudo usermod -aG docker "$USER"    # then log out and back in
```

## Install

```bash
git clone <your-remote> /opt/finstone
cd /opt/finstone
./infra/scripts/install.sh --server-name finstone.lan
```

One command, which is Rule 3 of the development rules in [the README](../README.md) and
not a slogan: every manual step is a tax paid on every future install and on the
one restore that matters.

```
--server-name NAME   what nginx answers to (default: this host's name)
--engine ENGINE      postgres (default) or sqlite
--no-nginx           skip the proxy and publish the ports directly
--no-systemd         skip boot-time start and the nightly backup
```

Then open `http://finstone.lan`. There is nothing to type: the client reads the
address the page came from. **Change** in the header is still there for pointing
this client at a different server.

### What it actually did

| | |
|---|---|
| `.env` | Created if absent, `chmod 600`, with a Postgres password **generated on this machine** |
| `/etc/systemd/system/finstone.service` | Brings the stack up at boot |
| `/etc/systemd/system/finstone-backup.{service,timer}` | Nightly backup, keeping 14 |
| `/etc/nginx/sites-{available,enabled}/finstone` | The reverse proxy |
| Docker | Images built, stack started, health checked |

The password is generated once and **never regenerated**, because rotating it on
an existing install leaves the database volume holding the old one and nothing
able to connect — an upgrade that takes your ledger away. It is also never
copied from a development `.env`: a secret shared between machines is not a
secret.

Re-running the script is the upgrade path, and is safe:

```bash
cd /opt/finstone && git pull && ./infra/scripts/install.sh
```

## One container, one origin

The image carries the built client and serves it beside the API, so a browser
talks to exactly one origin and CORS never comes up. nginx is a plain reverse
proxy with nothing to route.

```
browser ──▶ nginx :80 ──▶ 127.0.0.1:8000 ──┬── /          the dashboard
                                           └── /api/v1/   the API
```

The container binds to `127.0.0.1` only (`FINSTONE_BIND` in `.env`), which makes
the proxy the only way in — and with no authentication anywhere, a second open
port is not a convenience, it is the whole ledger.

This is also why there is nothing to type on first load: the client reads the
address the page came from. It can still be pointed elsewhere, so a hosted
deployment and a separately-served client both keep working — architecture §5.2
is about nothing being *baked in at build time*, and nothing is.

## Getting statements onto the box

Drop them in `uploads/prod/` — over Samba, `scp`, Syncthing, whatever you
already use — then:

```bash
./infra/scripts/ingest.sh --profile prod
```

That wrapper exists for one reason: the real-data path needs
`FINSTONE_ALLOW_PROD=1`, and a variable you have to remember is a variable
somebody eventually puts in their shell profile, at which point the guard is
gone. The script sets it for exactly one command.

`uploads/` is mounted **read-only**. The pipeline copies originals into the
content-addressed store and never writes to your own folder.

## Backups, and the restore that makes them real

Installed and enabled by default. The reason is in
[backups.md](backups.md) and is worth repeating: the ledger's *rows* can be
rebuilt by reparsing the store, but its *decisions* cannot. Every category rule,
every hand-set category, every row marked as a transfer or hidden or paid back
exists in exactly one place.

```bash
./infra/scripts/backup.sh                    # now, keeping the newest 14
./infra/scripts/restore.sh <archive>         # the whole thing back
./infra/scripts/restore.sh <archive> --dry-run
```

A restore stops the app first. Replacing every row under a live dashboard would
show figures that were never true of any ledger, which is worse than a minute of
downtime.

The archive is logical JSONL, not `pg_dump`, so it restores onto either engine
and onto a different machine — the migration path and the disaster path are one
path, and the rare one is exercised by the ordinary one.

**A copy on the same disk survives a mistake, not a failure.** Send them
somewhere else, with a key that cannot delete:

```bash
restic -r b2:your-bucket:finstone backup data/backups
```

### Test it

Quarterly. An untested backup is a rumour.

```bash
./infra/scripts/backup.sh
docker compose --profile cli run --rm cli finstone status --profile prod
# restore into a scratch database and compare the counts — backups.md has the recipe
```

That exact comparison is how this deployment was verified: 235 documents, 14
accounts, 5,137 transactions, live and restored, identical.

## TLS

Not turned on by the installer, because it cannot be done honestly from a
script — what is right depends on the name.

- **A real domain pointed here.** `sudo certbot --nginx -d finstone.example.com`.
  Certbot rewrites the site file and adds the redirect.
- **A `.lan` / `.local` name**, which is the normal case on-prem. Let's Encrypt
  cannot issue for a name it cannot resolve. Either use DNS-01 against a domain
  you own, or run `mkcert` and trust your own CA on the handful of devices that
  will ever open this.

## A stopgap, if you need one now

HTTP Basic at nginx. Be clear about what this is:

- It **does** stop the smart TV, the housemate's laptop and the IoT doorbell
  from browsing your finances by accident or curiosity.
- It **does not** protect against anyone who can see your traffic, because
  without TLS the credentials are base64 on every request — encoding, not
  encryption.
- It is **not** the authentication this project needs. It has no accounts, no
  sessions, no revocation, and no way to tell you it was used.

```bash
sudo apt install apache2-utils
sudo htpasswd -c /etc/nginx/.finstone-htpasswd you
```

Then inside the `server` block in `/etc/nginx/sites-available/finstone`:

```nginx
auth_basic           "Finstone";
auth_basic_user_file /etc/nginx/.finstone-htpasswd;
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

One prompt covers the whole app — a benefit of the single-origin layout, since
the browser applies the credential to the API calls as well as the page. Note
that re-running `install.sh` rewrites the site file and will drop those two
lines; put them back, or keep them in a separate `include`d file.

## Day to day

```bash
sudo systemctl status finstone            # is it meant to be up
docker compose ps                         # is it up
docker compose logs -f app                # what it is doing
systemctl list-timers finstone-backup     # when the next backup runs
journalctl -u finstone-backup             # how the last one went
```

The CLI runs in its own container, deliberately apart from the API so a long
ingest cannot take the dashboard down with it:

```bash
docker compose --profile cli run --rm cli finstone status --profile prod
docker compose --profile cli run --rm cli finstone review --profile prod --decided
```

## On this hardware

An N95 has four slow cores and no spare thermal headroom, which shows up in
exactly two places.

- **The first build takes minutes**, mostly the client's npm install. The
  systemd unit sets `TimeoutStartSec=0` so systemd does not kill it halfway and
  report a failure that is really a slow box. Pulling the published image
  instead skips this entirely — see [install.md](install.md).
- **Ingest is CPU-bound** in PDF text extraction. It is a background job and it
  runs in its own container; leave it alone and read the dashboard.

Serving the dashboard is not a load. Postgres, the API and nginx together idle
in a few hundred megabytes, and the whole ledger — 246 documents, 5,332
transactions, originals included — backs up to a 25 MB archive. Fourteen nightly
copies is under 400 MB, which is why the timer takes a full backup every night
instead of something cleverer.

## When it will not come up

| Symptom | Cause |
|---|---|
| `cannot talk to the Docker daemon` | Not in the `docker` group, or you have not logged out since being added |
| `port is already allocated` | Something else has 80 or 8000. Change `FINSTONE_PORT` in `.env` |
| The container restarts in a loop | `docker compose logs app`. Almost always a migration — the schema guard refuses to run at the wrong revision, on purpose |
| nginx 502 | The stack is down, or `FINSTONE_BIND` is not `127.0.0.1:` while the proxy expects it there |
| The app asks for a server and nothing works | Enter the address you type in the browser, including `http://`. Not `localhost` — that is the phone, not the box |
| `bash: $'\r': command not found` | The repo was cloned with CRLF endings. `.gitattributes` prevents this; a very old clone predates it — re-clone |
