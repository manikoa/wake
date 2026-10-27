# Linking events across agents

Decided October 27, 2026.

How Wake stitches one trace and one provenance graph across processes: agents, the proxy, and the tools and agents they call. Every SDK, in any language, and the proxy follow these rules, so their events link to each other. The Python SDK (`sdk/python/src/wake/links.py`) and the proxy (`proxy/src/extproc.rs`, `proxy/src/mcp.rs`) implement them, and the reference fleet (`examples/fleet`) checks them end to end. A new SDK implements this page, and it's done when it can join the fleet.

Nothing standard carries trace context and causal parents across A2A yet ([A2A issue 2103](https://github.com/a2aproject/A2A/issues/2103)), so Wake carries its own. Switch to the standard when one lands.

The event fields themselves (`wake.parents`, `wake.origins`) are in [events.md](events.md).

## A link

A link says which events a call or a message comes from:

- **Trace context:** a W3C `traceparent` (Trace Context Level 1): `00-<32 hex trace id>-<16 hex span id>-<2 hex flags>`, lowercase hex, version not `ff`, neither id all zeros. It puts the receiving side's events in the sender's trace, under the span it names.
- **Parents:** one or more event ids, the `wake.event.id`s of the events the receiving side's next event descends from.

Trace context alone names no event, so it adds no parents. The proxy still uses it to put its record in the caller's trace, for callers that have OpenTelemetry but no Wake SDK; an SDK may ignore it. Parents without valid trace context still link: they stand, and the receiving side's events start a trace of their own.

## Where a link travels

| Carrier | Trace context | Parents | Used for |
|---|---|---|---|
| HTTP request headers | `traceparent` | `wake-parents`: ids joined with `,` | MCP over Streamable HTTP, A2A over HTTP. agentgateway passes them through, and the MCP route's config hands the proxy these and `mcp-session-id`, never credentials |
| HTTP response headers | `traceparent` | `wake-parents` | A2A replies: the proxy names its record of the reply |
| MCP `params._meta` | `traceparent` | `dev.wake/parents`: array of ids | MCP clients that can't set headers per call. agentgateway v1.6.0 drops `_meta` before the proxy sees it, so an SDK also copies it into the headers |
| A2A message `metadata` | `traceparent` | `dev.wake/parents`: array of ids | End to end between agents. The proxy reads it and never rewrites it |

## Reading a link

A link arrives from another process, so it's untrusted input. Read it this way, and never fail or slow the call over it:

- Header names are case-insensitive.
- An id is 1 to 128 characters from `A-Z a-z 0-9 . _ : -`. Drop anything else, trim spaces around ids, and drop duplicates.
- Keep at most 64 ids.
- A `traceparent` that doesn't parse is ignored; keep the parents.
- When a request carries a link both in headers and in the message metadata, the **headers win**. Through the proxy, the headers name the proxy's record of the message, which is itself a child of the sender's event named in the metadata. So the chain stays whole and has no shortcut around the proxy.

## Sending a link

Inside a process, the SDK keeps the current link per task of execution (a `ContextVar` in Python, `AsyncLocalStorage` in TypeScript). A recorded tool call makes its own event current while it runs, on top of whatever was current before. Links end in any order, since two integrations may record the same call, so an ended link is skipped rather than popped.

An outgoing call or message names the latest event recorded under the current link, or, when none has been, the link's own event. Outside any link it carries nothing: no headers, no metadata.

- **Inside a recorded tool call**, the current link is the tool call's, so a call the tool makes names the tool call. The proxy records an MCP call made this way as a child of the SDK's tool call event, so the two records of one call are joined.
- **While an agent handles a received message**, the latest event is its last step, so a reply names the step that produced it.

## Receiving a link

The receiving side makes the link current while it handles the call (in Python, `wake.links.received` or `wake.a2a.received`). Then:

- A run started under the link joins the link's trace.
- The run's **first** event has the link's parents among its own. Later events chain from the first, so they don't repeat them.
- That first event's input isn't the user's: it came from whatever sent it, which the link's ancestors say. So it doesn't get the origin `user`.
- The same holds for a run started inside a recorded tool call, in the same process or another: the run descends from the tool call.

## The proxy in the middle

For an A2A exchange through agentgateway, the proxy records two `agent_message` events and passes links on in both directions:

1. **The message.** Its parents come from the request's link (headers, else metadata), and its trace from the link's trace context. The request headers forwarded to the receiver are replaced with the proxy's own link: `traceparent` with the message event's span, and `wake-parents` with its id.
2. **The reply.** Its parents are the ids the reply message's metadata names: the receiver's last step, when the receiver has an SDK. Failing that, its parent is the message it answers. The reply event's id is chosen when the response headers arrive, and the response headers going back to the sender are set to its link.
3. **A task's later replies** (`tasks/get`, `GetTask`, and resubscriptions) that name no parents link to the message that started the task. The proxy remembers which message started each task, for the last 10,000 tasks it saw start.

For an MCP call, the proxy records a `tool_call` event whose parents come from the request's link: the SDK's event for the same call.

## Results coming back

What a call gets back can come from recorded events, such as an A2A reply. The SDK notes those ids on the current tool call (in Python, `wake.links.returned`; Wake's A2A HTTP client does it from the response's `wake-parents`, and `wake.a2a.replied` from reply metadata when there's no proxy).

Those ids become parents of **the step that reads the tool's result**, the next model call, and never of the tool call itself. The tool call is an ancestor of the message, the receiver's steps and the reply, so making it a child of the reply would make a cycle.

## The edges, end to end

From a support agent asking a research agent, through the proxy, as in the reference fleet:

```
support model call
  └─ support tool call (ask_research)            parent: the model call that asked for it
       └─ proxy agent_message, support → research   parent: the tool call (request link)
            └─ research's first step                parent: the message (proxy's link)
                 └─ ... research's steps ...
                      └─ proxy agent_message, research → support   parent: research's last step (reply metadata)
support's next model call                        parents: the tool call, and the reply (response link)
```

A parent always starts before its child, and no implementation adds a parent that descends from the event, so provenance is a DAG.

## What can't carry a link

Calls a framework makes outside the SDK's reach are recorded, but carry no link: tools Claude Code runs itself (Bash, WebFetch, external MCP servers it calls directly), and agents and tools with no SDK. Through the proxy they're still recorded and signed, with no SDK parent, and the completeness metric counts them as unlinked rather than guessing.
