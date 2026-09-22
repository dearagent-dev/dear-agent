# Model providers

A provider is configuration handed to the runner/harness. Herald does not implement
inference; it points the harness at a model and lets it run.

## Categories

| Category | Examples | Use |
|---|---|---|
| **Hosted frontier** | Anthropic, OpenAI, OpenRouter | best quality, per-token cost |
| **Hosted cheap/fast** | DeepSeek V4.1 Flash (API), GLM-5.3-Flash (API) | high volume, agentic |
| **Local OpenAI-compatible** | llama.cpp `llama-server`, Colibri `coli serve`, vLLM | privacy, no per-token cost, batch |

## Configuration

Keep providers declarative and secret-free in-repo:

```
providers:
  - id: anthropic
    kind: openai-compatible        # or native adapter
    base_url: https://api.anthropic.com
    model: claude-...
    api_key_env: ANTHROPIC_API_KEY # name of the env var; never the value
  - id: local-colibri
    kind: openai-compatible
    base_url: http://127.0.0.1:8000/v1
    model: deepseek-v4.1-flash
    api_key_env: HERALD_LOCAL_KEY
```

Rules:
- Secrets are referenced by **environment variable name**, never stored in git.
- The same task can be re-run against a different provider (A/B, or quality vs speed).
- Local endpoints are first-class; a slow model is expected and fine for overnight work.

## Slow local models

Local models on CPU can run well below 1 tok/s. This changes *planning*, not correctness:

- Prefer **few, large, single-shot** tasks over many small agent loops.
- Prefer **long budgets** (hours) and durable resume.
- Prefer **input-oriented** prompts (review a diff/plan, produce a call chain) over open-
  ended generation.
- Keep the harness's prompt cache warm; avoid per-turn prompt churn (dates, re-reads).

Herald's queue must therefore tolerate hours-long tasks without treating slowness as
failure.

## Selection

Provider selection precedence: task hint → project default → global default. Record the
provider and model in the task evidence so results are reproducible.

## Deciding *which* provider

Rather than a static mapping, selection can be a typed decision: a small System One model
(see [ADR 0004](decisions/0004-decision-model.md)) scores a task as `local` or `hosted` — local
for mechanical work, hosted for architecture/security/debugging — with a calibrated
confidence. Below the threshold, fall back to the deterministic default. This runs behind a
`Decider` port so no core code depends on the vendor, and it is advisory: it picks a model,
it never authorizes an action.

The decision layer is built from two ports in the core, each with pluggable adapters:

- `ModelCatalog` — *what models exist*, with price, context and capabilities. Adapters:
  `OpenRouterCatalog` (live `/models`, free-first selection), or a static/local list.
- `ModelProvider` — *how to run a chosen model*. Adapters: `OpenAICompatibleProvider`
  (OpenRouter, vLLM, llama.cpp), and `JevDecider` for TypeSafe's System One API.

`SelectionPolicy` is vendor-neutral and picks the most convenient model from any catalog
(free first, then cheapest, then larger context; text-only, JSON-capable). Configure with
`HERALD_DECIDER=openrouter` and `OPENROUTER_API_KEY`; unset a model to use the free tier, or
set `HERALD_DECIDER_MODEL` to pin one. `HERALD_DECIDER_ALLOW_PAID=true` lets it choose a paid
model when no free one qualifies.

## Routing the harness per task

The same decision can pick the *harness*, not just the model. Set `HERALD_HARNESSES` to map
routing classes to harnesses, e.g. `local:opencode,hosted:claude`, and `HERALD_HARNESS_DEFAULT`
(the class used when there is no decider or the decision is unknown). The `RoutingRunner`
then asks the decider per task and dispatches: mechanical work to the light harness, reasoning
work to the strong one. With no decider configured, everything uses the default, so behavior
is unchanged. This is how "the router picks the model/harness per job" becomes real without
touching the executor.
