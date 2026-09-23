# ADR 0003 — Repository and transport credentials

- **Status:** accepted
- **Date:** 2026-09-18

## Context

Dear Agent's runner must clone a repository, and the git plane must push an `dear-agent/<slug>`
branch and open a draft PR. The control plane must read and send mail. Every one of these
needs a credential, and a run may execute untrusted, attacker-influenced instructions. A
single broad credential is therefore the highest-value target in the system: stealing it
would let an injected run write to any repository, including `main`.

## Decision

1. **One credential per purpose, with the least privilege that works.**
   - Clone/read: a **read-only deploy key** or a GitHub App installation with
     `contents: read`.
   - Push: a **write deploy key** or App with `contents: write` and
     `pull requests: write`, scoped to the target repositories only.
   - Mail: a Fastmail API token scoped to `Email` (and `Email submission` for sending).
   - Model: a scoped provider key.

2. **Prefer a GitHub App over a personal access token.** A classic PAT with the `repo`
   scope grants access to *every* repository and cannot be limited to `dear-agent/*`. A GitHub
   App installation token is repository-scoped and short-lived. If a PAT is unavoidable,
   use a **fine-grained PAT** limited to selected repositories with the minimum permissions.

3. **Credentials never live in git.** They are Kubernetes `Secret`s, referenced by
   `secretKeyRef` (or projected from an external store via the External Secrets Operator or
   the Secrets Store CSI driver). Manifests in the repository contain only references.

4. **Mount by role, not globally.** The runner Job receives only the read credential and
   the model key; the push step receives the write credential. `automountServiceAccountToken`
   is `false`, and each workload runs under its own `ServiceAccount`.

5. **Never log a credential.** Evidence, escalation messages and PR bodies never include
   secrets; the transport carries metadata and links only.

## Rationale

- The blast radius of a single broad token defeats the project's core guarantee ("agents
  never write `main`").
- Per-role secrets mean a compromised runner cannot push, and a compromised push step
  cannot read other repos.
- External secret stores give rotation and audit without embedding values in the cluster.

## Alternatives

- **Single PAT for everything.** Simplest to set up, rejected: unscoped and long-lived.
- **SSH agent forwarding into the Job.** Rejected: exposes the agent socket to the run.
- **Baking the credential into a custom runner image.** Rejected: the secret ends up in an
  image layer and in the registry.

## Consequences

- More setup: an App/deploy keys per repository and several `Secret`s.
- The deployment docs must describe rotation (App installation tokens rotate; deploy keys
  and PATs need manual or automated rotation).
- `GIT_SSH_COMMAND` is pinned to the mounted key and to `StrictHostKeyChecking=yes`, so the
  run cannot be redirected to an attacker-controlled host.
