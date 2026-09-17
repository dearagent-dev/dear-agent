# Herald

**An async, email-first inbox that turns messages into Git pull requests produced by coding agents.**

Herald is the **control plane** for background coding work. You send an email (or any
async message), an agent works in its own Git worktree, and the result comes back as a
**pull request you review**. Code never travels over the transport — only task metadata,
status, and links. **Git is the artifact plane; email is the queue.**

> Why: existing coding-agent harnesses (OpenCode, Claude Code, Codex) are excellent at
> running a task, but the *enqueue* side is unsolved for unattended, offline-first work.
> Chat transports (Telegram/OpenClaw/Hermes) are synchronous and carry code badly.
> Herald makes the inbox the queue and the PR the deliverable.

## Why Herald

- **Fully asynchronous.** Send a task and disconnect. Work survives restarts, reboots and
  client disconnects; results wait in an inbox.
- **No code over the wire.** The transport carries a task, a repo, and a branch/PR link —
  never source. Removing source from the transport removes the enqueueing problem *and*
  makes every result a reviewable PR.
- **Pluggable models.** A hosted frontier model (Anthropic/OpenAI/OpenRouter) or a **local
  model** (llama.cpp / Colibri / vLLM behind an OpenAI-compatible endpoint). Slow local
  models are first-class for overnight batches.
- **Pluggable harnesses.** OpenCode, Claude Code, Codex, or a custom runner. Herald does
  not replace your harness; it wraps it.
- **Human-gated.** Approval requests land in an *Action* queue; a reply approves or
  rejects. `main` is never written by an agent.
- **Creative when idle.** With an empty queue, Herald can propose its own work from recent
  repository activity and trends (a "night shift"), instead of idling.

## Architecture (conceptual)

```
        ┌──────────────┐   inbound    ┌───────────────┐   enqueue    ┌──────────────┐
        │  Transport   │─────────────▶│  Normalizer   │─────────────▶│    Queue     │
        │ email/JMAP/… │              │ → Task        │              │ (durable)    │
        └──────▲───────┘              └───────────────┘              └──────┬───────┘
               │ outbound (status/approval)                                │ claim
               │                                                           ▼
        ┌──────┴───────┐   notify     ┌───────────────┐   branch/PR   ┌──────────────┐
        │  Notifier    │◀─────────────│   Runner      │──────────────▶│  Git plane   │
        │ (reply ctx)  │              │ + Provider    │               │ branch + PR  │
        └──────────────┘              └───────┬───────┘               └──────────────┘
                                              │ no queued work
                                              ▼
                                       ┌──────────────┐
                                       │ Idle/Creative│
                                       │    loop      │
                                       └──────────────┘
```

See [`docs/architecture.md`](docs/architecture.md).

## Components

| Component | Responsibility |
|---|---|
| **Transport** | inbound: receive a message → webhook/IMAP/JMAP; outbound: send status and approval mail. See [`docs/transports.md`](docs/transports.md). |
| **Normalizer** | message → `Task` (repo, instructions, constraints, reply token). |
| **Queue** | the mailbox itself: task records, states, dedupe, approvals. See [`docs/queue.md`](docs/queue.md). |
| **Runner** | executes a harness in an isolated worktree; drives provider selection. |
| **Provider** | model backend (hosted or local OpenAI-compatible). See [`docs/providers.md`](docs/providers.md). |
| **Git plane** | worktree, branch, commit, push, draft PR. Nothing reaches `main`. |
| **Notifier** | sends results/approvals back over the transport, threading replies. |
| **Idle loop** | proposes new work when the queue is empty. |

## Transports

| Provider | Send | Receive | Notes |
|---|---|---|---|
| **Cloudflare Email Service** | REST/SMTP/Worker binding | Email Routing → Worker `email()` | Optional forwarder; contains no business logic. |
| **AgentMail.to** | API | webhooks/WebSockets/IMAP | Drop-in "inbox API for agents". |
| **Fastmail (JMAP)** | JMAP | JMAP Push | **Recommended primary: the mailbox is the queue.** |
| **forwardemail.net** | API/SMTP/IMAP | webhook/forward | Usable fallback. |
| **Postmark / Resend / SES** | API | inbound webhooks (some) | Good for outbound notifications. |
| **IRC** | — | bouncer history | Fun, but not store-and-forward; secondary. |

## Model providers

Hosted (Anthropic, OpenAI, OpenRouter) or **local** via any OpenAI-compatible endpoint
(llama.cpp `llama-server`, Colibri `coli serve`, vLLM). A local, slow model is a valid
target: Herald's queue tolerates hours-long tasks.

## Status

**M1 (queue).** The design (M0) is complete. The durable queue is the transport mailbox
(Fastmail JMAP first); there is no database. Start with [`AGENTS.md`](AGENTS.md) and
[`docs/roadmap.md`](docs/roadmap.md).

## Non-goals

- Not a chat UI, and not a Telegram/Slack bot.
- Not a replacement for OpenCode/Claude Code/Codex.
- Never transports source code, patches, or attachments over the message transport.
- Never merges to `main`; every change is a PR a human approves.

## License

MIT — see [`LICENSE`](LICENSE).

## Prior art

Herald learns from `ElectricJack/agent-queue` (durable task graph + review gates),
`voundbrand/overnight` (review-driven PR loop) and `a20185/OvernightAgent` (resumable,
worktree-isolated runs). It combines their ideas around a transport-agnostic, Git-first
model.
