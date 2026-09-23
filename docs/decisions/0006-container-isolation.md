# 0006 — Pluggable isolation: bubblewrap or a per-harness container

- **Status:** accepted
- **Date:** 2026-09-23
- **Related:** [0002](0002-deployment-topology.md), [0003](0003-credentials.md),
  [../security.md](../security.md), [../architecture.md](../architecture.md)

## Context

M6.2 added an OS sandbox: `dear_agent.sandbox.BubblewrapSandbox` wraps the harness argv in
`bwrap` with the worktree as the only writable path and egress denied by default. It is the
right default on a host that already has the harness installed.

But it assumes the harness and its whole runtime live on the host. In the deployment target
(one Job per task, ADR 0002) the image is already the unit of packaging, and harnesses ship
**official container images** — OpenCode publishes `ghcr.io/anomalyco/opencode`. Installing
and pinning three harness runtimes inside Dear Agent's own image is the thing containers exist to
avoid. And bwrap cannot give a harness a different runtime per task; a container can.

The design goal: Dear Agent decides *how* a harness is jailed, the harness's packaging stays the
harness's business (golden rule 3), and the model/endpoint stays configuration (golden rule
5).

## Decision

**Isolation is pluggable. `DEAR_AGENT_ISOLATION` selects the mechanism: `bwrap` (default) or
`podman`. Container isolation is expressed as per-harness metadata.**

1. **`DEAR_AGENT_ISOLATION=bwrap|podman`.** `bwrap` keeps today's behaviour (OpenCode jailed by
   `SandboxPolicy`, egress denied by default). `podman` runs the selected harness in a
   container via `dear_agent.sandbox.ContainerSandbox`.
2. **`ContainerSandbox` runs `podman run --rm`** with `--security-opt no-new-privileges`, the
   worktree bind-mounted read-write at `/work` (`--workdir /work`), and the configured mounts
   exposed. It is a `Sandbox`, so a runner does not know which jail it is under.
3. **Per-harness metadata on `HarnessInfo`:** `image`, `mounts`, `container_env`. A harness
   with an official image ships it as a default (OpenCode:
   `ghcr.io/anomalyco/opencode:latest`); a harness without one must be given
   `DEAR_AGENT_HARNESS_IMAGE` or it fails loudly — never silently unsandboxed.
4. **Credentials are mounts, read-only by default.** The same metadata works for any image
   user: a container path starting with `~` resolves against `DEAR_AGENT_HARNESS_CONTAINER_HOME`
   (default `/root`). `DEAR_AGENT_HARNESS_MOUNTS` overrides the mounts wholesale
   (`host:container[:ro|rw]`, comma-separated).
5. **Container env is an allowlist**, not the host environment: `DEAR_AGENT_HARNESS_CONTAINER_ENV`
   names the variables passed with `--env NAME=VALUE`. Egress is `host` by default (a hosted
   model must answer); `DEAR_AGENT_HARNESS_CONTAINER_NETWORK=none` denies it.
6. **No Containerfiles in this repo.** Dear Agent consumes images; it does not build harness
   images. A harness with no usable image simply waits for one (Codex, deferred).

## Rationale

- The image is the harness's own distribution channel; reusing it means Dear Agent never vendors a
  harness (golden rule 3) and never has to chase a harness's runtime dependencies.
- Metadata on `HarnessInfo` keeps the mapping in one place and lets the `EnvHarnessCatalog`
  stay the single source of "what a harness is".
- A `Sandbox` that is a container keeps `HarnessRunner` unchanged: the jail is still just a
  function from argv to argv, so the two mechanisms are interchangeable per deployment.
- Read-only credential mounts and an env allowlist keep the least-privilege posture of
  [security.md](../security.md) §3 in the container world.

## Consequences

- **`build_runner` gains a branch on `DEAR_AGENT_ISOLATION`.** Under bwrap only OpenCode is jailed
  (the default policy denies egress, which a subscription harness needs); under podman every
  harness is jailed, because the container supplies the network the harness needs.
- **A container runtime (`podman`) is now a supported dependency** for `podman` isolation;
  `bwrap` remains the zero-dependency default.
- **Codex is deferred:** it has no official image, so it runs containerised only once an
  operator sets `DEAR_AGENT_HARNESS_IMAGE`. No Containerfile is added for it.
- **New configuration surface** (`DEAR_AGENT_HARNESS_*`, `DEAR_AGENT_CONTAINER_BINARY`) documented in
  `TASKS.md`; defaults may need adjusting to the image's user (`DEAR_AGENT_HARNESS_CONTAINER_HOME`).

## Alternatives

- **Install every harness in Dear Agent's own image.** Rejected: vendoring harnesses, a larger
  attack surface, and a rebuild per harness release.
- **Only bwrap.** Rejected: forces the harness runtime onto the host and cannot vary per task.
- **A Containerfile per harness.** Rejected: the harnesses with official images do not need one,
  and maintaining the others is not Dear Agent's job.
- **gVisor / Kata.** Deferred, not rejected: they harden the runtime further but add a
  dependency the current threat model does not yet require.

## Open questions

- Whether to ship a documented default image for a subscription harness (Claude Code) or keep
  requiring `DEAR_AGENT_HARNESS_IMAGE`.
- Whether the container should default to `--userns=keep-id` for host-owned worktrees.
- Egress policy inside the container (allowlist the Git remote and model endpoint) vs the
  current `host` default.
