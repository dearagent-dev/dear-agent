# Verifying the end-to-end path on OpenShift (M9.7)

This runbook proves the **whole** path in a real cluster, with the two-container (sidecar)
runner and the new execution environment and forge slices:

```
POST /inbound (HMAC)  →  PostgreSQL queue  →  sweep CronJob  →  runner Job (control + harness)
   →  harness runs in its own container  →  control commits/pushes  →  draft PR via the forge API
```

It changes nothing until you pass `--apply`/`--enqueue`, and it is idempotent. The companion
script `scripts/verify-openshift.sh` runs the preflight and the enqueue for you; this document
is the source of truth for the manual path and the troubleshooting.

> Status: **prepared, not yet run against a cluster.** Follow it and record the result in
> `TASKS.md` (M9.7) — that is what closes the item.

## 0. Prerequisites

- An OpenShift/Kubernetes cluster and `oc` (or `kubectl`), `kustomize`, `curl`.
- A repository you can push to and whose forge you can token against (GitHub/GitLab/Gitea).
- The images published: the control-plane image and the **harness image**. `main` publishes both
  (`quay.io/dear-agent/dear-agent` and `quay.io/dear-agent/harness`) via `.github/workflows/publish.yml`.
  To build and push locally instead:

  ```sh
  podman build -t quay.io/dear-agent/dear-agent:latest .
  podman build -f deploy/harness/Containerfile \
    --build-arg DEAR_AGENT_IMAGE=quay.io/dear-agent/dear-agent:latest \
    -t quay.io/dear-agent/harness:latest deploy/harness
  podman push quay.io/dear-agent/dear-agent:latest
  podman push quay.io/dear-agent/harness:latest
  ```

## 1. Secrets (ADR 0003/0009)

All values are referenced, never committed. Create them out of band (see
[`../deploy/README.md`](../deploy/README.md) for the general list):

```sh
NS=dear-agent
oc -n "$NS" create secret generic dear-agent-postgres \
  --from-literal=POSTGRESQL_USER=dear-agent \
  --from-literal=POSTGRESQL_PASSWORD="$PGPW" \
  --from-literal=POSTGRESQL_DATABASE=dear-agent \
  --from-literal=POSTGRESQL_ADMIN_PASSWORD="$PGPW" \
  --from-literal=DATABASE_URL="postgresql://dear-agent:$PGPW@dear-agent-postgres:5432/dear-agent"
oc -n "$NS" create secret generic dear-agent-git-read  --from-file=ssh-privatekey="$HOME/.ssh/dear_agent_read"
oc -n "$NS" create secret generic dear-agent-git-push  --from-file=ssh-privatekey="$HOME/.ssh/dear_agent_push"
oc -n "$NS" create secret generic dear-agent-inbound   --from-literal=DEAR_AGENT_INBOUND_SECRET="$INBOUND_SECRET"
# The forge token for the draft PR (one of GH_TOKEN/GITHUB_TOKEN, GITLAB_TOKEN, GITEA_TOKEN):
oc -n "$NS" create secret generic dear-agent-forge     --from-literal=GH_TOKEN="$FORGE_TOKEN"
```

### The harness model credential

The harness needs a model credential to run. OpenCode reads it from `OPENCODE_CONFIG_CONTENT`
(a JSON config with the provider and key); Dear Agent passes an allowlisted env var to the
harness when it is named in `DEAR_AGENT_HARNESS_ENV`. Provide it as a secret and reference it on
the runner (and the sidecar control container):

```sh
oc -n "$NS" create secret generic dear-agent-model \
  --from-literal=OPENCODE_CONFIG_CONTENT='{"provider":{"myprovider":{"options":{"baseURL":"...","apiKey":"..."}}}}'
```

```sh
# add to the runner/control container in the runner template and sidecar template:
envFrom:
  - secretRef: { name: dear-agent-model, optional: true }
env:
  - name: DEAR_AGENT_HARNESS_ENV
    value: "OPENCODE_CONFIG_CONTENT"
```

For a subscription harness (Claude Code/Codex) mount its config instead, or provide the provider
key the harness expects and allowlist it the same way.

### Repository policy and sender allowlist

Inbound is allowlisted by default: with no project policy every repo is refused
(`DEAR_AGENT_REQUIRE_REPO_POLICY`, ADR/queue policy). For the test repo either add a policy and
sync it, or set `DEAR_AGENT_REQUIRE_REPO_POLICY=false` on the control plane. If
`DEAR_AGENT_ALLOWED_SENDERS` is set, the `--sender` you use must be in it. The webhook also
requires `DEAR_AGENT_INBOUND_SECRET` (already created above) or it answers `503`.

## 2. Deploy and enable the sidecar

```sh
# In deploy/base/config.yaml set DEAR_AGENT_RUNNER_TEMPLATE_CONFIGMAP to the sidecar template and
# a real DEAR_AGENT_MODEL, then:
scripts/verify-openshift.sh --namespace "$NS" --overlay dev --apply
```

The preflight (`scripts/verify-openshift.sh` with no mutation flags) checks: the Deployment and
Postgres StatefulSet exist and are healthy, every required secret/key is present, the sidecar
template and its harness image are wired, and the config selects it. It warns when
`dear-agent-model` is absent or when the single-container template is selected (credentials are
visible to the harness there).

## 3. Health check

```sh
oc -n "$NS" rollout status deploy/dear-agent-control-plane --timeout=120s
oc -n "$NS" port-forward svc/dear-agent-control-plane 8080:80 &
curl -fsS http://127.0.0.1:8080/health
```

## 4. Enqueue a task and watch it

```sh
scripts/verify-openshift.sh --namespace "$NS" \
  --repo git@github.com:dearagent-dev/dear-agent-lab-python.git \
  --instructions "Create a file VERIFY.md with the single line: verified." \
  --enqueue --watch
```

`--enqueue` signs the raw body with `DEAR_AGENT_INBOUND_SECRET` (`X-Dear-Agent-Signature:
sha256=<hmac>` over `sender\nbody`) and POSTs to `/inbound` through a port-forward. The response
is `202` with the ingest counts (`accepted`/`rejected`). The sweep `CronJob` then creates one
runner `Job` per queued task.

Alternatively, enqueue from inside the cluster (no port-forward):

```sh
oc -n "$NS" create job --from=cronjob/dear-agent-sweep sweep-manual
oc -n "$NS" get jobs -l app.kubernetes.io/component=runner -w
```

## 5. Confirm the draft PR

Watch the runner Job and, when it finishes, read the task evidence:

```sh
oc -n "$NS" get jobs,pods -l app.kubernetes.io/component=runner
oc -n "$NS" logs job/<runner-job> -c control | tail
oc -n "$NS" exec deploy/dear-agent-control-plane -- dear-agent task show "<task-id>"
oc -n "$NS" exec deploy/dear-agent-control-plane -- dear-agent task events "<task-id>"
```

Expected: `verify.passed` (if the task declares a verify command) then `task.done
pr_url=https://…/<repo>/pull/<n>`. The PR is a **draft** and a human lands it.

## 6. Cleanup

```sh
oc -n "$NS" delete job -l app.kubernetes.io/component=runner
# close the draft PR, and remove the scratch repo changes if any.
```

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Responds `503` on `/inbound` | `DEAR_AGENT_INBOUND_SECRET` missing | create `dear-agent-inbound` |
| Responds `401` | wrong HMAC | sign the **raw** body plus `sender\n`; check the secret |
| Responds `403` | sender not allowed | add to `DEAR_AGENT_ALLOWED_SENDERS` |
| Task `rejected` | repo has no policy and `DEAR_AGENT_REQUIRE_REPO_POLICY=true` | add a policy or set it `false` |
| Job fails `HARNESS_MISSING` | harness image/binary missing | use `quay.io/dear-agent/harness`, check the bundle shim |
| Job fails `PUBLISH_FAILED` | push key or forge token absent/insufficient | check `dear-agent-git-push` and `dear-agent-forge` |
| Harness cannot reach the model | no model credential or egress blocked | wire `dear-agent-model`; check the runner egress `NetworkPolicy` |
| `CreateContainerConfigError` | referenced Secret has no keys | every `secretKeyRef` is `optional: true`; create the keys |

## 8. Record the result

If it passes, mark **M9.7** done in `docs/roadmap.md` and move the "in-cluster PR path" line out
of `TASKS.md` → *Known gaps*, noting the commit/tag and the cluster. If it fails, add the
finding with the failing Job logs.
