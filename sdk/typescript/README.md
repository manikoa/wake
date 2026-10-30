# wake-sdk (TypeScript)

Records what every agent saw, said and did, and sends it to the Wake collector. It never signs anything, and recording never blocks or breaks the agent: if the collector is down, events are dropped and the agent keeps working. Given the brake's rules, a `Quarantine`, it does refuse on purpose: a tool call or run a halt covers or the flow policy keeps, an OpenAI Agents model call that could run a hosted tool either holds back, a memory write a quarantine locks or the flow policy keeps, and every one of them while the rules are stale (see the stopping, flow policy and Quarantine sections below). It records the same events as the Python SDK in [`sdk/python`](../python), with the same attributes, so the two link to each other across agents.

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
- Each run is linked within itself, content matching included, so two copies of one agent running at once, at the top of one workflow or inside one tool call, never take each other's steps as parents. Runs are told apart by the task span the SDK opens for each, as it does by default.
- An agent run inside a tool call, as with `agent.asTool()`, is an instance of its own, `<agent>#<its first agent span's id>`, in `wake.agent.instance`, with `wake.agent.lineage` naming the agent that called it (see docs/events.md). What it said last feeds the response that reads the tool's result.

Inputs and outputs are recorded when the SDK's `traceIncludeSensitiveData` is on, as it is by default. Without it, events have empty payloads, and the SDK's loop links them instead: a tool call comes from its agent's latest response, and a response reads that response and the tool results since. Chat Completions models link as Responses models do. A function tool runs with its event current, and an agent run inside it, such as one used as a tool, descends from that call.

### Stopping an agent in its process

Give it the brake's rules, and guard the agents you run:

```ts
import { Quarantine } from "wake-sdk/quarantine";

const processor = install(recorder, { quarantine: new Quarantine("http://wake-proxy:9003", { agent: "support" }) });
const result = await run(processor.guard(triageAgent, researcherAgent), "Refund order 1234");
```

`guard()` gives each function tool, agent used as a tool and MCP server of the agents it's given, and of every agent they hand off to, a tool input guardrail. The SDK doesn't say which agent an agent tool runs, so pass the agents used as tools too. A call is refused before it runs when a halt covers the agent making it, the instance its run is or any that started it, or the recorder's agent: the model gets the refusal as the tool's result, and the call is recorded as a `denied` event naming the halt (`wake.brake.halt_id`). A refused agent tool starts no run. A guardrail the SDK runs before a call's approval (`toolExecution.preApprovalInputGuardrails`) refuses it the same way, and is recorded even though the SDK opens no span for the call. Bare agents in `handoffs` become the handoff the SDK would make of them.

- **Tools the agent runs itself.** A shell with a local shell, an `applyPatchTool` editor and a computer run in the agent's process, so `guard()` wraps each to refuse such a call as it starts, recorded the same way. The shell and editor answer with the refusal as the tool's output. A computer's output is a screenshot, which can't carry it, so a refused computer action throws `AgentHalted`, and the SDK answers the model with an empty screenshot, as it does for any computer action that fails. A tool of a kind Wake doesn't know makes `guard()` throw a `TypeError`, rather than run unstopped.
- **Handoffs.** A handoff is offered only while neither the agent offering it nor the one it hands off to is halted. One the model picked before a halt landed is refused as it starts: it throws `AgentHalted`, which ends the run, and is recorded as a `denied` `agent_message` in the name of the agent handing off, with `wake.brake.receiver` naming the one it would have started.
- **Which copy is calling.** The run is placed by the SDK's span current at the call, as its events are. While Wake can't place it, a halt on any copy of the agent it has seen start refuses the call.
- **Hosted tools.** Web search, file search, code interpreter, image generation, tool search, programmatic tool calling and a shell in a hosted container run at OpenAI, inside the model call, so they can't be refused one call at a time. `guard()` makes the instructions of an agent that offers any of them a function, which the SDK calls once before every model call. It checks each such tool as a call by the agent to a tool of its name, halts first, then the flow policy. If any is held back, the model call isn't made: each is recorded as a `denied` event naming it, and the run ends with `AgentHalted`, saying `Wake halted billing (halt-7), so its model call, which could run web_search at OpenAI, wasn't made.` Otherwise the model gets the instructions the agent had, a string or what its function returns.
- **Hosted MCP.** `guard()` has each hosted MCP tool ask before every call (`requireApproval: 'always'`), and answers each request itself: halts, then the flow policy, for a call by the agent to the MCP tool's own name. A refused call is rejected with the refusal as its reason, and recorded as `denied`, with `wake.mcp.server`. An allowed call gets the answer of the tool's own `onApproval`, when its own setting would have asked about the call, and is approved otherwise. Wake can't tell a read-only tool, so a `readOnly` filter leaves the answer to `onApproval`.
- **Hosted MCP calls the app approves.** A hosted MCP tool with no `onApproval` whose setting asks for some calls is approved by a person, through run interruptions. Wake can't answer for one, or interrupt, so `guard()` leaves such a tool as it is, and says so once for each. A call the app approves, once or for good (`alwaysApprove`), runs at OpenAI inside the model call whose input carries the approval, as does one to a server deferred tool search found, which `guard()` never sees. The SDK has no hook before a model call that can change its input but the run's `callModelInputFilter`, so give it `processor.checkApprovals()`, in the run's options or the `Runner`'s config: `new Runner({ callModelInputFilter: processor.checkApprovals() })`, or `processor.checkApprovals(ownFilter)` to check what the app's own filter returns. It checks each approval in the input as `guard()` answers a request: halts, then the flow policy, for a call by the agent to the MCP tool's own name. One held back has its approval turned into a rejection in the input sent, with the refusal as its reason, and is recorded as `denied`, with `wake.mcp.server`. One let through enters the agent then. Wake finds the request an approval answers in the response that asked, which the trace shows with sensitive data, or in the input. One it can't find, as with the conversation kept at OpenAI (`previousResponseId` or `conversationId`) and sensitive data off, it can't check, so it turns it down, under `halt-check-failed`: `Wake couldn't find the request this approves, so this tool call wasn't run.` Each approval is decided once, so a later model call that carries it again gets the same answer. Without the filter, these calls aren't checked here.
- **What it can't reach.** The SDK doesn't check an agent used as a tool before it runs it, so a halt on that agent alone refuses its calls from inside its run, rather than the call that starts it.
- **It fails closed and trusts the agent's code,** as for Claude below: stale rules, and a check that fails, refuse every call.

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

Wake reads the messages the SDK streams back from Claude Code and passes them on unchanged. Each response is a model call, and each tool use and its result a tool call. A subagent started with the Agent tool is recorded as its own agent, named `support/<subagent type>`. Several of one type can run at once, so each is also an instance, `support/<subagent type>#<the Agent tool use that started it>`, in `wake.agent.instance`, with `wake.agent.lineage` naming what started it (see docs/events.md). One `agent_message` hands it the task once it has started, and another carries its answer back: when the tool returns, or, for a subagent the tool leaves running in the background, as it does by default, when Claude Code says it has finished. Links come from the SDK's ids:
- A tool call's parent is the model call that asked for it.
- A model call's parents are the call before it and the tool results and messages new in its context.
- A later turn of the same session continues from the last.

`options()` adds hooks that open each tool call before it runs. `tools()` wraps in-process tools so they run with their event current, so an MCP or A2A call they make through the proxy, or a memory write, links back to the tool call. Tools Claude Code runs itself, such as Bash, WebFetch or an external MCP server, are recorded from the stream but can't carry the link.

### Stopping one subagent in its process

Give it the brake's rules, and its hooks also refuse the tool calls of a halted agent, or of one halted subagent instance, before they run:

```ts
import { Quarantine } from "wake-sdk/quarantine";

const claude = new WakeClaudeAgent(recorder, { quarantine: new Quarantine("http://wake-proxy:9003", { agent: "support" }) });
```

Wake's brake halts a subagent by the identity it calls with. One that calls through the proxy with credentials of its own is stopped there. One that runs in its parent's process with its parent's credentials can't be told from its parent outside that process, so this stops it inside: the rules carry the halts in force, fetched from the proxy every couple of seconds, and a tool call whose agent, subagent type, instance or any instance that started it is halted is refused, and recorded as a `denied` event naming the halt (`wake.brake.halt_id`). An Agent tool call is refused the same way: no subagent starts, so no `agent_message` hands one a task. So is one that would start a subagent of a type a halt covers, `support/<subagent type>`, whoever makes it. That reaches Bash, file edits and anything else Claude Code runs itself, which never pass the proxy.

- **Which copy is calling.** Claude Code names a subagent in its hooks by an `agent_id`, not by the Agent tool use Wake names the instance by. Wake learns which is which from the subagent's calls it has seen both ways, or from the Agent tool's result, which names the `agent_id`. Before it has, the caller could be any instance the session started, or one started by any of them that Wake hasn't read yet, since the hooks can run ahead of the code reading the stream. So a call is refused if a halt covers any of them.
- **Background subagents.** A subagent the Agent tool leaves running in the background stays its instance until Claude Code says it has finished, in later turns too, so a halt on it holds after the tool has returned.
- **It fails closed.** Rules older than the quarantine's ttl (30 seconds by default) refuse every tool call, as they refuse memory writes, under `stale-rules`. A check that fails refuses the call too, under `halt-check-failed`. A proxy that doesn't serve halts, being older than this, stops nothing here: the proxy deny still holds.
- **It trusts the agent's code.** It runs in the agent's own process, so it holds only while that code does: a compromised agent can leave it out. The brake says so when it confirms a subagent stopped this way.

The OpenAI Agents SDK and LangChain integrations stop their agents the same way, through the same rules (`stop.ts`); see their sections.

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

A handoff is recorded as an `agent_message` event in the receiving agent's run, and it feeds that agent's next event. It descends from the sending run's latest event, however the app words it, so what the receiver does keeps the origins of what the sender read. A reply descends from the replying run's latest event the same way. A run started inside another agent's run is an instance of its own, `<name>#<its span id>`, in `wake.agent.instance`, with `wake.agent.lineage` naming the run that started it, so two runs of one agent at once can be told apart. When prompts are built from strings, so no message ids link one call to the next, an event's parents are the earlier events in the same trace whose output appears in its input.

### Stopping an agent in its process (LangChain and LangGraph)

Give the handler the brake's rules, and use a `WakeToolNode` where a graph would use a `ToolNode`:

```ts
import { Quarantine } from "wake-sdk/quarantine";
import { WakeCallbackHandler, WakeToolNode } from "wake-sdk/langgraph";

const handler = new WakeCallbackHandler(recorder, { quarantine: new Quarantine("http://wake-proxy:9003", { agent: "clavr" }) });
const tools = new WakeToolNode(handler, [refund]);
```

A halt covers a run by its name, the instance it is and the runs that started it, or the recorder's agent.
- **Runs.** `handler.agent()` refuses to start a run a halt covers: it throws `AgentHalted` before calling its function, and records the attempt as a `denied` event in the name of the run that tried, with `wake.brake.receiver` naming the run refused. So a halted run starts nothing.
- **Tool calls.** The `WakeToolNode` refuses a call a halt covers before it runs: the model gets the refusal as the tool's result, with status `error`, and the call is recorded as a `denied` event naming the halt, under the model call that asked for it.
- **Any other tool call** a halt covers, as from `createReactAgent`'s own `ToolNode`, a node of the app's own or a direct `invoke`, is refused as it starts. The handler throws `AgentHalted` from its tool start callback, and sets `raiseError`, so LangChain passes it on: the tool never runs. A `ToolNode` answers the model with it as an error message, `Error: <the refusal>` with a request to fix its mistakes, as it does any error by default; one given `handleToolErrors: false` throws it on, ending the graph's run. A direct `invoke` rejects with it. Either way the call is recorded as `denied` once, as it is when a `WakeToolNode` refuses it first.
- **It fails closed and trusts the agent's code,** as for Claude above: stale rules, and a check that fails, refuse every call and run.

## Flow policies in the agent's process

When wake-proxy enforces a flow policy ([docs/proxy.md](../../docs/proxy.md), "Flow policies"), it serves it with the rules a `Quarantine` fetches, and the integrations above apply it where they check halts, to the calls the proxy never sees: a tool the agent runs itself, a handoff, a subagent or a run started in the same process. The memory adapter applies its rules for memory stores (see [Memory](#flow-rules-for-memory-stores)). Once no halt covers a call, it's refused the same way when a rule keeps it from what entered the agent making it within the policy's window, decided as wake-proxy decides (`sdk/testdata/flow_policy.json` pins the decisions for wake-proxy and both SDKs). Without a policy served, nothing more is checked.

- **What's checked.** A tool call meets the rules naming no tools, receivers, path prefixes or stores, then those naming its tool. A handoff, a subagent the Agent tool starts or a run `handler.agent()` starts meets the rules for the agent it reaches. An OpenAI Agents model call meets the rules for each hosted tool it offers, as a call to that tool, and a hosted MCP call meets them as the model asks to make it, or, with `checkApprovals()`, as the app's approval of it goes to the model. A rule's `callers` hold an agent's subagents and the runs it starts too. Rules with `path_prefixes` only meet requests at the proxy, and rules with `stores` only the memory adapter's writes, deletes and rollbacks.
- **What enters an agent,** kept by its name for the whole process (`entered`, in `wake-sdk/flow`). Never the user's own prompt.
  - A tool's result, as `tool:<name>`, once the tool has run. An MCP tool goes by its MCP name, so Claude's `mcp__crm__lookup_order` enters as `tool:lookup_order`.
  - An OpenAI Agents hosted tool's call, as `tool:<name>`, by the name the agent offers it under, such as `web_search`, as the response holding it comes in, before anything that follows from that response is checked. A hosted MCP call goes by the MCP tool's own name. Without sensitive data the trace leaves the response out, so every hosted tool the model call offered enters, but hosted MCP, which enters as Wake lets each call through.
  - A handoff, a subagent's start or its answer, and a run's reply or result, as `agent:<sender>`, bringing what had entered the sender. A subagent, an agent run inside a tool call, or a named run started inside another, starts with what had entered the agent starting it. A Claude subagent left running in the background answers when it stops, by the `SubagentStop` hook `options()` adds, before Claude Code hands its answer on, or as the stream says it has finished if that comes first; it's noted once.
  - A memory read: the origins its label carries, but `user`, when the label vouches for the content, and `memory:unlabeled` when no label does.

  Past 256 origins within the window, or 10,000 agents, an agent counts as having every origin, and any rule that applies to its calls refuses them.
- **What a refusal says.** It's recorded as a `denied` event under `wake.brake.halt_id` `policy:<rule id>`, whose payload has the origins the rule kept in `origins`, and why in `every_origin` when that was every origin. The agent is told, naming the caller as a halt's refusal does: `Wake's flow policy (policy:no-pay-after-web) holds billing back after tool:fetch_page, so this tool call wasn't run.`, or, for every origin, `Wake's flow policy (policy:no-pay-after-web) holds billing back, because more origins entered it than Wake keeps, so this tool call wasn't run.` A model call refused for a hosted tool ends `so its model call, which could run web_search at OpenAI, wasn't made.` A policy wake-proxy can't use, or one that isn't valid, refuses every call: `Wake's flow policy can't be read, so this tool call wasn't run.`, under `policy:unreadable-policy`. Stale rules refuse every call first, under `stale-rules`.
- **What it can't see.** The OpenAI Agents SDK integration notes what enters an agent from the SDK's trace, as it records, so with tracing off nothing is noted. That SDK doesn't say which agent an agent tool runs, so a rule for that agent as a receiver isn't checked as it starts, only its calls are, and with `mcpConfig.includeServerInToolNames` an MCP tool enters under the name the SDK gives it. Without sensitive data, Wake knows the hosted tools a model call offered by what `guard()` saw; for an agent it doesn't cover, by the SDK's own hosted tools' names the agent's span lists, so a hosted tool given a name of its own isn't noted. A hosted MCP call the app approves enters as `checkApprovals()` lets it through, and without that filter only as the trace shows it. A Claude server tool's result is noted as the stream shows it, which can be after the agent's next call.
- **Waiting for the trace.** The OpenAI Agents SDK hands each span to its trace processors one after another, and Wake comes after OpenAI's own exporter, which can hold up a response's end while it sends a batch. So each check Wake makes for an agent first waits until it has seen the end of every response of that agent's run whose start it saw, at most 5 seconds (`RESPONSE_WAIT_MILLIS`). Past that it refuses, as a check that failed, under `halt-check-failed`, or `policy:unreadable-policy` for a memory change.
- **It's a second layer.** It runs in the agent's own process, so it stops a well-behaved agent fooled by what it read, but a compromised agent can switch it off. wake-proxy goes on enforcing the policy on every call through it, and a network that lets agents out only through the proxy stops one that tries to go around it.

## The recorder

The recorder sends to `WAKE_ENDPOINT`, or `http://localhost:4318` by default. Over TLS with a private CA, set `WAKE_CA_FILE` to the CA's certificate. Wake reads only its own `WAKE_*` settings. The app's `OTEL_*` variables configure the app's telemetry and never Wake's, and Wake registers nothing with OpenTelemetry's global API. The app's proxy settings, such as `HTTPS_PROXY` with `NODE_USE_ENV_PROXY`, apply to its own traffic, and Wake's goes straight to the collector. Wake records every event even under an app span its sampler left unsampled. `flush()` waits at most its timeout, and resolves false if events were dropped since the last flush. `health()` counts events sent and dropped, and `failed`, those the SDK couldn't record. What's still queued is sent when the process has nothing left to do; call `shutdown()` before the process exits otherwise, such as on SIGTERM.

## Memory

The memory adapter records each write to agent memory, and each read from it. It labels each entry with where its content came from before it lands, so a later read, by any agent in any session and from either SDK, links back to the write. Entries with no label, such as ones planted or written outside Wake, or changed since they were written, aren't linked, so whatever follows from them is flagged.

For LangGraph's long-term memory store, wrap the store:

```ts
import { WakeStore } from "wake-sdk/langgraph";
import { Memory } from "wake-sdk/memory";

const store = new WakeStore(new PostgresStore(...), new Memory(handler));
const graph = builder.compile({ store });
```

The label is kept in each value under `__wake__` and taken out before values reach the app. A delete is recorded as a `memory_write` with `wake.memory.deleted` set. A batch is checked whole before any of it is recorded, so if one put or delete is refused, none is recorded or run.

For memory files, such as an agent's notes file, use `await memory.writeFile(path, text)` and `await memory.readFile(path)`. The label goes next to the file as `<file>.wake.json`. For any other store:
- Before writing, call `await memory.write(store, entry, content, current, label)`, where `current` and `label` are what the entry holds now and its label, if anything. Save the label it resolves to with the entry.
- Before deleting, call `await memory.delete(store, entry, current, label)`.
- After reading, call `memory.read(store, entry, content, label)`.

`new Memory(...)` takes any of the integrations: a `WakeCallbackHandler`, a `WakeTracingProcessor` or a `WakeClaudeAgent`.

Each write and read carries `wake.memory.sha256`, the digest of what was written or read, which the label holds too. A store value's digest is the same whichever SDK wrote it or reads it: JavaScript can't tell `500` from `500.0`, so both SDKs write a whole number as an integer.

### Rollback

`WakeStore` and memory files keep the versions an entry held, up to 16, in its label. Content changed since its label was written isn't kept. To restore a version:

```ts
await memory.rollbackFile(path);                        // the version before this one
await memory.rollbackFile(path, targetEventId);         // the version that write made
await store.rollback(["facts", "user-1"], "acme-bank"); // a store entry
```

A rollback is a write, so the quarantine, the flow policy and the gate check it as one. It also rejects with `MemoryLocked` when the quarantine wouldn't serve the version it restores. A version that names no write, or doesn't match its own digest, rejects with an `Error`, and nothing is restored. The rollback is recorded as a `memory_write` with `wake.memory.rollback_to` naming the write it restores. It resolves to the new label, which names the rollback and keeps the version it replaced, so a rollback can be rolled back too.

### Write gate

To vet what agents put in memory, give the adapter a gate. It's called before each write with the store, the entry, the content's text and the origins upstream of it, and before each delete with `null` for the content. It may return a promise:

```ts
const memory = new Memory(handler, {
  gate: (store, entry, content, origins) => {
    if (content === null) return !entry.startsWith("policy/"); // a delete
    return !origins.includes("tool:read_email") || !content.includes("account");
  },
});
```

A rollback is asked about too, with the content and origins of the version it restores. A write, delete or rollback the gate refuses rejects with `MemoryLocked` and is recorded as a `denied` event under `write-gate-denied`. The gate fails closed: a gate that throws or rejects refuses, with its error in the event's payload, and so does a gate the adapter can't ask because it couldn't work out the write's origins. Without a gate, recording never holds up a write.

### Quarantine

When the brake quarantines memory, give the adapter the rules wake-proxy serves:

```ts
import { Quarantine } from "wake-sdk/quarantine";

const quarantine = new Quarantine("http://wake-proxy:9003", { agent: "billing" });
await quarantine.ready;
const memory = new Memory(handler, { quarantine });
```

It fetches them as it starts, waiting at most a second, which `ready` waits for, then every couple of seconds in the background. The fetches go straight to wake-proxy, whatever the app's proxy settings say.
- **A locked agent:** its writes and deletes reject with `MemoryLocked` and don't happen.
- **A quarantined entry:** it reads as absent from `WakeStore`, drops out of searches, and makes `readFile` throw `MemoryQuarantined`. It can't be overwritten or deleted: it's kept, as evidence.
- **Every refusal** is recorded as a `denied` event.

One process can host several agents, as named runs (`handler.agent("email", ...)`), Claude subagents, or the agents of an OpenAI Agents workflow. The brake locks each by its own name, so the rules are fetched for each run's name too, from the run's first write, which waits at most a second for them. A run's writes are refused when its name, or the agent the `Quarantine` was given, is locked.

It fails closed. When a fetch fails the last rules stand for `ttlMillis` (30 seconds by default). If no fetch has succeeded, or the rules are older than that, every write throws `MemoryLocked` and no entry is served, under the id `stale-rules`, until a fetch succeeds. `quarantine.health()` says whether the rules are fresh, and going stale is logged as a warning. Each fetch names the process as well as the agent, and gives its ttl in whole seconds, rounded up. The proxy notes fetches by both, and holds a process's note back for its ttl, since until then the process may still act on the rules it last fetched. The brake confirms a quarantine, or a halt an integration stops in process, only once every process of the agent it still waits on has it. Once an integration that stops halted instances in process uses the `Quarantine` (the Claude Agent SDK integration, OpenAI Agents' `guard()`, or a LangChain handler given it), its fetches say so too, and the brake calls a halted instance stopped in process only when the agent's processes say they stop instances.

### Flow rules for memory stores

The flow policy wake-proxy serves with the `Quarantine`'s rules can name memory stores, to keep what entered an agent out of them:

```json
{"id": "no-web-into-facts", "stores": ["facts", "customers/*"], "forbid_origins": ["tool:fetch_page", "memory:unlabeled"]}
```

A store is named as the adapter names it: a `WakeStore` namespace joined with `/`, such as `customers/42`, `file` for memory files, and the `store` given to `write()` and `delete()`. Each is exact, or a prefix ending in `*`. A rule naming stores may name `callers` too, but not `tools`, `receivers` or `path_prefixes`.

- **What it holds.** Each write, delete and rollback through the adapter, `WakeStore`'s batches and memory files included, and nothing else: no tool call, no handoff, no read. A rule naming no stores never holds a change to memory. The rules naming the store decide in file order, on what entered the agent making the change within the policy's window, named as the integration's own checks name it, its lineage and the recorder's agent included.
- **When.** After the quarantine, and before the gate: what the quarantine refuses isn't checked, and the gate isn't asked about what the policy refuses. A batch is checked whole first, so a refused change leaves none of it run.
- **What a refusal does.** The change rejects with `MemoryLocked` under `policy:<rule id>`, nothing is written, and it's recorded as a `denied` event from the memory adapter, as the gate's are, whose payload adds `origins`, the origins the rule kept, or `["*"]`, and `every_origin`, why, when that was every origin.
- **It fails closed.** A policy wake-proxy can't use, one that isn't valid, or a check that fails, refuses every change under `policy:unreadable-policy`. Stale rules refuse every change first, under `stale-rules`. When the integration can't tell which agent is changing memory, as for Claude calls alike that could be different agents', it counts as one with every origin, so any rule naming the store refuses the change, whatever callers it names. With no policy served, or no `Quarantine` given, nothing changes.

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

## A sub-agent's own AWS session

When each sub-agent assumes its agent's AWS role in a session of its own, name the session for it, and Wake's brake can deny that session alone, leaving the role's other sessions, its parent's and its siblings', be (`aws_sub_agent_sessions` in the brake's inventory):

```ts
import { sessionName } from "wake-sdk/aws";

const name = sessionName(instance); // the sub-agent's identity, as the brake halts it
await sts.send(new AssumeRoleCommand({ RoleArn: role, RoleSessionName: name, SourceIdentity: name }));
```

The name is the sub-agent's identity when IAM takes it as it is (2 to 64 of `A-Z a-z 0-9 _ + = , . @ -`), and otherwise a readable form of it ending in a hash, the same name the brake gives it (checked against `sdk/testdata/aws_session_names.json`). Two identities can share a name, as when one reads like another's hashed name; halting either then denies the sessions of both, more than the halt named but never less. Prefer `SourceIdentity`: AWS never lets a session change it, and it carries over when the session assumes another role.

## When the collector is down

Events go out from a queue in the background, so the agent never waits on the collector.
- **The queue:** at most 2,048 events (`maxQueueSize`) and 64 MiB (`maxQueueBytes`).
- **Batches:** each goes in at most 8 MiB, well under the collector's request limit.
- **Large payloads:** a payload over 4 MiB, such as a model's context late in a long run, is cut to its first 2 MiB and marked truncated, with the whole payload's size and SHA-256 (see [docs/events.md](../../docs/events.md)).

Events that can't be sent are dropped and counted:

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
