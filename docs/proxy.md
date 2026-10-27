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

Both halves are in `proxy/` (October 27, 2026), and both were run against agentgateway v1.6.0 with the collector. One gRPC server serves both hooks: ExtMCP for MCP, and Envoy's ext_proc for A2A.

### MCP

- **Pairing request and response.** The request hook returns the event's id as `wake_event_id` metadata, and the gateway config reads it back in the response hook as `mcpGuardrails.wake_event_id`. Against agentgateway v1.6.0, every response was paired this way. If the id ever doesn't come back, responses are paired in order within the same method and server, and the processor counts which way each response was paired. A request with no response after the timeout is recorded with no output, since JSON-RPC errors skip the response hook.
- **The event.** Each recorded call is a `tool_call` event:
  - `wake.capture` is `proxy`.
  - `wake.agent.id` is the caller's SPIFFE ID, or its JWT subject, from gateway CEL.
  - The origin is `tool:<tool name, resource URI or prompt name>`.
  - `wake.mcp.method`, `wake.mcp.server` and `wake.mcp.session` say what was called.
  - The payload holds the call's params and result, byte for byte.
- **Joining the caller's trace.** agentgateway v1.6.0 drops `params._meta` before calling the processor, so a caller links an MCP call with HTTP headers: `traceparent` for the trace, and `wake-parents` for the comma-separated ids of the events that led to the call. The gateway config forwards only those and `mcp-session-id`, never credentials. `_meta.traceparent` and `_meta["dev.wake/parents"]` still work for gateways that pass `_meta` through. Without either, a proxy event has no parents, and the completeness metric counts it as uncaused.

  The Python SDK sends the link for the tool call it's recording: the traceparent of the SDK's tool event, and that event's id as the parent. So the proxy's record of a call is a child of the SDK's record of the same call. It comes through `wake.mcp.link_interceptor` for LangChain's MCP adapters, or through `wake.mcp.http_client()` with `meta=wake.links.meta()` for the MCP client directly (see `sdk/python/README.md`). A proxy event with no SDK parent is a call the agent's SDK didn't report.
- **Canonical bytes.** `collector/event/testdata/canonical.json` holds spans and their exact canonical bytes, generated by the collector's tests, and the proxy's encoder is tested against it. In the other direction, `proxy/testdata/signed_event.json` is an event the proxy signed, and the collector's tests check it in Go.
- **Fuzzing.** cargo-fuzz targets cover MCP params and headers, the stream of hooks and canonical encoding. Property tests check the same things on stable Rust.

### A2A

agentgateway calls the processor's ext_proc service on an A2A route (`a2a: {}`). One gRPC stream is one HTTP exchange between two agents.

- **What's recorded.** A `message/send` or `message/stream` request becomes an `agent_message` event recorded where the receiver gets it:
  - `wake.agent.id` is the receiver, named per route in the gateway config.
  - The origin is `agent:<sender>`, from the sender's SPIFFE ID or JWT subject.
  - The parents come from the `wake-parents` header, or from the message's `metadata["dev.wake/parents"]`.
  - The trace comes from `traceparent`, or the message metadata's.

  The reply becomes a second `agent_message`, in the sender's name with the receiver as its origin. Its parents are whatever the reply's message metadata names, else the message it answers. A `tasks/get` reply is recorded the same way. Agent cards and other GETs aren't recorded.
- **Linking the receiver.** The request headers forwarded to the receiver are set to `traceparent`, carrying the message event's span, and `wake-parents`, carrying its id. A receiver with a Wake SDK, or any OTel-instrumented HTTP stack, carries the trace on, and an SDK links its next step to the message. Rewriting `params.message.metadata` as well would mean the processor rewrites bodies, so it waits until a receiver needs it.
- **Bodies.** agentgateway buffers request and response bodies (`bufferedPartial`), and the processor answers each with "continue" and no mutation, so the gateway forwards the original bytes. A `text/event-stream` reply would be held until it ended if buffered. So in the response headers phase the processor switches it to full duplex (`allowModeOverride`) and sends each chunk straight back; streams were checked to arrive as they're written. At most 1 MiB of each body goes into the payload, marked `truncated` beyond that. A body over the gateway's buffer limit is recorded from its prefix.
- **Recording doesn't fully fail open here.** agentgateway applies `failOpen` only until body bytes reach the processor: "After the request body starts streaming to the server, a failure returns an error even with failOpen". The processor is also in the path of every streamed reply, because it sends each chunk back. So if the processor dies in the middle of an exchange, that one call fails; calls that start while it's down go through unrecorded. agentgateway has no ext_proc observability mode (`observability_mode` is always false). The fix is upstream: an observe-only ext_proc mode, or an A2A hook like ExtMCP. Until then, the processor answers each message before doing any of its own work.
- **Not yet.** Linking a `tasks/get` reply to the message that created the task, which takes state across exchanges. And A2A over gRPC or REST bindings, which agentgateway doesn't treat as A2A.

## How the certificate travels

Decided October 27, 2026. Anyone must be able to check a proxy event from the log alone, years later, after the SVID that signed it has expired. So the event carries the certificate:

- The proxy sets `wake.signature.certificate`: base64 of the DER X.509-SVID, followed by any intermediates. Like every `wake.signature*` attribute, it's outside the signed bytes; the CA's signature is what vouches for it.
- The log entry keeps it in an optional `certificate` field (`log/v0.3.0`).
- Checking an entry, which `wakelog.Entry.Check` does:
  - The certificate chains to a trusted SPIRE trust bundle as of the event's start time.
  - It isn't a CA.
  - Its one `spiffe://` URI SAN equals `signer`.
  - Its key is P-256.
  - The ECDSA signature, ASN.1 DER over SHA-256, checks out over `event`.
- The collector (`trust_bundles`) and the ingester are configured with the trust bundles to accept. Publishing bundles next to the log is left until a design partner needs it.

The cost is about 600 bytes per proxy event. Storing each certificate once next to the log and referring to it by digest would save the bytes, but it adds a second lookup to every check, so it waits until the size matters.
