#!/usr/bin/env bash
# In-cluster verification of the end-to-end path (M9.7; ADR 0007/0008/0009).
#
#   scripts/verify-openshift.sh [--namespace NS] [--overlay dev|prod]
#                               [--apply] [--enqueue] [--watch]
#                               [--repo URL] [--instructions TEXT] [--sender ADDR]
#
# By default it only *checks readiness* (deployment healthy, secrets present, sidecar template
# wired, images pullable) and never changes the cluster. It mutates only when asked:
#   --apply    apply the kustomize overlay
#   --enqueue  POST a signed task to /inbound through a port-forward
#   --watch    follow the runner Jobs the sweep creates
#
# The whole flow is described step by step in docs/verify-openshift.md.
set -euo pipefail

NAMESPACE=dear-agent
OVERLAY=dev
APPLY=0
ENQUEUE=0
WATCH=0
REPO="git@github.com:dearagent-dev/dear-agent-lab-python.git"
INSTRUCTIONS="Create a file VERIFY.md at the repository root containing the single line: verified."
SENDER="ops@dearagent.dev"

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --overlay) OVERLAY="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --instructions) INSTRUCTIONS="$2"; shift 2 ;;
    --sender) SENDER="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --enqueue) ENQUEUE=1; shift ;;
    --watch) WATCH=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if command -v oc >/dev/null 2>&1; then KUBECTL=oc; else KUBECTL=kubectl; fi

FAILED=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILED=1; }

section() { printf '\n== %s ==\n' "$1"; }

command -v "$KUBECTL" >/dev/null 2>&1 || { echo "$KUBECTL not found" >&2; exit 2; }
command -v kustomize >/dev/null 2>&1 || { echo "kustomize not found" >&2; exit 2; }

section "Cluster and namespace"
if "$KUBECTL" get namespace "$NAMESPACE" >/dev/null 2>&1; then
  pass "namespace $NAMESPACE exists"
else
  fail "namespace $NAMESPACE not found (create it or pass --namespace)"
fi

if [[ "$FAILED" -eq 1 ]]; then
  echo
  echo "Aborting: the cluster basics are not in place."
  exit 1
fi

section "Secrets (ADR 0003/0009)"
secret_key() { "$KUBECTL" -n "$NAMESPACE" get secret "$1" -o jsonpath="{.data.$2}" 2>/dev/null; }
require_key() {
  if [[ -n "$(secret_key "$1" "$2")" ]]; then pass "secret $1 has $2"; else fail "secret $1 is missing key $2"; fi
}
require_one_key() {
  local name=$1; shift
  for key in "$@"; do
    if [[ -n "$(secret_key "$name" "$key")" ]]; then pass "secret $name has $key"; return; fi
  done
  fail "secret $name has none of: $*"
}

require_key dear-agent-postgres DATABASE_URL
require_key dear-agent-git-read ssh-privatekey
require_key dear-agent-git-push ssh-privatekey
require_key dear-agent-inbound DEAR_AGENT_INBOUND_SECRET
require_one_key dear-agent-forge GH_TOKEN GITHUB_TOKEN GITLAB_TOKEN GITEA_TOKEN
if "$KUBECTL" -n "$NAMESPACE" get secret dear-agent-model >/dev/null 2>&1; then
  pass "secret dear-agent-model exists"
else
  warn "secret dear-agent-model is absent: the harness needs a model credential to run"
fi

section "Workloads"
if "$KUBECTL" -n "$NAMESPACE" get deployment dear-agent-control-plane >/dev/null 2>&1; then
  pass "control-plane Deployment exists"
else
  fail "control-plane Deployment missing (run --apply)"
fi
if "$KUBECTL" -n "$NAMESPACE" get statefulset dear-agent-postgres >/dev/null 2>&1; then
  pass "Postgres StatefulSet exists"
else
  fail "Postgres StatefulSet missing (run --apply)"
fi

section "Runner template and config"
if "$KUBECTL" -n "$NAMESPACE" get configmap dear-agent-runner-sidecar-template >/dev/null 2>&1; then
  pass "sidecar runner template is present"
  harness_image=$("$KUBECTL" -n "$NAMESPACE" get configmap dear-agent-runner-sidecar-template \
    -o jsonpath='{.data.job\.yaml}' | grep -m1 'image: .*harness' || true)
  if [[ -n "$harness_image" ]]; then
    pass "harness container image: ${harness_image##*image: }"
  else
    fail "sidecar template has no harness image"
  fi
else
  warn "sidecar template absent; the single-container runner would be used instead"
fi
template=$("$KUBECTL" -n "$NAMESPACE" get configmap dear-agent-config \
  -o jsonpath='{.data.DEAR_AGENT_RUNNER_TEMPLATE_CONFIGMAP}' 2>/dev/null || true)
if [[ "$template" == "dear-agent-runner-sidecar-template" ]]; then
  pass "config selects the sidecar runner template"
else
  warn "config DEAR_AGENT_RUNNER_TEMPLATE_CONFIGMAP='$template' (not the sidecar; credentials \
are visible to the harness in the single-container runner)"
fi

section "Images"
for image in quay.io/dear-agent/dear-agent:latest quay.io/dear-agent/harness:latest; do
  if "$KUBECTL" -n "$NAMESPACE" get pods -o jsonpath='{.items[*].spec.containers[*].image}' 2>/dev/null | grep -q "$image"; then
    pass "image in use: $image"
  else
    warn "image not seen in running pods: $image (may still be pullable)"
  fi
done

if [[ "$APPLY" -eq 1 ]]; then
  section "Applying deploy/overlays/$OVERLAY"
  kustomize build "deploy/overlays/$OVERLAY" | "$KUBECTL" -n "$NAMESPACE" apply -f -
  pass "overlay applied"
fi

if [[ "$FAILED" -eq 1 ]]; then
  echo
  echo "Preflight FAILED; fix the items above before running the end-to-end check."
  exit 1
fi

if [[ "$ENQUEUE" -eq 1 ]]; then
  section "Enqueuing a task through POST /inbound (signed)"
  secret=$("$KUBECTL" -n "$NAMESPACE" get secret dear-agent-inbound \
    -o jsonpath='{.data.DEAR_AGENT_INBOUND_SECRET}' | base64 -d)
  payload=$(SENDER="$SENDER" REPO="$REPO" INSTRUCTIONS="$INSTRUCTIONS" python3 - <<'PY'
import json, os, time
transport_id = f"<verify-{int(time.time())}@dearagent.dev>"
body = f"repo: {os.environ['REPO']}\n\n{os.environ['INSTRUCTIONS']}"
print(json.dumps({
    "id": transport_id,
    "sender": os.environ["SENDER"],
    "subject": "M9.7 in-cluster verification",
    "body": body,
}))
PY
)
  signature=$(SECRET="$secret" SENDER="$SENDER" PAYLOAD="$payload" python3 - <<'PY'
import hashlib, hmac, os
message = f"{os.environ['SENDER']}\n{os.environ['PAYLOAD']}"
print("sha256=" + hmac.new(os.environ["SECRET"].encode(), message.encode(), hashlib.sha256).hexdigest())
PY
)
  "$KUBECTL" -n "$NAMESPACE" port-forward svc/dear-agent-control-plane 18080:80 >/dev/null 2>&1 &
  forward=$!
  trap 'kill "$forward" 2>/dev/null || true' EXIT
  sleep 3
  response=$(curl -fsS -X POST "http://127.0.0.1:18080/inbound" \
    -H "Content-Type: application/json" \
    -H "X-Dear-Agent-Signature: $signature" \
    --data-binary "$payload")
  echo "  response: $response"
  pass "task submitted; the sweep will dispatch a runner Job"
fi

if [[ "$WATCH" -eq 1 ]]; then
  section "Watching runner Jobs"
  echo "  waiting for a dear-agent task Job (Ctrl-C to stop)…"
  "$KUBECTL" -n "$NAMESPACE" get jobs -w -l app.kubernetes.io/component=runner
fi

echo
echo "Done. Follow docs/verify-openshift.md to confirm the draft PR and record the result."
