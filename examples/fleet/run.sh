#!/usr/bin/env bash
# Runs the reference fleet end to end: brings it up on the staging recorder, sends one
# customer request through the support agent, checks the signed log with wake-log, and
# checks provenance across the agents with check.py. CI runs it; it needs Docker and
# python3.
set -euo pipefail
cd "$(dirname "$0")"
compose() { docker compose -f compose.yaml "$@"; }
work=$(mktemp -d)
finish() {
  status=$?
  compose --profile run logs --no-color > "$work/logs.txt" 2>&1 || true
  if [ "$status" -ne 0 ]; then tail -n 300 "$work/logs.txt" >&2 || true; fi
  compose --profile run down -v >/dev/null 2>&1 || true
}
trap finish EXIT

python3 test_check.py
compose up -d --build --wait collector wake-proxy agentgateway tools research billing
compose run --rm support
# The agents send what they still have queued as they stop.
compose stop research billing tools

# Wait until the log's signed checkpoint stops growing: every event is in.
last=-1 steady=0
for _ in $(seq 1 60); do
  compose cp collector:/var/lib/wake/log/checkpoint "$work/checkpoint" >/dev/null 2>&1 || true
  size=$(sed -n 2p "$work/checkpoint" 2>/dev/null || echo 0)
  if [ "${size:-0}" -gt 0 ] && [ "$size" = "$last" ]; then
    steady=$((steady + 1))
    [ "$steady" -ge 3 ] && break
  else
    steady=0
  fi
  last=$size
  sleep 1
done
echo "signed log holds ${last} entries"

compose run --rm --no-deps --entrypoint /wake-log collector verify -log /var/lib/wake/log \
  -log-key /etc/wake/keys/log.pub -key /etc/wake/keys/collector.pub -bundle /etc/wake/identity/bundle.pem \
  -entries > "$work/entries.jsonl"
python3 check.py "$work/entries.jsonl"
