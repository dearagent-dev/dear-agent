# TASKS.md — current work

Short-lived, per-slice work list. `AGENTS.md` is the durable contract; this file is the
current state. Keep it short and delete finished items.

## In progress

_None — M8 is complete on branch `dear-agent-m8-postgres-deploy` (PR #55)._

### M8 — Durable state in PostgreSQL (ADR 0005) — done

The mailbox is ingress; PostgreSQL is the durable queue (tasks, state, approvals, decision
log). See [ADR 0005](docs/decisions/0005-state-store.md).

- [x] **M8.1** Deployment: `deploy/components/postgresql/` (PG18 on UBI 9 `StatefulSet` +
  `Service` + `PVC`), `scripts/dev-postgres.sh` (podman), manifest tests.
- [x] **M8.2** `PostgresQueue` + schema; `JmapQueue` retired; queue backend decoupled from
  the transport (`DEAR_AGENT_QUEUE`, `DEAR_AGENT_DATABASE_URL`); CI Postgres service.
- [x] **M8.3** `PostgresApprovalStore` and `PostgresDecisionLog`; `dear-agent/db.py` composes the
  schema. File stores remain a single-process fallback.
- [x] **M8.4** Sweep/runner on the database; the parsed `TaskSpec` is persisted on the task,
  so a runner needs no mailbox. No backfill needed at this scale.
- [x] **HarnessCatalog/HarnessInfo** (`runners/catalog.py`): `accepts_model`, `auto`/
  `local-agent`, model passed only to harnesses that accept one.
- [x] **MVP local path**: `dear-agent task enqueue`, persisted specs, and
  [docs/getting-started.md](docs/getting-started.md).

### Reliability + ops (same branch)

- [x] Publish failures (`git`/forge) fail the task cleanly (`PUBLISH_FAILED`) instead of
  leaving it `running`; `DEAR_AGENT_MAX_ATTEMPTS` stops crash loops.
- [x] `Queue.claim_next` with `FOR UPDATE SKIP LOCKED`; `dear-agent run` (no id) claims atomically.
- [x] `dear-agent health`, `dear-agent task requeue`, `dear-agent approval pending`.
- [x] Idle proposals are idempotent across ticks (deterministic transport id).
- [x] Database `NetworkPolicy`; CI builds the UBI 9 image and smoke-checks the CLI.

### Inbound approval gate (branch `dear-agent-inbound-human-gate`) — done

- [x] Inbound tasks whose decider verdict says a human should look are parked in `action`
  and released by a `run` approval, exactly like suspicious messages
  (`HumanGate`/`decision_needs_human`, `DEAR_AGENT_DECIDER`, default `rules`). Fail-open: no
  decider means no gate. The draft PR remains the landing gate.

## Next

- **PITR**: continuous WAL archiving to object storage. (Logical backups landed
  (`deploy/components/backup/`, a daily `pg_dump` keeping the last seven); connection
  resilience landed; the Postgres component is verified live on OpenShift.)

## Configuration (see .env, never committed)

- State: `DEAR_AGENT_QUEUE=memory|postgres` (default `memory`), `DEAR_AGENT_DATABASE_URL`.
- Transport: `DEAR_AGENT_BACKEND=memory|jmap|imap|agentmail`. AgentMail: `AGENTMAIL_API_TOKEN`,
  `DEAR_AGENT_MAILBOX` (inbox id; unset discovers it), `AGENTMAIL_BASE_URL`. JMAP: `FASTMAIL_API_TOKEN`,
  `FASTMAIL_ACCOUNT_ID`, `DEAR_AGENT_MAILBOX`. IMAP/SMTP: `DEAR_AGENT_IMAP_HOST|PORT|USER|PASSWORD|SSL|MAILBOX|DONE_MAILBOX`,
  `DEAR_AGENT_SMTP_HOST|PORT|USER|PASSWORD|STARTTLS|SSL|FROM`.
- Inbound auth: `DEAR_AGENT_AUTH_DOMAINS` (DMARC-aligned domains), `DEAR_AGENT_AUTH_MECHANISMS`
  (default `dmarc`), `DEAR_AGENT_AUTH_SERV_ID` (default `messagingengine.com`),
  `DEAR_AGENT_ALLOWED_SENDERS` (addresses/`@domain`; unset = accept any sender).
- Inbound rate limit: `DEAR_AGENT_RATE_LIMIT` (messages per sender per minute, default `60`),
  applied to both the webhook and the email path.
- Repo allowlist: `DEAR_AGENT_REQUIRE_REPO_POLICY` (default `true` for webhook/email ingress:
  with no project policy, every repo is refused; set `false` for an open deployment).
- HTTP entrypoint: `DEAR_AGENT_HOST`, `DEAR_AGENT_PORT`, `DEAR_AGENT_MAX_BODY_BYTES`
  (default 1 MiB), `DEAR_AGENT_HTTP_TIMEOUT` (idle socket timeout in seconds, default `15`),
  `DEAR_AGENT_HTTP_MAX_WORKERS` (concurrency cap, default `32`).
- Reliability: `DEAR_AGENT_ERROR_BUDGET_FAILURES` (0 = off), `DEAR_AGENT_ERROR_BUDGET_WINDOW`
  (seconds, default 3600). Events are in `dear_agent_event`; `dear-agent task events <id>`.
- Sandbox: `DEAR_AGENT_SANDBOX=bwrap|none`, `DEAR_AGENT_SANDBOX_NETWORK` (default false),
  `DEAR_AGENT_SANDBOX_READABLE`, `DEAR_AGENT_SANDBOX_WRITABLE`, `DEAR_AGENT_SANDBOX_ENV`. `$HOME` is
  never mounted (credentials stay invisible).
- Isolation: `DEAR_AGENT_ISOLATION=bwrap|podman` (default `bwrap`). Podman runs each harness in
  its per-harness image: `DEAR_AGENT_HARNESS_IMAGE` (required for a harness without a default
  image, e.g. Codex), `DEAR_AGENT_HARNESS_MOUNTS` (`host:container[:ro|rw]`, `~` = host/image
  home), `DEAR_AGENT_HARNESS_CONTAINER_ENV`, `DEAR_AGENT_HARNESS_CONTAINER_NETWORK` (default `host`),
  `DEAR_AGENT_HARNESS_CONTAINER_HOME` (default `/root`), `DEAR_AGENT_HARNESS_CONTAINER_WORKDIR`
  (default `/work`), `DEAR_AGENT_HARNESS_CONTAINER_USERNS=keep-id`, `DEAR_AGENT_HARNESS_CONTAINER_ARGS`,
  `DEAR_AGENT_HARNESS_CONTAINER_SELINUX=auto|z|Z|disable|none` (default `auto`; `auto` relabels
  the worktree with `:Z` on SELinux hosts), `DEAR_AGENT_CONTAINER_BINARY` (default `podman`).
  Hardening: `DEAR_AGENT_HARNESS_CONTAINER_CAP_DROP` (default `true`, `--cap-drop ALL`),
  `DEAR_AGENT_HARNESS_CONTAINER_PIDS` (default `512`; `none` to omit), and
  `DEAR_AGENT_HARNESS_CONTAINER_READONLY=true` for a read-only rootfs with a `/tmp` tmpfs.
- Harness: `DEAR_AGENT_HARNESS=opencode|claude|codex|auto|command`, `DEAR_AGENT_HARNESS_BINARY`,
  `DEAR_AGENT_HARNESS_COMMAND`, `DEAR_AGENT_HARNESSES`, `DEAR_AGENT_HARNESS_DEFAULT`, `DEAR_AGENT_SANDBOX`,
  `DEAR_AGENT_MAX_ATTEMPTS` (default 3), `DEAR_AGENT_VERIFY_ALLOW` (comma/semicolon-separated
  `verify:` commands, matched exactly and run under the sandbox).
- Decision: `DEAR_AGENT_DECIDER=rules|jev|openai-compat|none`, `DEAR_AGENT_DECIDER_MODEL`,
  `DEAR_AGENT_DECIDER_ENDPOINT`, `DEAR_AGENT_DECIDER_BASE_URL`, `DEAR_AGENT_DECIDER_THRESHOLD`,
  `DEAR_AGENT_DECIDER_LOG` (`<path>` or `postgres`), `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`.
- Approvals: `DEAR_AGENT_APPROVALS_FILE` (file fallback when not using Postgres).
