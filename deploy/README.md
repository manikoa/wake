# Running the recorder

`compose.yaml` runs the open source recorder in one place, such as a staging environment:

- **collector:** receives events over OTLP/HTTP with TLS on port 4318, signs the SDK's events, checks the proxy's, and keeps the signed log in the `data` volume. Only `spiffe://wake.local/proxy` may sign proxy events (`WAKE_PROXY_ID` changes it). Its health check is on port 13133 and its own metrics, for Prometheus, on port 8888 at `/metrics`, both published on this host's loopback; alert on `wake_log_dropped_events`.
- **wake-proxy:** records MCP calls and A2A messages for agentgateway. It's reachable only on the compose network.
- **agentgateway:** your agents call MCP servers and A2A agents through it on port 3000. Point its backends at yours in `agentgateway.yaml`.
- **First start:** one-shot containers create the collector's signing key, the log's checkpoint key, and a development identity, kept in the `keys` and `identity` volumes. The identity is a CA standing in for SPIRE, the proxy's SVID, and the collector's TLS certificate. A restart reuses them.

```bash
docker compose -f deploy/compose.yaml up --build
```

## Pointing agents at it

**SDK:** send events to the collector over TLS, trusting the development CA:

```bash
docker compose -f deploy/compose.yaml cp collector:/etc/wake/identity/bundle.pem ./wake-ca.pem
export WAKE_ENDPOINT=https://localhost:4318 OTEL_EXPORTER_OTLP_CERTIFICATE=$PWD/wake-ca.pem
```

**MCP and A2A:** agents call through agentgateway at `http://localhost:3000`. The A2A example route is at `/agents/research`.

**The deny lever:** every route also asks wake-proxy whether the brake has halted the call, and fails closed: while wake-proxy is down, calls through the gateway are denied. The brake reaches wake-proxy's control API at `https://wake-proxy:9002` on this network, with the SVID in `identity/brake.pem` and `.key`, the only identity the API serves. Halts are kept in the `proxy-state` volume. See "The deny lever" in `docs/proxy.md`.

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
- The identity is a development CA.
- The log is on local disk.

Production keeps the collector's key in KMS, gets SVIDs from SPIRE, and writes the log to the customer's bucket with object lock.
