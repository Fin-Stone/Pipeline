# Agent Documentation Sync Contract

## Purpose

This document defines how repository agents should keep architecture, implementation, and documentation aligned.

## Core rule

If an agent changes the runtime model, repo shape, data contract, delivery flow, security posture, or operational expectations, it must update the markdown documentation in the same change set.

## Required synchronization targets

At minimum, review and update the following whenever there is a meaningful design or delivery change:

- [../finance-pipeline-architecture.md](../finance-pipeline-architecture.md)
- [../README.md](../README.md)
- [../AGENTS.md](../AGENTS.md)
- [repo-structure.md](repo-structure.md)
- [development-rules.md](development-rules.md)
- [ingestion.md](ingestion.md)

## Trigger examples

Update the docs if the change affects:

- how the app boots
- how services are arranged
- how storage or schema is modeled
- how automation or approvals work
- credential handling or security assumptions
- monitoring and backup expectations
- any user-visible operating flow
- which components sit behind a decoupling seam, or the measured cost of one
- how ingestion routes, validates, or quarantines documents

## Agent completion checklist

Before declaring a task complete, an agent must confirm all of the following:

1. The implementation change is documented.
2. The repo topology is still consistent with the docs.
3. The startup model still matches the documented one-command workflow.
4. All impacted markdown files were updated in the same task.
5. The two rules in [development-rules.md](development-rules.md) were followed: new
   dependencies sit behind a port, any abstraction measured above 15% is flagged with a
   number, and nothing read from `uploads/prod/`.

## Failure mode to avoid

The most common failure mode is a code change that updates behavior without updating the design document. That creates drift and makes the next agent or operator infer the wrong runtime model.

To prevent that drift, the docs must be treated as part of the implementation surface, not as afterthought notes.
