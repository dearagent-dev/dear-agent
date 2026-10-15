# TASKS.md — current work

Short-lived, per-slice work list. [`AGENTS.md`](AGENTS.md) is the durable contract; this file is
the current state, and finished items are deleted rather than archived (the history lives in
`git log` and [`docs/roadmap.md`](docs/roadmap.md)).

## Now — 2026-10-15

### Deep adversarial review of `main` (`f554f30`)

Codex ran an adversarial review of the whole project: **19 findings (3 blockers, 8 high, 8
medium/low)**. The fixes are in **PR #159** (`dear-agent-repo-trust`), one commit per slice, each
with a test that fails without it:

| Fixed | Finding |
|---|---|
| `verify` ran in the **control** container (DSN, git keys, forge token) over files the harness wrote | 1 |
| The shared worktree let the harness execute code in the control container via hooks/config | 2 |
| The repo allowlist could be ridden by a lookalike host (`evil.example/github.com/o/r`) | 3, 14, 16 |
| An email backend with no auth gate accepted mail from anyone (now fails closed) | 4 |
| IMAP `RFC822` marked mail seen, losing an ingress that never reached the queue | 5 |
| A task released by the lease sweep collided with its own finished Job and never retried | 6 |
| A reviewer `revise` waited out the 3600s handshake (the sidecar had exited after `--once`) | 7 |
| Approval redemption burned the single-use token without releasing the task | 9 |
| The environment descriptor was detected before the repository was cloned | 10 |
| AgentMail turned a transient fetch failure into an "empty message" that got acked | 11 |
| The decision log propagated a database error out of the decider | 17 |
| Docs claimed a boundary the code no longer had (and vice versa) | 19 |

The handshake was rewritten with it: the harness container serves until the control container
writes a shutdown marker (`finally`, plus an idle timeout), and requests/results are written
atomically with a sequence number.

**Deferred, deliberately** (each is a design change, not a patch):

- **8** Post-publish crashes are not reconciled: a PR can exist while the task is retried.
- **12** Rate limiting is process-local, so a multi-replica deployment cannot bound a sender.
- **13** Completion notification is outside the durable state transition (no outbox).
- **18** Schema management has no versioned migration gate.

### Cluster state (`api-virt-daytwo-dev`, namespace `dear-agent`) — verified live 2026-10-15

The namespace **exists and is deployed** (the September teardown note is history, not state):
control plane + Postgres `StatefulSet`, the `dear-agent-poll` (every minute) and
`dear-agent-sweep` (every two minutes) CronJobs, the **two-container** runner template, and the
email/the webhook ingress. Verified tonight, end to end:

- mail/webhook → PostgreSQL queue → sweep → runner Job (control + harness) → commit/push →
  **draft PR** (`dear-agent-lab-python` #10), with the `verify` gate executing in the harness
  container. That run used a deterministic harness because of the model balance below; it proves
  the plumbing, not the model.
- `scripts/demo.sh preflight` and `scripts/verify-openshift.sh` (preflight) both pass against it.

**Deployed images**: `quay.io/dear-agent/dear-agent:reviewfixes` and
`:harness-reviewfixes` (built from PR #159 so the demo can run the fixed code before it lands).
Once the PR is merged, CI publishes `:dev` and `:harness-dev`; switch back with

```sh
oc -n dear-agent set image deploy/dear-agent-control-plane control-plane=quay.io/dear-agent/dear-agent:dev
oc -n dear-agent set image cronjob/dear-agent-poll poll=quay.io/dear-agent/dear-agent:dev
oc -n dear-agent set image cronjob/dear-agent-sweep sweep=quay.io/dear-agent/dear-agent:dev
oc -n dear-agent patch cm dear-agent-config --type merge -p '{"data":{"DEAR_AGENT_RUNNER_IMAGE":"quay.io/dear-agent/dear-agent:dev","DEAR_AGENT_HARNESS_IMAGE":"quay.io/dear-agent/dear-agent:harness-dev"}}'
```

**Blocker for the live demo**: the **OpenRouter balance is negative** (the preflight reads
`-$0.12`). A run dies mid-way with an opaque `Unexpected server error`. Top the account up (a few
dollars is plenty), or point `DEAR_AGENT_MODEL` at a provider/model with credit. Two OpenCode
defaults are already handled in the `dear-agent-model` secret so that a funded key works:

- `small_model` set to the text model: the default is `google/gemini-nano-banana-2.1`, an *image*
  model, which OpenRouter refuses below $1.
- `limit.output` capped at 6000: OpenCode asks for up to 32k output tokens, which a low-limit key
  cannot afford.

**Inbox gate**: `DEAR_AGENT_ALLOWED_SENDERS=ricardo.arguello@gmail.com` +
`DEAR_AGENT_AUTH_DOMAINS=gmail.com` are set, so only that sender is accepted. To let an audience
send their own mail, add their addresses or set `DEAR_AGENT_ALLOW_UNAUTHENTICATED_INBOUND=true`.

### Demo

- [`docs/demo.md`](docs/demo.md) — the runbook (scenario, timings, failure table, cleanup).
- `scripts/demo.sh preflight|enqueue|poll|sweep|watch|run|reset` — drives the cluster and
  triggers the CronJobs by hand so the audience waits for the agent, not a timer.
- `scripts/demo_console.py` — the read-only live console (`http://127.0.0.1:8765`): the mail as
  the agent received it, the pipeline lighting up from the event log, and the credential-free
  harness container's output.

## Known gaps (for review)

- The four deferred review findings above.
- **Harness injection**: OpenCode uses a portable bundle; Claude Code and Codex use
  `npm install -g …` (`HarnessInfo.install`), so the environment image must provide `npm`.
- **Forge**: GitLab uses the `Draft:` title prefix (assumption); Bitbucket Server is unsupported.
  The opt-in single-container runner exposes the write key and forge token to the harness
  (ADR 0007 residual).
- **Bundle supply chain**: the harness bundle copies binaries from a harness image; the shim is
  injected on `PATH` ahead of the worktree.
- `docs/reviews/REVIEW-BRIEF.md` is a local artifact (untracked, see `.gitignore`).

## End-to-end test recipe (local, ADR 0008 session)

- Launch a Postgres: `scripts/dev-postgres.sh up` (creds `dear-agent`/`dear-agent`); the `memory`
  queue cannot be shared across `task enqueue` and `run` (separate processes).
- Env: `DEAR_AGENT_QUEUE=postgres`, `DEAR_AGENT_DATABASE_URL=...`,
  `DEAR_AGENT_HARNESS=opencode`, `DEAR_AGENT_ISOLATION=podman`,
  `DEAR_AGENT_HARNESS_MOUNTS='~/.local/share/opencode/auth.json:~/.local/share/opencode/auth.json:ro'`,
  `DEAR_AGENT_HARNESS_CONTAINER_SELINUX=disable` (SELinux host), `DEAR_AGENT_VERIFY_ALLOW=<cmd>`.
- Example tasks: Python (`add` subtracts, `is_even` inverted), Terraform (`var.aws_region`),
  Ansible (task-name typo, missing `git` package) in the `dear-agent-lab-*` repositories.
- `dear-agent task enqueue "<instructions>" --repo <url> --verify "<cmd>"`, then
  `dear-agent run --repo /tmp/<x> <task-id>`; it pushes `dear-agent/<slug>` and opens a draft PR.
- Against the cluster, prefer `docs/demo.md` (it triggers the CronJobs instead of waiting).

## Configuration (see `.env`, never committed)

- State: `DEAR_AGENT_QUEUE=memory|postgres` (default `memory`), `DEAR_AGENT_DATABASE_URL`.
- Transport: `DEAR_AGENT_BACKEND=memory|jmap|imap|agentmail`. AgentMail: `AGENTMAIL_API_TOKEN`,
  `DEAR_AGENT_MAILBOX`, `AGENTMAIL_BASE_URL`. JMAP: `FASTMAIL_API_TOKEN`, `FASTMAIL_ACCOUNT_ID`,
  `FASTMAIL_SESSION_URL`, `DEAR_AGENT_MAILBOX`. IMAP/SMTP: `DEAR_AGENT_IMAP_HOST|PORT|USER|PASSWORD|SSL|MAILBOX|DONE_MAILBOX`,
  `DEAR_AGENT_SMTP_HOST|PORT|USER|PASSWORD|STARTTLS|SSL|FROM`.
- Inbound auth: `DEAR_AGENT_AUTH_DOMAINS` (DMARC-aligned domains), `DEAR_AGENT_AUTH_MECHANISMS`
  (default `dmarc`), `DEAR_AGENT_AUTH_SERV_ID` (default `messagingengine.com`),
  `DEAR_AGENT_ALLOWED_SENDERS` (addresses/`@domain`). **An email backend refuses to start with
  neither**, unless `DEAR_AGENT_ALLOW_UNAUTHENTICATED_INBOUND=true` is set explicitly (see
  `auth.build_email_gate`).
- Inbound rate limit: `DEAR_AGENT_RATE_LIMIT` (per sender per minute, default `60`), process-local.
- Repo allowlist: `DEAR_AGENT_REQUIRE_REPO_POLICY` (default `true` for webhook/email ingress) and
  `DEAR_AGENT_POLICY_DIR`; `DEAR_AGENT_GIT_HOST` (the trusted host a hostless policy pattern may
  match; default `github.com`).
- HTTP entrypoint: `DEAR_AGENT_HOST`, `DEAR_AGENT_PORT`, `DEAR_AGENT_MAX_BODY_BYTES`,
  `DEAR_AGENT_HTTP_TIMEOUT`, `DEAR_AGENT_HTTP_MAX_WORKERS`.
- Reliability: `DEAR_AGENT_ERROR_BUDGET_FAILURES` (0 = off), `DEAR_AGENT_ERROR_BUDGET_WINDOW`,
  `DEAR_AGENT_MAX_ATTEMPTS` (default 3), `DEAR_AGENT_MAX_RUNNING` (health threshold).
- Sandbox: `DEAR_AGENT_SANDBOX=bwrap|none`, `DEAR_AGENT_SANDBOX_NETWORK|READABLE|WRITABLE|ENV`.
  `$HOME` is never mounted. Isolation: `DEAR_AGENT_ISOLATION=bwrap|podman`, with
  `DEAR_AGENT_HARNESS_IMAGE`, `DEAR_AGENT_HARNESS_MOUNTS`, `DEAR_AGENT_HARNESS_CONTAINER_*`.
- Runner/harness: `DEAR_AGENT_HARNESS=opencode|claude|codex|auto|command`,
  `DEAR_AGENT_HARNESS_BINARY|COMMAND|ENV|DEFAULT`, `DEAR_AGENT_HARNESSES`,
  `DEAR_AGENT_HARNESS_TIMEOUT` (the control's wait for the harness container, default 3600),
  `DEAR_AGENT_HARNESS_IDLE_TIMEOUT` (how long the harness container keeps serving, default 900),
  `DEAR_AGENT_HARNESS_DIR` (set by the two-container template), `DEAR_AGENT_VERIFY_ALLOW`.
- Harness bundle (ADR 0008): `DEAR_AGENT_HARNESS_BUNDLE`, `_BUNDLE_IMAGE`, `_BUNDLE_CACHE`.
- Environment build: `DEAR_AGENT_BUILD_ENVIRONMENT`, `DEAR_AGENT_ENVIRONMENT_IMAGE`,
  `DEAR_AGENT_HARNESS_FEATURE`.
- Deploy: `DEAR_AGENT_RUNNER_TEMPLATE_CONFIGMAP`, `DEAR_AGENT_RUNNER_IMAGE`,
  `DEAR_AGENT_HARNESS_IMAGE`, `DEAR_AGENT_WORKTREES_ROOT`, `DEAR_AGENT_GIT_PUSH_KEY`,
  `DEAR_AGENT_RUNNER_BACKEND` + `DEAR_AGENT_RECIPIENT` (outbound status mail).
- Forge (ADR 0009): `DEAR_AGENT_FORGE=auto|github|gitlab|gitea|bitbucket|gh`; tokens `GH_TOKEN`/
  `GITHUB_TOKEN`, `GITLAB_TOKEN`, `GITEA_TOKEN`, `BITBUCKET_TOKEN`+`BITBUCKET_USERNAME`/`_EMAIL`.
- Adversarial review (ADR 0010): `DEAR_AGENT_REVIEWER=none|openai-compat`,
  `_BASE_URL`, `_MODEL`, `_API_KEY`, `_TIMEOUT`, `DEAR_AGENT_DEBATE_ROUNDS`.
- Decision: `DEAR_AGENT_DECIDER=rules|jev|openai-compat|none`, `_MODEL`, `_ENDPOINT`, `_BASE_URL`,
  `_THRESHOLD`, `_LOG` (`<path>` or `postgres`), `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`.
- Approvals: `DEAR_AGENT_APPROVALS_FILE` (file fallback when not using Postgres).

## History (short)

M0–M9 are done; [`docs/roadmap.md`](docs/roadmap.md) has the per-slice detail. The project was
renamed **Herald → Dear Agent** and moved to the `dearagent-dev` org (PR #102): package
`dear_agent`, CLI `dear-agent`, env `DEAR_AGENT_*`, tables `dear_agent_*`. Deploy: Quay image
`quay.io/dear-agent/dear-agent` published from CI, domain `dearagent.dev` on GitHub Pages, branch
protection requiring `test (3.12)`, `test (3.13)`, `image`, `secrets`.
