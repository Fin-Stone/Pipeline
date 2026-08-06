# systemd units

Templates. `INSTALL_DIR`, `RUN_USER` and `COMPOSE` are substituted by
[../scripts/install.sh](../scripts/install.sh) on the way to
`/etc/systemd/system/`.

They are kept here as templates rather than generated from nothing so that what
systemd ends up running is reviewable in the repository, not only on the box.

| Unit | What it is for |
|---|---|
| `finstone.service` | Brings the stack up at boot, takes it down on shutdown |
| `finstone-backup.service` | One backup. Run it by hand to test the timer's work |
| `finstone-backup.timer` | Nightly, catching up if the box was off |

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
