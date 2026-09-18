# Roadmap

Milestones are ordered so each is independently useful and testable. Pick the **first
unchecked slice** of the earliest milestone. One slice = one branch = one draft PR.

## M0 — Design (done)

- [x] Concept, architecture, transport/queue/provider/security designs, ADRs, AGENTS.md
  contract.

## M1 — Queue first (mailbox as the queue)

- [x] **M1.1** Verify Fastmail JMAP: custom `$herald-*` keywords, `Email/query` by
  `Message-ID`, and `Email/set` `ifInState` (CAS) — verified against a real account
  (`scripts/verify_fastmail_jmap.py`); all checks pass.
- [x] **M1.2** `Task` model + `Queue` port + `MemoryQueue` with idempotent enqueue
  (`Message-ID`), atomic claim, guarded transitions and the lease sweep; TDD, no network.
- [x] **M1.3** `JmapQueue` backend: the same port over Fastmail mailboxes/keywords, using
  `ifInState` for claims and `$herald-attempt-N` for attempts; unit-tested against a fake
  client and verified live against Fastmail (enqueue, claim, get, stale-lease sweep).
- [x] **M1.4** CLI: `herald task ls|show|claim|complete|fail` (no network; memory backend
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

- [x] **M3.1** `Runner` interface + `Worktree` (isolated `herald/<slug>` worktree created
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
- [x] **M6.5** Packaging (`Dockerfile`, `.dockerignore`) + OpenShift manifests (Kustomize
  base/overlays, runner `JobTemplate`), a per-role credential strategy (ADR 0003), and
  manifest tests asserting the hardening invariants. Verified live on OpenShift.

## Later / ideas

- HTTP entrypoint hardening: inbound webhook endpoint (the `ControlPlane` is ready; only
  the route and its authentication are missing), request limits and timeouts.

- JMAP `PushSubscription`/EventSource for low-latency triggering (replaces the sweep):
  `JmapClient` now exposes `event_source_url`, `push_create` and `push_destroy`; the
  long-lived EventSource listener that turns a `StateChange` into an ingest pass remains.
- IMAP transport as a secondary backend.
- Multi-provider debate (two models review each other before a PR).
- Per-project policies (which repos, which providers, which tasks allowed).
