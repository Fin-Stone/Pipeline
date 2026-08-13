# Roadmap

What is built, what is next, and what is deliberately waiting. The ordering is
the operator's.

This is the tracking document. [finance-pipeline-architecture.md](../finance-pipeline-architecture.md)
is still the design source of truth and says *how* each of these should work;
this says **when**, and nothing else. A section here that disagrees with the
architecture is wrong.

---

## Where the line is

**Alpha 1 is next, and everything below it is scheduled rather than pending.**
Phase 1 — ingestion, storage, reconciliation — is done and in daily use, along
with category rules, transfer matching, paybacks, recurrence detection and the
dashboard.

Items are listed with the thing that makes each hard, because that is what
decides the order they can actually be done in.

---

## Alpha 1

**Alpha testing of what is built now.** No new capability is scheduled into it:
the point is to run the current system against real use and find out what it
gets wrong, which is information nothing on this page can supply in advance.

One thing to hold in view for the whole of it, because it does not change until
Alpha 4: **there is no authentication.** Alpha testing happens on a trusted
network or through a tunnel, and every install document opens by saying so.

---

## Alpha 2 — the corrections a person needs to make

Everything here is an operator being unable to say something true. The ledger
is right; the ways to talk to it are missing.

| Item | Why it is not trivial |
|---|---|
| **Rename and merge a category** | `POST` adds and `DELETE` removes; neither exists. Rules already reference a category by *id*, so a rename cannot orphan them — but a merge has to rewrite every enrichment naming both, and that rewrite is the work. |
| **Category weight or budget** | Categories carry `position` and nothing else, so nothing can say a month was over or under. Needs a figure per category per period, and a decision about what a budget means on a category with no spending in the window. |
| **Review what is already categorised** | `/review` lists only unmatched counterparties, so a wrong rule among the existing set is invisible until somebody happens to see the row. Needs a view keyed on rules rather than on what is left over. |
| **Retrospective enrolment from a selection** (§3.2a interaction 2) | Marking keys off one row's merchant and amount. The operator wants to pick three scattered rows and have the rule derived from them — and shown back before it is saved, because a rule inferred from three rows will claim future ones. |
| **The series state machine** (§3.2a) | `detected` / `declared` / `watching` / `confirmed`. Only dismiss and mark exist today, and nothing alerts. `recurrence_series` is kept empty for exactly this — see `app/storage/schema.py`. The governing rule: a declaration is an input to detection, never an output of it. |
| **Explain a break in the pattern** (§3.2a interaction 3) | A payment that is smaller, larger or absent is often a promotion or a payment holiday. The explanation has to be *verified* against what actually happened, or it teaches the operator to trust a signal that stopped being checked. |

---

## Alpha 3 — the enrichment tiers

The two stages of §3.1 that do not exist yet. Both make categorisation better
without changing what the operator has to do.

| Item | Why it is not trivial |
|---|---|
| **k-NN on pgvector** (stage 2, build order 14) | The image is already pgvector so nothing has to change to run it. The work is embedding `description_norm`, storing vectors, and using distance as confidence — and it only pays off on the labels stage 1 and the review queue have been accumulating. This is what makes the system improve as it is corrected. |
| **LLM residual tier, in the product** (stage 3) | Today it is an out-of-band ritual: `finstone propose` → paste into several models by hand → `category/*_reply.json` → `finstone rules`. Needs a configured endpoint or key, and `model_version` plus a prompt hash recorded on the row so a result is reproducible. §5.2 binds here: a self-hosted install with no model configured must lose *typing*, never capability. |

---

## Alpha 4

Three tracks that are independent of each other and of the two above.

- **Ops and observability.** The notifier still only logs — an ntfy or Gotify
  class is one factory line. Then the dead-man's switch §8.2 calls the failure
  that actually hurts, a Grafana pipeline-health surface, and Prometheus
  metrics. `GET /reconciliation` already exists for a monitor to watch.
- **Dashboard / PWA.** A manifest and service worker, so the name stops being
  aspirational; the `/tv` route and its 10-foot layout; a screen for
  reconciliation drift.
- **Tenancy and authentication.** Per-server, OIDC, and no password column
  ever — architecture §5.2. With it: per-account access grants beyond the
  owner/shared split, role enforcement at the API boundary, tenant provisioning
  and invitations. The schema has carried the shape since migration `0001`;
  what changes is how a `TenantContext` is *resolved* — from configuration to
  an authenticated session.

  **This is the gate on leaving a trusted network.** Everything before it runs
  where the network is the access control, which is why auth lands after alpha
  testing and before beta rather than at either end.

---

## Beta

Reserved.

## Beta 2

**Ingestion.** The channels that get statements in without a person:

- IMAP auto-fetch (build order 11) — the cheapest automation in the system and
  the only one needing no credential handling at all.
- Web portals: Playwright plus `bw` against Vaultwarden (§6b).
- Mobile-only banks: a physical stock Android device driven by Maestro (§6c).
  Emulators are closed — Play Integrity — and this is why.
- The self-healing agent tier (§7), which is only worth building once the two
  above are breaking regularly.

Also here, because they are ingestion and are blocked on evidence rather than
on work:

- **DBS card adapter.** The only sample has no activity in it, so its row
  format, date format and debit/credit convention are unobservable. An adapter
  would be guessing at the only part that matters.
- **CSV / OFX / QFX.** Deferred until a source exists; the extension allowlist
  already accepts one. Worth chasing for a reason beyond convenience: such a
  file may carry an MCC, and a real MCC is worth more than any amount of rule
  authoring — see §3.1a.
- **Splitting one payback across two charges.** A link consumes the whole
  inflow. The schema carries an amount so this needs no migration; the
  unallocated remainder would have to keep counting as income.

---

## Not on this roadmap

Parked with the terms they would have to meet, so that picking one up is a
decision rather than a rediscovery:

- **Cohort observations on recurring services** (§12.1). The only idea in the
  design that moves data across the tenant boundary everything else is built
  on.
- **A rewrite in Go or .NET.** The ports in `app/ports/` are what would make it
  bounded. Worth knowing first: PDF text extraction is roughly 98% of
  per-document time, so the achievable speedup is bounded by the replacement's
  PDF library and not by the language.

## Related documents

- [finance-pipeline-architecture.md](../finance-pipeline-architecture.md) — the design, and the source of truth
- [api-contracts.md](api-contracts.md) — what a client is written against, and what it does not promise yet
- [ingestion.md](ingestion.md) — Phase 1, as built
- [../README.md](../README.md) — the three development rules every item here is still bound by
