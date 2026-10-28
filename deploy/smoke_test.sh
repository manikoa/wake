#!/usr/bin/env bash
# Starts the recorder from compose.yaml, sends events from the Python SDK over TLS, and
# checks they're signed into the log. Then it checks the deny lever: a halted agent's own
# MCP call through agentgateway is denied, a streamed A2A reply under way is cut when a halt
# lands, and both are signed into the log and counted in wake-proxy's metrics. CI runs it;
# it needs Docker, uv and python3.
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

compose up -d --build --wait collector wake-proxy agentgateway a2a-standin
compose cp collector:/etc/wake/identity/bundle.pem "$work/ca.pem"

# The health check answers, and Prometheus can scrape the collector's and wake-proxy's
# metrics.
for _ in $(seq 1 20); do curl -fsS http://localhost:13133/ >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://localhost:13133/ >/dev/null
curl -fsS http://localhost:8888/metrics | grep -q '^otelcol_'
for _ in $(seq 1 20); do curl -fsS http://localhost:9464/metrics >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://localhost:9464/metrics | grep -q '^wake_proxy_state_ok 1$'


# Python 3.13 checks certificates strictly by default, so the development identity must pass.
(cd ../sdk/python && WAKE_ENDPOINT=https://localhost:4318 WAKE_CA_FILE="$work/ca.pem" \
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
    -log-key /etc/wake/keys/log.pub -key /etc/wake/keys/collector.pub -bundle /etc/wake/identity/bundle.pem \
    -signer proxy=spiffe://wake.local/proxy "$@"
}
verify

# The deny lever: the brake halts the smoke-test agent, with the brake's SVID, and the
# agent's own outbound MCP call is denied. Each identity comes from the development CA.
mkdir -p "$work/agents"
for f in brake.pem brake.key agents/smoke-test.pem agents/smoke-test.key agents/research.pem agents/research.key; do
  compose cp "collector:/etc/wake/identity/$f" "$work/$f"
done
python3 deny_check.py "$work" || fail "the deny lever didn't deny the halted agent's call, or cut the one under way"

# wake-proxy counted both.
curl -fsS http://localhost:9464/metrics > "$work/metrics.txt"
grep -Eq '^wake_proxy_denied_total [1-9]' "$work/metrics.txt" || fail "wake_proxy_denied_total didn't count the denial"
grep -Eq '^wake_proxy_cut_total [1-9]' "$work/metrics.txt" || fail "wake_proxy_cut_total didn't count the cut"

# The denied call and the cut one are signed events in the log, in the agent's name.
for _ in $(seq 1 30); do
  verify -entries > "$work/entries.jsonl" 2>/dev/null || true
  if python3 - "$work/entries.jsonl" <<'PY'
import json, sys
found = {}
for line in open(sys.argv[1]):
    entry = json.loads(line)
    a = (entry.get("event") or {}).get("attributes") or {}
    if not (entry.get("signed") and a.get("wake.event.kind") == "denied" and a.get("wake.agent.id") == "smoke-test"
            and a.get("wake.capture") == "proxy"):
        continue
    payload = entry.get("payload")
    payload = json.loads(payload) if isinstance(payload, str) else payload or {}
    halt = a.get("wake.brake.halt_id")
    if halt == "smoke-halt" or (halt == "smoke-cut" and payload.get("cut") is True and payload.get("receiver") == "research"):
        found[halt] = f"entry {entry.get('index')}, signed by {entry.get('signer')}"
if len(found) == 2:
    print(f"the denial is {found['smoke-halt']}, and the cut {found['smoke-cut']}")
    sys.exit(0)
sys.exit(1)
PY
  then
    exit 0
  fi
  sleep 1
done
fail "the denied call or the cut one isn't in the signed log"
