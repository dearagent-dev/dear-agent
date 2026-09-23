# Getting started (local MVP)

This walks through running Herald on one machine: PostgreSQL for durable state, a harness
(OpenCode) doing the coding, and Git/GitHub for the deliverable. It is the fastest way to see
the whole pipeline work before deploying to Kubernetes.

## What you need

- **Python 3.12+** and this repo (`pip install -e ".[dev]"`).
- **podman** (or Docker) for the local database.
- **A harness** on your `PATH`: `opencode` (default), `claude`, or `codex`. For a local model,
  point OpenCode at it with `--model provider/model`.
- **git** and an authenticated **`gh`** (Herald opens a draft PR through the GitHub CLI).
- Credentials only for the transport you use (see [security.md](security.md)). Nothing secret
  is needed for a fully local run with `herald task enqueue`.

## 1. Start the database

```sh
scripts/dev-postgres.sh up     # prints HERALD_DATABASE_URL; keeps data in a named volume
```

Export what it prints:

```sh
export HERALD_QUEUE=postgres
export HERALD_DATABASE_URL='postgresql://herald:herald@127.0.0.1:5432/herald'
```

The schema is created automatically at startup; there is no separate migration step.

## 2. Choose the harness

```sh
export HERALD_HARNESS=opencode           # or claude | codex | auto | command
# export HERALD_HARNESS_BINARY=/path/to/opencode   # optional override
export HERALD_SANDBOX=bwrap              # default; falls back to no sandbox if bwrap is absent
```

Only harnesses that accept a model (OpenCode) are passed `--model`; Claude Code and Codex use
their own subscription. `HERALD_HARNESS=auto` picks the first harness found on `PATH`.

## 3. Enqueue a task

The email path is [below](#the-email-path); for a quick run you can enqueue directly:

```sh
herald task enqueue "fix the typo in the CLI help and add a test" \
  --repo git@github.com:you/your-repo.git \
  --model deepseek/deepseek-v4.1-flash
```

This stores the task and its parsed spec in PostgreSQL. `herald task ls` lists queued tasks.

## 4. Run it

```sh
herald run --repo /tmp/herald-source \
  --model deepseek/deepseek-v4.1-flash \
  <task-id>
```

Omit `<task-id>` to run the oldest queued task (a local stand-in for the Kubernetes sweep):

```sh
herald run --repo /tmp/herald-source
```

`--repo` is a local directory Herald clones/uses read-only; the harness runs in an isolated
worktree. On success Herald commits, pushes an `herald/<slug>` branch and opens a **draft
PR**, then marks the task `done`. Agents never write `main`; a human lands the PR.

Add `--recipient you@example.com` (with a JMAP transport) to get status by email.

```sh
herald task show <task-id>   # state, attempts, lease
```

## The email path

Herald is email-first. With Fastmail JMAP configured:

```sh
export HERALD_BACKEND=jmap               # the transport; the queue is still Postgres
export FASTMAIL_API_TOKEN=... FASTMAIL_ACCOUNT_ID=...
herald-http                              # POST /inbound (HMAC), or:
herald listen                            # ingest on JMAP push events
```

Then mail a task: a `repo: <url>` line plus instructions. Normalization rejects attachments
and missing repos with a threaded explanation; re-delivery is a no-op (dedupe on `Message-ID`).

## Operating without Kubernetes

In production a `CronJob` sweep dispatches one Job per task. Locally, skip it and run tasks
yourself with `herald run` as above (without an id it atomically claims the oldest queued
task, so two `herald run` loops can share the queue safely). Everything else — the queue,
approvals, the decision log — is the same PostgreSQL database.

A task that keeps crashing is left `failed` after `HERALD_MAX_ATTEMPTS` (default 3) instead of
retrying forever; put it back with `herald task requeue <id>`.

## Decision layer (optional)

Routing/gates can use a decider (Jev, or any OpenAI-compatible endpoint):

```sh
export HERALD_DECIDER=jev TYPESAFE_API_KEY=...      # or openai-compat, or rules (default)
herald decide "audit the auth module for timing side-channels"
```

With no decider configured, Herald uses the deterministic rules. See
[providers.md](providers.md) and [ADR 0004](decisions/0004-decision-model.md).

## When the queue is empty (idle)

Herald can propose its own work from recent repository activity (recent commits, TODO/FIXME
markers). Proposals enter the queue through the normal path and are run like any task; they
are never executed directly:

```sh
herald idle --repo /path/to/checkout --max 1
herald task ls          # the proposal is a normal task
```

## Where to go next

- [architecture.md](architecture.md) — data flow and components.
- [queue.md](queue.md) — the task model and states.
- [deploy/README.md](../deploy/README.md) — Kubernetes/OpenShift.
- [roadmap.md](roadmap.md) — what is done and what is next.
