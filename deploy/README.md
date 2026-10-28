# Running the recorder

`compose.yaml` runs the open source recorder in one place, such as a staging environment:

- **collector:** receives events over OTLP/HTTP with TLS on port 4318, signs the SDK's events, checks the proxy's, and keeps the signed log in the `data` volume. Only `spiffe://wake.local/proxy` may sign proxy events (`WAKE_PROXY_ID` changes it). Its health check is on port 13133 and its own metrics, for Prometheus, on port 8888 at `/metrics`, both published on this host's loopback; alert on `wake_log_dropped_events`.
- **wake-proxy:** records MCP calls and A2A messages for agentgateway, which reaches its hooks on a Unix socket in the `proxy-socket` volume. No other service mounts it, since anything that can call the hooks can get events signed.
- **agentgateway:** your agents call MCP servers and A2A agents through it on port 3000, over mTLS, each presenting its own SVID. Point its backends at yours in `agentgateway.yaml`.
- **First start:** one-shot containers create the collector's signing key, the log's checkpoint key, a development identity and the halts state, kept in volumes. The identity is a CA standing in for SPIRE, the proxy's SVID, the collector's and agentgateway's TLS certificates, the brake's SVID, and an SVID for each agent in `WAKE_DEV_AGENTS` (default `research,smoke-test`). agentgateway gets only its own certificate and the trust bundle, in the `gateway-identity` volume. A restart reuses them all.

```bash
docker compose -f deploy/compose.yaml up --build
```

## Pointing agents at it

**SDK:** send events to the collector over TLS, trusting the development CA:

```bash
docker compose -f deploy/compose.yaml cp collector:/etc/wake/identity/bundle.pem ./wake-ca.pem
export WAKE_ENDPOINT=https://localhost:4318 WAKE_CA_FILE=$PWD/wake-ca.pem
```

**MCP and A2A:** agents call through agentgateway at `https://localhost:3000`, presenting their SVIDs. The A2A example route is at `/agents/research`. Each agent's development SVID is in the `identity` volume:

```bash
docker compose -f deploy/compose.yaml cp collector:/etc/wake/identity/agents/research.pem ./research.pem
docker compose -f deploy/compose.yaml cp collector:/etc/wake/identity/agents/research.key ./research.key
curl --cacert wake-ca.pem --cert research.pem --key research.key https://localhost:3000/agents/research/.well-known/agent-card.json
```

**Identity is required.** agentgateway refuses a caller without an SVID from the trust bundle at the TLS handshake, so the brake always knows which agent a call is from. `agents.json` maps each agent's name to the identities it authenticates with; the brake halts agents by those names, and the proxy records them by those names. Add each of your agents there, or a halt on any agent denies its calls too, as a caller nobody can name. `WAKE_AGENTS` swaps in another file.

To authenticate agents with JWTs from your identity provider instead of SVIDs, replace the gateway's `tls.root` with a `jwtAuth` on the gateway, not on a route, since the gateway's deny runs before any route's policies:

```yaml
gateways:
  default:
    port: 3000
    jwtAuth:
      mode: strict
      issuer: https://example.okta.com/oauth2/default
      audiences: [agentgateway]
      jwks:
        url: https://example.okta.com/oauth2/default/v1/keys
```

and list each agent's JWT subject in `agents.json`. `proxy/tests/gateway_configs.rs` checks the shipped configs authenticate one of these two ways.

**The deny lever:** agentgateway asks wake-proxy whether the brake has halted each call, once for the gateway, for every route, and once more on each A2A route, for the agent it reaches. It fails closed: while wake-proxy is down, calls through the gateway are denied. The brake reaches wake-proxy's control API at `https://wake-proxy:9002` on this network, with the SVID in `identity/brake.pem` and `.key`, the only identity the API serves. Its probe through the gateway presents the same SVID. Halts are kept in the `proxy-state` volume, which the `proxy-state` one-shot initializes when it's new. A wake-proxy that starts without it denies every call until it's initialized again (`docker compose run --rm wake-proxy init --state-dir /var/lib/wake-proxy`). See "The deny lever" in `docs/proxy.md`.

The signed log is in the `data` volume at `/var/lib/wake/log`, with checkpoints signed by `keys/log.pub`. Check it, its tree and every event's signature, with `wake-log` (see `log/README.md`); `-entries` prints what's in it:

```bash
docker compose run --rm --no-deps --entrypoint /wake-log collector verify -log /var/lib/wake/log \
  -log-key /etc/wake/keys/log.pub -key /etc/wake/keys/collector.pub -bundle /etc/wake/identity/bundle.pem \
  -signer proxy=spiffe://wake.local/proxy
```

The provenance view and its ingester run on top of this in `manikoa/wake-platform`.

## Before production

These are for staging:
- Signing keys are files in a volume, and the collector and the ingester can read each other's.
- The identity is a development CA, and every agent's development SVID is in the `identity` volume.
- The log is on local disk.

Production keeps the collector's key in KMS, gets SVIDs from SPIRE for wake-proxy, agentgateway (its gateway `tls.spiffe`) and every agent, and writes the log to the customer's bucket with object lock.
