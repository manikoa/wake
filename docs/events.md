# Event format

Every Wake event is an OpenTelemetry span. Standard GenAI attributes are used where they exist; those conventions are still marked Development, so Wake's own fields live under `wake.` and can move onto the standard when it settles.

Start with these fields. Add one only when the provenance view or a design partner needs it.

## Wake attributes

| Attribute | Type | Meaning |
|---|---|---|
| `wake.event.id` | string (UUID) | Unique id for the event. Other events list it in `wake.parents`. |
| `wake.event.kind` | string | `model_call`, `tool_call`, `agent_message`, `memory_read`, `memory_write`, or `denied` for a call the brake stopped |
| `wake.agent.id` | string | The agent that did it |
| `wake.agent.instance` | string | The running instance of the agent that did it, for an agent that can run as several at once, such as a subagent. Absent otherwise. See Instances |
| `wake.agent.lineage` | string[] | With `wake.agent.instance`: the agent and instances that started it, outermost first |
| `wake.capture` | string | What recorded it: `sdk`, `proxy` or `memory_adapter` |
| `wake.token.subject` | string | Set by the proxy when the caller presented a JWT whose `sub` it could read: whom the token acts for. For a delegated token, often a user or the agent that delegated, while `wake.agent.id` names the agent acting. See Tokens |
| `wake.token.actors` | string[] | With a delegated token: the agents acting under it, the one acting now first, then each before it (its nested `act` claims) |
| `wake.token.actors_cut` | bool | Set, to true, when the chain went on past what `wake.token.actors` keeps: its earliest actors are unknown |
| `wake.token.id` | string | The token's `jti`, when it has one |
| `wake.token.expires_unix` | int | The token's `exp`, in Unix seconds |
| `wake.token.bound` | bool | Whether the token is bound to its holder's key (it has a `cnf` claim, as DPoP and mTLS-bound tokens do), rather than usable by whoever holds a copy. Set on every event that records a token, so it, not `wake.token.subject`, says whether the caller presented one |
| `wake.call.ok` | bool | Set by the proxy on a call it saw answered: whether it went well. On an MCP `tool_call`, a result came back that doesn't say `isError`; a call with no response seen didn't (agentgateway skips the response hook on a JSON-RPC error). On an A2A reply, the receiver answered with a 2xx, a result, no JSON-RPC error and no task that failed, was rejected or was canceled. Left out when the answer was cut short or can't be read. A run charter's checks that it's done count only calls that went well |
| `wake.a2a.task_state` | string | Set by the proxy on an A2A reply that gives a task's state: the last one it gives, in v0.3's spelling (`submitted`, `working`, `input-required`, `completed`, `canceled`, `failed`, `rejected`, `auth-required`, `unknown`), which v1.0's `TASK_STATE_COMPLETED` and the like map onto. `completed` is the receiver saying its task is done |
| `wake.parents` | string[] | Ids of the events whose output fed this one |
| `wake.origins` | string[] | Sources of content that entered the system at this event: `user`, `tool:<name>`, `agent:<id>`, `memory:<entry id>` |
| `wake.payload` | string (JSON) | What was seen and said: the event's inputs and outputs. Not signed; the signed digest commits to it. A payload too large to send whole is cut short and says so: the proxy keeps 1 MiB of each body, as a string, and adds `"truncated": true`. The SDKs send `{"truncated": true, "bytes", "sha256", "prefix"}` in place of one over 4 MiB, with the whole payload's size and hex SHA-256, and its first 2 MiB as a string. The collector reads a payload sent as bytes as the bytes it holds, and refuses one of any other type (see Secrets) |
| `wake.payload.sha256` | string | Hex SHA-256 of `wake.payload`'s UTF-8 bytes. Set by the signer from the payload it received, once its secrets are redacted (see Secrets), replacing any value the sender gave. An event with no payload has none: the signer removes one the sender gave |
| `wake.payload.redacted` | string[] | Set by the signer when it redacts secrets from the payload: the kinds it replaced, sorted, such as `["github_token", "jwt"]`. A list the sender gave is removed, and a placeholder already in the payload isn't counted. Signed, so a credential crossing an agent boundary is on the record even though the credential isn't |
| `wake.signature` | string | Base64 signature, added by the signer: Ed25519 from the collector, ECDSA P-256 from the proxy (see [signing.md](signing.md)) |
| `wake.signer` | string | SPIFFE id of the signer |
| `wake.signature.certificate` | string | Set by a signer that signs with an X.509-SVID, such as the proxy: base64 of the DER SVID followed by any intermediates. Not signed; the CA's signature vouches for it |
| `wake.signature.valid` | bool | Set by the collector: whether the signature checked out. Always overwritten, so a sender can't claim it. |
| `wake.signature.error` | string | Set by the collector when `wake.signature.valid` is false: why. Not in the log entry; checking the entry again gives the reason |
| `wake.received_unix_nano` | int | Set by the collector: when it received the event, in Unix nanoseconds. Always overwritten. Not signed by the event's signer, since the proxy signs before the collector receives; the log entry holds it, under the log's signed checkpoint |

Framework specific context goes under the framework's name, for example `wake.langgraph.node`.

## Standard attributes used

- `gen_ai.operation.name`: `chat` for model calls, `execute_tool` for tool calls, `invoke_agent` for an agent run
- `gen_ai.request.model`
- `gen_ai.tool.name` and `gen_ai.tool.call.id`
- `gen_ai.agent.name`

## Structure spans

A span without `wake.event.kind` isn't an event. It only groups events, for example the `invoke_agent` span around one run of a LangGraph graph, so that every event of the run shares a trace.

## Origins and parents

An event's `wake.origins` names only the content that **entered** at that event: a tool result, a user's message seen for the first time, a message from another agent, or a memory entry. Everything else an event saw arrives through `wake.parents`. The full set of origins behind an event is the union over its ancestors, computed in the provenance graph.

## Instances

An agent's name, `wake.agent.id`, is what the brake halts, the inventory lists and the memory adapter locks, so it stays the same however many copies of the agent run. When several can run at once, such as five subagents of one type started by the same agent, each is an **instance** of its own, and its events carry two more attributes:

- `wake.agent.instance` names the instance: the agent's name, `#`, and an id unique within the agent that started it. A Claude subagent's is the id of the Agent tool use that started it, as in `support/researcher#toolu_01A9`.
- `wake.agent.lineage` lists what started it, outermost first: the agent at the top, then each instance between it and this one. A subagent the support agent started has `["support"]`.

The `agent_message` handing an instance its task is recorded in the instance's name, so it carries the instance. The answer going back is recorded in the starter's name, and carries the starter's instance, if it has one. A memory read or write made inside a tool an instance called carries the instance too. An agent that only ever runs as one has neither attribute, and neither does the agent at the top of a lineage. Origins name agents, not instances: what an instance says to another agent enters as `agent:<its agent's name>`.

When an SDK can't tell which instance made a call, it leaves the instance out rather than guess. A Claude tool call can start before the message saying which subagent asked for it is read, even before the one saying that subagent started; it's attributed then only when Claude Code's hooks have named the subagent before, by an `agent_id` Wake has matched to its instance. Two in-process Claude tool calls with the same name and arguments can't be told apart while both wait to run, so a memory read or write inside either names no instance unless both are the same instance's.

What makes an instance, by integration:

- **Claude Agent SDK:** each subagent the Agent tool starts, `<agent>/<subagent type>#<the Agent tool use id>`. The `agent_message` handing it its task is recorded once it has started, so an Agent tool call refused before then, as by a halt, makes no instance: it's recorded as the refused tool call it is. One the tool leaves running in the background, as it does by default, stays that instance until Claude Code says it has finished, in later turns too, and its answer is recorded then.
- **OpenAI Agents SDK:** an agent run inside a tool call, as with `agent.as_tool()`, `<agent>#<the tool call's span id>` (the SDK's own span id), started by the agent that made the call. A response can call one agent as a tool several times at once, so each run inside a tool call is also linked as a workflow of its own: one copy's steps never take another's as parents, whatever their arguments. An agent a handoff reaches inside such a run is an instance of that run too. Runs of the workflow's own, handoffs included, aren't instances.
- **LangChain and LangGraph:** a named run (`handler.agent(name)`) started inside another agent's run, `<name>#<its invoke_agent span id>`, started by that run. A LangGraph node is a step of the agent running the graph, not an agent of its own, so a node run several times at once isn't an instance.

These come from the SDK, inside the agent's process, so they say what the agent reported, like the rest of an SDK event. Instances of one agent share its process and credentials, so the proxy sees them as that agent.

## Tokens

When a caller presents a JWT at the gateway, the proxy records what the token says on the events it captures for that call: its `tool_call`, both `agent_message`s of an A2A exchange (the sender's token, on the message and on the reply), and any `denied` event, cut calls included. It never records the token itself: the gateway computes these facts from the claims of the token it verified and passes only them (see "Who's calling" in [proxy.md](proxy.md)). A caller that authenticates with an X.509-SVID presents no token, and its events have none of these attributes.

A token from OAuth token exchange ([RFC 8693](https://www.rfc-editor.org/rfc/rfc8693)) acts for its `sub` and is held by the agent its `act.sub` names, with earlier actors nested deeper in `act`. The gateway names the caller, `wake.agent.id`, by the agent acting; `wake.token.subject` and `wake.token.actors` keep whom it acts for and the chain the delegation passed through, so the record shows which user a sub-agent acted for, and through which agents. A chain is kept to its first 8 actors, and a claim too long to be an identifier (over 1,024 bytes) is left out rather than cut. A chain that goes on past what's kept, past 8 actors or past an actor whose claim was left out, is marked with `wake.token.actors_cut`, and so is a token whose `sub` is missing or left out, since whom it acts for is unknown. The deny lever treats its call as one from a caller it can't name (see "A halt" in [proxy.md](proxy.md)).

## Messages between agents

A message from agent A to agent B is an `agent_message` event in B's run, recorded where B receives it:

- `wake.agent.id` is B.
- `wake.origins` is `agent:<A's id>`, since this is where A's content enters B.
- `wake.parents` are the events in A whose output the message carries.
- `wake.payload` is `{"from": "<A>", "to": "<B>", "content": ...}`.

The message is a parent of B's next event. An answer going back from B to A is another `agent_message`, this time in A's run, and it's a parent of the step in A that reads it. How the link travels between processes is in [linking.md](linking.md).

## Denied calls

The proxy's deny lever records each call it denies as a `denied` event, captured by the proxy and signed like its other events (the memory adapter records the memory it refuses the same way; see Memory):

- `wake.agent.id` is the caller, under its agent's name when the proxy's agents file lists its identity, or `unknown`.
- `wake.parents` come from the call's link, so the event joins the caller's trace under the step that made the call.
- `wake.brake.halt_id` names the first halt that denied it, and `wake.brake.receiver` the agent the call was sent to, when there is one.
- `wake.payload` is `{"caller", "identity", "receiver", "method", "host", "path", "halt", "halts"}`: `identity` is what the caller authenticated as, when its name differs, and `halts` every halt that covered the call.

A call a **flow policy** denies (see [proxy.md](proxy.md)) is recorded the same way, under `wake.brake.halt_id` `policy:<rule id>`.
- **Payload:** it adds `origins`, the origins that entered the caller and that the rule keeps from the call. When the caller counted as having every origin, it's `*`, and `every_origin` says why.
- **No `wake.origins`:** no content entered at the denial.

A call a **run charter** denies (see [proxy.md](proxy.md), "Run charters") is recorded the same way, under `wake.brake.halt_id` `charter:<charter id>`. Its payload adds `charter`, the charter's id; `why`: `never`, `budget`, `expired`, or `unreadable` for a `never` that can't be read; and `detail`, in words: the forbids that deny the call, the limit it's past, or when the charter expired.

A call already under way when a halt lands is **cut**, and recorded the same way: an A2A exchange the proxy is carrying, such as a streamed reply, or an MCP call whose result was on its way back to the caller.
- **Payload:** it adds `"cut": true`, and `recorded_as`, the id of the event that recorded the call, when there is one.
- **Its MCP form:** the `method` is the MCP method and the `host` the MCP server. The event's parent is the call's own `tool_call`, which records what the tool returned, though the caller never got it.
- **What the exchange carried before the cut** is recorded as usual.

An SDK that stops an agent in its own process records each call it refuses as a `denied` event of its own, captured by `sdk`. The Claude Agent SDK, OpenAI Agents SDK and LangChain integrations do, in both SDKs (see their READMEs).
- **What it records:** the tool call the agent tried, with its arguments and the refusal as its output. Its parent is the model call that asked for it, and the model's next call reads the refusal.
- **Its halt:** `wake.brake.halt_id` names the halt that covered the call, or `stale-rules` while the SDK's rules are stale, or `halt-check-failed` when it couldn't check them: it fails closed.
- **No `wake.origins`:** nothing entered at the denial. The tool never ran, so nothing of it reached the agent, and a later step's chain doesn't count it.
- **A refused run:** a LangChain run refused before it started is recorded as the `agent_message` that would have handed it its task, in the name of the run that tried, with `wake.brake.receiver` naming the run refused, and no `wake.origins` either.

## Memory

The memory adapter records each write to a memory store or memory file as a `memory_write` event, and each read as a `memory_read`. Both have `wake.capture` `memory_adapter`, `wake.memory.store` and `wake.memory.entry`.

- **A write's parents** are the tool call making it, plus the earlier events in the trace whose output it carries. Before the entry lands, the adapter hands back a label for the store to keep with it:

  ```json
  {"event":"<the write's event id>","origins":["tool:read_email","user"],"sha256":"<digest of the content>","agent":"<id>"}
  ```

  `origins` is the union of origins upstream of the write that the SDK recorded. `wake.memory.origins` on the write holds the same list. A store keeps the label in the entry under `__wake__`; a memory file keeps it next to the file as `<file>.wake.json`.
- **A read's origin** is `memory:<store>/<entry>`. When the entry's label digest matches what was read, the read's parent is the write, and `wake.memory.origins` repeats the label's origins. An entry with no label (`wake.memory.labeled` false), or one changed since it was written (`wake.memory.changed` true), gets no parent. So anything that follows from it is flagged, rather than trusted, by the completeness metric. The read's payload holds the label's `event`, `origins`, `sha256` and `agent`, and not its history.
- **The digest**, `wake.memory.sha256`, is the hex SHA-256 of the content's UTF-8 bytes. For a memory file that's its text, byte for byte as it is on disk. For a store it's the value without its label, as JSON: keys sorted in code point order, `", "` and `": "` between items, non-ASCII as it is, and each whole number an integer, so `500.0` is `500`, since JavaScript can't tell them apart. It's the label's `sha256`, and the payload's `content` is the text it covers, less any credentials redacted from the payload before signing (see Secrets): the digest is always of the content as written, and the provenance graph compares digests, never a digest with the payload. A write carries it for what it wrote, a read for exactly what it read, and a rollback for what it restored; a delete has none. The SDKs share test vectors for it in `sdk/testdata/memory_digests.json`. Like every `wake.*` attribute it's signed, so the provenance graph can check a read against the write its parent names, and a rollback against the write it restores.

The label is a pointer, not evidence: anything that can write the store can forge one. The signed `memory_write` it names, and the digest, are what make it checkable.

- **A delete** is a `memory_write` with `wake.memory.deleted` true and no label. Its parents are the tool call making it.
- **History.** When a write replaces content its label vouches for, the new label keeps that version in `previous`: the content, and its label's `event`, `origins`, `sha256` and `agent`, newest first, at most 16. Content changed since its label was written isn't kept, since no label vouches for it. A memory file's history is in its `.wake.json`. A store's is in the value's `__wake__`, which never reaches the app.
- **A rollback** restores a version from the history, the newest unless it names another. It's a write, so it's refused as one: when the quarantine locks the agent, or hides the entry as it is now. It's also refused when the quarantine wouldn't serve the version itself, by its write, origins or agent, since the version's fresh label is one the quarantine no longer matches. The version must name a write and match its own digest, or nothing is restored, since anything that can write the store can write its history too. The rollback is a `memory_write` whose only parent is the write it restores, which `wake.memory.rollback_to` names. Its `wake.memory.origins` are that version's, and its `wake.memory.sha256` the restored content's. The new label names the rollback, so later reads link to it, and keeps the version it replaced, so a rollback can be rolled back.

**Write gate.** An app can give the adapter a gate, which sees each write before it lands: the store, the entry, the content and the origins upstream of it. It sees each delete too, with no content, and each rollback, with the version's content and origins. What it refuses is a `denied` event like a quarantine's, with `wake.brake.halt_id` `write-gate-denied`. The gate fails closed. A gate that raises refuses, with its error in the payload's `error`. So does a gate that can't be asked because the write's origins couldn't be worked out, with `error` `origins unavailable`. With no gate, failing to work out a write's origins never blocks it: it lands unlabeled. The store wrapper checks every put and delete in a batch before it records any, so a refusal leaves no `memory_write` for a write that never ran.

**Quarantine.** When the brake quarantines memory, the adapter refuses the writes and deletes of agents it locks, and stops serving entries the quarantine selects: by the agent that wrote them, their origins, or the write that made them (all from the label), by key, or because no label vouches for them. Quarantined entries stay in the store, as evidence, so deleting one is refused too. Each refused write, delete or read is a `denied` event from the memory adapter, with `wake.memory.refused` (`write`, `delete` or `read`) and `wake.brake.halt_id` naming the quarantine. A refused read's or delete's parent is the write it came from. The adapter fails closed: while it holds no current rules from the proxy, or can't check them, it refuses every write and serves no entry, with `wake.brake.halt_id` `stale-rules` or `quarantine-check-failed`. It does the same when the proxy doesn't know what it quarantined, because its state can't be read or was never initialized: its rules then carry `hidden_by` (`unreadable-state` or `uninitialized-state`).

## How the SDK finds parents

Where the framework gives message ids, parents follow them: the AI and tool messages in a model call's context, and the model call that requested a tool call. Where an app builds prompts from strings, so nothing links one call to the next, the SDK compares content within the trace: an earlier event is a parent when its output appears in the new event's input, or, for a tool call, when the tool's arguments appear in its output. Short outputs aren't matched, so a parent found this way is unlikely to be chance. An event with no parent found is left unlinked rather than given a guess.

## Secrets

The log keeps every payload for good, so a credential an agent puts in a message, a tool call or memory would be readable there, by anyone who can read the log, for as long as it's kept. Before an event is signed, its signer replaces every credential it recognizes in the payload: the collector for the SDK and memory adapter events it signs, and the proxy for its own. The signature and `wake.payload.sha256` cover the payload as it's kept.

The collector logs the events it can't trust too, as evidence: one whose signature fails, one that repeats an attribute, and one unsigned that it doesn't sign. It redacts their payloads the same way first, listing what it replaced in `wake.payload.redacted` and setting `wake.payload.sha256` from what's kept, so the payload can still be read from the log and checked against its digest. They stay invalid, and a signature one came with no longer covers it. An event the proxy validly signed is different: the collector can't change it without breaking the signature that vouches for it, so its payload is logged as the proxy signed it. A proxy from before redaction (October 28, 2026) signs credentials as they are, and has to be upgraded.

Each credential becomes a placeholder naming its kind and the first 12 hex digits of the credential's SHA-256, so two mentions of one credential can be matched without either being readable:

```text
[redacted jwt sha256:56aec5e5c6aa jti:tok-7]
```

| Kind | What it matches |
|---|---|
| `private_key` | A PEM private key block, `-----BEGIN … PRIVATE KEY-----`, or an OpenPGP `-----BEGIN PGP PRIVATE KEY BLOCK-----`, to its END line, or as far as its body runs if it's cut short. Its line breaks can be raw or escaped for JSON at any depth, as in a key in JSON held in a string in the payload, and so can the `+`, `/` and `=` some encoders escape: .NET writes `+` as `\u002B`, PHP `/` as `\/` |
| `jwt` | A signed JWT: three base64url parts, the first two JSON objects (`eyJ…`). Its placeholder keeps the token's `jti` when that's a plain id (up to 128 of `A-Z a-z 0-9 . _ : -`) that doesn't itself look like a credential, so it can be matched to `wake.token.id` |
| `github_token` | `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` tokens and `github_pat_` tokens |
| `google_api_key`, `google_oauth_token` | `AIza…` keys and `ya29.…` access tokens |
| `slack_token` | `xoxa-`, `xoxb-`, `xoxp-`, `xoxo-`, `xoxs-`, `xoxr-` tokens and `xapp-` app tokens |
| `llm_api_key` | `sk-ant-…`, `sk-proj-…`, `sk-svcacct-…`, `sk-admin-…` and older `sk-…` keys |
| `stripe_key` | `sk_live_…` and `rk_live_…` keys |
| `bearer_token` | `Bearer` (or `bearer`, `BEARER`) and a token of at least 20 characters, when it isn't a JWT |
| `basic_auth` | `Basic` (or `basic`, `BASIC`) and base64 of `user:password`, as in an `Authorization` header. Base64 that isn't UTF-8 text with a colon in it is left, so a word after "Basic" isn't taken for credentials |
| `url_password` | The password in a URL's `scheme://user:password@`. The rest of the URL is kept |
| `aws_secret_key` | An AWS secret access key, 40 characters of base64, right after `aws_secret_access_key`, `AWS_SECRET_ACCESS_KEY`, `SecretAccessKey` or `secretAccessKey` and then `=`, `:` or `>` (STS's XML), with any quotes and spaces between |

Only formats that name themselves are matched, so ordinary text is never taken for a secret. Not caught: a password or key in a form nothing identifies, and an AWS secret access key on its own or under another name, since 40 characters of base64 could be anything. A secret is matched only where a word starts: at the start of the payload, after a whole JSON escape such as `\n` or `\u00e9`, or after a character that can't be part of a word, other than the `]` that ends a placeholder. So `task-` followed by an MD5 isn't taken for an `sk-` key, and a match never starts inside an escape: the SDKs send `®yJ…` as `\u00aeyJ…`, which holds no JWT. What's replaced never holds a quote or part of an escape, so a JSON payload stays JSON, however deep its strings nest JSON, and redacting twice changes nothing more. `wake.payload.redacted` lists only what the signer replaced: a placeholder already in the payload, whatever put it there, isn't counted. The patterns run in order, in time linear in the payload whatever an agent sends: Rust's regex crate in the proxy, and Go's regexp in the collector, which matches a secret that starts a word by hand, only where one of its literals is, since Go's regexp would step through every byte to find a word start. A test holds those to the regexps. `collector/event/testdata/redaction.json` holds both to the same output, byte for byte, and both are fuzzed, with non-ASCII as it is and escaped, as the SDKs send it.

A payload is text: a string, or bytes, which the collector reads as the bytes they hold, so they're redacted, digested and logged like a string. A value of any other type has no text to redact, since a map or a list shows bytes inside it as base64: the collector won't sign an event that has one, and never logs it.

The SDKs send payloads whole, over TLS, to the collector, which redacts them before signing. Redacting in the agent's process would add work to every call the agent makes, which recording mustn't.

## What gets signed

The signer signs the UTF-8 bytes of this JSON object, serialized with keys sorted and no whitespace:

```json
{"attributes":{...},"end_time_unix_nano":0,"span_id":"<hex>","start_time_unix_nano":0,"trace_id":"<hex>"}
```

`attributes` holds every `wake.*` and `gen_ai.*` attribute except `wake.signer`, any attribute starting with `wake.signature`, `wake.payload` and `wake.received_unix_nano`. Encode without HTML escaping, so `<`, `>` and `&` stay as they are.

A double that is NaN or infinite has no JSON form, so an event holding one in a signed attribute has no signed bytes. The collector marks it invalid with the reason and goes on with the rest of the request. It can't be logged, so it's counted in `wake_log_dropped_events` with reason `unloggable`. The shared vectors in `collector/event/testdata/canonical.json` include these cases, marked `invalid`.

The payload is signed through its digest. Before signing an event that has a payload, the signer sets `wake.payload.sha256` from the payload's bytes, so the signature commits to exactly that payload, however large it is and wherever it's stored. A component that signs its own events, such as the proxy, does the same and still sends `wake.payload` on the span. The collector marks an event invalid when its payload doesn't match the signed digest, the same as a bad signature. Why the payload stays out of the signed bytes is in [large-payloads.md](large-payloads.md).

## What goes in the log

Each event becomes one log entry: a JSON object holding the exact signed bytes, the payload when it fits, the signature, the signer, when the collector received it and the collector's check.

```json
{"event":{...signed bytes...},"payload":"<the payload>","signature":"<base64>","signer":"<name>","certificate":"<base64>","received_unix_nano":0,"valid":true}
```

A log entry holds at most 65,535 bytes. `payload` is a JSON string holding the payload's exact bytes, and it's left out when the entry would be larger with it, or when the payload isn't valid UTF-8. The payload is then stored next to the log's tiles, at `payload/sha256/<wake.payload.sha256>`, in the same bucket or directory. It's written before the entry is appended. An event with no payload has neither `payload` nor a digest.

`certificate` is there for a signer that signs with an X.509-SVID, such as the proxy, and left out otherwise. `received_unix_nano` is the collector's `wake.received_unix_nano`. It's outside the signed bytes, but the log's signed checkpoint covers it like the rest of the entry, so read entries through a check of the log (`wakelog.Verify`).

Anyone with the signer's public key, or for an SVID signer the SPIRE trust bundle, can check an entry on its own:

1. Check `signature` over `event`. For the collector, use its public key. For an SVID signer, `signer` must be listed for the event's `wake.capture` among the signers you trust, since a bundle vouches for every workload in its trust domain, agents included. `certificate` must chain to the trust bundle as of `received_unix_nano`, give or take 5 minutes, have exactly one `spiffe://` URI SAN equal to `signer`, and hold a P-256 key, and the signature is ASN.1 DER ECDSA over the SHA-256 of `event`. The event must start no more than 24 hours before `received_unix_nano`, and no more than 5 minutes after it. A note format signer listed for no capture may sign any; once a capture lists signers, only they count for it.
2. Take the payload from `payload`, or from `payload/sha256/<wake.payload.sha256>` next to the log.
3. Check that its SHA-256 is the signed `wake.payload.sha256`.

The log module's `ParseEntry`, `Check` and `ReadPayload` do these steps. Spans that only group events aren't logged.
