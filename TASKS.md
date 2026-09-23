# TASKS.md — current work

Short-lived, per-slice work list. `AGENTS.md` is the durable contract; this file is the
current state. Keep it short and delete finished items.

## In progress

### M7 — Decider abstraction refactor

Branch: `herald-m7-decider-refactor` (WIP committed at `97c8d90`, pushed).
Goal: remove the "Model / ModelCatalog / SelectionPolicy / OpenRouter-models" plane and
replace it with a decider abstraction. Decision: **"with System One we have enough"** —
Herald has no direct-LLM path; the harness codes, the decider only decides.

Contract (already decided):
- Port stays named **`Decider`**; `protocol` metadata is `"system-one"` (native) vs
  `"openai-compat"` (emulated, `native_types=False`).
- **`DeciderInfo`** `{id, protocol, endpoint, model, native_types, local, free, api_key_env}`.
- **`DeciderCatalog`** (`list_deciders()`) + **`DeciderProvider`** (`decider_for(info)`).
- **`DeciderPolicy`**: native > emulated, local > hosted, free > paid; `preferred` id wins;
  `require_native` forbids emulation; fallback is `RuleDecider`.
- `OpenAICompatibleDecider` is kept as the *emulated* adapter (honestly labelled).

Done in the WIP:
- `src/herald/decision/port.py`: `ModelInfo/ModelCatalog/ModelProvider` →
  `DeciderInfo/DeciderCatalog/DeciderProvider`.
- `src/herald/decision/policy.py`: new `DeciderPolicy`.
- Deleted: `selection.py`, `openrouter.py`.

Remaining:
1. Move `ROUTE_QUESTION` / `HUMAN_QUESTION` / `QUESTION_MODEL` / `QUESTION_NEEDS_HUMAN`
   (were in `selection.py`) into `router.py`.
2. Fix imports in `factory.py`, `router.py`, `cli/commands.py`.
3. Add `EnvDeciderCatalog` (reads `HERALD_DECIDER_*`) and a `DeciderProvider` that builds
   `JevDecider` / `OpenAICompatibleDecider` / `RuleDecider`.
4. Rewrite `build_decider()` without `if primary ==`: catalog + policy + provider, falling
   back to rules.
5. Delete `tests/test_model_catalog.py`; fix `tests/test_decision.py` (uses
   `OpenRouterProvider.catalog`), `tests/test_openai_decider.py`, `tests/test_decision_calibration.py`.
6. `ruff check` + `pytest` green; verify live with Jev (key in `.env`, gitignored).
7. PR, CI, squash-merge.

## Next (after the refactor)

- **`HarnessCatalog` + `HarnessInfo` + `local-agent`**: choose the local binary
  (`opencode`/`claude`/`codex`) with metadata (`accepts_model`: flag vs subscription), clone
  the repo to `/tmp/herald-<taskid>`, run the harness, push `herald/<slug>` + draft PR, clean
  up. This is what unblocks real work on Ricardo's laptop (`opencode` + DeepSeek V4.1 Flash).
  Note: harness adds a model only when it accepts one (OpenCode does via `--model`; Claude
  Code / Codex use their subscription).

## Configuration (see .env, never committed)

- Decision: `HERALD_DECIDER=jev|openai-compat|rules|none`, `HERALD_DECIDER_THRESHOLD`,
  `HERALD_DECIDER_LOG`, `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`.
- Harness: `HERALD_HARNESS=opencode|claude|codex|command`, `HERALD_HARNESS_BINARY`,
  `HERALD_HARNESS_COMMAND`, `HERALD_HARNESSES`, `HERALD_HARNESS_DEFAULT`.
