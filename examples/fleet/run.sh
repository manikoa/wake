#!/usr/bin/env bash
# Runs the reference fleet end to end: brings it up on the staging stack, sends one
# customer request through the support agent, checks that each agent and the tools server
# can reach the rest of the fleet, the model providers and the internet only through
# agentgateway (reach.py), sends the same request again under a flow policy (policy.json),
# checks the signed log with wake-log, and checks provenance across the agents, and the
# policy's refusals, with check.py. CI runs it; it needs Docker and python3.
set -euo pipefail
cd "$(dirname "$0")"
compose() { docker compose -f compose.yaml "$@"; }
work=$(mktemp -d)
# The flow policy wake-proxy enforces, from $WAKE_POLICY (deploy/compose.yaml). At first it
# has no rules, so the first request goes as the game day expects: the poisoned page gets
# its payment out. policy.json comes in for the second.
window=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["window_seconds"])' policy.json)
printf '{"window_seconds": %s, "rules": []}\n' "$window" > "$work/policy.json"
export WAKE_POLICY="$work/policy.json"
finish() {
  status=$?
  compose --profile run logs --no-color > "$work/logs.txt" 2>&1 || true
  if [ "$status" -ne 0 ]; then tail -n 300 "$work/logs.txt" >&2 || true; fi
  compose --profile run down -v >/dev/null 2>&1 || true
}
trap finish EXIT

python3 test_check.py
compose up -d --build --wait collector wake-proxy agentgateway tools research billing notify
started=$(date +%s)
compose run --rm support

# Nothing of the fleet's can be reached around agentgateway (see "Network" in README.md).
# The request just went through every backend, the stand-ins and wake-proxy, so each one
# is up. Each listener is tried by name and by its IP address on each of its networks.
listeners=""
for target in tools:3001 research:9101 billing:9102 notify:9103 idp:8080 e2b:8080 memory-rules:9003 \
    wake-proxy:9001 wake-proxy:9002 wake-proxy:9003 wake-proxy:9464 collector:4318 collector:8888 collector:13133; do
  service=${target%:*} port=${target#*:}
  id=$(compose ps -q "$service")
  # A listener in another container's network namespace is at that container's addresses.
  mode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$id")
  [ "${mode#container:}" != "$mode" ] && id=${mode#container:}
  ips=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$id")
  listeners+="$service $target"$'\n'
  for ip in $ips; do listeners+="$service $ip:$port"$'\n'; done
done
# Addresses on the internet, by IP so the check doesn't rest on DNS. None may connect from
# an agent's network, which is internal, so the agent's only way out is agentgateway: with
# real models too, whose calls go through it (models.yaml).
internet="1.1.1.1:443 8.8.8.8:53 9.9.9.9:443"
# The listeners a container mustn't reach: all but those of the services matching $1, and
# the internet.
around() {
  printf '%s' "$listeners" | grep -Ev "^($1) " | cut -d' ' -f2
  printf '%s\n' $internet
}
compose run --rm --no-deps -T support python reach.py support agentgateway:3000 collector:4318 idp:8080 \
  -- $(around 'collector|idp')
compose exec -T research python reach.py research agentgateway:3000 collector:4318 idp:8080 \
  -- $(around 'research|collector|idp')
# Billing also keeps a sandbox at E2B, and its memory adapter fetches its rules.
compose exec -T billing python reach.py billing agentgateway:3000 collector:4318 idp:8080 e2b:8080 memory-rules:9003 \
  -- $(around 'billing|collector|idp|e2b|memory-rules')
# Notify, the TypeScript agent, has python3 for this check only.
compose exec -T notify python3 /src/examples/fleet/reach.py notify agentgateway:3000 collector:4318 idp:8080 \
  -- $(around 'notify|collector|idp')
# The tools server sends no events, so it has no way to the collector either.
compose exec -T tools python reach.py tools agentgateway:3000 idp:8080 -- $(around 'tools|idp')

# Then the same request with policy.json in force, which keeps what a web page says from
# triggering billing's payments. wake-proxy reads it again within two seconds, and serves it
# to the SDKs, as billing's memory adapter sees.
cat policy.json > "$work/policy.json"
served() {
  compose exec -T billing python -c 'import json, sys, urllib.request
flow = json.load(urllib.request.urlopen("http://memory-rules:9003/v1/memory", timeout=5)).get("flow") or {}
sys.exit(0 if flow.get("rules") else 1)'
}
for _ in $(seq 1 30); do served && break; sleep 1; done
served || { echo "wake-proxy didn't take up policy.json" >&2; exit 1; }
# A rule decides on what wake-proxy saw enter each agent, and it saw nothing before it
# started, so until it has watched for a whole window every agent counts as having every
# origin (docs/proxy.md). The second request waits for that, so what's refused is refused
# for what the page brought in.
wait=$((started + window + 1 - $(date +%s)))
if [ "$wait" -gt 0 ]; then echo "waiting ${wait}s for wake-proxy to have watched a whole window"; sleep "$wait"; fi
compose run --rm support

# The agents send what they still have queued as they stop.
compose stop research billing notify tools

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
python3 check.py --policy "$work/entries.jsonl"
