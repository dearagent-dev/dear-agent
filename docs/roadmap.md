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
- [ ] **M1.5** Approvals threaded by JMAP reply + single-use token.

## M2 — Transport (email in/out)

- [ ] **M2.1** `Transport` interface + a `MemoryTransport` for tests.
- [ ] **M2.2** Normalizer: raw message → `Task` (repo/base/instructions), reject code.
- [ ] **M2.3** Fastmail JMAP transport (primary): push/poll, threading, keywords/state.
- [ ] **M2.4** Inbound authentication (shared secret / signed reply token), rate limit.
- [ ] **M2.5** Outbound threading + status/approval messages (no source).

## M3 — Runner + Git plane

- [ ] **M3.1** `Runner` interface + isolated worktree (ephemeral Job, `emptyDir`).
- [ ] **M3.2** OpenCode runner adapter (headless `opencode run`), structured events.
- [ ] **M3.3** Claude Code + Codex adapters.
- [ ] **M3.4** Git plane: branch `herald/<slug>`, commit, push, **draft PR**.
- [ ] **M3.5** Evidence as threaded replies (branch, SHA, PR url); logs to object storage.

## M4 — Human-in-the-loop

- [ ] **M4.1** Approval request → reply → approve/reject round trip.
- [ ] **M4.2** PR landing remains human-only; document the wall.
- [ ] **M4.3** Failure/escalation messages.

## M5 — Idle / creative loop

- [ ] **M5.1** Signal collection: recent repo activity (commits, changed files).
- [ ] **M5.2** Proposal generation anchored to activity + trends.
- [ ] **M5.3** Proposal queue (approval-gated by default); never auto-execute blind.
- [ ] **M5.4** Rate/budget limits so idle work cannot starve real work.

## M6 — Providers, hardening, ops

- [ ] **M6.1** Provider registry (hosted + local OpenAI-compatible), A/B re-runs.
- [ ] **M6.2** OS sandbox for runs (e.g. bubblewrap) + network policy.
- [ ] **M6.3** Observability: task metrics, per-run evidence, health endpoint.
- [ ] **M6.4** Prompt-injection hardening pass against `docs/security.md`.
- [ ] **M6.5** Packaging (container image) + OpenShift manifests (Kustomize) + GitOps
  (ArgoCD).

## Later / ideas

- JMAP `PushSubscription`/EventSource for low-latency triggering (replaces the sweep).
- IMAP transport as a secondary backend.
- Multi-provider debate (two models review each other before a PR).
- Per-project policies (which repos, which providers, which tasks allowed).
