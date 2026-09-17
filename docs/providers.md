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
