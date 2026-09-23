# ADR 0001 — Core language: Python 3.12+

- **Status:** accepted
- **Date:** 2026-09-17

## Context

Dear Agent needs transport adapters (email/JMAP), a durable queue, subprocess orchestration of
coding-agent harnesses, and Git/PR automation. It runs unattended on a Kubernetes/OpenShift
cluster and must be easy to build, audit and maintain. The durable queue is PostgreSQL, with
the transport mailbox as ingress (see [ADR 0005](0005-state-store.md); it supersedes
[ADR 0002](0002-deployment-topology.md) in part).

## Decision

Use **Python 3.12+** for the control plane, the queue port and the adapters. The deployment
target is **Kubernetes/OpenShift**. Secondary components (a mail forwarder, a scheduled
sweep wrapper) may use another language but must contain no business logic.

## Rationale

- Mature, well-documented libraries for IMAP/JMAP and subprocess management.
- The standard library (JSON, subprocess, dataclasses) covers the queue port and harness
  invocation without an external service dependency.
- No compile step: fast iteration, and a reader of the repository can run it.
- Coding-agent harnesses and OpenAI-compatible providers expose first-class Python SDKs or
  CLI contracts.

## Alternatives

- **Go**: single static binary and excellent process supervision; weaker email/JMAP
  ergonomics and slower iteration at this stage.
- **TypeScript/Node**: aligns with the wider agent ecosystem; adds a runtime and npm
  dependency surface for long-running workers.

## Consequences

- Ship one container image per component; pin dependencies.
- Keep transport, queue and runner behind ports/adapters so any component can be rewritten
  without touching the core.
