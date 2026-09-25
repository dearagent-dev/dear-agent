# 0008 — Execution environments: the run is a session, the harness is injected

- **Status:** accepted
- **Date:** 2026-09-24
- **Related:** [0002](0002-deployment-topology.md), [0005](0005-state-store.md),
  [0006](0006-container-isolation.md), [0007](0007-harness-isolation.md),
  [../architecture.md](../architecture.md), [../security.md](../security.md)

## Context

M3 wrapped a harness CLI (`opencode run <prompt>`) and ran it in an isolated Git worktree.
M6.2 added an OS sandbox and M6.6/ADR 0006 made isolation pluggable (`bwrap` or a per-harness
`podman` container). ADR 0007 split the runner into a `control` and a `harness` container so the
harness never holds a git or database credential.

Two assumptions have since broken:

1. **The harness image was treated as the project's environment.** ADR 0006 makes the
   *harness's own image* the unit of packaging (`ghcr.io/anomalyco/opencode`). That image ships
   the agent and its runtime, **not the project's toolchain** (Python, Terraform, Ansible, a
   JDK, …). Dear Agent is language-agnostic, so it cannot know, and must not bake, every
   project's toolchain into a harness image. Mounting the developer's laptop binaries is not an
   option either: there is no laptop in the cluster, and every laptop is different.

2. **The environment was ephemeral per command.** `ContainerSandbox.wrap()` builds a fresh
   `podman run --rm` argv for *each* command, so the harness and the `verify` gate run in two
   different containers. Whatever the harness installs (a language runtime, `make`, a venv)
   is destroyed before `verify` runs. "Let the harness provision the pod" only works if the pod
   survives from the harness run to the verify run.

There is also a **lifetime/location** question the current design does not answer: the control
plane is asynchronous (email arrives while nobody is watching), so execution cannot assume the
developer's laptop is on. The harness must be able to run on an always-on self-hosted host
(homelab, VPS, Kubernetes/OpenShift, a self-hosted CI runner) — but the same code path should
still work locally.

The industry has already converged on an answer for "describe a portable development
environment": **Dev Containers** (`devcontainer.json` + *Features* + prebuilds), with **Ansible
Execution Environments** (`execution-environment.yml` → `ansible-builder` → image) as the
Ansible-specific instance of the same shape (descriptor → image → run). Crucially, the industry
answer to "the dev container does not contain the harness" is a **Feature**: Anthropic ships
`ghcr.io/anthropics/devcontainer-features/claude-code`, and community Features exist for
OpenCode and Codex.

## Decision

**A task runs in one long-lived *environment session*; the harness is *injected* into that
environment; the environment is described by the repository, not by Dear Agent.**

1. **The session is the unit of execution, not the command.** One environment instance is
   started per task and lives from before the harness run until after `verify`, so anything the
   harness provisions is visible to `verify`. `Sandbox` grows a lifecycle
   (`start`/`exec`/`close`, expressed through the existing `wrap`/`close` pair) and
   `TaskExecutor` owns it.

2. **The environment is declared by the repository.** A new `Environment` port resolves a
   descriptor in precedence order:
   `.devcontainer/devcontainer.json` → `execution-environment.yml` (Ansible EE) →
   `Containerfile`/`Dockerfile` → `mise.toml`/`.tool-versions` → fallback (base image + the
   harness provisions). Dear Agent consumes descriptors; it does not invent toolchains.

3. **The harness is injected, never assumed.** A new `HarnessInjector` makes the harness
   available inside the environment by one of three mechanisms, in preference order:
   - **Feature (build-time):** compose the repo's `devcontainer.json` with a harness Feature and
     build an image (prebuild in CI or an in-cluster BuildConfig). Reproducible and offline at
     run time.
   - **Bundle (run-time):** materialize a portable bundle (the harness binary plus its dynamic
     loader and libraries) and mount it into any environment. Verified: the musl-linked OpenCode
     binary runs in a glibc UBI container when invoked through its bundled
     `ld-musl-x86_64.so.1` with `LD_LIBRARY_PATH` pointing at the bundle. No build, no network.
   - **Bootstrap (run-time fallback):** run the harness's own installer at session start. Needs
     network and a shell; not hermetic.

4. **Where the session runs is a port (`RunnerHost`), not a branch.** Local `podman`, a
   self-hosted Kubernetes/OpenShift Job, and a self-hosted CI runner are all in scope. **Hosted
   agent backends are out of scope** (the vendor would own the environment; that is a different
   trust model, deferred).

5. **The credential split of ADR 0007 stands.** The harness environment holds the *model*
   credential and nothing else; the git push and the database DSN stay in the control plane.
   Provisioning a repository-declared environment is executing repository code, so it is
   sandboxed and subject to the same untrusted-input rules as any other command.

## Rationale

- **The harness's shell commands must hit the project's toolchain**, so the harness process has
  to run *inside* the environment. This is why a separate harness container is not enough
  without an exec bridge, and why "inject the harness into the environment" (rather than "inject
  the environment into the harness") is the correct direction.
- **Descriptors are how the industry makes environments portable.** Adopting
  `devcontainer.json`/EE avoids inventing a format, reuses prebuild tooling, and matches what
  developers already commit to their repos.
- **A session makes provisioning meaningful.** Installing a runtime is pointless if the next
  command runs elsewhere. The session is the smallest change that makes "the harness provisions
  the pod" true.
- **Injection decouples two independent lifecycles.** The environment changes with the project;
  the harness changes with the agent. Composing them at the boundary (Feature/bundle) means
  neither has to know the other.

## Consequences

- **`sandbox.py` changes shape.** `ContainerSandbox` becomes stateful: the first `wrap()` starts
  a named container (`podman run -d … sleep infinity`) and later calls return `podman exec`;
  `close()` removes it. `build_wrapped_argv` stays a pure function for the existing
  per-command tests. `BubblewrapSandbox`/`NoSandbox` gain a no-op `close()`.
- **The runner and the verifier must share one session.** Today `build_runner` rebinds its
  `sandbox` parameter locally under `DEAR_AGENT_ISOLATION=podman`, so the verifier silently gets
  the `bwrap` sandbox. `build_worker` must resolve the sandbox once and hand the *same* object to
  the runner, the verifier and the executor.
- **`TaskExecutor` gains a session lifecycle** (`close()` in its `finally`), so a container is
  not leaked on a failed run.
- **Routing shares the session.** `RoutingRunner` is a `SessionProvider`: it caches the per-task
  choice and hands the chosen harness's session to the verifier, so a routed run verifies in the
  environment it ran in.
- **The descriptor cannot escape the checkout.** A `build.dockerfile`/`build.context` that
  resolves outside the repository is rejected, so a malicious descriptor cannot hand host files
  to `podman build` as build context.
- **In OpenShift the image must be prebuilt** (or the bundle mounted through an `initContainer`
  + `emptyDir`), because a restricted SCC forbids nested containers. The runner Job then *is* the
  environment; the session degrades to "the pod".
- **New surface area:** an `environment/` package (descriptor, builder, injector) and
  configuration for the bundle cache and the harness Feature. Docs in
  [../getting-started.md](../getting-started.md) and [../architecture.md](../architecture.md)
  travel with it.
- **The session must give the harness a writable home.** With `--cap-drop ALL` (the default)
  and an image whose `HOME` directory is not writable by its user — UBI's `/root` is `0550` —
  the harness cannot create its cache and fails. The session must point `HOME` at a writable
  path (the worktree) or the image must provide one.
- **Bundle mounts need SELinux relabeling on enforcing hosts.** `DEAR_AGENT_HARNESS_CONTAINER_SELINUX=Z`
  (or `disable`) is required for the mounted shim to be executable; `auto` deliberately leaves
  non-worktree mounts alone.

## Alternatives

- **Bake every project's toolchain into the harness image.** Rejected: not language-agnostic,
  rebuild per project, and the harness image belongs to the harness (golden rule 3).
- **Mount the developer's host toolchain into the container.** Rejected: no host in the cluster,
  and every laptop differs; it also widens the trust boundary.
- **Keep per-command containers and have `verify` re-provision.** Rejected: doubles the work,
  needs network on every run, and is not reproducible.
- **A Dear-Agent-specific environment format.** Rejected for now: `devcontainer.json` is the
  standard and EE covers the Ansible world; a bespoke format is a last resort if neither fits.
- **Hosted agent backends (vendor owns the environment).** Out of scope per decision 4;
  revisit only if the trust model is revisited.

## Open questions

- Build in-cluster (BuildConfig/Kaniko) versus prebuild in CI; who owns the registry.
- Caching the harness bundle (PVC) and its supply-chain story (signature/pin).
- How far to trust a repository-supplied descriptor; whether setup commands are allowlisted or
  approved like other untrusted input.
- Dev Container `docker-compose`/multi-service environments: in scope later or never?
- Non-root `remoteUser` and UID mapping against the host-owned worktree.

## First slice (implemented on branch `dear-agent-execution-environment`)

1. `EnvironmentSession`: the `ContainerSandbox` lifecycle (start once, `exec` harness and
   `verify`, `close`), with `TaskExecutor` owning it and `build_worker` sharing one session
   between the runner and the verifier.
2. `HarnessInjector` with the **bundle** mechanism (`environment/bundle.py`): materialize a
   portable harness bundle from the harness image and mount it into the session, so an
   environment image that lacks the harness can still run it.
3. Tests for the session lifecycle and the injector; a live proof where the harness provisions
   a toolchain and `verify` observes it in the same session.

**Verified live** (`podman`, 2026-09-24):

- *Session persistence:* the session ran `apk add make python3` as the harness step, then a
  later `podman exec` ran `make --version` in the **same** container and saw the installed
  `make`. The old per-command model ran the verify step in a fresh container and failed.
- *Bundle injection:* the OpenCode bundle (musl binary + loader + `libstdc++`/`libgcc`) ran
  `opencode --version` (`1.18.32`) inside a `registry.access.redhat.com/ubi9/ubi` container
  that does not contain OpenCode. Needed `DEAR_AGENT_HARNESS_CONTAINER_SELINUX=Z` and a
  writable `HOME`.

The environment descriptor resolver and the Feature/prebuild path are the next slices.
