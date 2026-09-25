# Architecture

Dear Agent has one job: turn an **asynchronous inbound message** into a **reviewable Git pull
request**, then tell the sender. Everything else is a detail of how that is done safely
and idempotently.

## Data flow

```
inbound message ──▶ Transport ──▶ Normalizer ──▶ Queue ──▶ Scheduler ──▶ Runner
                  (mailbox ingress)  (Task)     (Postgres)  (sweep)      │
                                                                        │ worktree + model
                                                                        ▼
outbound message ◀── Notifier ◀── Evidence ◀──────────────────────── Git plane
   (status/approval)                                                (branch + PR)
```

## Components

### Transport
Inbound: accept a message (webhook push, IMAP/JMAP poll) and hand the raw payload plus a
stable transport id to the Normalizer. Outbound: send status and approval messages,
threaded to the original conversation. See [transports.md](transports.md).

Concretely, inbound has two entrypoints: `JmapTransport.poll()` (the mailbox is ingress)
and `POST /inbound` on the control-plane HTTP server for push transports, authenticated
with an HMAC over the raw body and disabled (503) when `DEAR_AGENT_INBOUND_SECRET` is unset.
Both feed the same `ControlPlane.ingest`.

**Interface (to implement):**
```
inbound()  -> Iterable[RawMessage]         # push handler or poller
send(thread, subject, body, headers=None)  # reply in-thread
```

### Normalizer
Maps a `RawMessage` to a `Task`. Extracts: repo URL, base branch, instructions,
constraints, requested provider/model, and a reply token. Rejects messages without a
resolvable repo. Never trusts the message for commands. See [security.md](security.md).

The `ControlPlane` wires the inbound loop: authorize (`InboundGate`) → normalize → enqueue.
A rejected message gets a threaded explanation; an unauthorized one is dropped without a
reply, so a forged sender cannot use Dear Agent as a backscatter amplifier.

### Queue
The durable source of truth is **PostgreSQL**; the transport mailbox is **ingress** only.
Dedupe is a unique index on the transport message id, and claiming is a conditional
`UPDATE ... FOR UPDATE SKIP LOCKED`. The parsed `TaskSpec` is persisted with the task, so a
runner never reads the mailbox; approvals (single-use tokens) and the decision log live in the
same database. See [queue.md](queue.md) and
[ADR 0005](decisions/0005-state-store.md) (which supersedes
[ADR 0002](decisions/0002-deployment-topology.md) in part).

### Scheduler
A Kubernetes `CronJob` sweep lists `queued` tasks and starts one Job per task, claiming
each atomically before the work begins. The default is **one running task at a time**.

Concretely: `dear-agent sweep` returns stale `Running` tasks to `Queued` and renders the runner
`JobTemplate` (a ConfigMap) once per queued task, injecting the task id through a parsed
YAML value (never string interpolation); the Job runs `dear-agent run <task-id>`, which claims
the task, clones the repo read-only, creates a worktree and drives the executor. Creation
is idempotent per task id, so a re-run after a restart is safe.

### Runner
Executes a harness for a claimed task inside an isolated Git worktree and returns
evidence (branch, commit, logs, PR). The runner is a thin adapter over a harness CLI.
Dear Agent must not embed a harness. A `HarnessCatalog` describes the available harnesses
(OpenCode, Claude Code, Codex, or a custom `command`) with their metadata; the model is
passed only to a harness that accepts one (OpenCode), never to a subscription harness.

**Interface (to implement):**
```
run(task, worktree) -> RunResult
```
A runner adapter must: create/enter an isolated worktree, invoke the harness
non-interactively, capture structured events, and never touch the user's tree.

### Execution environment

A task runs in **one environment session** (ADR 0008): the harness and the `verify` gate
execute in the same container, so whatever the harness provisions is visible to the gate. The
harness is **injected** into that environment, never assumed present: the repository declares
the environment (`.devcontainer/devcontainer.json`, an Ansible `execution-environment.yml`, a
`Containerfile`, `mise`/`.tool-versions`, or a fallback base image) and Dear Agent composes the
harness in — a Dev Container *Feature* at build time, or a portable *bundle* at run time
(`environment/bundle.py`). Where the session runs is a port (`RunnerHost`): a local `podman`
container, a self-hosted Kubernetes/OpenShift Job, or a self-hosted CI runner. Hosted agent
backends are out of scope.

### Adversarial review (optional)

Before the draft PR is opened, an optional second model reviews the diff (the `Reviewer` port,
[ADR 0010](decisions/0010-adversarial-review.md)): on a "revise" verdict the harness runs one
**bounded** round with the reviewer's notes, then the PR is opened regardless. It is opt-in
(`DEAR_AGENT_REVIEWER=openai-compat`) and fail-open — a reviewer outage publishes as today — and
the human PR review remains the real gate.

### Provider
Model backend configuration handed to the harness (endpoint, model id, key reference).
Hosted or local. See [providers.md](providers.md).

### Git plane
Owns worktrees, branches and pull requests. **Agents never write `main`.** Every result is
an `dear-agent/<slug>` branch and a draft PR.

Only the **control process** commits, pushes and opens the PR — never the harness (ADR 0007).
The PR is opened through the forge's REST API behind a `Forge` seam
(`gitplane/forge.py`: GitHub/GitLab/Gitea/Bitbucket), selected from the repository's remote host
or `DEAR_AGENT_FORGE`, with a per-provider token passed by environment variable and no CLI
dependency (ADR 0009).

### Notifier
Threads status and approval requests back over the transport. Approvals are replied to and
matched by token + thread.

### Idle / creative loop
When the queue is empty, proposes new work anchored to recent repository activity and
external trends. Proposals are queued (optionally gated for approval), never executed
blindly. This is the "night shift" mode: useful work happens while no human is watching.

## Task lifecycle

```
received ─▶ queued ─▶ running ─▶ (draft PR) ─▶ action? ─▶ done
              │           │                        │
              │           └─────────────▶ failed   └─▶ awaiting-approval ─▶ approved/rejected
              └─▶ rejected (no repo / policy)
```

- `action?` is the human-in-the-loop queue: a task that needs a decision before it can
  finish. A reply carrying a valid single-use token moves it to `approved`/`rejected`.
- A finished task sends an outbound message with the PR link; the PR is the artifact.
- Re-delivery of the same transport message is a no-op (idempotency).

### The human wall

Dear Agent never lands a change. The pipeline stops at a **draft PR**: opening, approving or
merging it is a human action. The approval token authorises a *decision about a task*
(e.g. proceed, or land a plan), never a write to `main`. `GitPlane` refuses any commit, push
or PR whose branch is protected; the **real** boundary against a shell-capable harness is
remote branch protection plus a push credential scoped to `dear-agent/*` (the runner mounts
only the read key). The worst case of a hijacked agent is a malicious pull request a human
rejects.

When a run cannot finish — harness missing, timeout, error, or no changes — the task is
`failed` and an **escalation** message asks a human to look. Raw harness output never
travels over the transport.

## Concurrency and durability

- Tasks persist across restarts because PostgreSQL persists; an interrupted run is
  **resumable** (a sweep returns a stale `running` task to `queued`), never a lost task.
- Worktrees are disposable (`emptyDir` inside a Job); the branch/PR is durable.
- Serial execution is the default and is not a bug.
- Every task emits an append-only **event log** (`dear_agent_event`): claimed, verify
  passed/failed, publish failed, done/failed, plus ingest accept/reject/deny. It is the
  history a state column cannot express; `dear-agent task events <id>` reads it.
- An **error budget** (circuit breaker) stops starting new runs when too many tasks failed
  recently (`DEAR_AGENT_ERROR_BUDGET_*`), so a systemic failure does not grind on.
- `dear-agent health` reports a `stalled` count (running tasks whose lease is about to expire)
  as a soft watchdog; the sweep is the hard one.
- A task may declare `depends-on:` task ids; it stays `queued` (skipped by the sweep and
  `dear-agent run`) until they are `done`, and is failed if a dependency fails. A small typed
  dependency graph, not a workflow engine.
- **Per-project policy** is markdown (YAML front matter + prose) under `policy/`, projected
  into `dear_agent_policy` with `dear-agent policy sync`; the worker reads the row. A policy can
  enable/disable a repo (a disabled repo's tasks fail), default the base branch, and extend
  the verify allowlist for that repo — it only *narrows* or *defaults*, never widens what an
  untrusted message may do.

## Deployment

Dear Agent is **Kubernetes/OpenShift by design**. The control plane and the runner are standard
workloads: one Job per task, a `CronJob` sweep to claim work, and no always-on daemon. Durable
state is a PostgreSQL `StatefulSet` (or a local podman container). See
[ADR 0002](decisions/0002-deployment-topology.md) and
[ADR 0005](decisions/0005-state-store.md).

## Trust boundaries

- Inbound content is untrusted. It can influence *what* work is proposed, never *which
  commands run* without allowlist/approval. See [security.md](security.md).
- Agent execution is isolated (worktree + optional OS sandbox). Network policy is explicit.
- Model providers and transports are configured, not hard-coded.
