#!/usr/bin/env bash
# Starts the recorder from compose.yaml, sends events from the Python SDK over TLS, and
# checks they're signed into the log. CI runs it; it needs Docker and uv.
set -euo pipefail
cd "$(dirname "$0")"
compose() { docker compose -f compose.yaml "$@"; }
work=$(mktemp -d)
trap 'compose logs --no-color collector wake-proxy > "$work/logs.txt" 2>&1 || true; compose down -v >/dev/null 2>&1 || true' EXIT

compose up -d --build --wait collector wake-proxy agentgateway
compose cp collector:/etc/wake/identity/bundle.pem "$work/ca.pem"

# The health check answers, and Prometheus can scrape the collector's own metrics.
for _ in $(seq 1 20); do curl -fsS http://localhost:13133/ >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://localhost:13133/ >/dev/null
curl -fsS http://localhost:8888/metrics | grep -q '^otelcol_'


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

# The log publishes a checkpoint about once a second.
for _ in $(seq 1 20); do
  compose cp collector:/var/lib/wake/log/checkpoint "$work/checkpoint" >/dev/null 2>&1 || true
  size=$(sed -n 2p "$work/checkpoint" 2>/dev/null || echo 0)
  if [ "${size:-0}" -ge 5 ]; then
    echo "signed log holds $size entries"
    # Check it as anyone could: the checkpoint, the tree, and every event's signature.
    compose run --rm --no-deps --entrypoint /wake-log collector verify -log /var/lib/wake/log \
      -log-key /etc/wake/keys/log.pub -key /etc/wake/keys/collector.pub -bundle /etc/wake/identity/bundle.pem \
      -signer proxy=spiffe://wake.local/proxy
    exit 0
  fi
  sleep 1
done
echo "the signed log didn't reach 5 entries" >&2
cat "$work/logs.txt" >&2 || true
exit 1
