#!/usr/bin/env bash
# Starts the recorder from compose.yaml, sends events from the Python SDK over TLS, and
# checks they're signed into the log. Then it checks the deny lever: a halted agent's own
# MCP call through agentgateway is denied, and the denial is signed into the log too. CI
# runs it; it needs Docker, uv and python3.
set -euo pipefail
cd "$(dirname "$0")"
# smoke.yaml publishes wake-proxy's control API on loopback, for the test only.
compose() { docker compose -f compose.yaml -f smoke.yaml "$@"; }
work=$(mktemp -d)
trap 'compose logs --no-color collector wake-proxy agentgateway > "$work/logs.txt" 2>&1 || true; compose down -v >/dev/null 2>&1 || true' EXIT
fail() {
  echo "$1" >&2
  compose logs --no-color collector wake-proxy agentgateway >&2 || true
  exit 1
}

compose up -d --build --wait collector wake-proxy agentgateway
compose cp collector:/etc/wake/identity/bundle.pem "$work/ca.pem"

# Python 3.13 checks certificates strictly by default, so the development identity must pass.
(cd ../sdk/python && WAKE_ENDPOINT=https://localhost:4318 OTEL_EXPORTER_OTLP_CERTIFICATE="$work/ca.pem" \
  uv run --python 3.13 --locked --all-extras python - <<'PY'
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from wake import Recorder
from wake.langchain import WakeCallbackHandler

recorder = Recorder(agent="smoke-test")
handler = WakeCallbackHandler(recorder)
model = GenericFakeChatModel(messages=iter([AIMessage(content=f"reply {i}") for i in range(5)]))
with handler.agent("smoke-test", input="hello", origin="user"):
    for _ in range(5):
        model.invoke("hello", config={"callbacks": [handler]})
recorder.flush()
health = recorder.health()
print(health)
assert health.sent >= 5 and health.dropped == 0, health
PY
)

# Waits until the signed log holds at least $1 entries. The log publishes a checkpoint about
# once a second.
log_holds() {
  for _ in $(seq 1 30); do
    compose cp collector:/var/lib/wake/log/checkpoint "$work/checkpoint" >/dev/null 2>&1 || true
    size=$(sed -n 2p "$work/checkpoint" 2>/dev/null || echo 0)
    if [ "${size:-0}" -ge "$1" ]; then
      echo "signed log holds $size entries"
      return 0
    fi
    sleep 1
  done
  return 1
}
log_holds 5 || fail "the signed log didn't reach 5 entries"
# Check it as anyone could: the checkpoint, the tree, and every event's signature.
verify() {
  compose run --rm --no-deps --entrypoint /wake-log collector verify -log /var/lib/wake/log \
    -log-key /etc/wake/keys/log.pub -key /etc/wake/keys/collector.pub -bundle /etc/wake/identity/bundle.pem "$@"
}
verify

# The deny lever: the brake halts the smoke-test agent, with the brake's SVID, and the
# agent's own outbound MCP call is denied. Each identity comes from the development CA.
mkdir -p "$work/agents"
for f in brake.pem brake.key agents/smoke-test.pem agents/smoke-test.key agents/research.pem agents/research.key; do
  compose cp "collector:/etc/wake/identity/$f" "$work/$f"
done
python3 deny_check.py "$work" || fail "the deny lever didn't deny the halted agent's call"

# The denied call is a signed event in the log, in the agent's name.
for _ in $(seq 1 30); do
  verify -entries > "$work/entries.jsonl" 2>/dev/null || true
  if python3 - "$work/entries.jsonl" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    entry = json.loads(line)
    a = (entry.get("event") or {}).get("attributes") or {}
    if (entry.get("signed") and a.get("wake.event.kind") == "denied" and a.get("wake.agent.id") == "smoke-test"
            and a.get("wake.brake.halt_id") == "smoke-halt" and a.get("wake.capture") == "proxy"):
        print(f"the denial is entry {entry.get('index')}, signed by {a.get('wake.signer')}")
        sys.exit(0)
sys.exit(1)
PY
  then
    exit 0
  fi
  sleep 1
done
fail "the denied call isn't in the signed log"
