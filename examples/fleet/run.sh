#!/usr/bin/env bash
# Runs the reference fleet end to end: brings it up on the staging recorder, sends one
# customer request through the support agent, checks that each agent and the tools server
# can reach the rest of the fleet only through agentgateway (reach.py), checks the signed
# log with wake-log, and checks provenance across the agents with check.py. CI runs it; it
# needs Docker and python3.
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

# Nothing of the fleet's can be reached around agentgateway (see "Network" in README.md).
# The request just went through every backend, the stand-ins and wake-proxy, so each one
# is up. Each listener is tried by name and by its IP address on each of its networks.
listeners=""
for target in tools:3001 research:9101 billing:9102 idp:8080 e2b:8080 memory-rules:9003 \
    wake-proxy:9001 wake-proxy:9002 wake-proxy:9003 collector:4318; do
  service=${target%:*} port=${target#*:}
  id=$(compose ps -q "$service")
  # A listener in another container's network namespace is at that container's addresses.
  mode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$id")
  [ "${mode#container:}" != "$mode" ] && id=${mode#container:}
  ips=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$id")
  listeners+="$service $target"$'\n'
  for ip in $ips; do listeners+="$service $ip:$port"$'\n'; done
done
# The listeners a container mustn't reach: all but those of the services matching $1.
around() { printf '%s' "$listeners" | grep -Ev "^($1) " | cut -d' ' -f2; }
compose run --rm --no-deps -T support python reach.py support agentgateway:3000 collector:4318 idp:8080 \
  -- $(around 'collector|idp')
compose exec -T research python reach.py research agentgateway:3000 collector:4318 idp:8080 \
  -- $(around 'research|collector|idp')
# Billing also keeps a sandbox at E2B, and its memory adapter fetches its rules.
compose exec -T billing python reach.py billing agentgateway:3000 collector:4318 idp:8080 e2b:8080 memory-rules:9003 \
  -- $(around 'billing|collector|idp|e2b|memory-rules')
# The tools server sends no events, so it has no way to the collector either.
compose exec -T tools python reach.py tools agentgateway:3000 idp:8080 -- $(around 'tools|idp')

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
  -signer proxy=spiffe://wake.local/proxy -entries > "$work/entries.jsonl"
python3 check.py "$work/entries.jsonl"
