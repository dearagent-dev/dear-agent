# 0004 — A decision model at the edges (Jev / System One)

- **Status:** proposed
- **Date:** 2026-09-22
- **Supersedes:** none
- **Related:** [0001-language](0001-language.md), [0002-deployment-topology](0002-deployment-topology.md), [../security.md](../security.md), [../providers.md](../providers.md)

## Context

Herald makes several small judgments that sit awkwardly between a hand-written `if` (too
brittle) and a full chat-LLM call (too slow and too expensive to do per message):

- Is this inbound message a prompt-injection attempt? (`InjectionScanner`, M6.4)
- Is this message body actually source code / a patch smuggled over the transport?
  (golden rule 1 — today only attachment metadata is checked)
- Which model class should run this task: the small local endpoint or a strong hosted one?
  (provider registry, M6.1)
- Does this action need a human before it runs? (approval gate, M4)
- Is this idle proposal worth queueing at all? (`IdleLoop`, M5)

A new class of model appeared in September 2026 — **System One models**, first shipped as
**Jev** by TypeSafe AI — that targets exactly this shape of problem. This ADR records what
we verified, what we measured, and the narrow, optional, fail-open way Herald would use it.

## What Jev is (verified)

Jev is **not an LLM**. It does not generate text. You send a `state` (any context) and typed
`questions`; it returns typed `answers` with calibrated probabilities, in a single parallel
pass:

- `choice` — pick one of up to 255 labelled options → `choice` + `probabilities` + `confidence`
- `score` — place the state on an ordered rubric → `score` + distribution
- `noul` — a calibrated yes/no as a probability in `[0, 1]`

Because the answer space is fixed by the request, the response shape cannot be malformed
("no type errors"). TypeSafe reports 70–500 ms and $0.042/MTok input, output free. The
earlier "no hallucination" claim should be read precisely: the *format* cannot be invented,
but the *judgment* can still be wrong. The vendor's own docs call this out, and the
community reproductions note that returned distributions are **not** calibrated
probabilities of correctness.

Access is a hosted API (`api.typesafe.ai/v1/systemone`, model `jev-latest`, currently
`jev-1.13.0`). There are open, self-hostable reimplementations of the *shape* (Simple Jev,
SemIf, NanoJev, Laya, qwen-rlcd, and more), and an official `system-one-adapter-python`
that serves the same typed interface from OpenAI/Anthropic models. Jev itself is closed,
single-vendor, hosted-only.

## Measurements (2026-09-22, `jev-latest` / `jev-1.13.0`)

We ran Herald-shaped tasks against the hosted API.

| Test | Result |
|---|---|
| Prompt-injection, 6 crafted cases | 5/6 at `noul ≥ 0.5`; the ambiguous case returned `noul=0.27` and a low-confidence `review` verdict — honest uncertainty, not a miss |
| Prompt-injection, harder 15-case set | 12/15; all misses were the *subtle* exfil/via-CI variants, scored 0.22–0.45 with low confidence |
| Model routing, 14 tasks | **14/14**, with clean separation: local tasks confidence 0.99–1.0, complexity < 1.5; hosted tasks 0.91–1.0, complexity > 2.5 |
| Full dispatch (model + harness + human), 10 tasks | model 10/10; human gate 7/10 (every "miss" was conservatively asking for approval); harness 7/10 |
| Ambiguous input ("add pagination and a test") | `choice` confidence **0.23** — low confidence is a usable signal |

Cost per decision observed: ~300–420 input tokens; latency 0.65–0.85 s from this host.
Independent third-party evaluations are mixed-to-positive: a blind injection run on 662
messages reports ~96.5% accuracy / ROC-AUC 0.99, while TypeSafe's own workflows score
61.7–76.0% accuracy. It is a good classifier, not an oracle.

## Decision

1. **Use Jev as an *advisory decision layer*, never as a security boundary or a control
   path.** Same stance as `InjectionScanner`: it can raise `suspicious`, escalate, or bias a
   route; it can never authorize a shell command, a push, or a merge. Golden rule 7 stands.

2. **Wrap it behind a `Decider` port, like every other provider.** No core code imports a
   vendor SDK. Ship two adapters: `JevDecider` (hosted, via `TYPESAFE_API_KEY` /
   `HERALD_DECIDER_*`) and a `RuleDecider` that reproduces today's deterministic behavior.
   Selecting a decider is configuration, per M6.1 and golden rule 5.

3. **Local-first when self-hosting matters.** For deployments that cannot call a hosted
   service, point the same port at a self-hosted System One shape (a small `openjev`-style
   server over an OpenAI-compatible endpoint on the operator's host) or at the official
   `system-one-adapter` over an already-configured LLM. The interface is the contract, not
   the vendor.

4. **Fail open to the rule-based path.** If the decider is unreachable, over budget, or
   returns low confidence, Herald falls back to the deterministic rule and continues. A
   decision model outage must never stop the queue.

5. **Thresholds are earned, not guessed.** Every gate ships with a confidence cutoff
   validated against labeled examples from our own traffic; below it, take the safe branch
   (accept a benign task, reject/escalate a suspicious one, use the default model).

6. **Start with the two highest-value edges**, not everywhere:
   - **Model routing** (M6.1): one `choice` (`local` / `hosted`) plus a `noul` "needs a
     human", replacing a static provider mapping. This directly serves the goal of running
     small models locally and only paying for reasoning when it is needed.
   - **Injection + "no source over transport"** (M6.4 + golden rule 1): a `noul` pair
     wired into the control plane as an *additional* advisory signal next to the existing
     scanner and the attachment check.

## Consequences

- **Good:** cheap, fast, typed judgments that fit software directly; a principled way to
  route work to local vs hosted models; a stronger, honest sensitivity signal for incoming
  mail — with confidence we can branch on.
- **Cost:** one more optional external dependency (hosted, metered) and one more config
  surface. Mitigated by the port + rule fallback: with no key configured, Herald behaves
  exactly as today.
- **Risk:** over-trusting a classifier. Mitigated by (1)–(5): advisory only, fail open,
  earned thresholds, deterministic fallback, and never a boundary.
- **Not a replacement for a harness.** Jev does not write code; it decides. Herald still
  wraps OpenCode/Claude Code/Codex (golden rule 3).

## Alternatives considered

- **Rules only.** Keeps zero dependencies, but the injection misses above (inline diffs,
  encoded blobs, CI-disabling edits) are exactly what rules miss. Rules stay as fallback.
- **A full LLM classifier.** Works, but is 10–400× the cost and latency for a job that is a
  pure typed decision; it also reintroduces format-parsing failure modes Jev removes.
- **Vendor lock-in to TypeSafe.** Rejected: the `Decider` port plus open reimplementations
  keep the deployment vendor-neutral.

## Open questions

- Which self-hosted shape do we bless as the reference local decider (Simple Jev vs Laya)?
- Where does the decider run: in-process in the control plane, or its own Deployment?
- Do we log every decision for future threshold tuning (and where — with
  [0005](0005-state-store.md), the Postgres state store is the natural home; a file remains
  fine for a single-process dev run)?
