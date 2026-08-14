## What changed, and why

<!-- The reasoning, not the diff. What was true before, what is true now. -->

## Review checklist

`AGENTS.md` asks these before a change is finished. They are here so they are
answered while the change is fresh rather than remembered afterwards.

- [ ] **Repo shape / runtime / data model / deployment** — unchanged, or the
      markdown in the doc-sync checklist is updated in this same change.
- [ ] **`app/api/`** — untouched, or `docs/api-contracts.md` is updated here.
      Not in a follow-up: somebody else's installation is written against it.
- [ ] **Ports** — every new external dependency is reachable only through a
      port in `app/ports/`, and any abstraction measured above 15% is flagged
      below with a real number.
- [ ] **Tenancy** — every repository call passes a `TenantContext`, and every
      ledger query filters on `tenant_id`.
- [ ] **Money** — integer minor units, currency in its own column, no floats.
- [ ] **Portability** — no dialect-specific SQL in shared code; the change
      passes on both engines.
- [ ] **Bootstrap** — this still installs and runs from `bootstrap.sh` /
      `bootstrap.ps1` alone.

## Personal data

The scanner checks shapes — card numbers, NRIC, UEN, unit numbers, phone
numbers. It cannot tell whose data a value is, and it will never catch a real
merchant, a surname, or a genuine transaction description.

- [ ] Nothing here was read from `uploads/prod/`, `data/store/`, or
      `data/quarantine/files/`.
- [ ] Every value in a fixture or example is invented, including merchant
      names and descriptions.

<!--
If the answer to the first is "yes", stop and say so plainly. Per Rule 2 that
is a data incident, not a review comment — and deleting the value in a later
commit does not remove it from the history.
-->
