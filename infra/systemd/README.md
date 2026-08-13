# systemd units

Templates. `INSTALL_DIR`, `RUN_USER` and `COMPOSE` are substituted by
[../scripts/install.sh](../scripts/install.sh) on the way to
`/etc/systemd/system/`.

They are kept here as templates rather than generated from nothing so that what
systemd ends up running is reviewable in the repository, not only on the box.

| Unit | What it is for | Enabled by the installer |
|---|---|---|
| `finstone.service` | Brings the stack up at boot, takes it down on shutdown | yes |
| `finstone-backup.service` | One backup. Run it by hand to test the timer's work | — |
| `finstone-backup.timer` | Nightly, catching up if the box was off | yes |
| `finstone-ingest.service` | One pass over `uploads/`. Idempotent; safe to repeat | — |
| `finstone-ingest.timer` | Hourly, for a folder something else writes into | **no** |

## Why the ingest timer is installed and left off

A statement arrives in `uploads/` because something put it there, and until
Phase 2 fetches them on its own, that something is usually a person — who has
the Import tab in front of them and does not need a timer. Enabling it by
default would be a schedule that does nothing on most installs.

It is written and installed anyway, because the setup it *is* for is real: a
Syncthing folder, a scanner, a mail rule dropping attachments. Turn it on when
one of those is doing the putting.

```bash
sudo systemctl enable --now finstone-ingest.timer
sudo systemctl start finstone-ingest.service     # one pass, now
journalctl -u finstone-ingest.service
```

The unit runs [`../scripts/ingest.sh`](../scripts/ingest.sh), which is where
`FINSTONE_ALLOW_PROD` is set for exactly one command. `--ingest-profile` at
install time decides which profile it names; it defaults to `prod`, because an
operator enabling this means their own statements.

Nothing is lost by running it on an unchanged folder: staging skips what the
ledger already holds by digest, so a pass over nothing new is a directory walk.

## Why a service at all

Compose already restarts containers with `restart: unless-stopped`. The unit is
for the other half: a clean boot after somebody ran `docker compose down`, and
an ordering guarantee that the stack starts after Docker and the network are
genuinely up rather than merely enabled.

`TimeoutStartSec=0`, because the first start after an upgrade builds images and
on a low-power box that is minutes. systemd killing the build halfway and
marking the unit failed is a worse outcome than waiting.

## Why the backup timer is installed by default

The ledger's rows are regenerable — reparse the store and they come back. The
decisions in it are not. Every category rule, every hand-set category, every row
marked as a transfer or hidden or paid back exists in exactly one place.

That asymmetry is the whole argument, and it is why this is a default rather
than an option. See [../../docs/backups.md](../../docs/backups.md).

```bash
sudo systemctl start finstone-backup.service    # test it now
journalctl -u finstone-backup.service           # see how it went
systemctl list-timers finstone-backup           # when the next one runs
```

An untested backup is a rumour, and a timer nobody has ever fired is the same
thing with a schedule attached.
