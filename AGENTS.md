# AGENTS.md — Herald

This file is the operating contract for any coding agent (OpenCode, Claude Code, Codex,
or a future runner) working in this repository. Read it fully before changing anything.
It is intentionally explicit: Herald is a background agent for *unattended* work, so its
own development must be safe to run with an empty chair.

---

## 1. What Herald is

Herald is an **async, email-first inbox that turns messages into Git pull requests
produced by coding agents**. A message enqueues a task; an agent works in its own Git
worktree; the result is a **PR a human reviews**. The transport never carries source
code — **Git is the only artifact plane**.

Two product goals drive every design decision:

1. **Enqueueing is the hard problem.** Work must be able to arrive asynchronously
   (email/JMAP/webhook), be deduplicated, survive restarts, and be safe to re-deliver.
2. **Creative when idle.** When nothing is queued, Herald must be able to *propose* work
   from recent repository activity and trends, instead of sitting idle.

If a change does not serve one of those goals (or a milestone in
[`docs/roadmap.md`](docs/roadmap.md)), do not build it.

## 2. Golden rules (non-negotiable)

1. **No source code over the transport.** Never send patches, source files, diffs as
   files, or directory archives via email/JMAP/IRC. Only task metadata, status, and
   links (repo, branch, PR, commit SHA) travel. Code moves over Git only.
2. **The deliverable is a PR.** Agents never commit or push to `main` (or any protected
   base). Always an `herald/<slug>` branch + a **draft PR**. Human-gated landing.
3. **Never replace the user's harness.** Herald *wraps* OpenCode/Claude Code/Codex/a
   custom runner through a runner adapter. Do not fork or vend a harness into this repo.
4. **Enqueue is idempotent.** The same inbound message must never create two tasks.
   Deduplicate on the transport message id (`Message-ID`, JMAP `emailId`, webhook event
   id). Re-delivery is expected, not exceptional.
5. **Pluggable providers, hosted or local.** The model is a configuration detail. A local,
   slow, OpenAI-compatible endpoint (llama.cpp `llama-server`, Colibri `coli serve`,
   vLLM) is a first-class target. Never hard-depend on one vendor.
6. **No secrets in git.** Tokens, passwords and keys live in environment variables or
   untracked secret files. Add new secret paths to `.gitignore` immediately.
7. **Untrusted input.** Inbound messages are attacker-controlled. Never execute commands
   derived from a message without an allowlist or an explicit human approval. Agent runs
   happen in an isolated worktree and, where available, an OS sandbox. See
   [`docs/security.md`](docs/security.md).
8. **Durability over cleverness.** Tasks, states and evidence live in the durable queue
   (the transport mailbox) or in Git, never only in a model's context or a process's
   memory.

## 3. Status and where to start

**Current status: M1 (queue).** The M0 design is complete. The durable queue is the
transport mailbox (Fastmail JMAP first); there is no database. See
[`docs/decisions/0002-deployment-topology.md`](docs/decisions/0002-deployment-topology.md).

Start here:

1. Read [`docs/roadmap.md`](docs/roadmap.md) and pick the **first unchecked slice** of the
   earliest milestone.
2. Read the relevant design doc for that slice
   ([architecture](docs/architecture.md) · [transports](docs/transports.md) ·
   [queue](docs/queue.md) · [providers](docs/providers.md) ·
   [security](docs/security.md)).
3. Open an issue (or a task in `TASKS.md` if it exists) describing the slice before coding.

The recommended first slice is **M1.1 — verify Fastmail JMAP and model `Task` + the `Queue`
port**, because everything else depends on a correct, idempotent queue.

## 4. Language and tooling

- **Python 3.12+** for the control plane, queue port and adapters. Rationale: mature
  stdlib/IMAP/JMAP libraries, built-in JSON, and no compile step keep the maintenance
  surface small. (See [`docs/decisions/0001-language.md`](docs/decisions/0001-language.md).)
- **No database.** The durable queue is the transport mailbox (Fastmail JMAP first),
  accessed through a `Queue` port. (See
  [`docs/decisions/0002-deployment-topology.md`](docs/decisions/0002-deployment-topology.md).)
- **Transport adapters** are thin and contain no business logic; they map the mailbox to
  `Task` and back.
- **Deployment** is Kubernetes/OpenShift by design (ADR 0002): one Job per task, a
  `CronJob` sweep, no always-on daemon.
- The **`herald` CLI** is the operator entry point: `herald task ls|show|claim|complete|fail`
  (`--backend memory|jmap`).
- Formatting/linting/tests (add these as they come into existence):
  - `ruff format && ruff check`
  - `pytest`
  - Never merge code with failing tests or lint.

## 5. Repository layout (target)

```
herald/
├── AGENTS.md                 # this contract
├── README.md
├── docs/
│   ├── architecture.md
│   ├── transports.md
│   ├── queue.md
│   ├── providers.md
│   ├── security.md
│   ├── roadmap.md
│   └── decisions/            # ADRs, one file per decision
├── src/herald/               # Python package
│   ├── queue/                # Task model + Queue port over the mailbox (JMAP)
│   ├── transports/           # inbound/outbound adapters (jmap, imap, agentmail, irc)
│   ├── runners/              # harness adapters (opencode, claude, codex, custom)
│   ├── providers/            # model provider config (hosted + local)
│   ├── gitplane/             # worktree, branch, PR
│   ├── notify/               # outbound status/approval threading
│   └── idle/                 # creative loop for empty queues
├── deploy/                   # Kubernetes/OpenShift manifests (Kustomize)
│   ├── base/                 # control plane, runner Job/CronJob
│   └── overlays/             # dev, prod
├── tests/
└── scripts/                  # dev/ops helpers
```

## 6. How to work in this repo

- **One slice per branch.** Branch name `herald/<milestone>-<slug>` (e.g.
  `herald-m1-task-model`).
- **TDD.** Failing test first, minimal implementation, then refactor. Commit per
  meaningful step.
- **Draft PR always.** Open a draft PR against `main`; never merge it yourself. A human
  lands it.
- **Small, reviewable PRs.** If a slice grows past a day of work, split it and update the
  roadmap.
- **Docs travel with code.** Any behavior change updates the relevant `docs/*.md` and, if
  it is a design decision, adds an ADR under `docs/decisions/`.
- **Report uncertainty.** If a design doc is ambiguous, leave a `TODO(decision)` and open
  an issue rather than guessing.

## 7. Security checklist for every PR

- [ ] No secrets, tokens, or credentials added (check `git diff` and `.gitignore`).
- [ ] No path where an inbound message can trigger an unapproved shell command.
- [ ] Agent work runs in a dedicated worktree; the user's working tree is never touched.
- [ ] Outbound messages contain no source code, only metadata and links.
- [ ] New external calls (transport/provider) are documented and configurable.

See [`docs/security.md`](docs/security.md) for the prompt-injection threat model.

## 8. Open decisions (do not guess — ask or open an ADR)

- Sweep cadence: latency vs cluster churn for the `CronJob`.
- Concurrency: one task at a time vs N Jobs.
- Idle loop scope: propose-only vs auto-enqueue drafts for approval.
- Runner contract: exact CLI/stdio protocol between Herald and a harness (see
  `docs/architecture.md` → Runner).

## 9. Prior art to reuse, not reinvent

- `ElectricJack/agent-queue` — durable task graph, gates, worktree isolation, review.
- `voundbrand/overnight` — branches/PRs/checks as the queue; review-driven certification.
- `a20185/OvernightAgent` — resumable runs, four-gate verification, opencode adapter.

Copy the *patterns*, not the code.
