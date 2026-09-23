# TASKS.md — current work

Short-lived, per-slice work list. `AGENTS.md` is the durable contract; this file is the
current state. Keep it short and delete finished items.

## In progress

### M8 — Durable state in PostgreSQL (ADR 0005)

Branch: `herald-m8-postgres-deploy`.
Goal: the mailbox becomes **ingress**; PostgreSQL is the durable queue for tasks, state,
approvals and the decision log. Decision:
[ADR 0005](docs/decisions/0005-state-store.md) supersedes ADR 0002 in part.

Done:

- [x] **M8.1** ADR 0005 accepted; ADR 0002 marked superseded in part; docs aligned.
      `deploy/components/postgresql/` (PG18 on UBI 9 `StatefulSet` + headless `Service` +
      `PVC`) included by the dev/prod overlays; `scripts/dev-postgres.sh`; manifest tests.
- [x] **M8.2** `PostgresQueue` + schema (`src/herald/queue/postgres.py`), unique
      `transport_id` and guarded-`UPDATE` claim/lease; `JmapQueue` retired; queue backend
      decoupled from the transport (`HERALD_QUEUE`, `HERALD_DATABASE_URL`); CI Postgres
      service. Verified against a live Postgres (podman).

Next:

1. **M8.3** Move approvals and the decision log/labels into Postgres; drop the file stores.
2. **M8.4** One-time backfill of in-flight tasks from the mailbox; cut the sweep and runner
   over to the database.

Note: the M7 decider refactor is a separate branch (`herald-m7-decider-refactor`, PR #54).

## Configuration (see .env, never committed)

- State: `HERALD_QUEUE=memory|postgres` (default `memory`) and `HERALD_DATABASE_URL`
  (PostgreSQL 18 on UBI 9; local podman or in-cluster).
- Decision: `HERALD_DECIDER=rules|jev|openai-compat|none` (alias `openrouter`),
  `HERALD_DECIDER_MODEL`, `HERALD_DECIDER_ENDPOINT`, `HERALD_DECIDER_BASE_URL`,
  `HERALD_DECIDER_THRESHOLD`, `HERALD_DECIDER_LOG`, `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`.
- Harness: `HERALD_HARNESS=opencode|claude|codex|command`, `HERALD_HARNESS_BINARY`,
  `HERALD_HARNESS_COMMAND`, `HERALD_HARNESSES`, `HERALD_HARNESS_DEFAULT`.
