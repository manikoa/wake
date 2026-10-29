# wake-proxy

Records every MCP call and every A2A message between agents that goes through [agentgateway](https://agentgateway.dev) as a signed Wake event. That covers agents with no Wake SDK, and it gives an independent record of the calls agents with an SDK report themselves. The design is in [docs/proxy.md](../docs/proxy.md).

agentgateway calls wake-proxy through its MCP guardrails hook (ExtMCP), before each `tools/call`, `resources/read` and `prompts/get` reaches its MCP server and again when the result comes back. wake-proxy:

- **Never holds up a call.** It answers "pass" at once and queues the hook. A full queue, by count or by bytes, drops the hook and counts it. With `failOpen`, calls go through unrecorded if wake-proxy is down.
- **Never grows without bound.** Every queue is bounded by bytes, each call's params and result go into its payload up to 1 MiB, and batches to the collector stay under its request limit, so a flood of large calls can't take down the deny lever that shares the process.
- **Pairs each request with its response.** The request hook hands agentgateway the event's id, and the config passes it back to the response hook. When it doesn't come back, responses are paired in order within a session. A request with no response after `--response-timeout` (JSON-RPC errors skip the response hook) is recorded with no output.
- **Signs each event.** It uses its own ECDSA P-256 key and X.509-SVID, and the certificate travels with the event. agentgateway never holds the key.
- **Sends events to the collector** over OTLP/HTTP, in batches. If the collector can't take them, they're retried, then dropped and counted.

For A2A, agentgateway calls wake-proxy's ext_proc service on each A2A route, with the same port and process. Each message and its reply become `agent_message` events. The request forwarded to the receiving agent gains `traceparent` and `wake-parents`, so its next step can link to the message. Bodies stay unchanged, and streamed replies are passed back chunk by chunk. Unlike MCP, if wake-proxy dies in the middle of an A2A exchange, that call fails, because agentgateway's ext_proc only fails open until bodies reach the processor. See [docs/proxy.md](../docs/proxy.md).

An agent with a Wake SDK links its MCP calls into its own trace and provenance with two HTTP headers on the call: `traceparent`, and `wake-parents` (the comma-separated ids of the events that led to the call). agentgateway v1.6.0 drops `params._meta` before calling the processor, so headers are the way through it. The config forwards only those headers and `mcp-session-id`, never credentials.

It's also the brake's deny lever. agentgateway asks wake-proxy before every request whether it may go ahead, through the gateway's `extAuthz` policy, and on A2A routes once more for the agent the route reaches. wake-proxy denies a call, with a 403 and a signed `denied` event, when the brake has halted the fleet, the agent making the call, or the agent it's sent to, and, while any agent is halted, when it can't tell who's calling. The 403's `wake-halts` header names every halt that covers the call, and `wake-halt` the first.

A call already under way when a halt lands is cut, and recorded as a `denied` event marked `cut`:
- **A2A:** an exchange the proxy is carrying, such as a streamed reply, ends with an error, and agentgateway fails it.
- **MCP:** a result on its way back is kept from the caller, which gets the gateway's error. The tool already ran.

`--agents` maps each agent's SPIFFE IDs and JWT subjects (for a delegated token, the `act.sub` of the agent acting) to the name the brake halts it by. The brake puts and lifts halts over the control API, which serves only the brake's SVID. Halts are kept on disk, and the deny fails closed: until the state is initialized with `wake-proxy init`, every call is denied. See "The deny lever" in [docs/proxy.md](../docs/proxy.md).

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
# Prometheus, with --metrics-listen:
GET    http://wake-proxy:9464/metrics
```

## Run it locally

```bash
# Once: keys/bundle.pem, the proxy's and the brake's SVIDs, agentgateway's TLS certificate
# (keys/gateway/), and an SVID for an agent (keys/agents/research.pem and .key).
cargo run -- dev-identity --brake-id spiffe://wake.local/brake --gateway-host localhost --agent research
echo '{"research": ["spiffe://wake.local/agent/research"]}' > agents.json
cargo run -- init --state-dir "$PWD/state"                              # once: the halts state
cargo run -- serve --key keys/proxy.key --cert keys/proxy.pem --state-dir "$PWD/state" --agents agents.json \
  --control-listen 127.0.0.1:9002 --trust-bundle keys/bundle.pem
agentgateway -f agentgateway.yaml    # in another terminal; agentgateway v1.6.0
```

Agents then call `https://localhost:3000` with their SVIDs. `dev-identity` writes a stand-in trust bundle and certificates signed by it, for local development only. In production SPIRE issues short-lived SVIDs. Add the bundle to the collector's `trust_bundles`, and the proxy's SPIFFE ID to its `trusted_signers` under `proxy`, so it accepts the proxy's signatures.

wake-proxy must be reachable only by agentgateway: anything that can call its hooks can get events signed. `--listen` takes a Unix socket (`unix:/run/wake/proxy.sock`, in a directory only agentgateway shares) or a loopback address, and refuses anything else. `deploy/compose.yaml` uses the socket.

## Development

```bash
cargo test    # also checks the shipped agentgateway configs keep the deny lever whole
# Needs cargo-fuzz and a nightly toolchain. CI fuzzes every target nightly (.github/workflows/fuzz.yml).
cargo +nightly fuzz run mcp_request   # also capture_hooks, canonical, a2a_exchange and authz_check
```

With `--metrics-listen`, it serves counters for Prometheus at `/metrics`, and it logs them every minute too:
- **Traffic:** MCP calls and A2A exchanges seen, and events sent.
- **Losses:** calls seen but not recorded (`wake_proxy_hooks_dropped_total`), events dropped on their way to the collector (`wake_proxy_events_dropped_total`), and events truncated.
- **Pairing:** responses paired by id or by order. If responses are only ever paired by order, the gateway config isn't passing the event id back.
- **The brake:** calls denied and calls cut, the halts in force, and whether the halts state is ok.

Recording fails open, so alert on the two drop counters, and on `wake_proxy_state_ok` going to 0, which means every call is being denied. A batch the collector refuses as bad, with a 4xx other than 408 or 429, isn't sent again: it's dropped and counted at once.

Building needs `protoc`. `proto/ext_mcp.proto` is vendored from agentgateway v1.6.0. The signed bytes must match the collector's byte for byte, so `src/canonical.rs` is tested against `../collector/event/testdata/canonical.json`, which the collector's tests generate.
