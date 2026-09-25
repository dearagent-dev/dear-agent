# Model providers

A provider is configuration handed to the runner/harness. Dear Agent does not implement
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
    api_key_env: DEAR_AGENT_LOCAL_KEY
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

Dear Agent's queue must therefore tolerate hours-long tasks without treating slowness as
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

Dear Agent has no direct-LLM path: the harness codes, the decider only decides. The decision
layer is built from three ports in the core, each with pluggable adapters:

- `DeciderCatalog` — *what deciders exist*, with `protocol` (`system-one` native vs
  `openai-compat` emulated), endpoint, model, locality and credentials by name. The in-tree
  adapter is `EnvDeciderCatalog` (reads `DEAR_AGENT_DECIDER_*`); a static or remote catalog is a
  drop-in.
- `DeciderProvider` — *how to run a chosen decider*. The in-tree `EnvDeciderProvider` builds
  `JevDecider` (native System One), `OpenAICompatibleDecider` (emulated over a chat endpoint:
  OpenRouter, vLLM, llama.cpp), or `RuleDecider`.
- `DeciderPolicy` — *which one to prefer*: native over emulated, local over hosted, free over
  paid; a `preferred` id wins outright and `require_native` forbids emulation. If nothing
  qualifies, the deterministic `RuleDecider` is the fallback.

Configure with `DEAR_AGENT_DECIDER=rules|jev|openai-compat|none` (default `rules`). `jev` needs
`TYPESAFE_API_KEY`; `openai-compat` needs `DEAR_AGENT_DECIDER_BASE_URL` and `DEAR_AGENT_DECIDER_MODEL`
(with `OPENROUTER_API_KEY` as the default base URL's key). `DEAR_AGENT_DECIDER_THRESHOLD` sets the
confidence below which the rules answer is used, and `DEAR_AGENT_DECIDER_LOG` records every
decision for calibration.

### Self-hosted reference decider

The decision layer has no direct-LLM path and asks only small, typed questions, so the reference
self-hosted decider is **any local OpenAI-compatible server** — `llama.cpp`'s `llama-server`,
`vllm serve`, Colibri's `coli serve`, or a self-hosted Jev. There is no bespoke adapter to write;
`openai-compat` is the adapter, and the catalog marks a loopback endpoint as `local` so the
policy prefers it and no API key is required.

```sh
# e.g. llama.cpp: a small instruct model is enough (the decider answers choices/scores, not code)
llama-server -m qwen3-4b-instruct.gguf --port 8080

export DEAR_AGENT_DECIDER=openai-compat
export DEAR_AGENT_DECIDER_BASE_URL=http://127.0.0.1:8080/v1
export DEAR_AGENT_DECIDER_MODEL=qwen3-4b-instruct
# export DEAR_AGENT_DECIDER_THRESHOLD=0.6      # below this, the rules answer wins
# export DEAR_AGENT_DECIDER_LOG=postgres       # persist decisions for calibration

dear-agent decide "audit the auth module for timing side-channels"
```

A model-backed decider is always wrapped so an outage, a missing key, or a low-confidence answer
falls back to the deterministic `rules` (see `FallbackDecider`); the decider is advisory and
never a boundary. Keep the model small and local for privacy and cost — it only picks a class.

The same OpenAI-compatible plumbing powers the optional adversarial **reviewer** that looks at
the diff before the draft PR (ADR 0010). Enable it with `DEAR_AGENT_REVIEWER=openai-compat` and
`DEAR_AGENT_REVIEWER_BASE_URL`/`_MODEL`; point it at a **different model (or a local one)** than
the harness for an independent opinion. It can ask for one bounded revision and then the PR is
opened regardless; a reviewer outage never blocks the deliverable.

## Routing the harness per task

The same decision can pick the *harness*, not just the model. Set `DEAR_AGENT_HARNESSES` to map
routing classes to harnesses, e.g. `local:opencode,hosted:claude`, and `DEAR_AGENT_HARNESS_DEFAULT`
(the class used when there is no decider or the decision is unknown). The `RoutingRunner`
then asks the decider per task and dispatches: mechanical work to the light harness, reasoning
work to the strong one. With no decider configured, everything uses the default, so behavior
is unchanged. This is how "the router picks the model/harness per job" becomes real without
touching the executor.
