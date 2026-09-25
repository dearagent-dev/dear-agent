# 0009 — Open the PR through the forge API, not a CLI

- **Status:** accepted
- **Date:** 2026-09-24
- **Related:** [0003](0003-credentials.md), [0007](0007-harness-isolation.md),
  [../architecture.md](../architecture.md)

## Context

The deliverable of a task is a draft PR, and the **control process** opens it after the harness
exits (`TaskExecutor` → `GitPlane` → `Forge`). The harness never opens the PR and never holds a
push or forge credential (ADR 0007).

Until now the only `Forge` was `GhForge`, which shells out to `gh pr create --draft`. That has
three problems:

1. **It needs the `gh` binary on `PATH`.** The runner image is UBI 9 with `git`/`openssh-clients`
   and does **not** ship `gh`; installing it adds a dependency and a supply-chain surface for a
   single HTTP call.
2. **It is GitHub-only.** `GitPlane(forge=GhForge())` was hardcoded in `build_worker`, so a
   GitLab or Gitea remote could not open an MR/PR at all.
3. **It hides the credential.** `gh` reads its own keyring/config; the token is not passed
   explicitly, which makes the runner Job's credential wiring implicit.

## Decision

**Open the PR/MR through the forge's REST API with an explicit token, behind the existing
`Forge` protocol. No CLI.**

1. `gitplane/forge.py` implements `GithubApiForge`, `GitlabApiForge`, `GiteaApiForge` and
   `BitbucketApiForge`. Each
   resolves the repository from the worktree's `origin` remote, posts a draft PR/MR, and returns
   its URL. `default_http_post` uses the standard library (`urllib`), so there is no new
   dependency.
2. `DEAR_AGENT_FORGE` selects the mechanism: `auto` (default) picks the API forge from the
   remote host (`provider_for_host`), `github`/`gitlab`/`gitea`/`bitbucket` force one, and `gh`
   keeps the legacy CLI for anyone who prefers it.
3. **Tokens are environment variables named per provider** — `GH_TOKEN`/`GITHUB_TOKEN`,
   `GITLAB_TOKEN`, `GITEA_TOKEN`, and `BITBUCKET_TOKEN` + `BITBUCKET_USERNAME`/`BITBUCKET_EMAIL`
   (Basic auth for Bitbucket Cloud) — referenced by name only, never committed (golden rule 6).
   A missing token fails the task as `PUBLISH_FAILED` with a clear message.
4. `build_worker` wires `GitPlane(forge=build_forge())` instead of a hardcoded `GhForge`.

## Rationale

- An HTTP call needs no binary, so the runner image stays small and `gh` stops being a hidden
  dependency.
- Host-based selection makes the same runner work against GitHub, GitLab, Gitea/Forgejo and
  Bitbucket Cloud without per-deployment forks.
- Passing the token explicitly matches ADR 0003: one scoped credential per purpose, referenced
  by name, mounted by role.

## Consequences

- **The runner Job carries the forge token and the write git key.** `deploy/base/runner-template.yaml`
  and the sidecar `control` container set `DEAR_AGENT_GIT_PUSH_KEY` (a scoped write deploy key,
  ADR 0003) and mount a `dear-agent-forge` secret through `envFrom` (`GH_TOKEN`/`GITHUB_TOKEN`,
  `GITLAB_TOKEN`, `GITEA_TOKEN`, `BITBUCKET_TOKEN` + `BITBUCKET_USERNAME`). The clone keeps the
  read key. In the **sidecar** template the
  harness container receives neither, which is the credential-isolated path; the default
  single-container runner shares them with the harness (ADR 0007 residual).
- **`GitPlane.push` pins the write key** when `DEAR_AGENT_GIT_PUSH_KEY` is set, so the clone can
  stay on the read key; locally the ambient git credential is used.
- **Self-hosted hosts default to GitHub** unless `DEAR_AGENT_FORGE` says otherwise; the API base
  is derived from the remote host (GitHub Enterprise uses `/api/v3`, Gitea `/api/v1`, GitLab
  `/api/v4`, Bitbucket Cloud `/2.0`). Bitbucket Server/Data Center (self-hosted) is not
  supported yet.
- **The `gh` CLI path is kept** as an opt-in fallback and is not the default.
- Verified live: a task on a GitHub remote opened a draft PR through the API with `GH_TOKEN`
  and no `gh` invocation.

## Alternatives

- **Keep `gh` and install it in the image.** Rejected: a binary to package and update for one
  HTTP call.
- **One forge per deployment.** Rejected: the repository (not the deployment) determines the
  forge.
- **A Forge-specific SDK dependency.** Rejected: plain `urllib` keeps the dependency surface
  flat.

## Open questions

- GitLab draft MRs use a `Draft:` title prefix; some versions also accept a `draft` field.
  Whether to detect the version or keep the prefix (portable) is deferred.
- Whether to support Forgejo-specific fields (`squash`, reviewers) beyond what Gitea accepts.
