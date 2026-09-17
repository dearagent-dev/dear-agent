# Queue and task model

Herald's durable queue is the **transport mailbox itself**, not a database. This is the
project's core thesis: email is the queue, Git is the artifact plane. The first backend is
Fastmail JMAP (RFC 8620/8621); see [ADR 0002](decisions/0002-deployment-topology.md).

## Task

A task is a parsed inbound message. Its authoritative copy is the email; there is no row in
a database. The queue record (`Task`) holds only what the mailbox can persist; message-body
content is a `TaskSpec` produced by the Normalizer.

```
Task  (queue record — persisted in the mailbox)
  id             # JMAP Email id (stable, assigned by the server)
  transport_id   # Message-ID (RFC 5322) — the dedupe key
  thread_id      # JMAP threadId, the conversation to reply into
  sender         # from the Email header; advisory (authorization is separate)
  subject        # from the Email header
  state          # see lifecycle (mailboxes/keywords)
  attempts       # $herald-attempt-N keywords
  lease_until    # encoded in a $herald-lease-<epoch> keyword
  created_at     # Email.receivedAt

TaskSpec  (content — parsed from the message body by the Normalizer)
  repo_url       # required to run
  base_branch    # default: main
  instructions   # free text from the message (untrusted)
  model_request  # optional provider/model hint
```

`id` and `transport_id` come from the mail server; Herald never invents a task id. Branch,
commit SHA and PR url are not stored on the task either: they are delivered as threaded
replies.

## States

State lives in mailboxes and keywords, not a column:

| State | Mailbox / keyword |
|---|---|
| received | inbound message, no Herald keyword yet |
| queued | `Queued` mailbox, `$herald-queued` |
| running | `Running` mailbox, `$herald-running` |
| action | `Action` mailbox, `$herald-action` |
| done | `Done` mailbox, `$herald-done` |
| failed | `Failed` mailbox, `$herald-failed` |
| rejected | `Rejected` mailbox, `$herald-rejected` |
| approved / rejected | threaded reply carrying a valid single-use token |

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

- The dedupe key is the `Message-ID`. The mail server stores a message once, so a
  redelivered transport event cannot create a second task.
- Reprocessing is additionally prevented by state guards: a message already in `Running`
  or a terminal mailbox is never claimed again.
- Outbound notifications are threaded; a reply is matched by thread + single-use token.

## Atomic claim

Claiming is a single guarded JMAP call:

1. `Email/get` to obtain the current `state`.
2. `Email/set` with `ifInState` to move the message from `Queued` to `Running`.

If the state changed concurrently, the server returns `stateMismatch`; Herald re-reads and
retries. A bare `Email/query` followed by an unguarded `Email/set` is forbidden.

## Lease and resume

- A `Running` message carries a lease; a scheduled sweep moves messages whose lease expired
  back to `Queued`, so a crashed run resumes instead of being lost.
- **Attempts** are tracked with numbered keywords (`$herald-attempt-2`, …), since JMAP has
  no mutable numeric field. The sweep adds the next keyword when it requeues a stale task.

## Approvals

- A request for approval sends a threaded message with a short-lived, single-use token and
  the task's `Message-ID`.
- A reply matching the thread and token moves the task to `approved` or `rejected`.
- Expired tokens trigger a new request, not a silent failure.

## Concurrency

- Default: **one running task at a time**. A Kubernetes `CronJob` sweep claims the oldest
  `Queued` message and starts one Job.
- Concurrency is bounded by how many Jobs the sweep starts and by the guarded claim.

## Storage and retention

- There is **no database**. The mailbox is the durable store and an external dependency:
  provider retention, quota, rate limits and account availability bound the queue.
- Terminal messages are retained long enough to act as tombstones so a redelivery cannot
  resurrect a task.
- Large harness logs are not stored on the message; they go to object storage, and the
  message carries only links.
