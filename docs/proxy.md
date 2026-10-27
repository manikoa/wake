# The proxy on agentgateway

Proposed October 26, 2026. Waiting on the open items at the end.

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
- ExtMCP is synchronous: every call waits on the processor, and it fails closed by default. Recording mustn't block, so for capture the processor answers at once and does its work after replying. Whether capture fails open, which loses events, or closed, which stops agents when the processor is down, is open item 2.
- agentgateway's ext_proc is marked "very experimental" in its source. SSE with body rewriting needs testing early.
- Per-call deny doesn't stop calls already forwarded or long-lived streams. ext_proc can cut an open stream; the Brake phase has to verify that.
- There's no built-in "deny in force" acknowledgement. Verification would combine `/config_dump`, the processor's deny-list version, signed events for denied attempts, and a probe call with a test identity.

Upstream proposals that would help, none blocking: a request id and the params in `McpResponse`, calling `CheckResponse` on JSON-RPC errors, an observe-only async mode, and an A2A hook like ExtMCP.

## Open items to confirm

1. **Signature algorithm.** `docs/events.md` says Ed25519, but SPIRE X.509-SVID keys are P-256, P-384 or RSA-2048. Either sign proxy events with the SVID's P-256 key (ECDSA), so the collector checks two algorithms, or have the processor bind a short-lived Ed25519 key to its SVID and publish that binding. The first is simpler and keeps one identity.
2. **Fail open or closed for capture.** When the processor is down, ExtMCP can let calls through unrecorded or block them. "Recording never blocks" points to fail open for capture, with the gap raising a health signal. Deny, in Brake, has to fail closed, so the two may need separate guardrail entries.
