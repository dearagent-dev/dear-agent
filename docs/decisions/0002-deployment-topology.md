# ADR 0002 — The mailbox is the queue: Fastmail JMAP as source of truth, Kubernetes for execution

- **Status:** superseded in part by [0005](0005-state-store.md). The mailbox is now **ingress**,
  not the source of truth: PostgreSQL holds tasks and state. Kubernetes execution (one Job per
  task), the sweep, the Git artifact plane and the credentials model still stand.
- **Date:** 2026-09-17
- **Superseded by:** [0005](0005-state-store.md) (durable state moves to PostgreSQL)

## Context

Dear Agent's thesis is that **email is the queue and Git is the artifact plane**. A durable
queue must provide: durability across restarts, store-and-forward, deduplication of
re-delivery, an atomic claim, lease/resume after a crash, and well-defined state
transitions.

A mailbox provides the first three natively; **JMAP** (RFC 8620/8621) provides the
primitives for the rest, which plain IMAP does not expose as cleanly. The deployment target
is Kubernetes/OpenShift, and the chosen backend is **Fastmail JMAP**. This ADR records how
the queue is built on top of it.

## Decision

1. ~~**The Fastmail JMAP mailbox is the source of truth for tasks and state. There is no
   database.** No Postgres, no SQLite, no workflow engine.~~ **Superseded by
   [0005](0005-state-store.md):** the mailbox is ingress, PostgreSQL is the durable queue.

2. **State is modelled as mailboxes + keywords.** Mailboxes (or equivalent filters)
   `Queued`, `Running`, `Action`, `Done`, `Failed`, `Rejected`, plus companion keywords
   `$dear-agent-queued`, `$dear-agent-running`, `$dear-agent-action`, `$dear-agent-done`, … The lifecycle
   mirrors [architecture.md](../architecture.md) without a table.

3. **Dedupe is the RFC 5322 `Message-ID`** (and the JMAP `Email` id). A message exists
   exactly once; reprocessing is prevented by the state guards below, so a redelivered
   transport event cannot create a second task.

4. **Atomic claim uses JMAP `Email/set` with `ifInState`** (RFC 8620 optimistic
   concurrency). Claiming means moving an email from `Queued` to `Running` in one `Email/set`
   call with the state string read from `Email/get`. On `stateMismatch`, re-read and retry.
   A bare `Email/query` followed by an unguarded `Email/set` is forbidden: it is the
   read-then-write race [queue.md](../queue.md) prohibits.

5. **Lease and resume via a sweep.** A scheduled sweep moves `Running` messages whose age
   exceeds the lease back to `Queued`. An interrupted run is resumed, never lost.

6. **Execution is one Kubernetes Job per task** (OpenShift). Jobs are ephemeral, run
   non-root under an arbitrary UID (OpenShift SCCs), keep the Git worktree in an
   `emptyDir`, and deliver an `dear-agent/<slug>` branch plus a **draft PR**. The mailbox
   carries only state and links; harness logs go to object storage, never over the
   transport.

7. **Trigger is a scheduled sweep**, not an always-on listener: a Kubernetes `CronJob`
   queries the `Queued` mailbox on an interval and starts Jobs. This needs no VPS and
   scales to zero. A JMAP `PushSubscription`/EventSource listener is a later latency
   optimization behind the same interface.

8. **Everything mutable is stateless.** The control plane and runner hold no durable
   state; all of it lives in the mailbox and in Git.

## Rationale

- The mail server already persists, retries and threads; a separate store would only
  re-implement that and add a stateful tier to operate.
- `Message-ID` gives dedupe for free, which is golden rule 4's hard requirement.
- JMAP `ifInState` gives a real compare-and-swap, satisfying the atomic-claim requirement
  that raw IMAP only approximates.
- Kubernetes Jobs have no execution-time ceiling (unlike Lambda), can spawn the harness
  and clone a repo, and cost nothing while the queue is empty.
- JMAP is a standard; a `Queue` port keeps the core from being welded to one provider.

## Alternatives

- **Postgres/CNPG.** Robust and queryable, but redundant with the mailbox and reintroduces a
  stateful tier to operate.
- **Temporal.** Best-in-class durable workflows and the closest fit to "enqueue is the hard
  problem", but a large system to run and not permitted by golden rule 8 as written.
- **Kubernetes CRDs / etcd.** Most k8s-native, but etcd is a poor queue (≈1.5 MB object
  limit, strongly consistent writes) and it requires writing and maintaining an operator.
- **Brokers (NATS JetStream, Kafka, Redis Streams).** Good for triggering, weak as the
  authoritative task record with status, approvals and evidence.
- **Plain IMAP instead of JMAP.** More portable, but claim is limited to `UID MOVE` and
  metadata is coarser; JMAP's `ifInState` is preferable when available.

## Consequences

- **Golden rule 8** (`AGENTS.md`) lists the mailbox as an allowed durable store,
  alongside Git.
- **The first slice** is `Task` + a `Queue` port over JMAP + claim/sweep semantics.
- **Metadata is coarse.** JMAP has no mutable structured fields, so per-task artifact
  values (branch, SHA, PR url) are **not** stored on the email; they are threaded replies
  with links. Attempts are tracked with numbered keywords (`$dear-agent-attempt-2`, …).
- **The mailbox is an external dependency.** Provider retention, quota, rate limits and
  account availability bound the queue. Terminal messages must be retained long enough to
  serve as tombstones so a redelivery cannot resurrect a task.
- **`ifInState` for `Email` is account-scoped.** Heavy churn causes `stateMismatch`
  retries; acceptable at Dear Agent's expected volume, revisit if it is not.
- **No SQL reporting.** Observability is derived from mailbox state and external metrics,
  not from queries.
- **Verified against Fastmail** (`scripts/verify_fastmail_jmap.py`): custom `$dear-agent-*`
  keywords, `Email/query` by `Message-ID`, and `Email/set` `ifInState` (a stale state
  yields `stateMismatch`) all behave as required.
