# TASKS.md — current work

Short-lived, per-slice work list. `AGENTS.md` is the durable contract; this file is the
current state. Keep it short and delete finished items.

## In progress

_None._ **M7 decider refactor is done** (branch `herald-m7-decider-refactor`, PR pending):
the Model/ModelCatalog/SelectionPolicy/OpenRouter plane is gone, replaced by
`DeciderInfo` / `DeciderCatalog` / `DeciderProvider` / `DeciderPolicy`. `build_decider()`
is now catalog + policy + provider, with `EnvDeciderCatalog` reading `HERALD_DECIDER_*` and
`RuleDecider` as the fallback. Verified live with Jev.

## Next

### HarnessCatalog + HarnessInfo + local-agent

Choose the local binary (`opencode`/`claude`/`codex`) with metadata (`accepts_model`: flag
vs subscription), clone the repo to `/tmp/herald-<taskid>`, run the harness, push
`herald/<slug>` + draft PR, clean up. This is what unblocks real work on Ricardo's laptop
(`opencode` + DeepSeek V4.1 Flash). Note: a harness adds a model only when it accepts one
(OpenCode does via `--model`; Claude Code / Codex use their subscription).

## Configuration (see .env, never committed)

- Decision: `HERALD_DECIDER=rules|jev|openai-compat|none` (alias `openrouter`),
  `HERALD_DECIDER_MODEL`, `HERALD_DECIDER_ENDPOINT`, `HERALD_DECIDER_BASE_URL`,
  `HERALD_DECIDER_THRESHOLD`, `HERALD_DECIDER_LOG`, `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`.
- Harness: `HERALD_HARNESS=opencode|claude|codex|command`, `HERALD_HARNESS_BINARY`,
  `HERALD_HARNESS_COMMAND`, `HERALD_HARNESSES`, `HERALD_HARNESS_DEFAULT`.
