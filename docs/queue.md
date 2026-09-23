# Queue and task model

Herald's durable queue is **PostgreSQL**. The transport mailbox is **ingress**: email/JMAP/
webhook delivers a task request, and everything mutable — state, attempts, lease, approvals,
the decision log — lives in the database. Git remains the only artifact plane. See
[ADR 0005](decisions/0005-state-store.md), which supersedes the mailbox-as-queue design of
[ADR 0002](decisions/0002-deployment-topology.md) in part.

> **Status:** implemented. `PostgresQueue` (`src/herald/queue/postgres.py`) is the queue
> backend, selected with `HERALD_QUEUE=postgres` + `HERALD_DATABASE_URL`; the JMAP queue
> adapter is retired. The mailbox is ingress only.

## Task

A task is a parsed inbound message, persisted as a row. Its authoritative copy is the row;
the email is the immutable request that produced it. Message-body content is parsed into a
`TaskSpec` by the Normalizer at ingest.

```
Task  (row in the queue)
  id             # internal, stable id (UUID) — the task identity everywhere
  transport_id   # Message-ID / JMAP emailId / webhook event id — UNIQUE (the dedupe key)
  thread_id      # conversation to reply into (transport-specific)
  sender         # from the message header; advisory (authorization is separate)
  subject        # from the message header
  state          # see lifecycle (enum column)
  attempts       # integer, incremented by the sweep
  lease_until    # timestamptz, set on claim; NULL when not running
  created_at     # timestamptz
  updated_at     # timestamptz

TaskSpec  (content — parsed from the message body by the Normalizer)
  repo_url       # required to run
  base_branch    # default: main
  instructions   # free text from the message (untrusted)
  model_request  # optional provider/model hint
```

Branch, commit SHA and PR url are not stored on the task row either: they are delivered as
threaded replies and, when useful, recorded as evidence links.

## States

State is a column, not a mailbox:

| State | Meaning |
|---|---|
| received | inbound message accepted, not yet enqueued |
| queued | waiting for a worker |
| running | claimed, with a lease |
| action | needs a human decision |
| done | finished; a draft PR exists |
| failed | terminal failure |
| rejected | refused (no repo / policy) |
| approved / rejected | human decision on an `action` task |

The lifecycle is unchanged:

```
received ─▶ queued ─▶ running ─▶ action ─▶ done
              │           │          │
              │           ├─▶ failed └─▶ approved / rejected
              └─▶ rejected
```

- **running** carries a lease. If the lease expires (crash), the task returns to `queued`
  with an incremented attempt count (resumable).
- **action** needs a human decision.
- **done / failed / rejected** are terminal.

## Idempotency

- The dedupe key is the transport message id, enforced by a **unique index**. A redelivered
  transport event hits the constraint and is a no-op, never a second task.
- Reprocessing is additionally prevented by state guards: a task already `running` or in a
  terminal state is never claimed again.
- Outbound notifications are threaded; a reply is matched by thread + single-use token.

## Atomic claim

Claiming a specific task is one guarded `UPDATE` (compare-and-swap): a lost race is a no-op,
never a double claim, and there is no read-then-write window.

```sql
UPDATE herald_task
   SET state = 'running', lease_until = now() + $lease, updated_at = now()
 WHERE id = $id AND state = 'queued'
RETURNING *;
```

A caller that wants "the next task" lists `queued` oldest-first (`ORDER BY created_at, id`)
and claims each; a lost claim just moves on to the next. (A future `claim_next` could use
`SELECT ... FOR UPDATE SKIP LOCKED` to let many workers pull in parallel without listing.)

## Lease and resume

- A `running` task carries `lease_until`. A scheduled sweep moves rows whose lease expired
  back to `queued` and increments `attempts`, so a crashed run resumes instead of being lost.
- `attempts` is an integer column; no keyword gymnastics.

## Approvals

- A request for approval sends a threaded message with a short-lived, **single-use** token
  and the task id. Tokens are generated with a CSPRNG, compared in constant time where they
  travel over the wire, and stored in the database (a table with `used_at`/`expires_at`), so
  a token survives restarts and is shared across runner Jobs.
- A reply matching the thread and token (`approve <token>` / `reject <token>`) moves the
  task to `approved` or `rejected`.
- A used, unknown or expired token is rejected explicitly; an expired token triggers a new
  request, not a silent failure. The approval path never touches `main`: it only records a
  decision.

## Concurrency

- Default: **one running task at a time**. A Kubernetes `CronJob` sweep claims the oldest
  `queued` task and starts one Job.
- Concurrency is bounded by how many Jobs the sweep starts; the guarded claim makes raising
  it a configuration change, not a redesign.

## Storage and retention

- The durable store is PostgreSQL: provider retention, quota and rate limits no longer bound
  the queue.
- Terminal tasks are retained long enough to act as tombstones so a redelivery cannot
  resurrect a task; retention is ours to choose.
- Large harness logs are not stored in the row; they go to object storage, and the row
  carries only links.

## Deployment

- **In-cluster:** a PostgreSQL 18 `StatefulSet` + `Service` + `PVC`
  (`registry.redhat.io/rhel9/postgresql-18`), deployed as a kustomize component
  (`deploy/components/postgresql`).
- **Local:** an equivalent podman container (`scripts/dev-postgres.sh`).
- The application selects the backend with `HERALD_QUEUE=memory|postgres` (default `memory`)
  and connects with a single `HERALD_DATABASE_URL`; Postgres is never exposed outside the
  cluster or host.
