# 0010 — Adversarial review before the PR (multi-provider debate)

- **Status:** accepted
- **Date:** 2026-09-25
- **Related:** [0004](0004-decision-model.md), [0007](0007-harness-isolation.md),
  [0009](0009-forge-api.md), [../architecture.md](../architecture.md)

## Context

A harness produces a change and Dear Agent opens a draft PR. A single model can be confidently
wrong, and the fix for "the model reviewed its own work" is a **second, independent model**
looking at the diff before a human does. The roadmap calls this "multi-provider debate: two
models review each other before a PR."

Constraints, from the existing design:

- Dear Agent has **no direct-LLM path for coding**: the harness codes and the decider decides
  (ADR 0004). A review is a *judgment about a change*, not a second implementation, so it fits
  the decision-layer shape (a typed verdict, advisory, fail-open).
- The deliverable is still a **draft PR a human lands**. A model must never be the landing gate;
  the reviewer cannot reject or abandon a task.
- A reviewer that re-runs the harness costs time and tokens, so the loop must be **bounded** and
  **opt-in**, and any failure must fall back to today's behavior.

## Decision

**Optionally run a second model (`Reviewer`) over the diff before publishing; on a "revise"
verdict the harness runs one more bounded round with the reviewer's notes, then the PR is opened
regardless.**

1. **`Reviewer` port.** `review(diff, instructions) -> ReviewVerdict`, where the verdict is
   `approved: bool` plus a `summary` and `notes`. `NoReviewer` always approves. `ModelReviewer`
   is any OpenAI-compatible chat endpoint (local or hosted), asked for a strict JSON verdict
   (`{"verdict": "approve"|"revise", "summary", "notes"}`), stdlib-only.
2. **Opt-in and bounded.** `DEAR_AGENT_REVIEWER=none|openai-compat` (default `none`);
   `DEAR_AGENT_REVIEWER_BASE_URL`/`_MODEL`/`_API_KEY`/`_TIMEOUT`. `DEAR_AGENT_DEBATE_ROUNDS`
   (default `1`) bounds how many times a "revise" sends the change back to the harness. With no
   reviewer, behavior is exactly as before.
3. **Fail-open.** A missing key, an unreachable endpoint, malformed JSON, or a timeout makes the
   executor **publish as today** and emits `review.skipped`; the reviewer is advisory, never a
   boundary.
4. **The human still lands it.** The verdict is recorded as evidence and appended to the PR body
   (`Reviewer: approved` / `Reviewer: requested a revision`); the draft PR remains the gate.
5. **Different provider is configuration, not code.** The reviewer reads its own
   base URL/model/key, so an operator can point it at a different model from the harness. It runs
   in the control process, not the harness container, so it holds only the reviewer key.

## Rationale

- A second model is the cheapest independent check that exists; the diff is already in Git, so
  the input is well-defined.
- Keeping it a `Reviewer` port with a strict JSON verdict mirrors `Decider` (ADR 0004): typed,
  advisory, swappable, and never a control path.
- Bounding the loop and failing open preserves the guarantees: the queue never stalls on a model,
  and the PR always appears for a human.

## Consequences

- **The diff is sent to the reviewer's model provider.** That is inherent to a model review; for
  "no source leaves the box", point the reviewer at a local model. It is not the transport
  (golden rule 1 still holds: the mailbox never carries code).
- **Extra latency and cost** when enabled: one review call, plus one harness re-run per "revise"
  round. Default off.
- **`GitPlane` gains a `diff` read**; `TaskExecutor` gains a `reviewer` and `debate_rounds` and a
  small `_debate` step before commit/publish; `ExecutedTask` carries the verdict as evidence.
- **New configuration** (`DEAR_AGENT_REVIEWER_*`, `DEAR_AGENT_DEBATE_ROUNDS`) documented in
  `TASKS.md` and [../providers.md](../providers.md).

## Alternatives

- **The harness reviews itself.** Rejected: no independence; the whole point is a second model.
- **The reviewer as a blocking gate.** Rejected: makes a model the landing gate and can stall the
  queue; the human PR review is the real gate.
- **A full agentic reviewer that edits the code.** Rejected for now: that is a second harness, and
  it would double the untrusted execution surface. The reviewer returns notes; the harness acts.
- **Unbounded debate until approval.** Rejected: cost/latency blow-up and a model could loop.

## Open questions

- Whether to require the reviewer to be a *different* provider/model than the harness and enforce
  it, or leave it to configuration.
- Whether to persist verdicts beyond the event log for calibration (like the decision log).
