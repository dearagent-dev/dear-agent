# TASKS.md — current work

Short-lived, per-slice work list. `AGENTS.md` is the durable contract; this file is the
current state. Keep it short and delete finished items.

## In progress

_None — M8 is complete on branch `herald-m8-postgres-deploy` (PR #55)._

### M8 — Durable state in PostgreSQL (ADR 0005) — done

The mailbox is ingress; PostgreSQL is the durable queue (tasks, state, approvals, decision
log). See [ADR 0005](docs/decisions/0005-state-store.md).

- [x] **M8.1** Deployment: `deploy/components/postgresql/` (PG18 on UBI 9 `StatefulSet` +
  `Service` + `PVC`), `scripts/dev-postgres.sh` (podman), manifest tests.
- [x] **M8.2** `PostgresQueue` + schema; `JmapQueue` retired; queue backend decoupled from
  the transport (`HERALD_QUEUE`, `HERALD_DATABASE_URL`); CI Postgres service.
- [x] **M8.3** `PostgresApprovalStore` and `PostgresDecisionLog`; `herald/db.py` composes the
  schema. File stores remain a single-process fallback.
- [x] **M8.4** Sweep/runner on the database; the parsed `TaskSpec` is persisted on the task,
  so a runner needs no mailbox. No backfill needed at this scale.
- [x] **HarnessCatalog/HarnessInfo** (`runners/catalog.py`): `accepts_model`, `auto`/
  `local-agent`, model passed only to harnesses that accept one.
- [x] **MVP local path**: `herald task enqueue`, persisted specs, and
  [docs/getting-started.md](docs/getting-started.md).

### Reliability + ops (same branch)

- [x] Publish failures (`git`/forge) fail the task cleanly (`PUBLISH_FAILED`) instead of
  leaving it `running`; `HERALD_MAX_ATTEMPTS` stops crash loops.
- [x] `Queue.claim_next` with `FOR UPDATE SKIP LOCKED`; `herald run` (no id) claims atomically.
- [x] `herald health`, `herald task requeue`, `herald approval pending`.
- [x] Idle proposals are idempotent across ticks (deterministic transport id).
- [x] Database `NetworkPolicy`; CI builds the UBI 9 image and smoke-checks the CLI.

## Next

- **Gate high-risk inbound runs behind approval.** Idle proposals are already parked in
  `action` and released by a `run` approval; extend the same gate to inbound tasks whose
  decider verdict says a human should look (the draft PR remains the landing gate).
- **Connection resilience / PITR** for the long-lived control plane.
- Verify the Postgres component on a live OpenShift cluster (as M6 was verified).

## Configuration (see .env, never committed)

- State: `HERALD_QUEUE=memory|postgres` (default `memory`), `HERALD_DATABASE_URL`.
- Transport: `HERALD_BACKEND=memory|jmap`, `FASTMAIL_API_TOKEN`, `FASTMAIL_ACCOUNT_ID`.
- Harness: `HERALD_HARNESS=opencode|claude|codex|auto|command`, `HERALD_HARNESS_BINARY`,
  `HERALD_HARNESS_COMMAND`, `HERALD_HARNESSES`, `HERALD_HARNESS_DEFAULT`, `HERALD_SANDBOX`,
  `HERALD_MAX_ATTEMPTS` (default 3).
- Decision: `HERALD_DECIDER=rules|jev|openai-compat|none`, `HERALD_DECIDER_MODEL`,
  `HERALD_DECIDER_ENDPOINT`, `HERALD_DECIDER_BASE_URL`, `HERALD_DECIDER_THRESHOLD`,
  `HERALD_DECIDER_LOG` (`<path>` or `postgres`), `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`.
- Approvals: `HERALD_APPROVALS_FILE` (file fallback when not using Postgres).
