# TASKS.md — current work

Short-lived, per-slice work list. `AGENTS.md` is the durable contract; this file is the
current state. Keep it short and delete finished items.

## Session handoff (2026-09-24)

Context for the next session. The project was renamed **Herald → Dear Agent** and the
repository moved to the **`dearagent-dev`** org; every reference to the old name was purged.

### Done
- **Rename**: package `dear_agent`, CLI `dear-agent`, env `DEAR_AGENT_*`, DB tables
  `dear_agent_*`. Repo `dearagent-dev/dear-agent`. Example repos renamed to
  `dearagent-dev/dear-agent-lab-{terraform,ansible,python}`. `herald-*` branches (local+remote),
  the `herald-*` podman containers/images, the old Quay repo and the local directory were all
  removed. The local project directory is now `~/Documents/Projects/dear-agent`.
- **Security backlog (adversarial review) closed**: #72–#94 resolved; tracking #91 closed.
- **Deploy**: image `quay.io/dear-agent/dear-agent` published from CI
  (`.github/workflows/publish.yml`, Red Hat actions); domain `dearagent.dev` on GitHub Pages
  behind Cloudflare (Enforce HTTPS on); Renovate runs (`RENOVATE_TOKEN`).
- **Branch protection on `main`**: required checks `test (3.12)`, `test (3.13)`, `image`,
  `secrets` (strict, enforce admins). Never merge with red checks.
- **OpenShift**: namespace `dear-agent` deployed and verified live (Postgres `StatefulSet` +
  PVC, schema `dear_agent_*`, control-plane `/health` green, egress NetworkPolicy, and a
  daily `pg_dump` backup in `deploy/components/backup/`, verified).
- **AgentMail backend** (`DEAR_AGENT_BACKEND=agentmail`) implemented and smoke-tested live.

### Pending
1. **Example-repo READMEs** — done: PRs in `dear-agent-lab-{terraform,ansible,python}` #2.
2. **Historical PR titles** containing "herald" — done (#96, #60, #50, #47, #33, #32, #25;
   #102 is the rename PR itself and keeps its title).
3. **End-to-end test with complex tasks** — done, now with the ADR 0008 session
   (`DEAR_AGENT_ISOLATION=podman`): the harness installed its toolchain inside the session and
   the `verify` gate saw it. Python (`add`/`is_even` + `multiply`, `make test`), Terraform
   (`var.region`/`.logs` + `bucket_name`, `terraform fmt -check`), Ansible ("Instal"→"Install"
   + `git`, `ansible-playbook --syntax-check`) each opened a draft PR #3. Recipe below.
4. **PITR**: continuous WAL archiving to object storage (logical backups are done).

### End-to-end test recipe (local, ADR 0008 session)
- Launch a Postgres: `scripts/dev-postgres.sh up` (creds `dear-agent`/`dear-agent`); the
  `memory` queue cannot be shared across `task enqueue` and `run` (separate processes).
- Env: `DEAR_AGENT_QUEUE=postgres`, `DEAR_AGENT_DATABASE_URL=...`,
  `DEAR_AGENT_HARNESS=opencode`, `DEAR_AGENT_ISOLATION=podman`,
  `DEAR_AGENT_HARNESS_MOUNTS='~/.local/share/opencode/auth.json:~/.local/share/opencode/auth.json:ro'`,
  `DEAR_AGENT_HARNESS_CONTAINER_SELINUX=disable` (read the auth mount on an SELinux host),
  `DEAR_AGENT_VERIFY_ALLOW=<the exact verify command>`.
- The harness runs in a minimal Alpine image and **installs its own toolchain** (e.g.
  `apk add --no-cache python3 make`); because harness and verify share one session, the
  `verify` gate runs in the same container. Tools not in Alpine (Terraform) are downloaded into
  `/usr/local/bin`; the next slice (environment descriptor) removes that guesswork.
- Example tasks (intentional bugs to fix): Python (`add` subtracts, `is_even` inverted; add
  `multiply` + tests); Terraform (`var.aws_region` → `var.region`, `aws_s3_bucket.log` →
  `.logs`); Ansible (task name typo "Instal", missing `git` package).
- `dear-agent task enqueue "<instructions>" --repo git@github.com:dearagent-dev/dear-agent-lab-<x>.git --verify "<cmd>"`,
  then `dear-agent run --repo /tmp/<x> <task-id>`; it pushes an `dear-agent/<slug>` branch and
  opens a draft PR.

### Session caveat
The project directory was renamed while opencode was running, so the session's workspace root
points at the old `.../herald` path; restart opencode from `~/Documents/Projects/dear-agent`.

## In progress

### Execution environments (ADR 0008) — branch `dear-agent-execution-environment`

The harness and the `verify` gate now share **one environment session** (one container per
task), and the harness can be **injected** into an environment image that lacks it.

- [x] `ContainerSandbox` session lifecycle: first `wrap` starts a long-lived container
  (`podman run -d --entrypoint sleep … infinity`), later calls `podman exec` into it, `close`
  removes it. `build_wrapped_argv` stays pure for the per-command tests.
- [x] `TaskExecutor` owns the session (`close` in `finally`); `build_worker` resolves the
  sandbox once and shares it between the runner and the verifier (fixes the podman mismatch
  where the verifier silently got the bwrap sandbox).
- [x] `environment/bundle.py`: `HarnessBundle` materializes a portable OpenCode bundle (musl
  binary + loader + `libstdc++`/`libgcc`) and mounts it into the session, behind
  `DEAR_AGENT_HARNESS_BUNDLE=true`.
- [x] `environment/descriptor.py`: resolve a repository's environment descriptor in precedence
  order — Dev Container (`image` or `build`, JSONC), Ansible EE (`images.base_image.name`),
  `Containerfile`/`Dockerfile`, `mise`/`.tool-versions`, else fallback. `build_worker` uses a
  declared **image** for the session and injects the harness automatically (OpenCode); an
  explicit `DEAR_AGENT_HARNESS_IMAGE` still wins.
- [x] Tests + a live proof (session persistence; bundle injection into a UBI image).
- [ ] Next: the Dev Container **Feature/prebuild** path (compose `devcontainer.json` + a harness
  Feature into an image) and turning a `build`/`Containerfile` descriptor into an image.

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

- **Forge by API (no CLI), multi-forge.** Today `build_worker` hardcodes
  `GitPlane(forge=GhForge())`, and `GhForge` shells out to `gh pr create --draft`
  (`gitplane/gh.py`). That needs `gh` on `PATH` and a token, and only speaks GitHub. Decision:
  open the PR/MR through the forge **REST API** with a token, behind the existing `Forge`
  protocol. Tasks:
  - [ ] **API forge**: replace `GhForge` with a token-based API call
    (`POST /repos/{owner}/{repo}/pulls` with `"draft": true`, `Authorization: Bearer $TOKEN`),
    no `gh` dependency. Keep `gh` (or a token) only as an optional fallback.
  - [ ] **Forge selection**: pick the forge from the repo host (`github.com`, `gitlab.com`,
    self-hosted) and/or an explicit `DEAR_AGENT_FORGE=github|gitlab|gitea`, replacing the
    hardcoded `GhForge()`.
  - [ ] **GitLab/Gitea forges**: `GitLabForge` (`POST /projects/:id/merge_requests` with
    `draft`) and `GiteaForge`; each with its own token env (`GITLAB_TOKEN`, `GITEA_TOKEN`).
  - [ ] **Credential wiring**: pass the forge token to the runner Job (secret → env); note the
    runner template currently mounts only the **read** git key (`dear-agent-git-read`) and sets
    no forge token, so in-cluster PR creation does not work yet. Push needs a scoped **write**
    credential (ADR 0003).
  - [ ] Docs: extend [docs/decisions/0003-credentials.md](docs/decisions/0003-credentials.md)
    or add an ADR for the forge seam; update `deploy/` and `docs/security.md`.
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
- Harness bundle (ADR 0008): `DEAR_AGENT_HARNESS_BUNDLE=true` mounts a portable harness bundle
  into the session so an environment image without the harness can run it;
  `DEAR_AGENT_HARNESS_BUNDLE_IMAGE` (default: the harness image) and
  `DEAR_AGENT_HARNESS_BUNDLE_CACHE` (default `~/.cache/dear-agent/harness`). Requires
  `DEAR_AGENT_ISOLATION=podman`; on SELinux hosts set `DEAR_AGENT_HARNESS_CONTAINER_SELINUX=Z`.
- Decision: `DEAR_AGENT_DECIDER=rules|jev|openai-compat|none`, `DEAR_AGENT_DECIDER_MODEL`,
  `DEAR_AGENT_DECIDER_ENDPOINT`, `DEAR_AGENT_DECIDER_BASE_URL`, `DEAR_AGENT_DECIDER_THRESHOLD`,
  `DEAR_AGENT_DECIDER_LOG` (`<path>` or `postgres`), `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`.
- Approvals: `DEAR_AGENT_APPROVALS_FILE` (file fallback when not using Postgres).
