# 0007 — Isolate the harness from the runner's credentials

- **Status:** accepted (mechanism implemented; opt-in manifest)
- **Date:** 2026-09-24
- **Related:** [0006](0006-container-isolation.md), [../security.md](../security.md),
  [#94](https://github.com/dearagent-dev/dear-agent/issues/94)

## Context

The security review (C1) found that a compromised harness could reach the runner's credentials.
Two mitigations landed: the harness **environment** is an allowlist
(`runners.harness.harness_env`, #95) and the runner Job **no longer carries the mail token**.
The residual is structural: the harness is a child process of `dear-agent run`, sharing its
container, UID and PID namespace, so it can still read the parent's environment through
`/proc/<ppid>/environ` (recovering `DEAR_AGENT_DATABASE_URL`) and the mounted git credential at
`/run/secrets/git`.

The goal is that the harness process cannot reach **any** credential the runner holds. The
container already isolates from the host (ADR 0006); this is about **intra-container**
separation.

## Options considered

1. **Bubblewrap inside the runner image.** `bwrap` gives `--unshare-pid` + `--clearenv`, which
   would hide the parent's `/proc` and drop the env. **Rejected:** verified that `bubblewrap` is
   not in the UBI 9 repositories (`ubi-9-baseos`, `ubi-9-appstream`, `ubi-9-codeready-builder`
   all report "No matching Packages"), so it cannot be `dnf install`ed; and `bwrap` needs user
   namespaces, which OpenShift denies by default. Shipping it would require a source build and
   still be unreliable in-cluster.
2. **Run the harness as a different UID.** `/proc/<pid>/environ` is readable only by the owning
   UID, so a distinct UID would hide the parent's environment and the 0400 git key. **Rejected:**
   an OpenShift pod runs every process as one namespace UID and the container has no
   `CAP_SETUID`, so the parent cannot drop the harness to another UID.
3. **`ContainerSandbox` (podman) in-cluster.** The harness in its own container. **Rejected:**
   needs privileges the restricted SCC does not grant.
4. **Two containers in the runner pod (sidecar).** The harness runs in its **own container**
   with no secrets and no git credential; the worktree is a shared `emptyDir`. **Accepted.**

## Decision

**Isolate the harness in its own container within the runner Job.** Two containers share an
`emptyDir` worktree:

- **`control`** (has the DB DSN and the git key): claims the task, clones/updates the repo,
  creates the worktree, writes the prompt to the shared volume, waits for the harness, then
  commits, pushes and opens the draft PR.
- **`harness`** (no secrets, its own image with the harness): waits for the prompt, runs the
  harness in the shared worktree, writes its evidence back. It holds no transport, DB or git
  credential.

The prompt/evidence handshake is files in the shared volume (with a readiness/`done` marker),
so no source leaves Git and no credential crosses into the harness container.

## Implementation

The control/harness handshake is `runners/shared.py`: `DelegatingRunner` (control side)
writes a credential-free request to the shared directory and waits for the result;
`dear-agent harness-serve --dir <dir> --once` (harness side) runs the harness in the shared
worktree and writes the result back. `build_worker` uses `DelegatingRunner` when
`DEAR_AGENT_HARNESS_DIR` is set. The opt-in Job is
`deploy/base/runner-sidecar-template.yaml` (enable it with
`DEAR_AGENT_RUNNER_TEMPLATE_CONFIGMAP=dear-agent-runner-sidecar-template`).

## Consequences

- The runner becomes a two-container pod; the K8s dispatch and the runner CLI grow a
  control/harness split. This is a **staged** change, not a drop-in.
- The git credential and the DB DSN never appear in the harness container at all, which also
  removes the `/proc` concern.
- Until it lands, the residual is bounded by the earlier mitigations: the harness env is an
  allowlist, the mail token is absent, only a **read-only** git key is mounted, and the DB role
  should be least-privilege.

## Open questions

- Whether to build a dedicated harness image (per ADR 0006's `HarnessInfo.image`) for the
  sidecar, or reuse the runner image with the harness installed.
- The exact readiness/handshake protocol and its timeout.
- Whether to also run `verify` in the harness container.
