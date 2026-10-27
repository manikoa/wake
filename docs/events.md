# Event format

Every Wake event is an OpenTelemetry span. Standard GenAI attributes are used where they exist; those conventions are still marked Development, so Wake's own fields live under `wake.` and can move onto the standard when it settles.

Start with these fields. Add one only when the provenance view or a design partner needs it.

## Wake attributes

| Attribute | Type | Meaning |
|---|---|---|
| `wake.event.id` | string (UUID) | Unique id for the event. Other events list it in `wake.parents`. |
| `wake.event.kind` | string | `model_call`, `tool_call`, `agent_message`, `memory_read` or `memory_write` |
| `wake.agent.id` | string | The agent that did it |
| `wake.capture` | string | What recorded it: `sdk`, `proxy` or `memory_adapter` |
| `wake.parents` | string[] | Ids of the events whose output fed this one |
| `wake.origins` | string[] | Sources of content that entered the system at this event: `user`, `tool:<name>`, `agent:<id>`, `memory:<entry id>` |
| `wake.payload` | string (JSON) | What was seen and said: the event's inputs and outputs |
| `wake.payload.sha256` | string | Hex SHA-256 of `wake.payload` |
| `wake.signature` | string | Base64 Ed25519 signature, added by the signer (see [signing.md](signing.md)) |
| `wake.signer` | string | SPIFFE id of the signer |
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

The message is a parent of B's next event. An answer going back from B to A is another `agent_message`, this time in A's run.

## How the SDK finds parents

Where the framework gives message ids, parents follow them: the AI and tool messages in a model call's context, and the model call that requested a tool call. Where an app builds prompts from strings, so nothing links one call to the next, the SDK compares content within the trace: an earlier event is a parent when its output appears in the new event's input, or, for a tool call, when the tool's arguments appear in its output. Short outputs aren't matched, so a parent found this way is unlikely to be chance. An event with no parent found is left unlinked rather than given a guess.

## What gets signed

The signer signs the UTF-8 bytes of this JSON object, serialized with keys sorted and no whitespace:

```json
{"attributes":{...},"end_time_unix_nano":0,"span_id":"<hex>","start_time_unix_nano":0,"trace_id":"<hex>"}
```

`attributes` holds every `wake.*` and `gen_ai.*` attribute except `wake.signer` and any attribute starting with `wake.signature`. Encode without HTML escaping, so `<`, `>` and `&` stay as they are.

## What goes in the log

Each event becomes one log entry: a JSON object holding the exact signed bytes, the signature, the signer and the collector's check.

```json
{"event":{...signed bytes...},"signature":"<base64>","signer":"<name>","valid":true}
```

Anyone with the signer's public key can check an entry on its own. Spans that only group events aren't logged.
