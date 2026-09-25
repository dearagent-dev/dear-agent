# Getting started (local MVP)

This walks through running Dear Agent on one machine: PostgreSQL for durable state, a harness
(OpenCode) doing the coding, and Git/GitHub for the deliverable. It is the fastest way to see
the whole pipeline work before deploying to Kubernetes.

## What you need

- **Python 3.12+** and this repo (`pip install -e ".[dev]"`).
- **podman** (or Docker) for the local database.
- **A harness** on your `PATH`: `opencode` (default), `claude`, or `codex`. For a local model,
  point OpenCode at it with `--model provider/model`.
- **git** and an authenticated **`gh`** (Dear Agent opens a draft PR through the GitHub CLI).
- Credentials only for the transport you use (see [security.md](security.md)). Nothing secret
  is needed for a fully local run with `dear-agent task enqueue`.

## 1. Start the database

```sh
scripts/dev-postgres.sh up     # prints DEAR_AGENT_DATABASE_URL; keeps data in a named volume
```

Export what it prints:

```sh
export DEAR_AGENT_QUEUE=postgres
export DEAR_AGENT_DATABASE_URL='postgresql://dear-agent:dear-agent@127.0.0.1:5432/dear-agent'
```

The schema is created automatically at startup; there is no separate migration step.

## 2. Choose the harness

```sh
export DEAR_AGENT_HARNESS=opencode           # or claude | codex | auto | command
# export DEAR_AGENT_HARNESS_BINARY=/path/to/opencode   # optional override
export DEAR_AGENT_SANDBOX=bwrap              # default; falls back to no sandbox if bwrap is absent
# export DEAR_AGENT_ISOLATION=podman         # run the harness in its own image (see ADR 0006)
# export DEAR_AGENT_HARNESS_IMAGE=...        # required for a harness without a default image (Codex)
```

Only harnesses that accept a model (OpenCode) are passed `--model`; Claude Code and Codex use
their own subscription. `DEAR_AGENT_HARNESS=auto` picks the first harness found on `PATH`.

With `DEAR_AGENT_ISOLATION=podman` the harness runs in its per-harness container image
(OpenCode defaults to `ghcr.io/anomalyco/opencode:latest`) with the worktree at `/work`;
credentials are mounted read-only (`DEAR_AGENT_HARNESS_MOUNTS` overrides). See
[ADR 0006](decisions/0006-container-isolation.md). The run is **one session** (ADR 0008): the
harness and the `verify` gate execute in the same container, so anything the harness installs
(a language runtime, `make`) is visible to the gate.

### The execution environment (ADR 0008)

Dear Agent is language-agnostic, so it does not bake a toolchain: it takes the environment from
the repository and injects the harness into it.

- **Descriptor.** `detect()` reads, in order, `.devcontainer/devcontainer.json` (a Dev
  Container), an Ansible `execution-environment.yml`, a `Containerfile`/`Dockerfile`, or
  `mise.toml`/`.tool-versions`. A declared **image** becomes the session image;
  `DEAR_AGENT_HARNESS_IMAGE` overrides it. `build`/`Containerfile` recipes are detected but not
  built yet (next slice).
- **Harness injection.** An environment image does not contain the harness, so Dear Agent either
  mounts a portable **bundle** (OpenCode today: a shim on `PATH` runs the bundled binary through
  its own loader) or **bootstraps** it with the harness's install recipe (Claude Code and Codex
  via `npm install -g …`; the image must provide `npm`). Set `DEAR_AGENT_HARNESS_BUNDLE=true` to
  force the bundle, `DEAR_AGENT_HARNESS_BUNDLE_IMAGE` for the source image, and
  `DEAR_AGENT_HARNESS_BUNDLE_CACHE` for its cache. The bundle mount is relabeled on SELinux and
  the session gives the harness a writable `HOME` on a tmpfs, so it works in an image with an
  unwritable home (e.g. UBI).
- **Provisioning vs prebuild.** In the fallback (no descriptor) the harness installs the
  toolchain itself; the reproducible path is to compose a Dev Container **Feature** (or a
  `Containerfile`) with the harness and prebuild the image (next slice).

On an SELinux host (Fedora/RHEL) the worktree is relabelled with `:Z` automatically
(`DEAR_AGENT_HARNESS_CONTAINER_SELINUX=auto`, the default). Set it to `Z` to relabel the credential
mounts too, `disable` to pass `--security-opt label=disable`, or `none` to never touch labels.

The container also drops all capabilities (`--cap-drop ALL`), caps processes (`--pids-limit 512`),
and refuses privilege escalation by default; set `DEAR_AGENT_HARNESS_CONTAINER_READONLY=true` for a
read-only rootfs with a `/tmp` tmpfs.

## 3. Enqueue a task

The email path is [below](#the-email-path); for a quick run you can enqueue directly:

```sh
dear-agent task enqueue "fix the typo in the CLI help and add a test" \
  --repo git@github.com:you/your-repo.git \
  --model deepseek/deepseek-v4.1-flash
```

This stores the task and its parsed spec in PostgreSQL. `dear-agent task ls` lists queued tasks.

## 4. Run it

```sh
dear-agent run --repo /tmp/dear-agent-source \
  --model deepseek/deepseek-v4.1-flash \
  <task-id>
```

Omit `<task-id>` to run the oldest queued task (a local stand-in for the Kubernetes sweep):

```sh
dear-agent run --repo /tmp/dear-agent-source
```

`--repo` is a local directory Dear Agent clones/uses read-only; the harness runs in an isolated
worktree under `DEAR_AGENT_WORKTREES_ROOT` (default: a temp directory locally, `/work` in the
cluster). On success Dear Agent commits, pushes an `dear-agent/<slug>` branch and opens a **draft
PR**, then marks the task `done`. Agents never write `main`; a human lands the PR. `dear-agent
run` exits non-zero when the task fails, so a runner Job reflects the outcome.

Add `--recipient you@example.com` (with a JMAP transport) to get status by email.

```sh
dear-agent task show <task-id>   # state, attempts, lease
```

## How the PR is opened (forge)

The harness never opens the PR and never holds a push or forge credential (ADR 0007): the
control process commits, pushes and opens the draft PR after the harness exits. The PR/MR is
created through the forge **REST API** (ADR 0009), selected from the repository remote host:

```sh
export DEAR_AGENT_FORGE=auto                  # default: pick by remote host
# export DEAR_AGENT_FORGE=github|gitlab|gitea|gh
export GH_TOKEN=...                           # or GITHUB_TOKEN / GITLAB_TOKEN / GITEA_TOKEN
# export DEAR_AGENT_GIT_PUSH_KEY=/path/to/write-key   # a write key used only for the push
```

`DEAR_AGENT_FORGE=gh` keeps the legacy `gh` CLI (which must be installed); the API forges need
no binary. The clone uses the read credential; `DEAR_AGENT_GIT_PUSH_KEY` pins a scoped write key
for the push (ADR 0003).

## The email path

Dear Agent is email-first. With Fastmail JMAP configured:

```sh
export DEAR_AGENT_BACKEND=jmap               # the transport; the queue is still Postgres
export FASTMAIL_API_TOKEN='<your Fastmail API token>'
export FASTMAIL_ACCOUNT_ID='<your account id>'   # optional; discovered from the session
export DEAR_AGENT_RECIPIENT='you@example.com'        # where status/approvals are threaded back
dear-agent-http                              # webhook at POST /inbound (HMAC), or:
dear-agent listen                            # ingest on JMAP push events
```

Then mail a task: a `repo: <url>` line plus instructions. Normalization rejects attachments
and missing repos with a threaded explanation; re-delivery is a no-op (dedupe on `Message-ID`).

## Operating without Kubernetes

In production a `CronJob` sweep dispatches one Job per task. Locally, skip it and run tasks
yourself with `dear-agent run` as above (without an id it atomically claims the oldest queued
task, so two `dear-agent run` loops can share the queue safely). Everything else — the queue,
approvals, the decision log — is the same PostgreSQL database.

A task that keeps crashing is left `failed` after `DEAR_AGENT_MAX_ATTEMPTS` (default 3) instead of
retrying forever; put it back with `dear-agent task requeue <id>`.

## Decision layer (optional)

Routing/gates can use a decider (Jev, or any OpenAI-compatible endpoint):

```sh
export DEAR_AGENT_DECIDER=jev                 # or openai-compat, or rules (default)
export TYPESAFE_API_KEY='<your TypeSafe key>'
dear-agent decide "audit the auth module for timing side-channels"
```

With no decider configured, Dear Agent uses the deterministic rules. See
[providers.md](providers.md) and [ADR 0004](decisions/0004-decision-model.md).

## When the queue is empty (idle)

Dear Agent can propose its own work from recent repository activity (recent commits, TODO/FIXME
markers). Proposals are **approval-gated**: they are parked in the `action` state and never
run until a human releases them.

```sh
dear-agent idle --repo /path/to/checkout --max 1
dear-agent approval pending                 # pending requests (tokens redacted)
dear-agent approval pending --show-tokens   # reveal the single-use token
dear-agent approval approve <token>         # releases the proposal to run (queued)
```

Use `--no-gate` to enqueue proposals as ordinary runnable tasks instead (trusted repos only).

## Where to go next

- [architecture.md](architecture.md) — data flow and components.
- [queue.md](queue.md) — the task model and states.
- [deploy/README.md](../deploy/README.md) — Kubernetes/OpenShift.
- [roadmap.md](roadmap.md) — what is done and what is next.
