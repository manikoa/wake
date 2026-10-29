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
- It reads its own SVID from the files SPIRE writes (`--cert`, `--key`, and `--trust-bundle` for the control API). It checks them every 5 seconds, and uses a rotated SVID once its key and certificate match. Until then, and if a file is broken, it keeps the SVID in use. agentgateway never holds the key. Reading it from the SPIRE Workload API instead waits for SPIRE.
- It accepts calls only from agentgateway. Otherwise an agent could send it fake calls and get them signed. `--listen` takes a Unix socket (`unix:/run/wake/proxy.sock`), in a directory only agentgateway shares, or a loopback address, for agentgateway in the same pod; it refuses any other address. `deploy/compose.yaml` puts the socket in a volume only wake-proxy and agentgateway mount. mTLS checked against agentgateway's SVID, for the two on different hosts, isn't built yet.

The processor is a new parsing surface for attacker-controlled MCP and A2A bodies, so it gets fuzz targets from the first commit.

## Gaps

- ExtMCP's response phase carries no JSON-RPC id or request params. The plan is to return `wake_event_id` in request metadata and read it back in the response phase. The code suggests this works; a test has to show it.
- JSON-RPC errors skip `CheckResponse`, so a failed call's outcome comes from agentgateway's own spans.
- Server-initiated MCP messages (progress, sampling, elicitation) aren't hooked.
- ExtMCP is synchronous: every call waits on the processor, and it fails closed by default. Recording mustn't block, so for capture the processor answers at once and does its work after replying. Capture fails open (see the decisions below).
- agentgateway's ext_proc is marked "very experimental" in its source. SSE with body rewriting needs testing early.
- Per-call deny doesn't stop calls already forwarded or long-lived streams. ext_proc can cut an open stream; the Brake phase has to verify that. Built, and verified against agentgateway: see "Calls under way" below.
- There's no built-in "deny in force" acknowledgement. Verification would combine `/config_dump`, the processor's deny-list version, signed events for denied attempts, and a probe call with a test identity.

Upstream proposals that would help, none blocking: a request id and the params in `McpResponse`, calling `CheckResponse` on JSON-RPC errors, an observe-only async mode, and an A2A hook like ExtMCP.

## Decisions

1. **Proxy events are signed with ECDSA P-256, using the processor's X.509-SVID key.** SPIRE issues P-256, P-384 or RSA-2048 keys, not Ed25519, and signing with the SVID key keeps one identity per component. The collector keeps signing SDK and memory adapter events with Ed25519, so it checks two algorithms. It checks a proxy signature against the signer's SVID, and the SVID against the SPIRE trust bundle, instead of a fixed key file. How the SVID travels with each event, so anyone can check a log entry later on its own, is settled when the proxy is built.
2. **Capture fails open.** If the processor is down or slow, agentgateway lets the call through unrecorded, because recording never blocks. The gap must not be silent: agentgateway's own spans for the call still reach the collector, and calls the proxy saw but didn't record raise a health signal (its metrics, below). Deny, in the Brake phase, fails closed, so it goes in a separate guardrail entry with its own failure mode.

## Built so far

Both halves are in `proxy/` (October 27, 2026), and both were run against agentgateway v1.6.0 with the collector. One gRPC server serves both hooks: ExtMCP for MCP, and Envoy's ext_proc for A2A.

### MCP

- **Pairing request and response.** The request hook returns the event's id as `wake_event_id` metadata, and the gateway config reads it back in the response hook as `mcpGuardrails.wake_event_id`. Against agentgateway v1.6.0, every response was paired this way. If the id ever doesn't come back, responses are paired in order within the same MCP session (the gateway config sends `session`, from the `mcp-session-id` header, in both phases), method and server, and the processor counts which way each response was paired. A response that names an id the processor doesn't hold, because its request was already recorded without it or went through another replica, is recorded on its own rather than paired in order with some other call. A request with no response after the timeout is recorded with no output, since JSON-RPC errors skip the response hook.
- **The event.** Each recorded call is a `tool_call` event:
  - `wake.capture` is `proxy`.
  - `wake.agent.id` is the caller's SPIFFE ID, or from its JWT the agent acting (see "Who's calling"), from gateway CEL, under its agent's name when `--agents` lists it (see the deny lever below).
  - The origin is `tool:<tool name, resource URI or prompt name>`.
  - `wake.mcp.method`, `wake.mcp.server` and `wake.mcp.session` say what was called.
  - `wake.call.ok` says whether it went well: a result came back that doesn't say `isError`. A call with no response seen didn't, since agentgateway skips the response hook on a JSON-RPC error, so the response phase must be configured for any call to count as having gone well. A result cut short or that isn't a JSON object leaves it out.
  - The payload holds the call's params and result, byte for byte, up to 1 MiB each. Beyond that it holds a prefix, as a string, and is marked `truncated`; the tool's name and the call's links are read from the whole params first.
- **Joining the caller's trace.** agentgateway v1.6.0 drops `params._meta` before calling the processor, so a caller links an MCP call with HTTP headers: `traceparent` for the trace, and `wake-parents` for the comma-separated ids of the events that led to the call. The gateway config forwards only those and `mcp-session-id`, never credentials. `_meta.traceparent` and `_meta["dev.wake/parents"]` still work for gateways that pass `_meta` through. Without either, a proxy event has no parents, and the completeness metric counts it as uncaused.

  The Python SDK sends the link for the tool call it's recording: the traceparent of the SDK's tool event, and that event's id as the parent. So the proxy's record of a call is a child of the SDK's record of the same call. It comes through `wake.mcp.link_interceptor` for LangChain's MCP adapters, or through `wake.mcp.http_client()` with `meta=wake.links.meta()` for the MCP client directly (see `sdk/python/README.md`). A proxy event with no SDK parent is a call the agent's SDK didn't report.
- **Canonical bytes.** `collector/event/testdata/canonical.json` holds spans and their exact canonical bytes, generated by the collector's tests, and the proxy's encoder is tested against it. In the other direction, `proxy/testdata/signed_event.json` is an event the proxy signed, and the collector's tests check it in Go.
- **Memory.** The processor shares its process with the deny lever, so recording must never grow it until it's killed. Every stage is bounded by bytes as well as by count (`proxy/src/budget.rs`): hooks waiting for the capture task, requests waiting for their responses, A2A bodies being captured, and events on their way to the collector, 64 MiB each. Past a bound, recording drops and counts (`hooks_dropped`, `export_dropped`); nothing waits. A batch to the collector holds at most 4 MiB, under its 20 MiB request limit, so a run of large events goes out in several requests rather than one it refuses.
- **Fuzzing.** cargo-fuzz targets cover MCP params and headers, the stream of hooks, canonical encoding, A2A exchanges and the deny lever's checks. They run every night for five minutes each, from a corpus kept between runs, and for a minute on any pull request that changes them (`.github/workflows/fuzz.yml`); a crash fails the run and keeps its input. Property tests check the same things on stable Rust in every CI run.

### A2A

agentgateway calls the processor's ext_proc service on an A2A route (`a2a: {}`). One gRPC stream is one HTTP exchange between two agents. The rules for links, which the SDKs follow too, are in [linking.md](linking.md).

- **What's recorded.** A message request becomes an `agent_message` event recorded where the receiver gets it. Both protocol versions in use are read: v0.3 (`message/send`, `message/stream`) and v1.0 (`SendMessage`, `SendStreamingMessage`), which the a2a-sdk 1.x speaks. The event:
  - `wake.agent.id` is the receiver, named per route in the gateway config.
  - The origin is `agent:<sender>`, from the sender's SPIFFE ID or JWT (the agent acting, as for every caller), under its agent's name when `--agents` lists it.
  - The parents come from the `wake-parents` header, or from the message's `metadata["dev.wake/parents"]`.
  - The trace comes from `traceparent`, or the message metadata's. When only the metadata has it, the receiver's link headers wait for the body, which agentgateway holds until the processor answers it, so the message, its reply and the receiver's next step share the sender's trace.

  The reply becomes a second `agent_message`, in the sender's name with the receiver as its origin. It records the last task state the reply gives (`wake.a2a.task_state`, in v0.3's spelling, `completed` for v1.0's `TASK_STATE_COMPLETED`), and whether the call went well (`wake.call.ok`): a 2xx with a result, no JSON-RPC error and no task that failed, was rejected or was canceled. Its parents are whatever the reply's message metadata names, else the message it answers. A `tasks/get` or `GetTask` reply (and a resubscription's stream) is recorded the same way, and when it names no parents it links to the message that started the task: the processor remembers which message each task came from, for the last 10,000 tasks, under the agent the message went to, since a task id is only unique at the agent that made it. Agent cards and other GETs aren't recorded.
- **Linking the receiver.** The request headers forwarded to the receiver are set to `traceparent`, carrying the message event's span, and `wake-parents`, carrying its id. A receiver with a Wake SDK, or any OTel-instrumented HTTP stack, carries the trace on, and an SDK links its next step to the message (in Python, inside `wake.a2a.received`). Rewriting `params.message.metadata` as well would mean the processor rewrites bodies, so it waits until a receiver needs it.
- **Linking the sender back.** A receiver with a Wake SDK names the step that produced its reply in the reply message's metadata, and the reply event's parents come from there. The response headers going back to the sender are set the same way, to the reply event's span and id, so the sender's SDK makes the reply a parent of the step that reads it: the model call after the tool that sent the message, not the tool call itself, which would make a cycle. So provenance runs both ways across a hop: whatever the receiver read, such as a web page, is upstream of what the sender does next.
- **Bodies.** agentgateway buffers request and response bodies (`bufferedPartial`), and the processor answers each with "continue" and no mutation, so the gateway forwards the original bytes. A `text/event-stream` reply would be held until it ended if buffered. So in the response headers phase the processor switches it to full duplex (`allowModeOverride`) and sends each chunk straight back; streams were checked to arrive as they're written. At most 1 MiB of each body goes into the payload, marked `truncated` beyond that, and the bodies every exchange is capturing share a byte budget; past it, a body is recorded from the prefix it has. A body over the gateway's buffer limit is recorded from its prefix.
- **Recording doesn't fully fail open here.** agentgateway applies `failOpen` only until body bytes reach the processor: "After the request body starts streaming to the server, a failure returns an error even with failOpen". The processor is also in the path of every streamed reply, because it sends each chunk back. So if the processor dies in the middle of an exchange, that one call fails; calls that start while it's down go through unrecorded. agentgateway has no ext_proc observability mode (`observability_mode` is always false). The fix is upstream: an observe-only ext_proc mode, or an A2A hook like ExtMCP. Until then, the processor answers each message before doing any of its own work.
- **Checked end to end** (October 27, 2026) against agentgateway v1.6.0 with the a2a-sdk 1.2 client and server: `SendMessage` and a streamed `SendStreamingMessage` were each recorded with the sender's tool call as parent, the receiver's next step, recorded by the Python SDK inside `wake.a2a.received`, descended from the proxy's record of the message, and the reply event descended from that step and reached the sender in `wake-parents`, all in the sender's trace.
- **Not yet.** A2A over gRPC or REST bindings, which agentgateway doesn't treat as A2A. A task started by one replica of the processor and read through another isn't linked, since each remembers its own tasks.

### The deny lever

The brake's first lever (October 27, 2026). agentgateway calls the processor's ext_authz service before each request, and the processor answers from the halts in force.

- **Where it's checked.** Each shipped config puts `extAuthz` on the gateway itself, which agentgateway (v1.6) runs on every request before it picks a route. A route added later is covered without anyone remembering to. Each A2A route also has its own `extAuthz`, whose static `receiver` context names the agent the route reaches, so a call sent to a halted agent is denied too. `proxy/tests/gateway_configs.rs` checks the shipped configs for both, in CI.
- **Who's calling.** The gateway authenticates every caller: in `deploy/` and `proxy/`, each agent presents its X.509-SVID over mTLS, and a caller without one doesn't get past the handshake. A gateway-level `jwtAuth` with `mode: strict` works too (see `deploy/README.md`). Authentication has to be on the gateway: a route's own policies run after the gateway's `extAuthz`. The caller comes from the `wake` metadata the gateway config computes, with the same CEL as capture, and failing that from the mTLS peer's principal. The CEL takes the SPIFFE ID; else, from a JWT, the agent acting: a token from OAuth token exchange ([RFC 8693](https://www.rfc-editor.org/rfc/rfc8693)) names whoever it acts for in `sub` (often a user, or the agent that delegated) and the agent acting now in `act.sub`, so `act.sub` comes first and `sub` only when there's no `act`. Naming a delegated token by its `sub` would record a sub-agent's calls as its user's or parent's, and a halt on the sub-agent would never match them. Earlier actors in a chain, nested deeper in `act`, aren't the caller. Beside the caller, every hook gets what the caller's JWT says, never the token: its `sub`, `jti`, `exp`, its `act` chain and whether it has a `cnf` claim (the `token` in ext_authz's `wake` metadata and in ExtMCP's metadata, and `sender_token` in ext_proc's attributes). The proxy records them on the call's events as `wake.token.*` (see Tokens in [events.md](events.md)). With no JWT the gateway computes them empty, and nothing is recorded; `proxy/tests/gateway_configs.rs` checks every hook gets the same expression, and that no shipped config passes a credential. Checked October 28, 2026 against agentgateway v1.6.0 with signed tokens and the expression in `deploy/agentgateway.yaml`: a plain token is named by its `sub`, a delegated one by its `act.sub`, a chain by its current actor, and one whose `act` has no `sub`, or isn't an object, by its `sub`. `proxy/tests/gateway_configs.rs` checks every hook in the shipped configs names the caller this way, alike. End to end, with wake-proxy as the deny lever and a halt on the instance `support/researcher#toolu_1` put through the control API: a token delegating to that instance, acting for `user-42` under `support`, was denied with `wake-halts: halt-sub`; a token delegating to its sibling, `#toolu_2`, and support's own token went through. The signed `denied` event named the instance, and carried `wake.token.subject` `user-42`, `wake.token.actors` `["support/researcher#toolu_1", "support"]`, the token's id and expiry and `wake.token.bound` true, and no token.
- **Agent names.** `--agents` names a JSON file that maps each agent to the identities it authenticates with, its SPIFFE IDs and JWT subjects, the `act.sub` its delegated tokens name it by included (`deploy/agents.json`). It's the one place an identity becomes a name. The brake halts by name, and the denied, `tool_call` and `agent_message` events the proxy records name agents the same way, so they match the SDKs' `wake.agent.id`. A halt may name an agent's identity instead of its name. Without the file, an identity is its own name.
- **A halt** covers the fleet, named agents, or named tokens. A call is denied when an agent in scope makes it, or when it's sent to one. While any agent halt is in force, a call whose caller is unknown is denied too: no identity, or one the agents file doesn't list. It could be the halted agent's.
  - **Sub-agents** (October 28, 2026). A call made with a token is made under every agent in its `act` chain, and under whom they act for, the token's `sub` (for a token without a chain, the agent it was issued to). So halting an agent halts the sub-agents acting for it, however deep, whether the parent delegated for a user (and is in `act`) or delegated its own token (and is the `sub`). A halt may also name one sub-agent, by the identity its tokens name it with in `act.sub`, such as `support/researcher#toolu_1`. Sub-agents don't need to be in the agents file, and that's where the rule for unknown callers would bite: halting one unlisted sub-agent would deny all its siblings. So a caller the file doesn't list is placed, not unknown, when the token it presents is its own (it's the agent the token names as acting now), its chain is whole, and the chain or the subject is an agent the file lists. It acts under that agent by the identity provider's word, and only a halt that names it, its chain or its token covers it. A caller presenting a token that names another agent as acting isn't placed by it; over mTLS that's any caller whose SVID isn't the identity its token names, so with SVIDs, sub-agents are placed only when the identity provider names them by their SPIFFE IDs.
  - **A cut chain.** The proxy keeps 8 actors of a chain. A chain longer than that, one with an actor it couldn't read, or a token whose `sub` it couldn't read, could hide a halted agent in the part it didn't keep, so every agent halt denies its calls, even when the caller is listed.
  - **A token halt** names tokens by their `jti` and covers any call made with them, whoever presents them: one sub-agent's credential, stopped without stopping its parent, its siblings, or the next token the same sub-agent is issued. It names no agent, so it doesn't deny unknown callers. A halt naming a blank agent or token would stop nothing, and a scope with a field the proxy doesn't know would cover less than asked, so the control API refuses both. A memory quarantine locks agents by name, never tokens: an agent's memory adapter never sees one.
  - **Rolling back.** A wake-proxy from before token halts (October 28, 2026) reads a halt's `tokens` as nothing: a halt of tokens alone covers no call there, and one naming agents too covers only them. Lift token halts, or halt the agents, before running an older proxy on the same state.
- **A denied call** gets a 403. Its `wake-halts` header names every halt that covers the call, comma separated, in the order they were put, so a probe for any one of them finds it; `wake-halt` names the first, as before. It's recorded as a signed `denied` event (see [events.md](events.md)) in the caller's trace, with the caller's link as its parents. So the provenance view shows which step tried to act after the halt.
- **The brake's control API** runs on its own port (`--control-listen`), with the processor's SVID as its TLS certificate. It serves only a client whose X.509-SVID chains to the trust bundle and carries the brake's SPIFFE ID (`--brake-id`). `PUT /v1/halts/{id}` with a scope (`{"fleet": true}`, or `{"agents": [...], "tokens": [...]}`) puts a halt in force, `DELETE` lifts it, and `GET /v1/halts` lists what's in force with a version and an `epoch`. `GET /v1/callers` lists every identity ext_authz has seen calling since the proxy started, the last 10,000 of them: the agent the agents file names it as, whom its latest token acted for and its chain (and `actors_cut` when the chain was cut), the ids of the last 8 tokens it presented, and when it was first and last seen; `?identity=<id>` lists only that identity, and the callers whose chain names it. With it the brake can tell whether a sub-agent calls under credentials of its own, which a halt can stop alone, or only ever as its parent. It takes 32 connections at once, each with a 5 second TLS handshake and a 60 second lifetime, after which it's closed gracefully, so a client that connects and stalls can't keep the brake out. A change is in force at once. It answers 200 once it's on disk, so that answer is the lever's acknowledgement. If the disk write fails, the change stays in force, and the answer is a 500 with the state, whose `not_persisted` says why; `GET` shows it too, until a later change is written. A halt's `applied_at` is taken after it's in force, so no call in scope is allowed after it.
- **It fails closed.**
  - The halts are kept in `--state-dir`, an absolute path, so a restart doesn't lift one. ext_authz reads them without ever waiting on a disk write.
  - The state is initialized once, by `wake-proxy init`, which gives it an `epoch`. `deploy/compose.yaml` does that in a one-shot when the volume is new. A proxy that starts without its state, its volume missing or wiped, denies every call under `uninitialized-state` until a person runs `init` or the brake lifts that halt. Either starts the state over with a new epoch, which tells the brake that halts it put before may be gone.
  - A state file that can't be read denies every call under `unreadable-state` until it's replaced. It's kept aside next to the state, as `halts.unreadable-<time>.json`, before anything is written over it.
  - The gateway's `extAuthz` policy uses `failureMode: deny`, so if the processor can't answer, the call is denied. That's the opposite of capture, which fails open. Note what it means: with the deny lever configured, wake-proxy being down stops traffic. Never give the policy a `cache`, or a halt waits for the cache to expire.
- **Memory quarantine.** The same state holds the brake's memory quarantines (`PUT`/`DELETE /v1/quarantines/{id}`): which agents' memory writes are refused, and which entries aren't served. The SDKs' memory adapters fetch their rules from a read-only port (`--memory-listen`, `GET /v1/memory?agent=<id>`), and the proxy notes each fetch, for the last 10,000 agents, which the brake reads at `GET /v1/memory/seen` to confirm an adapter has a quarantine. Agents can reach that port, so it takes 512 connections at once, each open for at most 30 seconds. The adapter enforces it in the agent's own process, so it holds only while the agent's code does, and an agent names itself when it fetches. The deny lever doesn't rely on either. The rules also carry the halts in force (`halts`: each one's id, whether it covers the fleet, and the agents and sub-agents it names, never its tokens; a state that can't be read, or isn't initialized, is a halt of the fleet), so an SDK can stop its agent, or one sub-agent instance of it, in its own process (the Claude Agent SDK integration's cooperative stop): the calls that never reach the proxy, and the sub-agents the proxy can't tell from their parent. That holds only while the agent's code does too.
- **Verifying it.** The brake confirms the halt is in force at the version it set, in the epoch it set it in, through the control API. It sends a probe through the gateway for a fleet halt and expects a 403 whose `wake-halts` names the halt. Where the gateway requires mTLS, the probe presents the brake's own SVID; a fleet halt covers every caller. It also checks the signed log for calls in scope allowed after the halt time.
- **Checked** against agentgateway v1.6.0 with the reference fleet:
  - Halting billing turned support's message to billing into a 403, and no payment went out.
  - The halt survived a proxy restart.
  - Another SVID from the same bundle couldn't lift it.
  - A fleet halt denied MCP.
  - With the processor stopped, the gateway denied calls.
  - Both denied calls were recorded and signed.
- **Checked in CI** against `deploy/`'s mTLS gateway, by `deploy/smoke_test.sh`: halting the `smoke-test` agent by name denies its own MCP call, naming the halt, and not another agent's; a caller with no SVID is refused at the handshake; lifting the halt lets the call through; and the denial is signed into the log in the agent's name. agentgateway v1.6.0 accepts all three shipped configs (`--validate-only`).
- **Calls under way** (October 28, 2026). ext_authz stops a call before it starts. A call already under way when a halt lands is cut by the hook carrying it, which hears of each change to the halts at once and checks every call it holds, with the same rule as ext_authz.
  - **A2A:** an exchange the halt covers, from its sender or to its receiver, is cut by ending its ext_proc stream with an error. agentgateway fails a call whose processor errs once its body is under way, so a streamed reply stops where it is, and a request still waiting for its reply never gets one. A halt that lands between ext_authz and the first ext_proc message is caught there, and the call isn't forwarded.
  - **MCP:** the MCP hook keeps a result coming back to a caller the halt covers from it: the caller gets the gateway's error instead. The tool already ran; what it returned is still recorded, on the call's `tool_call` event. A request that reaches the hook after a halt is refused there, as ext_authz would have.
  - **What's recorded:** each cut is a signed `denied` event whose payload says `"cut": true` and names the event that recorded the call (`recorded_as`). What the exchange carried before the cut is recorded as usual.
  - **What it can't cut:** the MCP hook fails open, as capture does, so it cuts only while wake-proxy answers; ext_authz still fails closed for every call after. An MCP server's own stream to the agent (a `GET` for server-initiated messages) goes through neither hook. The gateway configs must give the hooks the caller the same way they give it to ext_authz, which `proxy/tests/gateway_configs.rs` checks.
  - **Checked in CI** against `deploy/`'s mTLS gateway (agentgateway v1.6.0), by `deploy/smoke_test.sh`. The `smoke-test` agent starts a streamed A2A call to a stand-in that streams for a minute, and is halted after two events. The stream ended by the time the halt was acknowledged, with no event after it, and the cut was signed into the log and counted in the metrics.
- **Not yet.** In the example fleet, the caller's name comes from a header any caller can set, so an agent-scoped halt there trusts it; in `deploy/` it comes from SPIFFE IDs.

### Flow policies

Contain's first control (October 28, 2026): rules that keep content from untrusted origins, such as a page a tool fetched or another agent's message, from triggering privileged calls. `--policy-file` names a JSON file:

```json
{"window_seconds": 900,
 "rules": [
   {"id": "no-pay-after-web", "callers": ["billing"], "tools": ["pay", "wire_*"],
    "forbid_origins": ["tool:fetch_page", "agent:research"]},
   {"id": "billing-hears-support-only", "receivers": ["billing"], "allow_origins": ["agent:support"]}
 ]}
```

- **What a rule decides on.** What entered the caller through the proxy within the window:
  - `tool:<target>`, once it has called that MCP tool, resource or prompt (the target its `tool_call` event records);
  - `agent:<sender>`, once a message from that agent was forwarded to it, and `agent:<each agent the sender's token places it under>` with it (see below);
  - `agent:<receiver>`, once a reply from that agent came back.

  It never decides on what the caller says: nothing the caller sends, such as a `wake-origins` header, is read. The proxy notes each origin before the call or the message goes on, so content can't reach an agent ahead of its note.
- **Across a delegation** (October 28, 2026). A sub-agent and the agent that delegated to it pass content both ways where the proxy can't see it, in the same process or over their own channel: the parent's task goes into the sub-agent, and the sub-agent's result comes back. Without accounting for that, either could launder the other's content. A parent that fetched a page could have a fresh sub-agent make the payment, and a sub-agent could fetch the page and hand its parent the result to pay with. So a call is made under every agent its token names, as for halts: each actor in its `act` chain and whom they act for, its `sub`, when the agents file lists it (or there's no file to say). A user a token acts for isn't an agent, and keeping origins under one would tie together every agent acting for that user. A rule checks what entered the caller or any of them, and what enters a caller (a tool's result, an agent's reply) is noted under each of them too. A message an A2A route delivers is noted under its receiver alone, since the route names no chain, but as coming from the sender and from each agent the sender's token places it under: a sub-agent speaks for its parent, so a rule forbidding `agent:planner` covers a message from planner's sub-agent. A rule that names callers covers the calls made under them, so a rule for `support` covers its sub-agents. A chain the proxy cut (past 8 actors, or past one it couldn't read) counts as having every origin, and every rule naming callers covers it. Tracked by token, so a sub-agent that calls with no token of its own, under its parent's SVID or token, already is its parent here.
- **What a rule applies to.** A rule applies to a call when every list it gives matches:
  - `callers`: agent names or identities;
  - `receivers`: the agent an A2A route reaches;
  - `path_prefixes`: the request path;
  - `tools`: MCP tools, resource URIs or prompts, each exact or a prefix ending in `*`.

  The MCP hook checks the rules that name tools, since only it sees the tool. It sees no receiver or path, so a rule can't name tools with either. ext_authz checks the rest, on every request.
- **Denying.**
  - `forbid_origins` denies a call when any origin that entered the caller matches. `allow_origins` denies it when one matches none, so a caller nothing entered passes. Patterns are exact, or a prefix ending in `*`.
  - The first rule that denies is the one named. The call gets a 403 from ext_authz, or the gateway's error from the MCP hook, naming `policy:<id>` as `wake-halt` does. It's recorded as a signed `denied` event whose payload names the origins behind the denial (see [events.md](events.md)).
- **It's coarse.** The proxy sees what reached an agent, not which of it feeds a given call, so everything that entered within the window counts for every call. A rule that forbids `agent:research` for billing's payments denies them for a window after any message from research. Tracking each piece of content needs the agents' SDKs, and isn't built.
- **It fails closed.** In each of these cases a caller counts as having every origin, so any rule that applies to its calls denies them:
  - for one window after wake-proxy starts, or after a reload lengthens the window, since what entered before isn't known;
  - when it has no identity;
  - past 256 origins within a window for one agent;
  - past 10,000 agents, for an agent not remembered;
  - when its token's delegation chain was cut.

  A caller the agents file doesn't list counts as matching every rule's `callers`, unless its own token places it under an agent the file lists, as for halts: a sub-agent is held to the rules that name it or its chain. A file that can't be read or isn't valid denies every call under `policy:unreadable-policy`. Invalid means an unknown field, a rule with no origins to keep, a repeated id, a `*` anywhere but at the end of a pattern, or a window outside 1 to 86,400 seconds. The file is read again within 2 seconds of a change.
- **Not yet.**
  - Several replicas each see only the calls through them, so an origin one noted isn't known to another.
  - Content entering through MCP paths the hooks don't see, such as `tools/list` descriptions and server-initiated messages, isn't noted.
  - The window counts from when a tool call was made, not from when its result came back.
  - The rules are a small JSON format of Wake's own, not Cedar, which the roadmap's stack names for policies in the proxy.

### Run charters

Contain (October 28, 2026): a run's stop conditions, written before it starts, which the proxy holds the run's calls to. The brake puts a charter over the control API (`PUT /v1/charters/{id}`) and ends it there; it decides when the run is done from the signed record and retires the run's credentials (wake-platform's `docs/brake.md`). The proxy keeps charters in the halts' state, on disk and versioned like halts, and the brake confirms one by the state's version.

```json
{"scope": {"agents": ["support/refunder#toolu_01A9"], "tokens": ["jti-7f3a"]},
 "expires_at": 1793232000,
 "never": "@id(\"no-deletes\") forbid(principal, action, resource == Wake::Tool::\"delete_customer\");\nforbid(principal, action, resource == Wake::Agent::\"payments\");",
 "budget": [{"tools": ["refund"], "calls": 1}, {"calls": 200}],
 "issued_at": 1793228400000000000}
```

- **Which calls are the run's.** Those its scope names, as a halt's does: made under the identities in `agents` (an agent, or a sub-agent calling under an identity of its own, and the sub-agents acting under it), or presenting a token in `tokens`. Never anything the caller sends, such as its trace, so a run can't step out of its charter by leaving a header out. A caller nobody can name or place, or whose token's chain was cut, could be the run's, so a charter naming agents covers it, as an agent halt does. A charter can't name the fleet. A run with no identity or token of its own is named by its agent, and its charter then holds every call that agent makes; the brake says so. A charter past its expiry that the brake holds rather than ends denies every call it covers, so until it's ended, like an agent halt, it denies callers nobody can name or place, fleet-wide.
- **`never`** is Cedar: `forbid` policies over the Wake schema below, the entities and action of the dashboard's Cedar for flow policies without their context. A call one forbids is denied. Policies are checked when the charter is put, and refused unless every one is a `forbid` (what isn't forbidden is allowed already), none is a template, and Cedar's validator passes them strictly against the schema, so a misspelled entity type, which would match no call, is refused rather than kept. A call a policy can't be evaluated for is denied: Cedar skips a policy that errors, and a skipped forbid would let the call through. The principal is the caller, as `Wake::Agent::"<name>"`, and it's `in` each agent its call is made under, its token's chain and whom it acts for: `principal in Wake::Agent::"support"` covers support and its sub-agents, where `principal ==` names one caller. A caller nobody can name or place is held to a policy if the policy would forbid the call to any agent the charter names. The resource is the tool, resource URI or prompt an MCP call reaches, as `Wake::Tool::"<name>"`, checked at the MCP hook, or the agent an A2A route reaches, as `Wake::Agent::"<name>"`, checked by ext_authz. A denial names each forbid by its `@id`, or by its place (`policy0` on).

  ```cedar
  namespace Wake {
    entity Agent in [Agent];
    entity Tool;
    action "call" appliesTo { principal: [Agent], resource: [Agent, Tool] };
  }
  ```
- **`budget`** limits the run's MCP calls (`tools/call`, `resources/read`, `prompts/get`): every one, or those to the `tools` a limit names, each exact or a prefix ending in `*`. A call past any limit is denied, and a denied call, by a halt, a flow policy or a charter, isn't counted. A call two charters cover is counted by both only when neither denies it. A caller nobody can name or place isn't counted, so one outside the run can't use the run's budget up; it's still held to `never` and the expiry.
- **`expires_at`**, in Unix seconds (one past the year 2286 is refused, as milliseconds by mistake): from then on, every call the run makes is denied by ext_authz and the MCP hook, whatever it reaches.
- **Denied** calls get a 403 from ext_authz, or the gateway's error from the MCP hook, naming `charter:<id>`, and a signed `denied` event whose payload says which charter, why and what it found ([events.md](events.md)).
- **The control API.** `PUT /v1/charters/{id}` refuses a charter that couldn't be enforced as written: a field it doesn't know, no scope or the fleet, a blank name, no `expires_at`, a `never` as above, over 16 KiB, over 100 policies or nested over 32 levels of brackets and if-then-else (Cedar's parser recurses once a level, and a deep enough `never` would overflow the stack and stop the proxy), over 32 limits, or a tool pattern with `*` anywhere but at the end. A proxy holds 256 charters at most, so the state the brake reads back stays bounded: past that, `PUT` answers 409 until one ends. `POST /v1/charters/check` checks one without putting it. `GET /v1/charters` lists those in force, each with what its budget has `used`, limit by limit. Put again, as the brake does when it can't confirm one, a charter keeps what it used; `DELETE` ends it and forgets that.
- **Not yet.**
  - Budgets are counted by each proxy, in memory, for the calls through it: several replicas each count their own, and a restart counts from zero. The brake's monitor counts the run's calls in the signed record as well.
  - `never` for tools, and budgets, are checked by the MCP hook, which the shipped configs run with `failureMode: failOpen`, as recording is: a call the hook can't answer for, as when wake-proxy is down, goes through unchecked by them. Expiry, and `never` for the agent an A2A route reaches, are checked by ext_authz, which fails closed. Where a charter's limits on tools must hold even then, set the MCP hook's `failureMode: deny`, at the cost that wake-proxy being down stops MCP calls.
  - A2A requests aren't counted against a budget. ext_authz can't tell a message from a task poll, and the A2A processor, which can, doesn't deny yet.

### Metrics

With `--metrics-listen`, wake-proxy serves its counters for Prometheus at `/metrics` (`deploy/compose.yaml` publishes it on the host's loopback, at 9464):
- **Traffic:** MCP calls and A2A exchanges seen, and events sent.
- **Losses:** calls seen but not recorded (`wake_proxy_hooks_dropped_total`), events dropped on their way to the collector (`wake_proxy_events_dropped_total`), and events whose payload was truncated.
- **Pairing:** MCP responses paired by id or by order.
- **The brake:** calls denied and cut, the halts in force, the state's version, and `wake_proxy_state_ok`, which is 0 while every call is denied because the state is lost or unreadable.

Recording fails open, so the two drop counters are the health signal for what wasn't recorded. A batch the collector refuses as bad, with a 4xx other than 408 or 429, isn't sent again: it's dropped and counted at once.

## How the certificate travels

Decided October 27, 2026. Anyone must be able to check a proxy event from the log alone, years later, after the SVID that signed it has expired. So the event carries the certificate:

- The proxy sets `wake.signature.certificate`: base64 of the DER X.509-SVID, followed by any intermediates. Like every `wake.signature*` attribute, it's outside the signed bytes; the CA's signature is what vouches for it.
- The log entry keeps it in an optional `certificate` field (`log/v0.3.0`).
- Checking an entry, which `wakelog.Entry.Check` does:
  - The signer is listed for the event's `wake.capture` in the trusted signers (the collector's `trusted_signers`, `wake-log verify -signer`). Agents get SVIDs from the same SPIRE, so a bundle alone would let any of them sign as the proxy.
  - The certificate chains to a trusted SPIRE trust bundle as of when the collector received the event (`received_unix_nano` in the entry), give or take 5 minutes. The event's start time is the signer's to choose, so it isn't used; it must fall no more than 24 hours before that receipt.
  - It isn't a CA.
  - Its one `spiffe://` URI SAN equals `signer`.
  - Its key is P-256.
  - The ECDSA signature, ASN.1 DER over SHA-256, checks out over `event`.
- The collector (`trust_bundles` and `trusted_signers`) and the ingester are configured with the trust bundles and signers to accept. Publishing bundles next to the log is left until a design partner needs it.

Updated October 27, 2026: the signer allowlist and the receipt time were added after an audit showed an agent's SVID could sign events as the proxy, and that a key from an expired SVID could still sign events dated while it was valid. The collector checks the SVID when the event arrives, so a proxy event that reaches it more than 5 minutes after its SVID expired is logged invalid. SPIRE rotates SVIDs at half their lifetime, which leaves the proxy at least that long to deliver. The proxy signs with the rotated SVID within 5 seconds of SPIRE writing it, so it never signs with an expired one while a new one is on disk.

The cost is about 600 bytes per proxy event. Storing each certificate once next to the log and referring to it by digest would save the bytes, but it adds a second lookup to every check, so it waits until the size matters.
