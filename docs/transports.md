# Transports

A transport receives async messages (inbound) and sends status/approval messages
(outbound). Dear Agent's core is transport-agnostic; these are adapters behind one interface:

```
inbound()  -> Iterable[RawMessage]         # webhook push or poller
send(thread, subject, body, headers=None)  # reply in-thread
```

Do not put business logic in a transport. Cloudflare Workers, if used, only forward
normalized events to the daemon.

## Inbound webhook

Besides polling a mailbox, the control plane exposes `POST /inbound` for push transports
(Cloudflare Email Worker, AgentMail, Postmark, …). The body is JSON:

```json
{
  "id": "<Message-ID or event id>",
  "thread_id": "<optional>",
  "sender": "dev@example.com",
  "subject": "add healthz",
  "body": "repo: https://github.com/o/r\nadd a /healthz endpoint",
  "headers": {},
  "attachments": [{"name": "patch.diff", "content_type": "text/x-diff", "size": 123}]
}
```

`id` is the dedupe key: re-delivery is idempotent. The request must carry
`X-Dear-Agent-Signature: sha256=<hmac>` over the raw body (with the sender bound in), computed
with `DEAR_AGENT_INBOUND_SECRET`; without that secret the endpoint returns 503. A valid
signature with a disallowed sender is 403, a bad signature is 401, malformed JSON is 400,
and an accepted message is 202. Attachments are still rejected by the Normalizer — they
are reported so the rejection is explicit, never stored.

## JMAP push (EventSource)

For Fastmail, `dear-agent listen` opens the session's `eventSourceUrl` with the API token and
ingests when an `Email` `StateChange` arrives. A `StateChange` says only *that* something
changed, so the listener re-polls (idempotent) rather than trusting the event. The SSE
parser and the callback are socket-free and unit-tested; `JmapClient.push_create` can also
register a webhook `url` so Fastmail POSTs to `POST /inbound` instead.

## IMAP + SMTP

For any provider that speaks standard mail — Gmail, Outlook/M365, Yahoo, iCloud, Fastmail,
self-hosted, or Proton via Bridge — `DEAR_AGENT_BACKEND=imap` uses `ImapSmtpTransport`: IMAP
lists `UNSEEN` and fetches the message, SMTP submits a threaded reply. `dear-agent poll` runs one
ingest pass (driven by a loop or a Kubernetes `CronJob`), sharing the same inbound wiring as
the webhook and JMAP paths. Processed messages are filed into
`DEAR_AGENT_IMAP_DONE_MAILBOX` (`Dear-Agent-Done`), so they are not reprocessed.

Config: `DEAR_AGENT_IMAP_HOST|PORT|USER|PASSWORD|SSL|MAILBOX|DONE_MAILBOX` and
`DEAR_AGENT_SMTP_HOST|PORT|USER|PASSWORD|STARTTLS|SSL|FROM`.

## Comparison

| Provider | Send | Receive | Async model | Notes |
|---|---|---|---|---|
| **Cloudflare Email Service** | REST / SMTP / Worker `env.EMAIL.send()` | Email Routing → Worker `email()` handler | webhook | Recommended primary. Serverless, send+receive in one platform, Agents SDK `onEmail` hook, Email MCP server, `wrangler` CLI, and an official `agentic-inbox` reference app. Sending is on the Workers paid plan; receiving works on free. |
| **AgentMail.to** | REST + Python/TS SDKs | webhooks / WebSockets / IMAP | webhook/ws | Drop-in "inbox API for AI agents": programmatic inboxes, threading, attachments, custom domains with SPF/DKIM/DMARC, MCP server. Fastest path if you do not want to build on Cloudflare. |
| **Fastmail (JMAP)** | JMAP `Email/set` | JMAP Push (EventSource) + `Email/get` | push/poll | Standards-based, fully async, excellent threading/search. Best if you want to own the agent glue and avoid vendor-specific APIs. |
| **Any IMAP/SMTP** | SMTP | IMAP (poll; IDLE later) | poll | Universal: Gmail, Outlook, Yahoo, iCloud, Fastmail, self-hosted, Proton via Bridge. `DEAR_AGENT_BACKEND=imap`; no vendor API. |
| **forwardemail.net** | API / SMTP | forwarding + webhook | webhook | Privacy-focused; usable fallback. Check API/webhook maturity for bidirectional threads. |
| **Postmark / Resend / SES / Mailgun** | REST API | inbound webhooks (Postmark, SES, Mailgun) | webhook | Strong outbound deliverability; inbound support varies. Good for notifications, weaker for full bidirectional threads. |
| **IRC** | server | bouncer (ZNC) history | realtime | Fun, but no store-and-forward: a disconnected agent misses messages. Secondary only. |

## Recommendation

- **Primary:** Cloudflare Email Service — send + receive + agent hook in one serverless
  platform. Add a thin Worker that normalizes inbound mail and POSTs to the Dear Agent
  daemon; use the REST API for outbound.
- **Fast alternative:** AgentMail.to — an inbox per agent with webhooks, if the goal is to
  validate the concept without building email plumbing.
- **Standards option:** Fastmail JMAP — for a vendor-neutral, push-based inbox.
- **Portability:** IMAP + SMTP (`DEAR_AGENT_BACKEND=imap`) — the universal fallback that also
  covers self-hosted and Proton (via Bridge), independent of any vendor API.

The core ships the interface above, so all of these are interchangeable: swapping providers
is a configuration change, not a code change.

## Message contract

Inbound message → task fields. Keep it human-friendly and parseable:

```
Subject: [dear-agent] owner/repo: <short task>

<free-form instructions>

Allowed metadata (optional, one per line):
  repo:      https://github.com/owner/repo
  base:      main
  model:     local:qwen3.6 | anthropic:claude | openai:gpt   (optional)
  branch:    dear-agent/<slug>                                   (optional)
  verify:    pytest -q                                       (optional; allowlisted)
  depends-on: <task-id>,<task-id>                            (optional)
```

Rules:
- Metadata lines are case-insensitive and stripped from the instructions.
- The repo may also be inferred from the recipient address (e.g. `owner-repo@dear-agent.…`);
  an explicit `repo:` line wins over the routing address.
- **Never** accept source code, patches, or attachments as the task. If present, reject
  with a reply explaining that code travels over Git only.
- A message with no resolvable repo, or with no instructions after metadata is removed, is
  rejected with a reason instead of being guessed at.
- Deduplicate on `Message-ID` (or the transport's stable event id).
- `verify:` runs in the worktree after the harness and before the PR; if it fails, the task
  fails and escalates. It executes only when its full argv matches an operator allowlist entry
  exactly (`DEAR_AGENT_VERIFY_ALLOW`), under the same sandbox as the harness, never through a
  shell (golden rule 7).
- `depends-on:` lists task ids that must be `done` before this task runs. Until then it stays
  `queued` and is skipped by the sweep and `dear-agent run`; if a dependency fails, the task is
  failed too. This is a small typed dependency graph (e.g. plan → implement → test).

The Normalizer returns either a `NormalizedTask` (`Task` + `TaskSpec`) or a `Rejected`
with a `RejectReason` (`attachments`, `no_repo`, `empty`). It never executes message text.

Outbound status/approval messages are always **in-thread** and contain only: state,
summary, and links (branch, commit SHA, PR). No source.

## Security

- The email transports verify the **receiving MTA's** `Authentication-Results` (SPF/DKIM/
  DMARC). `DEAR_AGENT_AUTH_DOMAINS` allowlists the DMARC-aligned domain and the `From` domain
  must match it, which defeats a spoofed `From`; `DEAR_AGENT_ALLOWED_SENDERS` narrows further.
  A missing verdict fails closed.
- The push/webhook path is stronger: authorize by the HMAC signature (`DEAR_AGENT_INBOUND_SECRET`)
  over the raw body, with the sender bound in, not by address alone.
- Rate-limit inbound per sender; a burst must not spawn a burst of agent runs.
