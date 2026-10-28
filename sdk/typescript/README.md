# wake-sdk (TypeScript)

Records what every agent saw, said and did, and sends it to the Wake collector. It never signs anything and never blocks or breaks the agent: if the collector is down, events are dropped and the agent keeps working. It records the same events as the Python SDK in [`sdk/python`](../python), with the same attributes, so the two link to each other across agents.

Node 22.15 or later. Each framework's support is its own entry point, and the frameworks are optional peer dependencies.

## LangGraph

```ts
import { Recorder } from "wake-sdk";
import { WakeCallbackHandler } from "wake-sdk/langgraph";

const handler = new WakeCallbackHandler(new Recorder({ agent: "email-agent" }));
await graph.invoke(inputs, { callbacks: [handler] });
```

A tool made with LangChain's `tool()` runs with its event current, so an MCP or A2A call it makes, a memory write, or a graph it runs, links back to the tool call. So does a tool from LangChain's MCP adapters, with Wake's hook (see [MCP calls](#mcp-calls-through-the-proxy)), when an agent or a `ToolNode` calls it for the model.

## OpenAI Agents SDK

```ts
import { install } from "wake-sdk/openai-agents";

install(new Recorder({ agent: "support" }));
const result = await run(triageAgent, "Refund order 1234");
```

Wake becomes one of the Agents SDK's trace processors. Each agent's run is named for the agent. Responses, tool calls and handoffs are recorded, linked by the ids the SDK gives:
- A tool call's parent is the response that requested it.
- A response's parents are the earlier responses and tool results in its input.
- A handoff is an `agent_message` that feeds the receiving agent's next step.

Inputs and outputs are recorded when the SDK's `traceIncludeSensitiveData` is on, as it is by default. Without it, events have empty payloads, and the SDK's loop links them instead: a tool call comes from its agent's latest response, and a response reads that response and the tool results since. Chat Completions models link as Responses models do. A function tool runs with its event current, and an agent run inside it, such as one used as a tool, descends from that call.

## Claude Agent SDK

```ts
import { createSdkMcpServer, query } from "@anthropic-ai/claude-agent-sdk";
import { WakeClaudeAgent } from "wake-sdk/claude-agent-sdk";

const claude = new WakeClaudeAgent(new Recorder({ agent: "support" }));
const crm = createSdkMcpServer({ name: "crm", tools: claude.tools([lookupOrder]) });
const options = claude.options({ mcpServers: { crm } });

for await (const message of claude.record(query({ prompt, options }), { prompt })) {
  ...
}
```

Wake reads the messages the SDK streams back from Claude Code and passes them on unchanged. Each response is a model call, and each tool use and its result a tool call. A subagent started with the Agent tool is recorded as its own agent, named `support/<subagent type>`. One `agent_message` hands it the task and another carries its answer back. Links come from the SDK's ids:
- A tool call's parent is the model call that asked for it.
- A model call's parents are the call before it and the tool results and messages new in its context.
- A later turn of the same session continues from the last.

`options()` adds hooks that open each tool call before it runs. `tools()` wraps in-process tools so they run with their event current, so an MCP or A2A call they make through the proxy, or a memory write, links back to the tool call. Tools Claude Code runs itself, such as Bash, WebFetch or an external MCP server, are recorded from the stream but can't carry the link.

## LangChain agents written as plain TypeScript

For agents built from LangChain models and tools without LangGraph, install the handler so every model and tool call made from there on is recorded, and name the agent behind each run. `handler.uninstall()` turns it off again.

```ts
const handler = new WakeCallbackHandler(new Recorder({ agent: "my-app" })).install();

await handler.agent("supervisor", { input: question, origin: "user" }, async () => {
  const plan = await llm.invoke(...);
  await handler.agent("email", { input: step }, async (run) => { // a handoff to another agent
    run.reply(await emailAgent.run(step));
  });
});
```

A handoff is recorded as an `agent_message` event in the receiving agent's run, and it feeds that agent's next event. When prompts are built from strings, so no message ids link one call to the next, an event's parents are the earlier events in the same trace whose output appears in its input.

## The recorder

The recorder sends to `WAKE_ENDPOINT`, or `http://localhost:4318` by default. Over TLS with a private CA, set `WAKE_CA_FILE` to the CA's certificate. Wake reads only its own `WAKE_*` settings: the app's `OTEL_*` variables configure the app's telemetry and never Wake's, Wake registers nothing with OpenTelemetry's global API, and it records every event even under an app span its sampler left unsampled. `flush()` waits at most its timeout, and resolves false if events were dropped since the last flush. `health()` counts events sent and dropped, and `failed`, those the SDK couldn't record. What's still queued is sent when the process has nothing left to do; call `shutdown()` before the process exits otherwise, such as on SIGTERM.

## Memory

The memory adapter records each write to agent memory, and each read from it. It labels each entry with where its content came from before it lands, so a later read, by any agent in any session and from either SDK, links back to the write. Entries with no label, such as ones planted or written outside Wake, or changed since they were written, aren't linked, so whatever follows from them is flagged.

For LangGraph's long-term memory store, wrap the store:

```ts
import { WakeStore } from "wake-sdk/langgraph";
import { Memory } from "wake-sdk/memory";

const store = new WakeStore(new PostgresStore(...), new Memory(handler));
const graph = builder.compile({ store });
```

The label is kept in each value under `__wake__` and taken out before values reach the app. A delete is recorded as a `memory_write` with `wake.memory.deleted` set.

For memory files, such as an agent's notes file, use `await memory.writeFile(path, text)` and `await memory.readFile(path)`. The label goes next to the file as `<file>.wake.json`. For any other store, call `memory.write(store, entry, content)` before writing, save the label it returns with the entry, and call `memory.read(store, entry, content, label)` after reading. `new Memory(...)` takes any of the integrations: a `WakeCallbackHandler`, a `WakeTracingProcessor` or a `WakeClaudeAgent`.

### Quarantine

When the brake quarantines memory, give the adapter the rules wake-proxy serves:

```ts
import { Quarantine } from "wake-sdk/quarantine";

const quarantine = new Quarantine("http://wake-proxy:9003", { agent: "billing" });
await quarantine.ready;
const memory = new Memory(handler, { quarantine });
```

It fetches them as it starts, waiting at most a second, which `ready` waits for, then every couple of seconds in the background. A locked agent's writes and deletes throw `MemoryLocked` and don't happen. A quarantined entry reads as absent from `WakeStore`, drops out of searches, makes `readFile` throw `MemoryQuarantined`, and can't be deleted: it's kept, as evidence. Each refusal is recorded as a `denied` event.

It fails closed. When a fetch fails the last rules stand for `ttlMillis` (30 seconds by default). If no fetch has succeeded, or the rules are older than that, every write throws `MemoryLocked` and no entry is served, under the id `stale-rules`, until a fetch succeeds. `quarantine.health()` says whether the rules are fresh, and going stale is logged as a warning. Each fetch names the process as well as the agent.

## MCP calls through the proxy

When an agent's MCP calls go through agentgateway, Wake's proxy records each one too. Link each call to the SDK's event for it, so the proxy's record joins the agent's trace and provenance. The link travels as two HTTP headers, `traceparent` and `wake-parents`.

With LangChain's MCP adapters, add the hook:

```ts
import * as mcp from "wake-sdk/mcp";

const client = new MultiServerMCPClient({ mcpServers, beforeToolCall: mcp.beforeToolCall });
```

The adapters open a connection with the headers for each call.

With the MCP client directly, give its transport Wake's fetch, and pass the link with each call from inside a recorded tool:

```ts
import { links } from "wake-sdk";

const transport = new StreamableHTTPClientTransport(new URL(url), { fetch: mcp.fetch() });
await client.callTool({ name, arguments: args, _meta: links.meta() });
```

Wake's fetch copies the link from `_meta` into the headers, since agentgateway drops `_meta` before the proxy sees the call, and adds the current event's link to a request without one. For any other outgoing call, `links.headers()` returns the headers for the current event, or `{}` outside a recorded tool call.

## Messages between agents (A2A)

When one agent sends another a message, link it so the receiver's steps join the sender's trace and descend from the step that sent it. With the `@a2a-js/sdk` client, send from inside a recorded tool call with Wake's fetch, and link the message:

```ts
import * as a2a from "wake-sdk/a2a";

const factory = new ClientFactory({ transports: [new JsonRpcTransportFactory({ fetchImpl: a2a.fetch() })] });
const client = await factory.createFromAgentCard(card);
const reply = await client.sendMessage({ message: a2a.link(message) });
```

On the receiving side, handle the message inside `received`, and link the reply there:

```ts
class Researcher implements AgentExecutor {
  async execute(context: RequestContext, bus: ExecutionEventBus) {
    const reply = await a2a.received(context, async () => {
      const answer = await agent.run(textOf(context.userMessage));
      return a2a.link(agentMessage(answer, context.contextId));
    });
    bus.publish(AgentEvent.message(reply));
    bus.finished();
  }
}
```

The reply names the receiver's last step, and the proxy names its record of the reply in the response's `wake-parents` header, which Wake's fetch reads. So the sender's next model call, the one that reads the tool's result, descends from the reply and from whatever the receiver read to produce it. Without the proxy, call `a2a.replied(reply)` on each reply the client returns.

A run started inside `received` is linked whatever records it: LangChain or LangGraph, the OpenAI Agents SDK or the Claude Agent SDK. Its first step's parent is the proxy's record of the message when the message went through agentgateway, or else the sender's tool call. For other servers, pass the request's headers and the message's metadata to `links.received({ headers, metadata }, fn)`. The same holds for a run started inside a recorded tool call: it descends from the tool call.

The current link is kept per task of execution in an `AsyncLocalStorage`, as [docs/linking.md](../../docs/linking.md) describes. A promise `fn` returns keeps the link current until it settles.

## When the collector is down

Events go out from a bounded queue in the background, so the agent never waits on the collector. Events that can't be sent are dropped and counted:

```ts
const recorder = new Recorder({ agent: "email-agent", onDrop: (count, reason) => page(count, reason) });
recorder.health(); // { sent, dropped, lastError, failed, ok }
```

The recorder also warns, through `log.warn`, when drops start and when the collector is reachable again. `onDrop` runs after the export that dropped the events, never inside a call the agent made, and a failing `onDrop` is logged and never reaches the agent. Reassign `log.warn` to send Wake's warnings somewhere other than `console.warn`.

The event format is in [docs/events.md](../../docs/events.md).

## Development

```bash
pnpm install
pnpm typecheck
pnpm test
```

The tests run the TypeScript sources directly with Node's type stripping, against the frameworks themselves: a scripted model for LangChain and the OpenAI Agents SDK, and for the Claude Agent SDK a fake Claude Code that speaks its stream-json protocol. `pnpm build` writes the package to `dist/`.
