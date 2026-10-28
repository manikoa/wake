# wake-proxy

Records every MCP call and every A2A message between agents that goes through [agentgateway](https://agentgateway.dev) as a signed Wake event. That covers agents with no Wake SDK, and it gives an independent record of the calls agents with an SDK report themselves. The design is in [docs/proxy.md](../docs/proxy.md).

agentgateway calls wake-proxy through its MCP guardrails hook (ExtMCP), before each `tools/call`, `resources/read` and `prompts/get` reaches its MCP server and again when the result comes back. wake-proxy:

- **Never holds up a call.** It answers "pass" at once and queues the hook. A full queue drops the hook and counts it. With `failOpen`, calls go through unrecorded if wake-proxy is down.
- **Pairs each request with its response.** The request hook hands agentgateway the event's id, and the config passes it back to the response hook. When it doesn't come back, responses are paired in order within the same session and caller; one whose id comes back late is recorded on its own, never given to another call. A request with no response after `--response-timeout` (JSON-RPC errors skip the response hook) is recorded with no output.
- **Signs each event.** It uses its own ECDSA P-256 key and X.509-SVID, and the certificate travels with the event. agentgateway never holds the key.
- **Sends events to the collector** over OTLP/HTTP, in batches. If the collector can't take them, they're retried, then dropped and counted.

For A2A, agentgateway calls wake-proxy's ext_proc service on each A2A route, with the same port and process. Each message and its reply become `agent_message` events. The request forwarded to the receiving agent gains `traceparent` and `wake-parents`, so its next step can link to the message. Bodies stay unchanged, and streamed replies are passed back chunk by chunk. Unlike MCP, if wake-proxy dies in the middle of an A2A exchange, that call fails, because agentgateway's ext_proc only fails open until bodies reach the processor. See [docs/proxy.md](../docs/proxy.md).

An agent with a Wake SDK links its MCP calls into its own trace and provenance with two HTTP headers on the call: `traceparent`, and `wake-parents` (the comma-separated ids of the events that led to the call). agentgateway v1.6.0 drops `params._meta` before calling the processor, so headers are the way through it. The config forwards only those headers and `mcp-session-id`, never credentials.

It's also the brake's deny lever. On each route with an `extAuthz` policy, agentgateway asks wake-proxy before every request whether it may go ahead. wake-proxy denies a call, with a 403 and a signed `denied` event, when the brake has halted the fleet, the agent making the call, or the agent it's sent to. The brake puts and lifts halts over the control API, which serves only the brake's SVID. Halts are kept on disk, and the deny fails closed. See "The deny lever" in [docs/proxy.md](../docs/proxy.md).

```bash
# The brake's side, with its SVID (dev-identity --brake-id writes keys/brake.pem and .key):
PUT    https://wake-proxy:9002/v1/halts/{id}   {"scope": {"fleet": true}}  or  {"scope": {"agents": ["billing"]}}
DELETE https://wake-proxy:9002/v1/halts/{id}
GET    https://wake-proxy:9002/v1/halts
PUT    https://wake-proxy:9002/v1/quarantines/{id}   {"selection": {"lock": {"agents": ["billing"]}, "origins": ["tool:fetch_page"]}}
DELETE https://wake-proxy:9002/v1/quarantines/{id}
GET    https://wake-proxy:9002/v1/memory/seen
# Memory adapters, read only, with --memory-listen:
GET    http://wake-proxy:9003/v1/memory?agent=billing
```

## Run it locally

```bash
cargo run -- dev-identity --brake-id spiffe://wake.local/brake   # once: keys/bundle.pem, proxy and brake SVIDs
cargo run -- serve --key keys/proxy.key --cert keys/proxy.pem \
  --control-listen 127.0.0.1:9002 --trust-bundle keys/bundle.pem       # halts kept in ./state
agentgateway -f agentgateway.yaml    # in another terminal; agentgateway v1.6.0
```

`dev-identity` writes a stand-in trust bundle and a certificate signed by it, for local development only. In production SPIRE issues short-lived SVIDs. Add the bundle to the collector's `trust_bundles`, and the proxy's SPIFFE ID to its `trusted_signers` under `proxy`, so it accepts the proxy's signatures.

wake-proxy must be reachable only by agentgateway, from beside it in the same pod or network namespace: anything that can call it can get events signed. It refuses hook connections from anything but loopback, and counts them (`hooks_refused`).

## Development

```bash
cargo test
# Needs cargo-fuzz and a nightly toolchain. CI fuzzes every target nightly (.github/workflows/fuzz.yml).
cargo +nightly fuzz run mcp_request   # also capture_hooks, canonical, a2a_exchange and authz_check
```

It logs counters every minute: events sent, hooks and events dropped, and responses paired by id or by order. If responses are only ever paired by order, the gateway config isn't passing the event id back.

Building needs `protoc`. `proto/ext_mcp.proto` is vendored from agentgateway v1.6.0. The signed bytes must match the collector's byte for byte, so `src/canonical.rs` is tested against `../collector/event/testdata/canonical.json`, which the collector's tests generate.
