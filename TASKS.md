# TASKS.md — current work

Short-lived, per-slice work list. `AGENTS.md` is the durable contract; this file is the
current state. Keep it short and delete finished items.

## In progress

### M8 — Durable state in PostgreSQL (ADR 0005)

Branch: `herald-m8-postgres-deploy`.
Goal: the mailbox becomes **ingress**; PostgreSQL is the durable queue for tasks, state,
approvals and the decision log. Decision:
[ADR 0005](docs/decisions/0005-state-store.md) supersedes ADR 0002 in part.

This slice (M8.1) is **infrastructure + decision only**:

- [x] ADR 0005 accepted; ADR 0002 marked superseded in part; docs aligned (AGENTS, README,
      architecture, queue, roadmap).
- [x] `deploy/components/postgresql/` — PostgreSQL 18 on UBI 9
      (`registry.redhat.io/rhel9/postgresql-18`) `StatefulSet` + headless `Service` + `PVC`,
      included by the dev/prod overlays; `herald-postgres` secret reference added.
- [x] `scripts/dev-postgres.sh` — the same image under podman for local development.
- [x] Manifest tests for the Postgres invariants (UBI/PG18, never exposed, persists data).

Next (M8.2+):

1. Schema + migrations and a `PostgresQueue` implementing the `Queue` port (unique
   `transport_id`, `FOR UPDATE SKIP LOCKED` claim, lease sweep); retire `JmapQueue`.
2. Move approvals and the decision log/labels into Postgres; drop the file stores.
3. One-time backfill of in-flight tasks from the mailbox; cut the sweep and runner over.

Note: the M7 decider refactor is a separate branch (`herald-m7-decider-refactor`, PR #54).

## Configuration (see .env, never committed)

- State: `HERALD_DATABASE_URL` (PostgreSQL 18 on UBI 9; local podman or in-cluster).
- Decision: `HERALD_DECIDER=rules|jev|openai-compat|none` (alias `openrouter`),
  `HERALD_DECIDER_MODEL`, `HERALD_DECIDER_ENDPOINT`, `HERALD_DECIDER_BASE_URL`,
  `HERALD_DECIDER_THRESHOLD`, `HERALD_DECIDER_LOG`, `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`.
- Harness: `HERALD_HARNESS=opencode|claude|codex|command`, `HERALD_HARNESS_BINARY`,
  `HERALD_HARNESS_COMMAND`, `HERALD_HARNESSES`, `HERALD_HARNESS_DEFAULT`.
