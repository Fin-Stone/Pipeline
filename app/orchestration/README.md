# Orchestration

**Empty, deliberately.** Scheduling today is a systemd timer calling the CLI —
see [../../infra/systemd/](../../infra/systemd/) — and a timer plus an
idempotent command is a complete answer for a pipeline whose every operation is
safe to repeat. Adding a scheduler to run two commands would be operational
cost bought with nothing.

This becomes real with automated retrieval (architecture §6 and §7), which
needs things a timer cannot express:

- **suspend-until-approved**, because some institutions will always require a
  tap on a phone, and a pipeline that cannot pause gracefully just fails
  nightly forever;
- retry with backoff, and the transient-versus-structural distinction that
  decides whether to retry at all;
- the agent tier's propose-review-merge loop, which is a workflow with a human
  in the middle of it.

See [../../docs/roadmap.md](../../docs/roadmap.md), Beta 2.
