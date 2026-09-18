# Deploying Herald on Kubernetes / OpenShift

Herald is Kubernetes/OpenShift by design (ADR 0002): one `Job` per task, a `CronJob`
sweep, and no database. The durable queue is the Fastmail JMAP mailbox.

```
deploy/
├── base/                 # control plane, runner Job, sweep CronJob, secrets refs
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

1. Set the image in the overlay (`ghcr.io/OWNER/herald`).
2. Create the four Secrets out of band (never commit values). See
   [`../docs/decisions/0003-credentials.md`](../docs/decisions/0003-credentials.md).

## Credentials (ADR 0003)

One credential per purpose, least privilege, referenced by `secretKeyRef`:

| Secret | Key | Purpose | Scope |
|---|---|---|---|
| `herald-git-read` | `ssh-privatekey` | clone/read repositories | read-only deploy key |
| `herald-git-push` | `ssh-privatekey` | push `herald/<slug>`, open draft PR | write deploy key, `herald/*` only |
| `herald-jmap` | `FASTMAIL_API_TOKEN` | read/send mail | `Email` (+ `Email submission`) |
| `herald-model` | provider-specific | model calls | scoped provider key |

Create them without putting values in git, for example:

```sh
oc create secret generic herald-jmap \
  --from-literal=FASTMAIL_API_TOKEN="$FASTMAIL_API_TOKEN"
oc create secret generic herald-git-read \
  --from-file=ssh-privatekey="$HOME/.ssh/herald_read"
oc create secret generic herald-git-push \
  --from-file=ssh-privatekey="$HOME/.ssh/herald_push"
oc create secret generic herald-model \
  --from-literal=API_KEY="$MODEL_API_KEY"
```

For production, project these from an external store (External Secrets Operator, Secrets
Store CSI driver, SealedSecrets) so rotation is automated and values never sit in the
cluster as plain objects.

**Do not use a classic PAT with the `repo` scope**: it reaches every repository and cannot
be limited to `herald/*`. Prefer a GitHub App installation, or a fine-grained PAT limited to
selected repositories.

## Hardening already baked into the manifests

- `automountServiceAccountToken: false` on every workload.
- Separate `ServiceAccount`s for the control plane, the runner (read) and the publisher.
- `runAsNonRoot`, `seccompProfile: RuntimeDefault`, all capabilities dropped.
- Runner worktree is an `emptyDir`; nothing persists in the pod except the pushed branch.
- `GIT_SSH_COMMAND` pins the mounted key and `StrictHostKeyChecking=yes`, so a run cannot
  be redirected to another host.
- A dedicated read-only secret is mounted into the runner; the write key stays out of it.

## Not yet wired

- The runner is a **`JobTemplate`** (`herald-runner-template` ConfigMap), not a static Job:
  a Job is immutable and one-per-task, so the scheduler renders it per claimed task. The
  rendering controller is not written yet; today the template documents the intended shape
  and passes the hardening invariants in CI.
- No Postgres: by design, the mailbox is the queue.

## Verified on a live OpenShift cluster

The base manifests were applied to a validation namespace and the control plane reached
`1/1 Running`, serving `/health` from inside the cluster. Two real issues were found and
fixed by doing this:

1. The image build failed because `pyproject.toml` references `LICENSE`; the `Dockerfile`
   now copies it.
2. Pods failed with `CreateContainerConfigError` when the referenced Secret had no keys, so
   every `secretKeyRef` is now `optional: true`; the app fails closed on a missing token at
   startup rather than the pod refusing to schedule.
