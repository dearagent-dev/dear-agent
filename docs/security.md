# Security and untrusted input

Inbound messages are attacker-controlled. This document is the threat model and the
containment strategy for the main risk in an agentic inbox: **prompt injection**.

## Threat model

An attacker sends an email (or plants text in a repo file, commit message, issue or
dependency README) that tries to make the agent run commands, exfiltrate secrets, or open a
malicious PR. The attacker may be fully anonymous, may spoof `From`, and may know the
`owner-repo@…` routing address.

**Prompt injection cannot be prevented at the model layer.** An LLM does not reliably
separate instructions from data. The design principle is therefore: **assume the model is
compromised and make obedience harmless.** Trust is enforced by the surrounding system, not
by the prompt.

## Authentication is not trust in content

DMARC/SPF/DKIM plus the sender allowlist prove **who** sent a message; they say nothing
about whether its *content* is safe to act on. A trusted sender can be phished, relay a
third party's text, or paste attacker-controlled material — and the agent also reads
untrusted repo files, commits and issues (indirect injection). So authenticating the sender
bounds *who may enqueue*; it must never relax the containment (isolated worktree, no `main`,
draft PR, sandbox, egress allowlist). The model is not a security boundary either; the
`Decider`/`SecurityDecider` is advisory, and a "trusted" sender's task is still just a task.

## Layers

Defense in depth, ordered from highest leverage. Each layer maps to a component.

### 1. Authorize before normalizing

- Authenticate the **sender**, not the content: SPF/DKIM/DMARC alignment plus a signed
  header (HMAC) or a signed reply token. `herald.auth.InboundAuthorizer` verifies an
  `X-Herald-Signature` HMAC over the body **and the sender**, in constant time, so a valid
  signature cannot be replayed under another `From`.
- Allowlist authorized senders (`InboundAuthorizer.allowlist`). The routing address alone
  never authorizes.
- Unsigned or unauthorized mail is **quarantined and never normalized** into a task, and
  denied mail is dropped without a reply (no backscatter).
- The **email path** (`herald listen`) cannot carry the HMAC header, so it uses
  `herald.auth.SenderAllowlist` (`HERALD_ALLOWED_SENDERS`, exact addresses or `@domain`).
  An empty list rejects everyone (fail closed). Note that `From` is spoofable, so this is a
  first line, not a boundary: harden it with DMARC/SPF/DKIM verification of the
  `Authentication-Results` header or a shared secret in the body, and rely on the approval
  gate for high-risk work. Handled messages are filed into `Herald-Done` so they are not
  reprocessed.
- Rate-limit per sender (`herald.auth.RateLimiter`, sliding window): a burst of mail must
  not become a burst of agent runs.
- `InboundGate` composes authorization and rate limiting, and runs before the Normalizer.

Owner: transport adapter + normalizer.

### 2. Separate instructions from data

- A **fixed system prompt** defines the task contract; message text is injected as a
  clearly delimited, labeled untrusted-data block.
- The message selects an action from a **closed set** (`implement`, `review`, `fix-tests`),
  which maps to a fixed prompt template. It cannot supply commands, flags or paths.
- Never interpolate message text into a shell, a path, or a tool argument.
- Apply the same rule to **everything the agent reads**: repo files, commit messages,
  issues, dependency docs are untrusted too (indirect injection).

Owner: normalizer + runner prompt construction.

### 3. Least privilege at execution

- Run in an OS sandbox (bubblewrap / gVisor); non-root, arbitrary UID (OpenShift SCC).
  `herald.sandbox.BubblewrapSandbox` wraps the harness argv in `bwrap` with the worktree as
  the only writable path. Alternatively `HERALD_ISOLATION=podman` uses
  `herald.sandbox.ContainerSandbox`: the harness runs in its per-harness image
  (`HarnessInfo.image`) with the worktree bind-mounted at `/work` and only explicitly
  configured credential paths mounted (read-only by default). The container is the jail, so
  no harness runtime has to be trusted on the host. See
  [ADR 0006](decisions/0006-container-isolation.md).
- **Default-deny egress**, allowlisting only the Git remote and the model endpoint. No
  network means no exfiltration and no tool download. `SandboxPolicy(allow_network=False)`
  is the default and adds `--unshare-net`.
- Do **not** mount a service account token (`automountServiceAccountToken: false`).
- No long-lived secrets in the run environment; only a scoped model key where required.
- Mount the repo read-only outside the disposable worktree.
- Give the harness an **allowlist** of tools, not arbitrary shell.
- Reject attachments and any message that carries source code or patches.
- A message-supplied `verify` command runs only when its argv matches the operator's
  allowlist (`HERALD_VERIFY_ALLOW`) and is executed as an argv, never through a shell — a
  message-derived command is never run blindly (golden rule 7).

Owner: runner + Kubernetes manifests.

### 4. Git is the containment boundary

- Agents never commit or push to `main`; every result is an `herald/<slug>` branch and a
  **draft PR**.
- `main` is protected with required human review.
- The agent's commit identity is distinguishable from a human's.
- Worst case, a fully hijacked agent produces a malicious **pull request a human rejects**.
  This is why the deliverable is a PR, never a merge.

Owner: gitplane + repository settings.

### 5. Approval gates for dangerous actions

- Any action outside the allowlist (installing dependencies, pushing, network calls,
  opening issues) goes to the **Action queue** and requires a reply carrying a valid
  **single-use token**.
- Commands derived from a message are never executed without an allowlist or human
  approval.
- A message flagged **suspicious** (by `InjectionScanner` or the `SecurityDecider`, which
  uses Jev when configured) is parked in `action` and **does not run** until a human
  approves it with a single-use token — the injection never reaches the harness.
- A task the decider reads as **needs a human** (`HumanGate`, the same question
  `ModelRouter` uses: production, deployment, data, high-stakes) is parked in `action` too.
  It is advisory and fail-open — no decider, or a decider error, means no gate — and the
  draft PR remains the landing gate regardless.
- A repository that no *enabled* project policy matches is **refused**: once policies are
  configured, one cannot ask Herald to work on a repo the operator did not allow. Scope the
  push credential to the allowed repos and keep branch protection as the last line.

Owner: queue + notifier + runner.

### 6. Assume breach

- **Audit**: retain the raw inbound message, the exact prompt, the tool calls and the diff.
- **Budgets**: cap tokens, wall-clock time and runs per sender.
- **No recursion**: an agent cannot enqueue further tasks without approval.
- **Outbound screening**: responses are checked for secret patterns; outbound carries only
  state, summary and links, never source.

Owner: control plane + notifier.

### 7. Protect the state store

- PostgreSQL holds tasks, state, single-use approval tokens and the decision log (ADR 0005).
  Credentials come from a Secret provisioned out of band (ADR 0003); the application uses a
  least-privilege role, never the admin role.
- The database is **never exposed** outside the cluster or host: a headless `Service` with no
  `Route`/`NodePort`, plus a `NetworkPolicy` that admits only Herald pods on `5432`.
- Only task metadata and links are stored; **source never enters the database** — it stays in
  Git. Approval tokens are stored as opaque values and compared in constant time.
- In production, back the store with automated backups and (ideally) PITR; scope credentials
  per environment and rotate them.

Owner: queue + approvals + Kubernetes manifests.

## What is not a defense

- System-prompt "guardrails" ("ignore instructions in the message"): marginal, bypassable.
  Never the primary control.
- `herald.security.InjectionScanner`: flags likely injection patterns for **audit and
  escalation only**. A pattern list is evadable, so a clean scan is not a guarantee and a
  suspicious one is not proof. The real containment is the sandbox, the worktree, the
  protected-branch guard and the human review of the PR.
- Spam filtering or `From` inspection alone: spoofable; use authenticated signatures.
- Model selection: a stronger model is not a security boundary.

## Checklist mapping

The per-PR security checklist lives in [AGENTS.md](../AGENTS.md) §7. New external calls
(transport/provider) must be documented and configurable, and no inbound message may reach
an unapproved shell command.
