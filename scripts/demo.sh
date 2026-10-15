#!/usr/bin/env bash
# Drive the Dear Agent demo on a cluster: send a task, watch it run, show the draft PR.
#
#   scripts/demo.sh preflight                       # is the cluster ready? (no changes)
#   scripts/demo.sh enqueue --repo URL --instructions TEXT
#   scripts/demo.sh poll                            # trigger the email-ingestion CronJob now
#   scripts/demo.sh sweep                           # trigger the dispatch CronJob now
#   scripts/demo.sh watch [--timeout SECONDS]       # follow the runner Job and print the PR
#   scripts/demo.sh run --repo URL --instructions TEXT   # enqueue + poll + sweep + watch
#   scripts/demo.sh reset [--close-prs]             # clean up what the demo created
#
# The CronJobs run every minute (poll) and two minutes (sweep) in production; the demo
# triggers them by hand so the audience does not watch a timer. Nothing here is required by
# the product path — it is the same queue, the same Job template and the same forge.
#
# See docs/demo.md for the runbook (scenario, timings, fallbacks).
set -uo pipefail

NAMESPACE="${NS:-dear-agent}"
REPO=""
INSTRUCTIONS=""
SENDER="${DEAR_AGENT_DEMO_SENDER:-ops@dearagent.dev}"
TIMEOUT=900
CLOSE_PRS=0
COMMAND="${1:-}"
shift || true

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --instructions) INSTRUCTIONS="$2"; shift 2 ;;
    --sender) SENDER="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --close-prs) CLOSE_PRS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if command -v oc >/dev/null 2>&1; then KUBECTL=oc; else KUBECTL=kubectl; fi
command -v "$KUBECTL" >/dev/null 2>&1 || { echo "$KUBECTL not found" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "python3 not found" >&2; exit 2; }

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mok\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!!\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mxx\033[0m %s\n' "$1"; }

control() { "$KUBECTL" -n "$NAMESPACE" exec deploy/dear-agent-control-plane -- "$@"; }

# --------------------------------------------------------------------------- preflight
preflight() {
  local failed=0
  step "Cluster"
  if "$KUBECTL" get namespace "$NAMESPACE" >/dev/null 2>&1; then
    ok "namespace $NAMESPACE"
  else
    bad "namespace $NAMESPACE is missing"; failed=1
  fi
  if [[ "$failed" == 1 ]]; then exit 1; fi

  step "Workloads"
  for kind_name in "deployment/dear-agent-control-plane" "statefulset/dear-agent-postgres"; do
    if "$KUBECTL" -n "$NAMESPACE" get "$kind_name" >/dev/null 2>&1; then ok "$kind_name"; else bad "$kind_name missing"; failed=1; fi
  done
  for cron in dear-agent-poll dear-agent-sweep; do
    if "$KUBECTL" -n "$NAMESPACE" get "cronjob/$cron" >/dev/null 2>&1; then ok "cronjob/$cron"; else bad "cronjob/$cron missing"; failed=1; fi
  done

  step "Credentials (presence only; values are never printed)"
  local secret_key names
  for secret in dear-agent-postgres dear-agent-inbound dear-agent-forge dear-agent-model; do
    if "$KUBECTL" -n "$NAMESPACE" get "secret/$secret" >/dev/null 2>&1; then ok "secret/$secret"; else warn "secret/$secret missing"; fi
  done
  if "$KUBECTL" -n "$NAMESPACE" get secret dear-agent-postgres -o jsonpath='{.data.DATABASE_URL}' >/dev/null 2>&1; then
    ok "database DSN is present"
  else
    bad "dear-agent-postgres has no DATABASE_URL"; failed=1
  fi
  if [[ "$($KUBECTL -n "$NAMESPACE" get secret dear-agent-forge -o jsonpath='{.data}' 2>/dev/null)" == *GH_TOKEN* ||
        "$($KUBECTL -n "$NAMESPACE" get secret dear-agent-forge -o jsonpath='{.data}' 2>/dev/null)" == *GITLAB_TOKEN* ]]; then
    ok "forge token is present (draft PR will open)"
  else
    warn "no forge token: the runner will fail at publish"
  fi

  step "Runner template"
  local template harness_image
  template="$($KUBECTL -n "$NAMESPACE" get cm dear-agent-config -o jsonpath='{.data.DEAR_AGENT_RUNNER_TEMPLATE_CONFIGMAP}' 2>/dev/null)"
  if [[ "$template" == "dear-agent-runner-sidecar-template" ]]; then
    ok "two-container runner (harness holds no credential)"
  else
    warn "runner template is '$template': the harness can reach the control container's credentials"
  fi
  if "$KUBECTL" -n "$NAMESPACE" get cm dear-agent-runner-sidecar-template >/dev/null 2>&1; then
    harness_image="$($KUBECTL -n "$NAMESPACE" get cm dear-agent-runner-sidecar-template -o jsonpath='{.data.job\.yaml}' | grep -m1 '__HARNESS_IMAGE__' || true)"
    if [[ -n "$harness_image" ]]; then
      ok "harness image is injected by the dispatcher ($($KUBECTL -n "$NAMESPACE" get cm dear-agent-config -o jsonpath='{.data.DEAR_AGENT_HARNESS_IMAGE}'))"
    fi
  fi

  step "Queue"
  if control dear-agent health >/dev/null 2>&1; then
    ok "control plane answers; queue healthy"
  else
    warn "control plane did not answer (rollout still in progress?)"
  fi
  control dear-agent task ls 2>/dev/null | sed 's/^/  /' || true

  step "Model credential (the demo's one external dependency)"
  # The harness calls the provider on every run. OpenCode asks for up to 32k output tokens by
  # default, and a key with a small spend limit dies mid-run with "This request requires more
  # credits" — or, less helpfully, an opaque "Unexpected server error". Check it *before* the
  # audience is watching.
  local model_cfg
  model_cfg="$($KUBECTL -n "$NAMESPACE" get secret dear-agent-model \
    -o jsonpath='{.data.OPENCODE_CONFIG_CONTENT}' 2>/dev/null | base64 -d 2>/dev/null || true)"
  if [[ -z "$model_cfg" ]]; then
    warn "secret dear-agent-model has no OPENCODE_CONFIG_CONTENT: the harness cannot call a model"
  else
    if ! MODEL_CFG="$model_cfg" python3 - <<'PY'
import json, os, urllib.request
cfg = json.loads(os.environ["MODEL_CFG"])
provider = next(iter(cfg.get("provider") or {}), "")
key = ((cfg.get("provider") or {}).get(provider) or {}).get("options", {}).get("apiKey", "")
model = cfg.get("small_model", "")
print(f"  provider: {provider} · small model: {model or '(default: may be an image model!)'}")
if provider != "openrouter" or not key:
    print("  (credit check skipped: only openrouter is probed)")
    raise SystemExit(0)
req = urllib.request.Request(
    "https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)["data"]
except Exception as exc:  # noqa: BLE001 - a network hiccup is a warning, not a failure
    print(f"  could not reach openrouter: {type(exc).__name__}")
    raise SystemExit(0)
left = float(data.get("total_credits", 0)) - float(data.get("total_usage", 0))
if left < 1:
    print(f"  openrouter balance is ${left:.2f}: a run will die mid-way.")
    print("  Top the account up, lower the model's output limit in OPENCODE_CONFIG_CONTENT,")
    print("  or point DEAR_AGENT_MODEL at a provider/model with credit.")
    raise SystemExit(3)
print(f"  balance ${left:.2f}")
PY
    then
      bad "the model credential is not usable (see above): fix it before demoing"; failed=1
    else
      ok "model credential is funded"
    fi
  fi

  if [[ "$failed" == 1 ]]; then
    echo; bold "Preflight FAILED — fix the items above before demoing."; exit 1
  fi
  echo; bold "Preflight OK — ready to demo."
}

# --------------------------------------------------------------------------- enqueue
enqueue() {
  step "Enqueue a task (signed webhook)"
  if [[ -z "$REPO" || -z "$INSTRUCTIONS" ]]; then
    bad "--repo and --instructions are required"; exit 2
  fi
  local secret payload signature
  secret="$($KUBECTL -n "$NAMESPACE" get secret dear-agent-inbound -o jsonpath='{.data.DEAR_AGENT_INBOUND_SECRET}' | base64 -d)"
  payload="$(SENDER="$SENDER" REPO="$REPO" INSTRUCTIONS="$INSTRUCTIONS" DEMO_TAG="${DEMO_TAG:-demo}" python3 - <<'PY'
import json, os, time
body = f"repo: {os.environ['REPO']}\n\n{os.environ['INSTRUCTIONS']}"
print(json.dumps({
    "id": f"<{os.environ['DEMO_TAG']}-{int(time.time())}@dearagent.dev>",
    "sender": os.environ["SENDER"],
    "subject": os.environ.get("DEMO_SUBJECT", "Demo para la interna de DayTwo"),
    "body": body,
}))
PY
)"
  signature="$(SECRET="$secret" SENDER="$SENDER" PAYLOAD="$payload" python3 - <<'PY'
import hashlib, hmac, os
message = f"{os.environ['SENDER']}\n{os.environ['PAYLOAD']}"
print("sha256=" + hmac.new(os.environ["SECRET"].encode(), message.encode(), hashlib.sha256).hexdigest())
PY
)"
  printf '%s' "$payload" > /tmp/dear-agent-demo-payload.json
  "$KUBECTL" -n "$NAMESPACE" port-forward svc/dear-agent-control-plane 18080:80 >/dev/null 2>&1 &
  local forward=$!
  # shellcheck disable=SC2064
  trap "kill $forward 2>/dev/null || true" RETURN
  sleep 3
  local response
  response="$(curl -fsS -X POST http://127.0.0.1:18080/inbound \
    -H 'Content-Type: application/json' -H "X-Dear-Agent-Signature: $signature" \
    --data-binary @/tmp/dear-agent-demo-payload.json)"
  echo "  $response"
  kill "$forward" 2>/dev/null || true
  if [[ "$response" == *'"accepted"'*'<demo'* || "$response" == *accepted* ]]; then
    ok "task enqueued"
  else
    warn "check the response above"
  fi
}

trigger() { # name from-cronjob
  local name="$1" cron="$2"
  if "$KUBECTL" -n "$NAMESPACE" get "job/$name" >/dev/null 2>&1; then
    "$KUBECTL" -n "$NAMESPACE" delete "job/$name" >/dev/null 2>&1 || true
  fi
  "$KUBECTL" -n "$NAMESPACE" create job --from="cronjob/$cron" "$name" >/dev/null
  ok "triggered $cron as job/$name"
}

poll() { step "Ingest mail now (poll CronJob)"; trigger "demo-poll-$RANDOM" dear-agent-poll; }
sweep() { step "Dispatch now (sweep CronJob)"; trigger "demo-sweep-$RANDOM" dear-agent-sweep; }

# --------------------------------------------------------------------------- watch
watch_task() {
  step "Follow the run"
  local before after runner_job task_id waited=0
  before="$($KUBECTL -n "$NAMESPACE" get jobs -l app.kubernetes.io/component=runner -o name 2>/dev/null || true)"
  echo "  waiting for a runner Job (up to ${TIMEOUT}s)…"
  while [[ $waited -lt $TIMEOUT ]]; do
    after="$($KUBECTL -n "$NAMESPACE" get jobs -l app.kubernetes.io/component=runner -o name 2>/dev/null || true)"
    runner_job="$(comm -13 <(printf '%s\n' "$before" | sort) <(printf '%s\n' "$after" | sort) | tail -1)"
    [[ -n "$runner_job" ]] && break
    sleep 5; waited=$((waited + 5))
  done
  if [[ -z "${runner_job:-}" ]]; then
    bad "no runner Job appeared; check 'dear-agent task ls' (queued?) and the sweep logs"
    return 1
  fi
  runner_job="${runner_job#job.batch/}"
  runner_job="${runner_job#job/}"
  ok "runner Job: $runner_job"

  task_id="$("$KUBECTL" -n "$NAMESPACE" get "job/$runner_job" \
    -o jsonpath="{.spec.template.metadata.annotations['dearagent.dev/task-id']}" 2>/dev/null || true)"
  [[ -n "$task_id" ]] && echo "  task id: $task_id"

  echo
  bold "--- harness container (the coding agent at work) " 
  "$KUBECTL" -n "$NAMESPACE" logs -f "job/$runner_job" -c harness 2>/dev/null | tail -40 &
  local log_pid=$!

  if ! "$KUBECTL" -n "$NAMESPACE" wait "job/$runner_job" --for=condition=complete --timeout="${TIMEOUT}s" >/dev/null 2>&1; then
    warn "the Job did not report complete; reading the final state anyway"
  fi
  kill "$log_pid" 2>/dev/null || true

  step "Outcome"
  "$KUBECTL" -n "$NAMESPACE" logs "job/$runner_job" -c control 2>/dev/null | tail -5 | sed 's/^/  /'
  if [[ -n "$task_id" ]]; then
    control dear-agent task show "$task_id" 2>/dev/null | sed 's/^/  /' || true
    control dear-agent task events "$task_id" 2>/dev/null | sed 's/^/  /' || true
  fi
  step "Draft PR"
  local pr_url
  pr_url="$("$KUBECTL" -n "$NAMESPACE" logs "job/$runner_job" -c control 2>/dev/null | grep -oE 'https://[^ ]+/pull/[0-9]+' | tail -1)"
  if [[ -n "$pr_url" ]]; then
    bold "  $pr_url   (a human reviews and lands it)"
  else
    warn "no PR URL in the logs — the task may have failed (see the events above)"
  fi
}

# --------------------------------------------------------------------------- reset
reset() {
  step "Cleaning up demo artifacts"
  "$KUBECTL" -n "$NAMESPACE" delete jobs -l app.kubernetes.io/component=runner --ignore-not-found >/dev/null 2>&1 || true
  ok "runner Jobs deleted"
  "$KUBECTL" -n "$NAMESPACE" delete jobs -l "batch.kubernetes.io/job-name" --ignore-not-found >/dev/null 2>&1 || true
  for job in $("$KUBECTL" -n "$NAMESPACE" get jobs -o name 2>/dev/null | grep -E 'demo-(poll|sweep)'); do
    "$KUBECTL" -n "$NAMESPACE" delete "$job" --ignore-not-found >/dev/null 2>&1 || true
  done
  ok "triggered Jobs deleted"
  control dear-agent task ls --limit 50 2>/dev/null | sed 's/^/  /' || true
  warn "queued tasks are NOT deleted: finish or reject them explicitly"
  if [[ "$CLOSE_PRS" == 1 ]]; then
    if command -v gh >/dev/null 2>&1; then
      local prs
      prs="$(gh pr list --state open --draft --json number,url --jq '.[].url' 2>/dev/null || true)"
      warn "close draft PRs by hand: ${prs:-none found}"
    fi
  fi
}

case "$COMMAND" in
  preflight) preflight ;;
  enqueue) enqueue ;;
  poll) poll ;;
  sweep) sweep ;;
  watch) watch_task ;;
  run) enqueue; poll; sleep 5; sweep; watch_task ;;
  reset) reset ;;
  ""|-h|--help) usage ;;
  *) echo "unknown command: $COMMAND" >&2; usage; exit 2 ;;
esac
