# 0005 — Postgres is the durable queue; the mailbox is ingress

- **Status:** accepted
- **Date:** 2026-09-23
- **Supersedes:** [0002](0002-deployment-topology.md) in part (the mailbox is no longer the
  source of truth; Kubernetes execution, the sweep and the Git artifact plane stand)
- **Related:** [0002](0002-deployment-topology.md), [0003](0003-credentials.md),
  [0004](0004-decision-model.md), [../queue.md](../queue.md), [../architecture.md](../architecture.md)

## Context

M0–M7 proved that a mailbox can carry a queue: `JmapQueue` does durable enqueue, dedupe on
`Message-ID`, an atomic `ifInState` claim and a lease sweep, verified live against Fastmail
([0002](0002-deployment-topology.md), M1). The thesis — *email is the queue, Git is the
artifact plane* — is real, not aspirational.

But the state that cannot fit an immutable email kept leaking out of the mailbox:

- **Approvals** (tokens with a TTL, single-use) already live in `FileApprovalStore` /
  `MemoryApprovalStore`; the code admits "a token cannot live on the immutable email".
- **Decision records and their labels** live in a JSONL file (`DecisionLog`), and labeling
  rewrites the whole file.
- **Attempts and lease** are encoded as keywords (`$dear-agent-attempt-N`, `$dear-agent-lease-<epoch>`)
  with read-modify-write and no atomic increment.
- `Task.id` (the server-assigned JMAP email id) is distinct from `transport_id`
  (`Message-ID`) only because the mailbox is the store.

And the topology makes file-based side state unworkable: execution is **one Job per task with
no always-on daemon** (ADR 0002). There is no shared process, so a file on one pod's disk is
neither durable nor visible to another. Any state store must be reachable over the network
and concurrency-safe.

In short: the mailbox is an excellent **ingress** and **audit** medium, and a poor **state**
medium. Dear Agent has outgrown it as the source of truth.

## Decision

**The durable queue and the source of truth for task state is PostgreSQL. The mailbox is an
ingress transport only.**

1. **Email/JMAP/webhook delivers a task request.** On receipt Dear Agent parses it and inserts a
   task row. Dedupe is the transport message id (`Message-ID`) enforced by a **unique index**;
   a redelivered message is a conflict, never a second task (golden rule 4).
2. **All lifecycle state lives in Postgres:** states, attempts, lease deadline, timestamps,
   approvals (tokens, single-use, TTL) and the decision log with its labels. Threaded email
   replies are **notifications**, not state.
3. **Atomic claim is a database operation**, not a JMAP call: a conditional
   `UPDATE ... WHERE state = 'queued' RETURNING` (or `SELECT ... FOR UPDATE SKIP LOCKED`),
   which gives real per-row concurrency instead of the account-scoped `ifInState` state.
4. **Git remains the only artifact plane**, and harness logs still go to object storage. The
   outbound email carries state, summary and links only (golden rule 1).
5. **The `Queue` port stays.** `JmapQueue` is superseded by a `PostgresQueue`; the transport
   and the queue become separate concerns, so a new transport (IMAP, webhook, IRC) no longer
   implies a new queue backend.
6. **Deployment.** PostgreSQL 18 on UBI 9 (`registry.redhat.io/rhel9/postgresql-18`) as a
   `StatefulSet` + `Service` + `PVC` in-cluster (a kustomize component), or an equivalent
   podman container locally. The application sees a single `DEAR_AGENT_DATABASE_URL`; the same
   schema and migrations run in both. Postgres is never exposed outside the cluster or host.

## Rationale

- A relational store is the right tool for mutable, queryable, concurrency-safe state:
  transactions, unique constraints for dedupe, `SKIP LOCKED` for claim, indexes for
  reporting. The mailbox offers none of these directly.
- One Job per task means no shared process; network-reachable state is mandatory. Postgres is
  already a first-class citizen of the deployment target (OpenShift), unlike an ad-hoc file.
- Keeping the mailbox as ingress preserves the product's differentiator — a human can still
  email work in and read status back — while removing the parts of the design that were
  fighting the medium.
- The `Queue` port already isolates the core from the backend, so this is an adapter swap plus
  a schema, not a rewrite of the domain.

## Consequences

- **Golden rule 8 is revised** (`AGENTS.md`): the durable queue is the database; the mailbox
  is ingress and audit. State never lives only in a model's context or a process's memory.
- **New operational dependency:** a stateful tier with backups, restore and (ideally) PITR.
  This is the cost the "no database" stance was avoiding; it is now paid deliberately.
- **Retention is ours again:** tombstones no longer depend on the email provider's retention,
  so a redelivery cannot resurrect a task even if mail is purged.
- **Retired:** JMAP `ifInState` claiming and the keyword encoding (`$dear-agent-attempt-N`,
  `$dear-agent-lease-<epoch>`). `Task` gains a single stable internal id.
- **Follow-ups:** a one-time backfill of in-flight tasks from the mailbox, a migration tool,
  and runner-Job connectivity/credentials to the database.

## Alternatives

- **Option A — mailbox authoritative, Postgres as a derived projection.** Rejected: it keeps
  two sources of truth and a reconciliation path, and the mutable state (approvals, labels,
  leases) has to live in Postgres anyway, so the projection buys little over making Postgres
  authoritative.
- **Keep the mailbox as the only store.** Rejected: the leaks above, plus the one-Job-per-task
  topology that makes file-based side state unusable.
- **Temporal / a broker (Kafka, NATS, Redis Streams).** Rejected as before (ADR 0002): heavier
  to operate than needed, and a single Postgres covers both queue and queryable state.

## Open questions

- Backfill of in-flight tasks from the mailbox: one-shot script vs dual-write cutover.
- Migration tooling: plain SQL under version control, or a library (e.g. Alembic).
- Backup/restore expectations and whether PITR is required for the deployment.
- Connection management for runner Jobs (pooling, per-role credentials, network policy).
