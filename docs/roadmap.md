# Roadmap

Milestones are ordered so each is independently useful and testable. Pick the **first
unchecked slice** of the earliest milestone. One slice = one branch = one draft PR.

## M0 — Design (done)

- [x] Concept, architecture, transport/queue/provider/security designs, ADRs, AGENTS.md
  contract.

## M1 — Queue first (mailbox as the queue)

- [x] **M1.1** Verify Fastmail JMAP: custom `$dear-agent-*` keywords, `Email/query` by
  `Message-ID`, and `Email/set` `ifInState` (CAS) — verified against a real account
  (`scripts/verify_fastmail_jmap.py`); all checks pass.
- [x] **M1.2** `Task` model + `Queue` port + `MemoryQueue` with idempotent enqueue
  (`Message-ID`), atomic claim, guarded transitions and the lease sweep; TDD, no network.
- [x] **M1.3** `JmapQueue` backend: the same port over Fastmail mailboxes/keywords, using
  `ifInState` for claims and `$dear-agent-attempt-N` for attempts; unit-tested against a fake
  client and verified live against Fastmail (enqueue, claim, get, stale-lease sweep).
- [x] **M1.4** CLI: `dear-agent task ls|show|claim|complete|fail` (no network; memory backend
  by default, `--backend jmap` for Fastmail).
- [x] **M1.5** Approvals: single-use, expiring tokens issued next to the queue and redeemed
  from a reply (`approve|reject <token>`); drives `action -> approved|rejected`.

## M2 — Transport (email in/out)

- [x] **M2.1** `Transport` interface + a `MemoryTransport` for tests (pull `poll()` and
  push `receive()`, outbound with no attachment field).
- [x] **M2.2** Normalizer: raw message → `Task` + `TaskSpec` (`repo`/`base`/`model`
  metadata, recipient-based repo, reject code/attachments/empty).
- [x] **M2.3** Fastmail JMAP transport: `poll()` maps the task mailbox to `RawMessage`
  (including attachment detection), threaded `send()` via `EmailSubmission/set`.
- [x] **M2.4** Inbound authentication: HMAC-signed sender (constant-time, sender-bound)
  plus a per-sender sliding-window rate limit, applied before normalization.
- [x] **M2.5** Notifier: threaded status and approval messages carrying state, summary and
  links only (no source).
- [x] **M2.6** `ControlPlane` inbound loop: gate → normalize → enqueue, with threaded
  explanations for rejected messages.

## M3 — Runner + Git plane

- [x] **M3.1** `Runner` interface + `Worktree` (isolated `dear-agent/<slug>` worktree created
  and removed without touching the main checkout) + a minimal `CommandRunner`.
- [x] **M3.2** OpenCode runner adapter: headless `opencode run` with the prompt as a
  separate argv element (never a shell), configurable binary/model/timeout.
- [x] **M3.3** Claude Code (`claude -p`) and Codex (`codex exec`) adapters on a shared
  `HarnessRunner` base with the same no-shell guarantee.
- [x] **M3.4** Git plane: commit, push and **draft PR** via an injectable `Forge`
  (`GhForge`); a protected-branch guard makes writing `main` impossible.
- [x] **M3.5** `TaskExecutor`: worktree → harness → commit → push → **draft PR** → task
  `done`/`failed` → evidence as threaded links. Source never leaves Git.

## M4 — Human-in-the-loop

- [x] **M4.1** Approval round trip end to end: the `ControlPlane` detects an
  `approve|reject <token>` reply, applies it to the task and reports a reused/expired token
  back as a fresh request.
- [x] **M4.2** PR landing remains human-only; the wall is documented in
  `docs/architecture.md` and enforced by the protected-branch guard.
- [x] **M4.3** Failure/escalation messages: missing harness, timeout, error and no-change
  runs fail the task and ask a human to look, without raw harness output.

## M5 — Idle / creative loop

- [x] **M5.1** Signal collection: recent commits and TODO/FIXME markers from the local
  checkout, read-only, no network.
- [x] **M5.2** Deterministic `ProposalGenerator`: TODO markers and recent commits become
  concrete proposals with evidence.
- [x] **M5.3** `IdleLoop` submits proposals through the normal pipeline; they enter the
  queue approval-gated and are never auto-executed.
- [x] **M5.4** `IdleBudget`: a queue-depth gate and a per-run cap so idle work cannot
  starve real work.

## M6 — Providers, hardening, ops

- [x] **M6.1** Provider registry (hosted + local OpenAI-compatible) with
  task → project → global precedence; secrets referenced by env-var name only.
- [x] **M6.2** OS sandbox: `SandboxPolicy` (default-deny network), `BubblewrapSandbox`
  wrapping the harness argv, wired into `HarnessRunner`.
- [x] **M6.3** Observability: in-process task `Metrics` and a `Health` check over the
  queue (a non-empty queue is not unhealthy; too many running tasks is).
- [x] **M6.4** Prompt-injection hardening pass: an advisory `InjectionScanner` (audit and
  escalation, explicitly not a boundary) wired into the control plane as a `suspicious`
  report.
- [x] **M6.5** Packaging (`Containerfile`, `.dockerignore`) + OpenShift manifests (Kustomize
  base/overlays, runner `JobTemplate`), a per-role credential strategy (ADR 0003), and
  manifest tests asserting the hardening invariants. Verified live on OpenShift.

## M7 — Decision layer (proposed; see ADR 0004)

Small, fast, typed judgments at the edges, dispatched through a `Decider` port with a
deterministic fallback. Advisory only — never a security boundary.

- [x] **M7.1** `Decider` port + `RuleDecider` (reproduces today's deterministic behavior)
  and an optional `JevDecider` (hosted TypeSafe API, `TYPESAFE_API_KEY`). Fail open to the
  rules when the decider is absent, unreachable, or below the confidence threshold.
- [x] **M7.2** Model routing: one `choice` (`local` / `hosted`) plus a `noul` "needs a
  human" drives the provider registry, replacing a static mapping. Local for mechanical
  work, hosted for architecture/security/debugging.
- [x] **M7.3** Advisory injection + "no source over transport": a `noul` pair feeding the
  `suspicious` report next to `InjectionScanner` and the attachment check, so inline
  diffs/encoded blobs get caught too.
- [x] **M7.4** Earned thresholds: `DecisionLog` (JSONL for now; moves to Postgres in M8)
  records every decision for tuning; `dear-agent decide` lets an operator test the routing, with
  rules or the live model.

## M8 — Durable state in PostgreSQL (ADR 0005)

The mailbox becomes ingress; PostgreSQL holds tasks, state, approvals and the decision log.
See [ADR 0005](decisions/0005-state-store.md).

- [x] **M8.1** Postgres deployment: a kustomize component (`StatefulSet` + `Service` + `PVC`,
  `registry.redhat.io/rhel9/postgresql-18`) and a local podman equivalent
  (`scripts/dev-postgres.sh`), sharing one `DEAR_AGENT_DATABASE_URL`.
- [x] **M8.2** Schema and a `PostgresQueue` implementing the `Queue` port (unique
  `transport_id`, guarded `UPDATE` claim, lease sweep); `JmapQueue` retired and the queue
  backend decoupled from the transport (`DEAR_AGENT_QUEUE`). Verified against a live Postgres.
- [x] **M8.3** Approvals and the decision log/labels in Postgres; the file stores remain only
  as a single-process fallback (`DEAR_AGENT_APPROVALS_FILE`, `DEAR_AGENT_DECIDER_LOG=<path>`).
- [x] **M8.4** The sweep and runner run on the database, and the parsed `TaskSpec` is
  persisted with the task, so a runner no longer reads the mailbox. No backfill is needed at
  the current scale (there is no production mailbox state to migrate).

## M9 — Execution environments and the forge (ADR 0008, ADR 0009)

A run is one **environment session**; the harness is injected into a repository-declared
environment and never assumed; the draft PR is opened through the forge **REST API**.

- [x] **M9.1** One session per task: `ContainerSandbox` starts a long-lived container the harness
  and the `verify` gate share; `TaskExecutor` owns its lifecycle.
- [x] **M9.2** Environment descriptor: detect Dev Container / Ansible EE / Containerfile / mise
  and use a declared image for the session (`environment/descriptor.py`).
- [x] **M9.3** Harness bundle: a portable OpenCode bundle (binary + loader + libs) injected into
  any environment image (`environment/bundle.py`).
- [x] **M9.4** Forge by API: GitHub/GitLab/Gitea draft PR/MR over REST, chosen by remote host or
  `DEAR_AGENT_FORGE`; read/write git keys and the forge token wired into the runner.
- [x] **M9.5** Routing shares the session with the verify gate (`SessionProvider`).
- [x] **M9.6** Feature/prebuild: `EnvironmentBuilder` builds a Containerfile with `podman` or a
  Dev Container with the `devcontainer` CLI (optionally adding a harness Feature); CI builds and
  publishes the sidecar harness image (`deploy/harness/Containerfile`). Harness injection is
  multi-harness: OpenCode via a portable bundle, Claude Code/Codex via bootstrap
  (`HarnessInfo.install` → `ContainerSandbox.setup`).
- [ ] **M9.7** Verify the in-cluster PR path live (sidecar runner + provisioned secrets):
  runbook + script ready ([`verify-openshift.md`](verify-openshift.md),
  `scripts/verify-openshift.sh`); run it and record the result.

## Later / ideas

- [x] **Gate high-risk inbound runs behind approval.** Inbound tasks whose decider verdict
  says a human should look are parked in `action` and released by a `run` approval, like
  suspicious messages (`HumanGate`, `DEAR_AGENT_DECIDER`).
- [x] **Connection resilience**: `db.ResilientConnection` reconnects on failure (lazily and once
  per statement) and sets TCP keepalives, so a dropped Postgres connection does not break the
  long-lived control plane.
- [x] **Automated backups**: a daily `pg_dump` to a dedicated PVC keeping the last seven
  (`deploy/components/backup/`).
- [ ] **PITR** (continuous WAL archiving to object storage): deferred — the daily dump is enough
  at this scale; revisit before the data becomes irreplaceable.
- [x] Idle proposals dedupe on the database (the deterministic transport id + the queue's
  unique index) and the budget is the DB queue depth; no further persistence is needed.
- [x] Live OpenShift verification of the Postgres component (StatefulSet/PVC up, schema
  created, control-plane `/health` green).

- [x] HTTP entrypoint hardening: inbound webhook endpoint (`POST /inbound`, HMAC auth,
  fail-closed without a secret), request-body cap and timeouts.

- [x] JMAP `PushSubscription`/EventSource for low-latency triggering (replaces the sweep):
  `JmapClient` exposes `event_source_url`, `push_create` and `push_destroy`; `dear-agent listen`
  opens the EventSource and re-polls (idempotently) on an `Email` `StateChange`.
- [x] IMAP transport as a secondary backend (`transports/imap.py`, `imap/`, `smtp/`;
  `DEAR_AGENT_BACKEND=imap`).
- [x] Self-hosted decision model: any local OpenAI-compatible server (llama.cpp, vLLM, Colibri,
  self-hosted Jev) is the reference decider via `openai-compat`; loopback endpoints are marked
  local and need no key. See [providers.md](providers.md#self-hosted-reference-decider).
- [x] Multi-provider debate: an optional second model reviews the diff before the PR and can
  request one bounded revision ([ADR 0010](decisions/0010-adversarial-review.md)).
- [x] Per-project policies (which repos, which providers, which tasks allowed).
