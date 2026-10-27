# wake-proxy

Records every MCP call and every A2A message between agents that goes through [agentgateway](https://agentgateway.dev) as a signed Wake event. That covers agents with no Wake SDK, and it gives an independent record of the calls agents with an SDK report themselves. The design is in [docs/proxy.md](../docs/proxy.md).

agentgateway calls wake-proxy through its MCP guardrails hook (ExtMCP), before each `tools/call`, `resources/read` and `prompts/get` reaches its MCP server and again when the result comes back. wake-proxy:

- **Never holds up a call.** It answers "pass" at once and queues the hook. A full queue drops the hook and counts it. With `failOpen`, calls go through unrecorded if wake-proxy is down.
- **Pairs each request with its response.** The request hook hands agentgateway the event's id, and the config passes it back to the response hook. When it doesn't come back, responses are paired in order within a session. A request with no response after `--response-timeout` (JSON-RPC errors skip the response hook) is recorded with no output.
- **Signs each event.** It uses its own ECDSA P-256 key and X.509-SVID, and the certificate travels with the event. agentgateway never holds the key.
- **Sends events to the collector** over OTLP/HTTP, in batches. If the collector can't take them, they're retried, then dropped and counted.

For A2A, agentgateway calls wake-proxy's ext_proc service on each A2A route, with the same port and process. Each message and its reply become `agent_message` events. The request forwarded to the receiving agent gains `traceparent` and `wake-parents`, so its next step can link to the message. Bodies stay unchanged, and streamed replies are passed back chunk by chunk. Unlike MCP, if wake-proxy dies in the middle of an A2A exchange, that call fails, because agentgateway's ext_proc only fails open until bodies reach the processor. See [docs/proxy.md](../docs/proxy.md).

An agent with a Wake SDK links its MCP calls into its own trace and provenance with two HTTP headers on the call: `traceparent`, and `wake-parents` (the comma-separated ids of the events that led to the call). agentgateway v1.6.0 drops `params._meta` before calling the processor, so headers are the way through it. The config forwards only those headers and `mcp-session-id`, never credentials.

## Run it locally

```bash
cargo run -- dev-identity            # once: keys/bundle.pem, keys/proxy.pem, keys/proxy.key
cargo run -- serve --key keys/proxy.key --cert keys/proxy.pem
agentgateway -f agentgateway.yaml    # in another terminal; agentgateway v1.6.0
```

`dev-identity` writes a stand-in trust bundle and a certificate signed by it, for local development only. In production SPIRE issues short-lived SVIDs. Add the bundle to the collector's `trust_bundles` so it accepts the proxy's signatures.

wake-proxy must be reachable only by agentgateway, over loopback or a pod-local socket. Anything that can call it can get events signed.

## Development

```bash
cargo test
# Needs cargo-fuzz and a nightly toolchain. CI fuzzes every target nightly (.github/workflows/fuzz.yml).
cargo +nightly fuzz run mcp_request   # also capture_hooks, canonical and a2a_exchange
```

It logs counters every minute: events sent, hooks and events dropped, and responses paired by id or by order. If responses are only ever paired by order, the gateway config isn't passing the event id back.

Building needs `protoc`. `proto/ext_mcp.proto` is vendored from agentgateway v1.6.0. The signed bytes must match the collector's byte for byte, so `src/canonical.rs` is tested against `../collector/event/testdata/canonical.json`, which the collector's tests generate.
