# Demo runbook (internal, ~10 minutes)

The story: **an email becomes a reviewed pull request, produced by a coding agent that never
holds a credential.** One message in, one draft PR out — and a human lands it.

Everything below runs against the `dear-agent` namespace of the cluster
(`api-virt-daytwo-dev`), with the two-container runner and a real mailbox. The rehearsal
command is `scripts/demo.sh run`.

## 1. What the audience sees

| # | On screen | Time |
|---|---|---|
| 1 | The architecture in one slide: `email → PostgreSQL queue → sweep → runner Job (control + harness) → draft PR` | 1 min |
| 2 | The email being sent (from your phone or laptop) to the mailbox | 30 s |
| 3 | `scripts/demo.sh preflight` — all green | 20 s |
| 4 | `scripts/demo.sh watch` — the poll ingests, the sweep dispatches, the harness runs, the Job finishes | 1–3 min |
| 5 | The **draft PR** in `dear-agent-lab-python`, with the fix and the tests passing | 1 min |
| 6 | The two-container boundary: `oc get job -o yaml` showing the harness container's env has no DSN, no git key, no token | 1 min |
| 7 | The human gate: the PR is a **draft**; the merge button is the only way it lands | 30 s |

Points 2–5 are the demo. Points 1, 6 and 7 are what make it credible; do not skip 6.

## 2. Before the audience arrives (10 minutes earlier)

```sh
cd ~/Documents/Projects/dear-agent
scripts/demo.sh preflight          # namespace, workloads, secrets, runner template, model, queue
scripts/demo.sh reset              # remove last rehearsal's Jobs
oc -n dear-agent exec deploy/dear-agent-control-plane -- dear-agent task ls
```

`preflight` exits non-zero if anything that would break the run live is wrong. It checks the
thing that already ruined one rehearsal: **the model credential**. The harness calls the
provider on every run, so a key that is out of credit (or capped so low that OpenCode's default
32k output tokens cannot be afforded) makes the agent die mid-run with an opaque
`Unexpected server error`. The check reads the balance and fails loudly:

```sh
$ scripts/demo.sh preflight
==> Model credential (the demo's one external dependency)
  provider: openrouter · small model: openrouter/deepseek/deepseek-chat-v3.1
  openrouter balance is $-0.12: a run will die mid-way.
  xx the model credential is not usable (see above): fix it before demoing
```

Two OpenCode details worth knowing before a demo:

- **`small_model`**: OpenCode asks a second, "small" model for the session title. Its default is
  `google/gemini-nano-banana-2.1` — an *image* model — which OpenRouter refuses on a small
  balance (`requires at least $1.00 in balance for image or video output`). Set
  `small_model` in `OPENCODE_CONFIG_CONTENT` to a text model.
- **output tokens**: OpenCode requests up to 32k output tokens. A key with a low spend limit
  answers `This request requires more credits, or fewer max_tokens`. Cap it in the same secret:

  ```jsonc
  // dear-agent-model → OPENCODE_CONFIG_CONTENT
  {
    "provider": { "openrouter": { "options": { "apiKey": "..." },
      "models": { "deepseek/deepseek-chat-v3.1": { "limit": { "context": 64000, "output": 6000 } } } } },
    "small_model": "openrouter/deepseek/deepseek-chat-v3.1"
  }
  ```

- The queue must be **empty** at the start (or say out loud what is already in it).
- Close any draft PR left by a rehearsal in `dear-agent-lab-python` — the demo should add the
  next number, not explain a stale one.
- Keep the mailbox ("Dear Agent" at Fastmail) empty; `scripts/demo.sh poll` prints the ingest
  counts, so a leftover message is visible.

Timings to expect: the poll CronJob runs every minute, the sweep every two, and the runner Job
takes **60–120 s** (clone, agent, commit, push, PR). `scripts/demo.sh` triggers both CronJobs
by hand so the audience waits for the *agent*, not for a timer.

## 3. The live sequence

```sh
# (1) Send the email — the presenter does this from their own client.
#     Body:
#       repo: https://github.com/dearagent-dev/dear-agent-lab-python.git
#
#       Fix the two bugs in src/calculator.py: add() must add and is_even() must be True
#       for even numbers. Run the unit tests afterwards.

# (2) Ingest it now instead of waiting for the minute tick, then follow the run.
scripts/demo.sh poll
scripts/demo.sh sweep
scripts/demo.sh watch
```

`scripts/demo.sh watch` waits for the runner Job, streams the harness container's output,
then prints the control container's last lines, the task's state/events and the **PR URL**.

If email is being unreliable (the mailbox is external to the cluster), the same run can start
from the signed webhook, which exercises everything except the mail provider:

```sh
scripts/demo.sh run \
  --repo https://github.com/dearagent-dev/dear-agent-lab-python.git \
  --instructions "Fix the two bugs in src/calculator.py: add() must add and is_even() must be True for even numbers. Run the unit tests afterwards."
```

## 4. The live console (recommended for the screen)

`scripts/demo_console.py` is a read-only web view of the same run, for the projector:

```sh
.venv/bin/python scripts/demo_console.py         # http://127.0.0.1:8765
```

It shows, side by side:

- **The mail the agent received** — sender, subject, repository, and the instructions the
  normalizer extracted from the body, exactly as the runner sees them.
- **What the agent does with it** — the pipeline as stages that light up as the events land
  (`task.accepted` → queued → runner Job → agent working → verify → draft PR), with the elapsed
  time and, on a failure, the reason.
- **The agent at work** — the tail of the credential-free `harness` container's stdout: the
  coding agent reading the repository and editing files. It never sees the database, the git key
  or the forge token.

It reads the queue through a port-forward and the log through `oc logs`; it writes nothing and
holds no credential of its own. Open it *before* the mail arrives: the panel flashes when a new
message lands, which is the moment the demo is about.

## 5. Showing the credential boundary (recommended)
```sh
oc -n dear-agent get job -l app.kubernetes.io/component=runner -o name | tail -1
oc -n dear-agent get job <job> -o jsonpath='{.spec.template.spec.containers[*].env[].name}{"\n"}{.spec.template.spec.containers[*].envFrom[*].secretRef.name}{"\n"}'
```

The `control` container has `DEAR_AGENT_DATABASE_URL`, the git keys and `dear-agent-forge`. The
`harness` container has only the model secret. That is ADR 0007, and it is why the harness can
make a bad pull request but cannot read the queue or the forge token.

## 6. If something goes wrong

| Symptom | What to say / do | Fix |
|---|---|---|
| No runner Job appears | "the sweep runs every two minutes; let me trigger it" | `scripts/demo.sh sweep` |
| Task parked in `action` | The decision layer asked for a human on purpose | `dear-agent approval pending --show-tokens`, then `approval approve <token>` |
| Job fails `PUBLISH_FAILED` | "it failed at the publish step, not the agent" | check `dear-agent-forge` and the repo URL (must be HTTPS) |
| Job fails `HARNESS_MISSING` | the harness image or the shim | check `DEAR_AGENT_HARNESS_IMAGE` and the image tag |
| The agent edits nothing | the task needs to be explicit | re-run with instructions that name the files |
| Job fails with `Unexpected server error` | the model key is out of credit or capped too low | `scripts/demo.sh preflight` names it; top up or cap `limit.output` |
| `/inbound` answers `503` | the inbound secret is missing | recreate `dear-agent-inbound` |
| `/inbound` answers `403` | the sender is not allowed | add it to `DEAR_AGENT_ALLOWED_SENDERS`, or drop the gate for the demo |
| `/inbound` answers `401` | HMAC mismatch | sign the **raw** body as `sender\nbody` |

Fallback if the mailbox is down or slow: the `scripts/demo.sh run` path above. Have it in your
history, do not fumble it on stage.

## 7. Cleanup

```sh
scripts/demo.sh reset                     # deletes the runner and triggered Jobs
gh pr close <n> --repo dearagent-dev/dear-agent-lab-python --delete-branch   # the draft PR
```

The queue keeps the finished task as evidence (`dear-agent task ls --state done`); leave it or
clear it with `dear-agent task requeue`/rejection, never by deleting rows by hand.

## 8. What the demo does **not** claim

- The landing is human-gated: no agent merges. The PR is a **draft**.
- The harness has the model credential by design; the *infrastructure* credentials are the ones
  it never sees.
- The demo uses the lab repository's intentionally broken calculator: the point is the pipeline,
  not the patch.
- The single-container runner (`DEAR_AGENT_RUNNER_TEMPLATE_CONFIGMAP=dear-agent-runner-template`)
  is **not** used: there the write key and the forge token are visible to the harness.
