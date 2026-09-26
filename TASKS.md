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
4. **Execution environment + forge** — done and merged (ADR 0008/0009): one session per task,
   injectable harness, repository-declared environment, draft PR through the forge REST API.
   Open gaps in [Known gaps](#known-gaps-for-review).
5. **PITR** — deferred (see Later); not needed at this scale.
6. **Remaining after the M9 session** — pick these up next; none is a self-contained code slice:
   - **M9.7** live in-cluster PR path (runbook + `scripts/verify-openshift.sh`; needs a cluster
     and provisioned secrets).
   - **Bundle multi-harness**: Claude Code / Codex are not single binaries (Node/npm), so they
     need a different packaging than the OpenCode musl bundle.
   - **Credential isolation in the single-container runner**: the write key and the forge token
     are visible to the harness (ADR 0007 residual); the sidecar is the isolated path.
   - Also closed this session: Bitbucket forge (ADR 0009), descriptor path-traversal hardening,
     bundle robustness, self-hosted decider docs, adversarial review (ADR 0010).

### SNO cluster teardown — 2026-09-25

The `dear-agent` namespace was **deleted** from the SNO cluster (`api-virt-daytwo-dev`) so a
course can use it. This removed the control-plane Deployment, the Postgres `StatefulSet` with
its **PVCs** (queue, decisions and backups data) and the **Secrets**. The images remain in Quay
(`quay.io/dear-agent/dear-agent`, `quay.io/dear-agent/harness`).

**Redeploy from scratch** before the next M9.7 check:

1. `oc new-project dear-agent` (or the target namespace).
2. Recreate the Secrets out of band — `dear-agent-postgres`, `dear-agent-git-read`,
   `dear-agent-git-push`, `dear-agent-forge`, `dear-agent-inbound`, `dear-agent-model`
   (see [`docs/verify-openshift.md`](docs/verify-openshift.md) §1; the values are not in git).
3. `scripts/verify-openshift.sh --namespace dear-agent --overlay dev --apply`.
4. Enable the sidecar template and a real `DEAR_AGENT_MODEL` in `deploy/base/config.yaml`, then
   re-run the M9.7 verification. State (queue/decisions) starts empty.

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

### Execution environments (ADR 0008) — done

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
- [x] `environment/builder.py`: `EnvironmentBuilder` builds a repository's environment image —
  `podman build` for a `Containerfile`, `devcontainer build` (with `--additional-features`) for a
  Dev Container, adding a harness Feature (`DEAR_AGENT_HARNESS_FEATURE`) so the harness is baked
  in. `DEAR_AGENT_BUILD_ENVIRONMENT=true` builds and uses it; a baked harness skips the bundle.
- [x] `environment/descriptor.py`: resolve a repository's environment descriptor in precedence
  order — Dev Container (`image` or `build`, JSONC), Ansible EE (`images.base_image.name`),
  `Containerfile`/`Dockerfile`, `mise`/`.tool-versions`, else fallback. `build_worker` uses a
  declared **image** for the session and injects the harness automatically (OpenCode); an
  explicit `DEAR_AGENT_HARNESS_IMAGE` still wins.
- [x] Tests + a live proof (session persistence; bundle injection into a UBI image).
- [ ] **Next**: verify the in-cluster PR path live (sidecar runner + secrets); see
  [Known gaps](#known-gaps-for-review).

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

## Known gaps (for review)

The execution environment ([ADR 0008](docs/decisions/0008-execution-environment.md)) and forge
([ADR 0009](docs/decisions/0009-forge-api.md)) slices are on `main`; these are the deliberate,
still-open gaps:

- **Harness injection**: OpenCode uses a portable **bundle**; Claude Code and Codex use
  **bootstrap** (`HarnessInfo.install` → `ContainerSandbox.setup`, `npm install -g …`), so the
  environment image must provide `npm`. The bundle has no `ldd` auto-discovery; the SELinux
  relabel and the writable home are handled by the session.
- **Forge**: GitLab uses the `Draft:` title prefix (assumption); Bitbucket Server (self-hosted)
  is not supported. The two-container runner is now the default, so this is isolated; the
  opt-in single-container runner (`DEAR_AGENT_RUNNER_TEMPLATE_CONFIGMAP=dear-agent-runner-template`)
  still exposes the write key and forge token to the harness (ADR 0007 residual).
- **Ops**: the sidecar harness image is published from `deploy/harness/Containerfile`, but the
  in-cluster PR path has not been verified live. Runbook + preflight/enqueue script ready:
  [`docs/verify-openshift.md`](docs/verify-openshift.md), `scripts/verify-openshift.sh`.

## Later

- **PITR** (continuous WAL archiving to object storage): not needed yet. The daily `pg_dump`
  (`deploy/components/backup/`, retention 7) plus connection resilience is enough at this scale;
  revisit before the data becomes irreplaceable.

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
  `DEAR_AGENT_ISOLATION=podman`; the bundle mount is relabeled on SELinux and the harness gets a
  writable tmpfs `HOME` automatically.
- Environment build (ADR 0008): `DEAR_AGENT_BUILD_ENVIRONMENT=true` builds the repository-declared
  environment image (Containerfile via `podman build`, Dev Container via `devcontainer build`);
  `DEAR_AGENT_ENVIRONMENT_IMAGE` (default `dear-agent-env:latest`) names it and
  `DEAR_AGENT_HARNESS_FEATURE` adds a harness Dev Container Feature so the harness is baked in.
- Forge (ADR 0009): `DEAR_AGENT_FORGE=auto|github|gitlab|gitea|bitbucket|gh` (default `auto`
  opens the PR/MR through the forge REST API, chosen from the repository remote host; `gh` keeps
  the CLI). Tokens by provider: `GH_TOKEN`/`GITHUB_TOKEN`, `GITLAB_TOKEN`, `GITEA_TOKEN`,
  `BITBUCKET_TOKEN` + `BITBUCKET_USERNAME`/`BITBUCKET_EMAIL`. Environment
  descriptor (ADR 0008): a repo-declared Dev Container/EE/Containerfile image is used for the
  session; `DEAR_AGENT_HARNESS_IMAGE` overrides it.
- Git push key: `DEAR_AGENT_GIT_PUSH_KEY` (path to a write deploy key used only for the push;
  unset locally uses the ambient credential). The runner templates mount `dear-agent-git-read`
  and `dear-agent-git-push` and the `dear-agent-forge` secret.
- Adversarial review (ADR 0010): `DEAR_AGENT_REVIEWER=none|openai-compat` (default `none`);
  `DEAR_AGENT_REVIEWER_BASE_URL`, `_MODEL`, `_API_KEY` (falls back to `OPENROUTER_API_KEY`),
  `_TIMEOUT`, and `DEAR_AGENT_DEBATE_ROUNDS` (default `1`) bound the revisions.
- Decision: `DEAR_AGENT_DECIDER=rules|jev|openai-compat|none`, `DEAR_AGENT_DECIDER_MODEL`,
  `DEAR_AGENT_DECIDER_ENDPOINT`, `DEAR_AGENT_DECIDER_BASE_URL`, `DEAR_AGENT_DECIDER_THRESHOLD`,
  `DEAR_AGENT_DECIDER_LOG` (`<path>` or `postgres`), `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`.
- Approvals: `DEAR_AGENT_APPROVALS_FILE` (file fallback when not using Postgres).
