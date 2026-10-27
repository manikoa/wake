# wake-proxy

Records every MCP call that goes through [agentgateway](https://agentgateway.dev) as a signed Wake event. That covers agents with no Wake SDK, and it gives an independent record of the calls agents with an SDK report themselves. The design is in [docs/proxy.md](../docs/proxy.md).

agentgateway calls wake-proxy through its MCP guardrails hook (ExtMCP), before each `tools/call`, `resources/read` and `prompts/get` reaches its MCP server and again when the result comes back. wake-proxy:

- **Never holds up a call.** It answers "pass" at once and queues the hook. A full queue drops the hook and counts it. With `failOpen`, calls go through unrecorded if wake-proxy is down.
- **Pairs each request with its response.** The request hook hands agentgateway the event's id, and the config passes it back to the response hook. When it doesn't come back, responses are paired in order within a session. A request with no response after `--response-timeout` (JSON-RPC errors skip the response hook) is recorded with no output.
- **Signs each event.** It uses its own ECDSA P-256 key and X.509-SVID, and the certificate travels with the event. agentgateway never holds the key.
- **Sends events to the collector** over OTLP/HTTP, in batches. If the collector can't take them, they're retried, then dropped and counted.

An agent with a Wake SDK can link its MCP calls into its own trace and provenance by putting `traceparent` and `dev.wake/parents` (the ids of the events that led to the call) in the call's `params._meta`.

## Run it locally

```bash
cargo run -- dev-identity            # once: keys/bundle.pem, keys/proxy.pem, keys/proxy.key
cargo run -- serve --key keys/proxy.key --cert keys/proxy.pem
agentgateway -f agentgateway.yaml    # in another terminal; agentgateway v1.6.0
```

`dev-identity` writes a stand-in trust bundle and a certificate signed by it, for local development only. In production SPIRE issues short-lived SVIDs.

wake-proxy must be reachable only by agentgateway, over loopback or a pod-local socket. Anything that can call it can get events signed.

## Development

```bash
cargo test
cargo +nightly fuzz run mcp_request   # also capture_hooks and canonical; needs cargo-fuzz
```

Building needs `protoc`. `proto/ext_mcp.proto` is vendored from agentgateway v1.6.0. The signed bytes must match the collector's byte for byte, so `src/canonical.rs` is tested against `../collector/event/testdata/canonical.json`, which the collector's tests generate.
