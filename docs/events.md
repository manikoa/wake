# Event format

Every Wake event is an OpenTelemetry span. Standard GenAI attributes are used where they exist; those conventions are still marked Development, so Wake's own fields live under `wake.` and can move onto the standard when it settles.

Start with these fields. Add one only when the provenance view or a design partner needs it.

## Wake attributes

| Attribute | Type | Meaning |
|---|---|---|
| `wake.event.id` | string (UUID) | Unique id for the event. Other events list it in `wake.parents`. |
| `wake.event.kind` | string | `model_call`, `tool_call`, `agent_message`, `memory_read`, `memory_write`, or `denied` for a call the brake stopped |
| `wake.agent.id` | string | The agent that did it |
| `wake.capture` | string | What recorded it: `sdk`, `proxy` or `memory_adapter` |
| `wake.parents` | string[] | Ids of the events whose output fed this one |
| `wake.origins` | string[] | Sources of content that entered the system at this event: `user`, `tool:<name>`, `agent:<id>`, `memory:<entry id>` |
| `wake.payload` | string (JSON) | What was seen and said: the event's inputs and outputs. Not signed; the signed digest commits to it |
| `wake.payload.sha256` | string | Hex SHA-256 of `wake.payload`'s UTF-8 bytes. Set by the signer from the payload it received, replacing any value the sender gave |
| `wake.signature` | string | Base64 signature, added by the signer: Ed25519 from the collector, ECDSA P-256 from the proxy (see [signing.md](signing.md)) |
| `wake.signer` | string | SPIFFE id of the signer |
| `wake.signature.certificate` | string | Set by a signer that signs with an X.509-SVID, such as the proxy: base64 of the DER SVID followed by any intermediates. Not signed; the CA's signature vouches for it |
| `wake.signature.valid` | bool | Set by the collector: whether the signature checked out. Always overwritten, so a sender can't claim it. |

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

## Memory

The memory adapter records each write to a memory store or memory file as a `memory_write` event, and each read as a `memory_read`. Both have `wake.capture` `memory_adapter`, `wake.memory.store` and `wake.memory.entry`.

- **A write's parents** are the tool call making it, plus the earlier events in the trace whose output it carries. Before the entry lands, the adapter hands back a label for the store to keep with it:

  ```json
  {"event":"<the write's event id>","origins":["tool:read_email","user"],"sha256":"<digest of the content>","agent":"<id>"}
  ```

  `origins` is the union of origins upstream of the write that the SDK recorded. `wake.memory.origins` on the write holds the same list. A store keeps the label in the entry under `__wake__`; a memory file keeps it next to the file as `<file>.wake.json`.
- **A read's origin** is `memory:<store>/<entry>`. When the entry's label digest matches what was read, the read's parent is the write, and `wake.memory.origins` repeats the label's origins. An entry with no label (`wake.memory.labeled` false), or one changed since it was written (`wake.memory.changed` true), gets no parent. So anything that follows from it is flagged, rather than trusted, by the completeness metric.

The label is a pointer, not evidence: anything that can write the store can forge one. The signed `memory_write` it names, and the digest, are what make it checkable.

**Quarantine.** When the brake quarantines memory, the adapter refuses the writes of agents it locks, and stops serving entries the quarantine selects: by the agent that wrote them, their origins, or the write that made them (all from the label), by key, or because no label vouches for them. Quarantined entries stay in the store, as evidence. Each refused write or read is a `denied` event from the memory adapter, with `wake.memory.refused` (`write` or `read`) and `wake.brake.halt_id` naming the quarantine. A refused read's parent is the write it came from. When the proxy doesn't know what it quarantined, because its state can't be read or was never initialized, its rules carry `hidden_by` (`unreadable-state` or `uninitialized-state`), and the adapter serves no entry.

## How the SDK finds parents

Where the framework gives message ids, parents follow them: the AI and tool messages in a model call's context, and the model call that requested a tool call. Where an app builds prompts from strings, so nothing links one call to the next, the SDK compares content within the trace: an earlier event is a parent when its output appears in the new event's input, or, for a tool call, when the tool's arguments appear in its output. Short outputs aren't matched, so a parent found this way is unlikely to be chance. An event with no parent found is left unlinked rather than given a guess.

## What gets signed

The signer signs the UTF-8 bytes of this JSON object, serialized with keys sorted and no whitespace:

```json
{"attributes":{...},"end_time_unix_nano":0,"span_id":"<hex>","start_time_unix_nano":0,"trace_id":"<hex>"}
```

`attributes` holds every `wake.*` and `gen_ai.*` attribute except `wake.signer`, any attribute starting with `wake.signature`, and `wake.payload`. Encode without HTML escaping, so `<`, `>` and `&` stay as they are.

The payload is signed through its digest. Before signing an event that has a payload, the signer sets `wake.payload.sha256` from the payload's bytes, so the signature commits to exactly that payload, however large it is and wherever it's stored. A component that signs its own events, such as the proxy, does the same and still sends `wake.payload` on the span. The collector marks an event invalid when its payload doesn't match the signed digest, the same as a bad signature. Why the payload stays out of the signed bytes is in [large-payloads.md](large-payloads.md).

## What goes in the log

Each event becomes one log entry: a JSON object holding the exact signed bytes, the payload when it fits, the signature, the signer and the collector's check.

```json
{"event":{...signed bytes...},"payload":"<the payload>","signature":"<base64>","signer":"<name>","certificate":"<base64>","valid":true}
```

A log entry holds at most 65,535 bytes. `payload` is a JSON string holding the payload's exact bytes, and it's left out when the entry would be larger with it, or when the payload isn't valid UTF-8. The payload is then stored next to the log's tiles, at `payload/sha256/<wake.payload.sha256>`, in the same bucket or directory. It's written before the entry is appended. An event with no payload has neither `payload` nor a digest.

`certificate` is there for a signer that signs with an X.509-SVID, such as the proxy, and left out otherwise.

Anyone with the signer's public key, or for an SVID signer the SPIRE trust bundle, can check an entry on its own:

1. Check `signature` over `event`. For the collector, use its public key. For an SVID signer, `certificate` must chain to the trust bundle as of the event's start time, have exactly one `spiffe://` URI SAN equal to `signer`, and hold a P-256 key, and the signature is ASN.1 DER ECDSA over the SHA-256 of `event`.
2. Take the payload from `payload`, or from `payload/sha256/<wake.payload.sha256>` next to the log.
3. Check that its SHA-256 is the signed `wake.payload.sha256`.

The log module's `ParseEntry`, `Check` and `ReadPayload` do these steps. Spans that only group events aren't logged.
