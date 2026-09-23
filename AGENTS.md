# AGENTS.md — Dear Agent

This file is the operating contract for any coding agent (OpenCode, Claude Code, Codex,
or a future runner) working in this repository. Read it fully before changing anything.
It is intentionally explicit: Dear Agent is a background agent for *unattended* work, so its
own development must be safe to run with an empty chair.

---

## 1. What Dear Agent is

Dear Agent is an **async, email-first inbox that turns messages into Git pull requests
produced by coding agents**. A message enqueues a task; an agent works in its own Git
worktree; the result is a **PR a human reviews**. The transport never carries source
code — **Git is the only artifact plane**.

Two product goals drive every design decision:

1. **Enqueueing is the hard problem.** Work must be able to arrive asynchronously
   (email/JMAP/webhook), be deduplicated, survive restarts, and be safe to re-deliver.
2. **Creative when idle.** When nothing is queued, Dear Agent must be able to *propose* work
   from recent repository activity and trends, instead of sitting idle.

If a change does not serve one of those goals (or a milestone in
[`docs/roadmap.md`](docs/roadmap.md)), do not build it.

## 2. Golden rules (non-negotiable)

1. **No source code over the transport.** Never send patches, source files, diffs as
   files, or directory archives via email/JMAP/IRC. Only task metadata, status, and
   links (repo, branch, PR, commit SHA) travel. Code moves over Git only.
2. **The deliverable is a PR.** Agents never commit or push directly to `main` (or any
   protected base). Always an `dear-agent-<slug>` branch + a PR. Landing is human-gated: a human
   reviews and explicitly approves, and only then does the agent merge (squash) — never
   before checks are green and approval is given.
3. **Never replace the user's harness.** Dear Agent *wraps* OpenCode/Claude Code/Codex/a
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
   (PostgreSQL; the transport mailbox is ingress) or in Git, never only in a model's
   context or a process's memory.

## 3. Status and where to start

**Current status: M8 (durable state in PostgreSQL) implemented** (PR #55). M0–M7 are done;
M0–M6 were verified live on OpenShift + Fastmail. The mailbox is **ingress**: durable tasks,
state, approvals and the decision log live in PostgreSQL — see
[`docs/decisions/0005-state-store.md`](docs/decisions/0005-state-store.md), which supersedes
[ADR 0002](docs/decisions/0002-deployment-topology.md) in part. The local MVP path is in
[`docs/getting-started.md`](docs/getting-started.md). The decision layer uses System One models
(Jev first) for routing/gates — see
[`docs/decisions/0004-decision-model.md`](docs/decisions/0004-decision-model.md). Dear Agent has
**no direct-LLM path**: a harness codes, a decider decides.

Start here:

1. Read [`TASKS.md`](TASKS.md) for the **current, in-progress slice** and its handoff; that
   supersedes the roadmap for what to do right now.
2. Otherwise read [`docs/roadmap.md`](docs/roadmap.md) and pick the **first unchecked slice**
   of the earliest milestone.
3. Read the relevant design doc for that slice
   ([architecture](docs/architecture.md) · [transports](docs/transports.md) ·
   [queue](docs/queue.md) · [providers](docs/providers.md) ·
   [security](docs/security.md)).
4. Open an issue (or add a task to `TASKS.md`) describing the slice before coding.

The recommended next slice is the first unchecked one in
[`TASKS.md`](TASKS.md) / [`docs/roadmap.md`](docs/roadmap.md).

## 4. Language and tooling

- **Python 3.12+** for the control plane, queue port and adapters. Rationale: mature
  stdlib/IMAP/JMAP libraries, built-in JSON, and no compile step keep the maintenance
  surface small. (See [`docs/decisions/0001-language.md`](docs/decisions/0001-language.md).)
- **PostgreSQL is the durable queue; the mailbox is ingress.** Tasks and state live in
  PostgreSQL (18 on UBI 9), accessed through the `Queue` port; email/JMAP/webhook only
  delivers the request. (See
  [`docs/decisions/0005-state-store.md`](docs/decisions/0005-state-store.md).)
- **Transport adapters** are thin and contain no business logic; they map the mailbox to
  `Task` and back.
- **Deployment** is Kubernetes/OpenShift by design (ADR 0002, ADR 0005): one Job per task, a
  `CronJob` sweep, no always-on daemon, and a PostgreSQL `StatefulSet` (or a local podman
  container) for durable state.
- The **`dear-agent` CLI** is the operator entry point: `dear-agent task ls|show|claim|complete|fail`
  (queue backend from `DEAR_AGENT_QUEUE=memory|postgres`; `--backend` selects the transport).
- Formatting/linting/tests (add these as they come into existence):
  - `ruff format && ruff check`
  - `pytest`
  - Never merge code with failing tests or lint.

## 5. Repository layout (target)

```
dear-agent/
├── AGENTS.md                 # this contract
├── README.md
├── docs/
│   ├── architecture.md
│   ├── getting-started.md
│   ├── transports.md
│   ├── queue.md
│   ├── providers.md
│   ├── security.md
│   ├── roadmap.md
│   └── decisions/            # ADRs, one file per decision
├── src/dear_agent/               # Python package
│   ├── db.py                 # Postgres connect + schema composition
│   ├── events.py             # append-only task event log + error budget
│   ├── deps.py               # task dependency graph (depends-on) resolution
│   ├── policy.py             # per-project policy: markdown projected into the store
│   ├── queue/                # Task model + Queue port (Postgres; mailbox is ingress)
│   ├── transports/           # inbound/outbound adapters (jmap, imap/smtp, webhook, memory)
│   ├── imap/                 # IMAP client (receive) for the IMAP/SMTP transport
│   ├── smtp/                 # SMTP client (send) for the IMAP/SMTP transport
│   ├── runners/              # harness adapters (opencode, claude, codex, custom) + catalog
│   ├── providers/            # model provider config (hosted + local)
│   ├── gitplane/             # worktree, branch, PR
│   ├── notify/               # outbound status/approval threading
│   └── idle/                 # creative loop for empty queues
├── deploy/                   # Kubernetes/OpenShift manifests (Kustomize)
│   ├── base/                 # control plane, runner Job/CronJob
│   ├── components/           # opt-in pieces (postgresql StatefulSet)
│   └── overlays/             # dev, prod
├── tests/
└── scripts/                  # dev/ops helpers
```

## 6. How to work in this repo

- **One slice per branch.** Branch name `dear-agent-<milestone>-<slug>` (e.g.
  `dear-agent-m1-task-model`).
- **TDD.** Failing test first, minimal implementation, then refactor. Commit per
  meaningful step.
- **PR per slice.** Open a PR against `main`. A human reviews and explicitly approves;
  once checks are green and approval is given, the agent lands it (squash). Never merge
  before that approval, and never push directly to `main`.
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
- Runner contract: exact CLI/stdio protocol between Dear Agent and a harness (see
  `docs/architecture.md` → Runner).

## 9. Prior art to reuse, not reinvent

- `ElectricJack/agent-queue` — durable task graph, gates, worktree isolation, review.
- `voundbrand/overnight` — branches/PRs/checks as the queue; review-driven certification.
- `a20185/OvernightAgent` — resumable runs, four-gate verification, opencode adapter.

Copy the *patterns*, not the code.
