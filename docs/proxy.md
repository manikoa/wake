# The proxy on agentgateway

Decided October 26, 2026.

## The question

`docs/repo-layout.md` says to check at the start of Record whether agentgateway's extension points cover Wake's signing and deny. If they do, the proxy stays in `proxy/`; if Wake has to fork, the fork gets its own public repository.

Checked against agentgateway v1.6.0 (October 2, 2026, the latest stable release), main at `a550bd2` and the 1.6 docs.

## The answer: no fork

agentgateway has no plugin or WASM hooks, and its crates aren't published, so Wake can't extend it in process. It has three external gRPC hooks that together cover what Wake needs:

| Need | Hook | What it gives Wake |
|---|---|---|
| MCP capture | ExtMCP (`mcpGuardrails`, `kind: remote`), `crates/protos/proto/ext_mcp.proto` | Each `tools/call`, `resources/read` and `prompts/get` with the JSON-RPC method, backend, original tool name, raw params or result, and metadata from CEL such as the caller's `source.spiffeId`, in both the request and the response phase |
| A2A capture and stitching | Envoy-compatible ext_proc on the `a2a` route | Full request and response bodies, streamed SSE included. A buffered body can be rewritten to put `traceparent` and the parent event id in `params.message.metadata` |
| MCP deny (Brake) | ExtMCP returns `AuthorizationError` | Fails closed. A `tools/call` gets an error result; other methods get JSON-RPC error -32001 |
| Fleet deny (Brake) | ext_authz | Denies any HTTP request, A2A and MCP `initialize` included, by identity |

agentgateway's own OpenTelemetry spans carry MCP and A2A attributes but no bodies unless CEL adds them, and they're unsigned and sampled. They're a cross-check, not the record.

## The shape

`proxy/` holds one Rust service, the **Wake processor**, plus the agentgateway config that points at it. Pin agentgateway v1.6.0, and move to 1.7 when it's generally available.

- It implements the ExtMCP, ext_proc and ext_authz servers (tonic, with vendored Apache 2.0 protos).
- It builds each `wake.*` event, signs it and exports it over OTLP to the collector. agentgateway can't sign, so "the proxy signs its own events" in `docs/signing.md` means this processor does.
- It gets its own SVID from the SPIRE Workload API. agentgateway never holds the key.
- It accepts calls only from agentgateway, over a pod-local Unix socket or mTLS checked against agentgateway's SVID. Otherwise an agent could send it fake calls and get them signed.

The processor is a new parsing surface for attacker-controlled MCP and A2A bodies, so it gets fuzz targets from the first commit.

## Gaps

- ExtMCP's response phase carries no JSON-RPC id or request params. The plan is to return `wake_event_id` in request metadata and read it back in the response phase. The code suggests this works; a test has to show it.
- JSON-RPC errors skip `CheckResponse`, so a failed call's outcome comes from agentgateway's own spans.
- Server-initiated MCP messages (progress, sampling, elicitation) aren't hooked.
- ExtMCP is synchronous: every call waits on the processor, and it fails closed by default. Recording mustn't block, so for capture the processor answers at once and does its work after replying. Capture fails open (see the decisions below).
- agentgateway's ext_proc is marked "very experimental" in its source. SSE with body rewriting needs testing early.
- Per-call deny doesn't stop calls already forwarded or long-lived streams. ext_proc can cut an open stream; the Brake phase has to verify that.
- There's no built-in "deny in force" acknowledgement. Verification would combine `/config_dump`, the processor's deny-list version, signed events for denied attempts, and a probe call with a test identity.

Upstream proposals that would help, none blocking: a request id and the params in `McpResponse`, calling `CheckResponse` on JSON-RPC errors, an observe-only async mode, and an A2A hook like ExtMCP.

## Decisions

1. **Proxy events are signed with ECDSA P-256, using the processor's X.509-SVID key.** SPIRE issues P-256, P-384 or RSA-2048 keys, not Ed25519, and signing with the SVID key keeps one identity per component. The collector keeps signing SDK and memory adapter events with Ed25519, so it checks two algorithms. It checks a proxy signature against the signer's SVID, and the SVID against the SPIRE trust bundle, instead of a fixed key file. How the SVID travels with each event, so anyone can check a log entry later on its own, is settled when the proxy is built.
2. **Capture fails open.** If the processor is down or slow, agentgateway lets the call through unrecorded, because recording never blocks. The gap must not be silent: agentgateway's own spans for the call still reach the collector, and calls the proxy saw but didn't record raise a health signal. Deny, in the Brake phase, fails closed, so it goes in a separate guardrail entry with its own failure mode.

## Built so far

The MCP half is in `proxy/` (October 27, 2026); A2A through ext_proc comes next.

- **Pairing request and response.** The request hook returns the event's id as `wake_event_id` metadata, and the gateway config reads it back in the response hook as `mcpGuardrails.wake_event_id`. Whether agentgateway carries it into the response hook still has to be shown with a real agentgateway. When the id doesn't come back, responses are paired in order within the same session, method and server. A request with no response after the timeout is recorded with no output, since JSON-RPC errors skip the response hook.
- **The event.** Each recorded call is a `tool_call` event:
  - `wake.capture` is `proxy`.
  - `wake.agent.id` is the caller's SPIFFE ID, or its JWT subject, from gateway CEL.
  - The origin is `tool:<tool name, resource URI or prompt name>`.
  - `wake.mcp.method`, `wake.mcp.server` and `wake.mcp.session` say what was called.
  - The payload holds the call's params and result, byte for byte.
- **Joining the caller's trace.** An SDK can link a call to its own trace and events through `params._meta`: `traceparent` for the trace, and `dev.wake/parents` for the ids of the events that led to the call. Without them, a proxy event has no parents, and the completeness metric counts it as uncaused.
- **Canonical bytes.** `collector/event/testdata/canonical.json` holds spans and their exact canonical bytes, generated by the collector's tests. The proxy's encoder is tested against the same file, so the two can't drift apart.
- **Fuzzing.** cargo-fuzz targets cover MCP params, the stream of hooks and canonical encoding. Property tests check the same things on stable Rust.

## Proposed: how the certificate travels

Anyone must be able to check a proxy event from the log alone, years later, after the SVID that signed it has expired. So the event carries the certificate:

- The proxy sets `wake.signature.certificate`, the base64 DER of its X.509-SVID. Like every `wake.signature*` attribute, it's outside the signed bytes; the CA's signature is what vouches for it.
- The log entry gains an optional `certificate` field holding it. That's an additive change to the `log` module, tagged `log/v0.3.0`.
- Checking an entry: the certificate chains to a trusted SPIRE trust bundle, its one `spiffe://` URI SAN equals `signer`, the event's start time falls within the certificate's validity, and the ECDSA P-256 signature (ASN.1 DER, over SHA-256) checks out over `event`.
- The collector and the ingester are configured with the trust bundles to accept. Publishing bundles next to the log is left until a design partner needs it.

The cost is about 600 bytes per proxy event. The alternative was storing each certificate once, next to the log like large payloads, and referring to it by digest. It saves the bytes but adds a second lookup to every check, so it waits until the size matters.
