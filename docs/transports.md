# Transports

A transport receives async messages (inbound) and sends status/approval messages
(outbound). Herald's core is transport-agnostic; these are adapters behind one interface:

```
inbound()  -> Iterable[RawMessage]         # webhook push or poller
send(thread, subject, body, headers=None)  # reply in-thread
```

Do not put business logic in a transport. Cloudflare Workers, if used, only forward
normalized events to the daemon.

## Comparison

| Provider | Send | Receive | Async model | Notes |
|---|---|---|---|---|
| **Cloudflare Email Service** | REST / SMTP / Worker `env.EMAIL.send()` | Email Routing → Worker `email()` handler | webhook | Recommended primary. Serverless, send+receive in one platform, Agents SDK `onEmail` hook, Email MCP server, `wrangler` CLI, and an official `agentic-inbox` reference app. Sending is on the Workers paid plan; receiving works on free. |
| **AgentMail.to** | REST + Python/TS SDKs | webhooks / WebSockets / IMAP | webhook/ws | Drop-in "inbox API for AI agents": programmatic inboxes, threading, attachments, custom domains with SPF/DKIM/DMARC, MCP server. Fastest path if you do not want to build on Cloudflare. |
| **Fastmail (JMAP)** | JMAP `Email/set` | JMAP Push (EventSource) + `Email/get` | push/poll | Standards-based, fully async, excellent threading/search. Best if you want to own the agent glue and avoid vendor-specific APIs. |
| **forwardemail.net** | API / SMTP | forwarding + webhook | webhook | Privacy-focused; usable fallback. Check API/webhook maturity for bidirectional threads. |
| **Postmark / Resend / SES / Mailgun** | REST API | inbound webhooks (Postmark, SES, Mailgun) | webhook | Strong outbound deliverability; inbound support varies. Good for notifications, weaker for full bidirectional threads. |
| **IRC** | server | bouncer (ZNC) history | realtime | Fun, but no store-and-forward: a disconnected agent misses messages. Secondary only. |

## Recommendation

- **Primary:** Cloudflare Email Service — send + receive + agent hook in one serverless
  platform. Add a thin Worker that normalizes inbound mail and POSTs to the Herald
  daemon; use the REST API for outbound.
- **Fast alternative:** AgentMail.to — an inbox per agent with webhooks, if the goal is to
  validate the concept without building email plumbing.
- **Standards option:** Fastmail JMAP — for a vendor-neutral, push-based inbox.

The core must ship the interface above so all three are interchangeable.

## Message contract

Inbound message → task fields. Keep it human-friendly and parseable:

```
Subject: [herald] owner/repo: <short task>

<free-form instructions>

Allowed metadata (optional, one per line):
  repo:      https://github.com/owner/repo
  base:      main
  model:     local:qwen3.6 | anthropic:claude | openai:gpt   (optional)
  branch:    herald/<slug>                                   (optional)
```

Rules:
- The repo may also be inferred from the recipient address (e.g. `owner-repo@herald.…`).
- **Never** accept source code, patches, or attachments as the task. If present, reject
  with a reply explaining that code travels over Git only.
- Deduplicate on `Message-ID` (or the transport's stable event id).

Outbound status/approval messages are always **in-thread** and contain only: state,
summary, and links (branch, commit SHA, PR). No source.

## Security

- SPF/DKIM/DMARC must be configured on the domain; reject or quarantine unauthenticated
  mail if the provider supports it.
- Treat the `From` as advisory; authorize by a shared secret in the body or a signed
  header/reply token, not by address alone.
- Rate-limit inbound per sender; a burst must not spawn a burst of agent runs.
