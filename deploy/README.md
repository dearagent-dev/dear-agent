# Deploying Dear Agent on Kubernetes / OpenShift

Dear Agent is Kubernetes/OpenShift by design (ADR 0002, ADR 0005): one `Job` per task, a `CronJob`
sweep, no always-on daemon, and a PostgreSQL `StatefulSet` for durable state. The transport
mailbox is ingress only.

```
deploy/
├── base/                 # control plane, runner Job, sweep CronJob, secrets refs
├── components/
│   └── postgresql/       # durable queue: PostgreSQL 18 StatefulSet + Service (ADR 0005)
└── overlays/
    ├── dev/
    └── prod/
```

Build and apply:

```sh
kustomize build deploy/overlays/dev | kubectl apply -f -
# or
oc apply -k deploy/overlays/dev
```

## Prerequisites

1. Set the image in the overlay (`quay.io/dear-agent/dear-agent`). The image is built from
   `registry.access.redhat.com/ubi9/python-312`, so it matches the OpenShift platform; it
   runs as an arbitrary UID with group 0 for OpenShift's SCC.
2. Create the Secrets out of band (never commit values). They are **not** part of the
   kustomize build, so `oc apply -k` can never reset a live credential. See
   [`../docs/decisions/0003-credentials.md`](../docs/decisions/0003-credentials.md).

## Database (ADR 0005)

PostgreSQL is the durable queue; the mailbox is ingress. The `postgresql` component is
opt-in and already included by the `dev`/`prod` overlays:

- `deploy/components/postgresql/` — a `StatefulSet` (`registry.redhat.io/rhel9/postgresql-18`)
  plus a headless `Service` and a `PVC`. The name is `dear-agent-postgres`, reachable at
  `dear-agent-postgres:5432` inside the namespace and **never** exposed with a Route/NodePort.
- The PVC uses the cluster default storage class. Pin one per environment with the patch
  commented in `overlays/prod/kustomization.yaml`.
- Credentials come from the `dear-agent-postgres` Secret: `POSTGRESQL_USER`, `POSTGRESQL_PASSWORD`,
  `POSTGRESQL_DATABASE` and `POSTGRESQL_ADMIN_PASSWORD` feed the StatefulSet, and `DATABASE_URL`
  is the DSN the application reads as `DEAR_AGENT_DATABASE_URL`.
- The workloads select the backend with `DEAR_AGENT_QUEUE=postgres` (set in `base/config.yaml`)
  and get `DEAR_AGENT_DATABASE_URL` from the Secret; the mailbox is ingress, not the queue.

For local development without a cluster, run the same image with podman:

```sh
scripts/dev-postgres.sh up     # prints DEAR_AGENT_DATABASE_URL
scripts/dev-postgres.sh psql
scripts/dev-postgres.sh down   # keeps the named volume
```

The image is Red Hat's, so log in first (`podman login registry.redhat.io`) if needed.

## Credentials (ADR 0003)

One credential per purpose, least privilege, referenced by `secretKeyRef`:

| Secret | Key | Purpose | Scope |
|---|---|---|---|
| `dear-agent-git-read` | `ssh-privatekey` | clone/read repositories | read-only deploy key |
| `dear-agent-git-push` | `ssh-privatekey` | push `dear-agent/<slug>`, open draft PR | write deploy key, `dear-agent/*` only |
| `dear-agent-forge` | `GH_TOKEN` / `GITLAB_TOKEN` / `GITEA_TOKEN` / `BITBUCKET_TOKEN`(+`BITBUCKET_USERNAME`) | open the draft PR/MR via the API (ADR 0009) | pull-request write on the target repos |
| `dear-agent-jmap` | `FASTMAIL_API_TOKEN` | read/send mail | `Email` (+ `Email submission`) |
| `dear-agent-model` | provider-specific | model calls | scoped provider key |
| `dear-agent-postgres` | `POSTGRESQL_*`, `DATABASE_URL` | database credentials | one database, least privilege |

Create them without putting values in git, for example:

```sh
oc create secret generic dear-agent-jmap \
  --from-literal=FASTMAIL_API_TOKEN="$FASTMAIL_API_TOKEN"
oc create secret generic dear-agent-git-read \
  --from-file=ssh-privatekey="$HOME/.ssh/dear_agent_read"
oc create secret generic dear-agent-git-push \
  --from-file=ssh-privatekey="$HOME/.ssh/dear_agent_push"
oc create secret generic dear-agent-forge \
  --from-literal=GH_TOKEN="$FORGE_TOKEN"
oc create secret generic dear-agent-model \
  --from-literal=API_KEY="$MODEL_API_KEY"
oc create secret generic dear-agent-postgres \
  --from-literal=POSTGRESQL_USER=dear-agent \
  --from-literal=POSTGRESQL_PASSWORD="$DEAR_AGENT_POSTGRES_PASSWORD" \
  --from-literal=POSTGRESQL_DATABASE=dear-agent \
  --from-literal=POSTGRESQL_ADMIN_PASSWORD="$DEAR_AGENT_POSTGRES_ADMIN_PASSWORD" \
  --from-literal=DATABASE_URL="postgresql://dear-agent:$DEAR_AGENT_POSTGRES_PASSWORD@dear-agent-postgres:5432/dear-agent"
```

For production, project these from an external store (External Secrets Operator, Secrets
Store CSI driver, SealedSecrets) so rotation is automated and values never sit in the
cluster as plain objects.

**Do not use a classic PAT with the `repo` scope**: it reaches every repository and cannot
be limited to `dear-agent/*`. Prefer a GitHub App installation, or a fine-grained PAT limited to
selected repositories.

## Hardening already baked into the manifests

- `automountServiceAccountToken: false` on every workload except the sweep, which needs a
  token to create runner Jobs (its `Role` can only read the template and create Jobs).
- Separate `ServiceAccount`s for the control plane, the runner (read) and the publisher.
- `runAsNonRoot`, `seccompProfile: RuntimeDefault`, all capabilities dropped.
- Runner worktree is an `emptyDir`; nothing persists in the pod except the pushed branch.
- The inbound webhook rejects oversized bodies (`DEAR_AGENT_MAX_BODY_BYTES`) before reading
  them and times out idle connections (`DEAR_AGENT_HTTP_TIMEOUT`).
- `GIT_SSH_COMMAND` pins the mounted key and `StrictHostKeyChecking=yes`, so a run cannot
  be redirected to another host.
- A dedicated read-only key is mounted for the clone; the push uses a scoped write key
  (`DEAR_AGENT_GIT_PUSH_KEY`) and the draft PR a forge token (`dear-agent-forge`, ADR 0009).
- An **opt-in two-container runner** (`runner-sidecar-template.yaml`, ADR 0007): the untrusted
  harness runs in its own container with no credential and only the shared worktree, while the
  control container carries the git keys and the forge token. Enable it with
  `DEAR_AGENT_RUNNER_TEMPLATE_CONFIGMAP=dear-agent-runner-sidecar-template`. The harness image is
  built from `deploy/harness/Containerfile` and published as `quay.io/dear-agent/harness`.
- An **egress allowlist** for runner Jobs (`deploy/components/egress/`), included by default:
  DNS, same-namespace (Postgres) and everything except link-local/metadata, so the model and
  Git work out of the box. Tighten it to the model/Git CIDRs or an egress proxy. It is the
  deploy-side half of "authenticating the sender is not trusting the content".
- **Automated backups**: `deploy/components/backup/` runs a daily `pg_dump` (`-Fc`) to a
  dedicated PVC, keeping the last seven. Restore with `pg_restore`. For **point-in-time
  recovery**, add continuous WAL archiving to your object store (`archive_mode=on` +
  `archive_command` on the Postgres `StatefulSet`) — a storage-dependent follow-up.

## Not yet wired

- The runner is a **`JobTemplate`** (`dear-agent-runner-template` ConfigMap), not a static Job:
  a Job is immutable and one-per-task, so the scheduler renders it per claimed task. The
  template invokes `dear-agent run <task-id>` (the transport defaults to memory; the queue comes
  from `DEAR_AGENT_QUEUE`), which claims the task, runs the harness in a worktree and opens the
  draft PR. `dear-agent sweep` (the CronJob) creates one Job per queued task with the task id
  injected.
- The runner carries **no mail credential**: the parsed spec is on the task row, and without
  `--recipient` it sends no notifications. To notify from the runner, add `--recipient` and
  `--backend jmap` and mount `dear-agent-jmap`.
- The runner's `--repo /work/source` is cloned read-only on first use from the task's
  `repo:` URL, using the mounted `dear-agent-git-read` key (`GIT_SSH_COMMAND`).
- Approvals and the decision log live in Postgres (`dear_agent_approval`, `dear_agent_decision`);
  the file stores are a single-process fallback only. The parsed `TaskSpec` is persisted on
  the task row, so a runner needs no mailbox access to run.
- **The base control-plane image ships no harness.** The harness image
  (`deploy/harness/Containerfile`, published as `quay.io/dear-agent/harness`) bundles OpenCode on
  top of it for the two-container runner. For the single-container runner, point at a repository
  that declares its environment (ADR 0008), inject the harness bundle
  (`DEAR_AGENT_HARNESS_BUNDLE=true`), or set `DEAR_AGENT_HARNESS=command` with
  `DEAR_AGENT_HARNESS_COMMAND`. Without a harness, runs fail as `HARNESS_MISSING` and escalate.

## Verified on a live OpenShift cluster

The base manifests were applied to a validation namespace and the control plane reached
`1/1 Running`, serving `/health` from inside the cluster. Two real issues were found and
fixed by doing this:

1. The image build failed because `pyproject.toml` references `LICENSE`; the `Containerfile`
   now copies it.
2. Pods failed with `CreateContainerConfigError` when the referenced Secret had no keys, so
   every `secretKeyRef` is now `optional: true`; the app fails closed on a missing token at
   startup rather than the pod refusing to schedule.

For the **full end-to-end path** (inbound → queue → sidecar runner → harness → draft PR) follow
[`../docs/verify-openshift.md`](../docs/verify-openshift.md) and its companion
`scripts/verify-openshift.sh`. That is the M9.7 check and it is **not yet recorded as run**.
