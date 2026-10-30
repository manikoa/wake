# wake-sdk

Records what every agent saw, said and did, and sends it to the Wake collector. It never signs anything, and recording never blocks or breaks the agent: if the collector is down, events are dropped and the agent keeps working. Given the brake's rules, a `Quarantine`, it does refuse on purpose: a tool call or run a halt covers or the flow policy holds back, a memory write a quarantine locks, and every one of them while the rules are stale (see the stopping, flow policy and Quarantine sections below).

## LangGraph

```python
from wake import Recorder
from wake.langchain import WakeCallbackHandler

handler = WakeCallbackHandler(Recorder(agent="email-agent"))
graph.invoke(inputs, config={"callbacks": [handler]})
```

## OpenAI Agents SDK

```python
from wake import Recorder
from wake.openai_agents import install

install(Recorder(agent="support"))
result = await Runner.run(triage_agent, "Refund order 1234")
```

Wake becomes one of the Agents SDK's trace processors. Each agent's run is named for the agent. Responses, tool calls and handoffs are recorded, linked by the ids the SDK gives:
- A tool call's parent is the response that requested it.
- A response's parents are the earlier responses and tool results in its input.
- A handoff is an `agent_message` that feeds the receiving agent's next step.
- Each run is linked within itself, content matching included, so two copies of one agent running at once, at the top of one workflow or inside one tool call, never take each other's steps as parents. Runs are told apart by the task span the SDK opens for each, as it does by default.
- An agent run inside a tool call, as with `agent.as_tool()`, is an instance of its own, `<agent>#<its first agent span's id>`, in `wake.agent.instance`, with `wake.agent.lineage` naming the agent that called it (see docs/events.md). What it said last feeds the response that reads the tool's result.

Inputs and outputs are recorded when the SDK's `trace_include_sensitive_data` is on, as it is by default. Without it, events have empty payloads, and the SDK's loop links them instead: a tool call comes from its agent's latest response, and a response reads that response and the tool results since. Chat Completions models, LiteLLM's included, link as Responses models do, and an agent run inside a tool call, such as one used as a tool, descends from that call. Install with the `openai-agents` extra.

### Stopping an agent in its process

Give it the brake's rules, and guard the agent you run:

```python
from wake.quarantine import Quarantine

processor = install(recorder, quarantine=Quarantine("http://wake-proxy:9003", agent="support"))
result = await Runner.run(processor.guard(triage_agent), "Refund order 1234")
```

`guard()` gives each function tool, agent used as a tool and MCP server of the agent, and of every agent it reaches as a tool or by a handoff, a tool input guardrail. A call is refused before it runs when a halt covers the agent making it, the instance its run is or any that started it, or the recorder's agent: the model gets the refusal as the tool's result, and the call is recorded as a `denied` event naming the halt (`wake.brake.halt_id`). A refused agent tool starts no run. Bare agents in `handoffs` become the handoff the SDK would make of them.

- **Tools the agent runs itself.** A local shell, a shell with a local environment, a custom tool, an `apply_patch` editor and a computer run in the agent's process, so `guard()` wraps each to refuse such a call as it starts, recorded the same way. The shell, custom tool and editor answer with the refusal as the tool's output. A computer's output is a screenshot, which can't carry it, so a refused computer action raises `AgentHalted`, and the SDK ends the run with it. A tool of a kind Wake doesn't know raises `TypeError` from `guard()`, rather than run unstopped.
- **Handoffs.** A handoff is offered only while neither the agent offering it nor the one it hands off to is halted. One the model picked before a halt landed is refused as it starts: it raises `AgentHalted`, which ends the run, and is recorded as a `denied` `agent_message` in the name of the agent handing off, with `wake.brake.receiver` naming the one it would have started.
- **Which copy is calling.** The run is placed by the SDK's span current at the call, as its events are. While Wake can't place it, a halt on any copy of the agent it has seen start refuses the call.
- **What it can't reach.** Hosted tools, such as web search, file search or a shell in a hosted container, run at OpenAI and can't be refused here. The SDK doesn't check an agent used as a tool before it runs it, so a halt on that agent alone refuses its calls from inside its run, rather than the call that starts it.
- **It fails closed and trusts the agent's code,** as for Claude below: stale rules, and a check that fails, refuse every call.

## Claude Agent SDK

```python
from wake import Recorder
from wake.claude_agent_sdk import WakeClaudeAgent

claude = WakeClaudeAgent(Recorder(agent="support"))
crm = create_sdk_mcp_server("crm", tools=claude.tools([lookup_order]))
options = claude.options(ClaudeAgentOptions(mcp_servers={"crm": crm}))

async for message in claude.record(query(prompt=prompt, options=options), prompt=prompt):
    ...
```

Wake reads the messages the SDK streams back from Claude Code and passes them on unchanged. Each response is a model call, and each tool use and its result a tool call. A subagent started with the Agent tool is recorded as its own agent, named `support/<subagent type>`. Several of one type can run at once, so each is also an instance, `support/<subagent type>#<the Agent tool use that started it>`, in `wake.agent.instance`, with `wake.agent.lineage` naming what started it (see docs/events.md). One `agent_message` hands it the task once it has started, and another carries its answer back: when the tool returns, or, for a subagent the tool leaves running in the background, as it does by default, when Claude Code says it has finished. Links come from the SDK's ids:
- A tool call's parent is the model call that asked for it.
- A model call's parents are the call before it and the tool results and messages new in its context.
- With `ClaudeSDKClient`, wrap each `receive_response()`, and a later turn continues from the last.

`options()` adds hooks that open each tool call before it runs. `tools()` wraps in-process tools so they run with their event current, so an MCP or A2A call they make through the proxy, or a memory write, links back to the tool call. Tools Claude Code runs itself, such as Bash, WebFetch or an external MCP server, are recorded from the stream but can't carry the link. Install with the `claude-agent-sdk` extra.

### Stopping one subagent in its process

Give it the brake's rules, and its hooks also refuse the tool calls of a halted agent, or of one halted subagent instance, before they run:

```python
from wake.quarantine import Quarantine

claude = WakeClaudeAgent(recorder, quarantine=Quarantine("http://wake-proxy:9003", agent="support"))
```

Wake's brake halts a subagent by the identity it calls with. One that calls through the proxy with credentials of its own is stopped there. One that runs in its parent's process with its parent's credentials can't be told from its parent outside that process, so this stops it inside: the rules carry the halts in force, fetched from the proxy every couple of seconds, and a tool call whose agent, subagent type, instance or any instance that started it is halted is refused, and recorded as a `denied` event naming the halt (`wake.brake.halt_id`). An Agent tool call is refused the same way: no subagent starts, so no `agent_message` hands one a task. So is one that would start a subagent of a type a halt covers, `support/<subagent type>`, whoever makes it. That reaches Bash, file edits and anything else Claude Code runs itself, which never pass the proxy.

- **Which copy is calling.** Claude Code names a subagent in its hooks by an `agent_id`, not by the Agent tool use Wake names the instance by. Wake learns which is which from the subagent's calls it has seen both ways, or from the Agent tool's result, which names the `agent_id`. Before it has, the caller could be any instance the session started, or one started by any of them that Wake hasn't read yet, since the hooks can run ahead of the code reading the stream. So a call is refused if a halt covers any of them.
- **Background subagents.** A subagent the Agent tool leaves running in the background stays its instance until Claude Code says it has finished, in later turns too, so a halt on it holds after the tool has returned.
- **It fails closed.** Rules older than the quarantine's ttl (30 seconds by default) refuse every tool call, as they refuse memory writes, under `stale-rules`. A check that fails refuses the call too, under `halt-check-failed`. A proxy that doesn't serve halts, being older than this, stops nothing here: the proxy deny still holds.
- **It trusts the agent's code.** It runs in the agent's own process, so it holds only while that code does: a compromised agent can leave it out. The brake says so when it confirms a subagent stopped this way.

The OpenAI Agents SDK and LangChain integrations stop their agents the same way, through the same rules (`wake.stop`); see their sections.

## LangChain agents written as plain Python

For agents built from LangChain models and tools without LangGraph, install the handler once so every model and tool call in the process is recorded, and name the agent behind each run. `handler.uninstall()` turns it off again.

```python
handler = WakeCallbackHandler(Recorder(agent="my-app")).install()

with handler.agent("supervisor", input=question, origin="user"):
    plan = llm.invoke(...)
    with handler.agent("email", input=step) as run:  # a handoff to another agent
        answer = await email_agent.run(step)
        run.reply(answer)
```

A handoff is recorded as an `agent_message` event in the receiving agent's run, and it feeds that agent's next event. It descends from the sending run's latest event, however the app words it, so what the receiver does keeps the origins of what the sender read. A reply descends from the replying run's latest event the same way. A run started inside another agent's run is an instance of its own, `<name>#<its span id>`, in `wake.agent.instance`, with `wake.agent.lineage` naming the run that started it, so two runs of one agent at once can be told apart. When prompts are built from strings, so no message ids link one call to the next, an event's parents are the earlier events in the same trace whose output appears in its input.

### Stopping an agent in its process (LangChain and LangGraph)

Give the handler the brake's rules, and a LangGraph `ToolNode` its two wrappers:

```python
from wake.quarantine import Quarantine

handler = WakeCallbackHandler(recorder, quarantine=Quarantine("http://wake-proxy:9003", agent="clavr"))
tools = ToolNode([refund], wrap_tool_call=handler.wrap_tool_call, awrap_tool_call=handler.awrap_tool_call)
```

A halt covers a run by its name, the instance it is and the runs that started it, or the recorder's agent.
- **Runs.** `handler.agent()` refuses to start a run a halt covers: it raises `AgentHalted` before the block runs, and records the attempt as a `denied` event in the name of the run that tried, with `wake.brake.receiver` naming the run refused. So a halted run starts nothing.
- **Tool calls.** The `ToolNode` refuses a call a halt covers before it runs: the model gets the refusal as the tool's result, with status `error`, and the call is recorded as a `denied` event naming the halt, under the model call that asked for it. A `create_agent` middleware can call the same two from its hooks of those names.
- **Any other tool call** a halt covers, as from `create_react_agent`'s own `ToolNode`, a node of the app's own or a direct `invoke`, is refused as it starts. The handler raises `AgentHalted` from its tool start callback, and sets `raise_error`, so LangChain passes it on, from sync and async runs alike: the tool never runs. A `ToolNode` with its default error handling raises it on, ending the graph's run; one given `handle_tool_errors=True` answers the model with it as an error message instead. A direct `invoke` raises it to its caller. Either way the call is recorded as `denied` once, as it is when a wrapped `ToolNode` refuses it first.
- **It fails closed and trusts the agent's code,** as for Claude above: stale rules, and a check that fails, refuse every call and run.

The recorder sends to `WAKE_ENDPOINT`, or `http://localhost:4318` by default. Over TLS with a private CA, set `WAKE_CA_FILE` to the CA's certificate. Wake reads only its own `WAKE_*` settings. The app's `OTEL_*` variables, such as its sampler, exporter headers or attribute limits, configure the app's telemetry and never Wake's. Its proxy settings, such as `HTTPS_PROXY`, and `REQUESTS_CA_BUNDLE` and `.netrc` apply to its own traffic, and Wake's goes straight to the collector. Wake records every event even under an unsampled parent. `flush()` waits at most its timeout, and returns False if events were dropped since the last flush. `health()` counts events sent and dropped, and `failed`, those the SDK couldn't record. Install with the `langchain` or `langgraph` extra.

## Flow policies in the agent's process

wake-proxy enforces a flow policy on the calls that pass it, and serves it with the brake's rules (see [docs/proxy.md](../../docs/proxy.md) and [docs/flow-everywhere.md](../../docs/flow-everywhere.md)). Each integration given those rules, a `Quarantine`, applies the same policy in the agent's own process, to the calls wake-proxy never sees: the agent's own tools, its handoffs and the runs it starts. Without a policy on wake-proxy, nothing more is checked.

- **What's checked.** Where halts are checked, once no halt refuses: a tool call, before it runs; a handoff, a Claude Agent tool call, an OpenAI agent used as a tool and `handler.agent()`, as reaching the agent they'd start. Rules apply as in wake-proxy: a tool call meets the rules naming no tools, receivers or paths, then those whose `tools` match it; reaching an agent meets those naming no tools or paths whose `receivers` are empty or name it; `callers` cover the agent, its subagents and runs it started. The first rule that denies is named. `sdk/testdata/flow_policy.json` holds decisions wake-proxy and both SDKs make alike.
- **What enters an agent,** and counts for the policy's window:
  - a tool's result, as `tool:<name>`, an MCP tool by its MCP name: `mcp__crm__lookup_order` enters as `tool:lookup_order`;
  - a message, handoff, subagent start or subagent answer from another agent, as `agent:<sender>`, with whatever had entered the sender, each as of when it entered the sender;
  - a memory read, the origins its label carries when the label vouches for what was read, else `memory:unlabeled`.

  The user's own prompt is never an origin.
- **What a refusal says.** It looks like a halt's in each integration, and is recorded as a `denied` event under `policy:<id>`, with the origins behind it in its payload:
  - `Wake's flow policy (policy:no-pay-after-web) holds billing back after tool:fetch_page, so this tool call wasn't run.`
  - Past 256 origins within the window, or 10,000 agents, an agent counts as having every origin: `Wake's flow policy (policy:no-pay-after-web) holds billing back, because more origins entered it than Wake keeps, so this tool call wasn't run.`, with `every_origin` in the payload.
  - A policy wake-proxy can't use, or that isn't valid, refuses everything under `policy:unreadable-policy`: `Wake's flow policy can't be read, so this tool call wasn't run.` Stale rules refuse everything already, under `stale-rules`.
- **The proxy and the network stay the backstop.** This runs in the agent's process, so it stops a well-behaved agent fooled by what it read, and a compromised one can leave it out. wake-proxy goes on enforcing the policy on every call through it, and the network should leave agents no way out but wake-proxy. Hosted tools, such as OpenAI's web search, run at the provider and aren't checked here, and messages to agents in other processes are wake-proxy's to check.

## Memory

The memory adapter records each write to agent memory, and each read from it. It labels each entry with where its content came from before it lands, so a later read, by any agent in any session, links back to the write. Entries with no label, such as ones planted or written outside Wake, or changed since they were written, aren't linked, so whatever follows from them is flagged.

For LangGraph's long-term memory store, which langmem uses too, wrap the store:

```python
from wake.langgraph import WakeStore
from wake.memory import Memory

store = WakeStore(PostgresStore(...), Memory(handler))
graph = builder.compile(store=store)
```

The label is kept in each value under `__wake__` and taken out before values reach the app. A delete is recorded as a `memory_write` with `wake.memory.deleted` set. A batch is checked whole before any of it is recorded, so if one put or delete is refused, none is recorded or run.

For memory files, such as an agent's notes file, use `Memory(handler).write_file(path, text)` and `read_file(path)`. The label goes next to the file as `<file>.wake.json`. Files are read and written as UTF-8, newlines as they are, whatever the locale. For any other store:
- Before writing, call `memory.write(store, entry, content, current, label)`, where `current` and `label` are what the entry holds now and its label, if anything. Save the label it returns with the entry.
- Before deleting, call `memory.delete(store, entry, current, label)`.
- After reading, call `memory.read(store, entry, content, label)`.

Each write and read carries `wake.memory.sha256`, the digest of what was written or read, which the label holds too. A store value's digest is the same whichever SDK wrote it or reads it: JavaScript can't tell `500` from `500.0`, so both SDKs write a whole number as an integer.

### Rollback

`WakeStore` and memory files keep the versions an entry held, up to 16, in its label. Content changed since its label was written isn't kept. To restore a version:

```python
memory.rollback_file(path)                         # the version before this one
memory.rollback_file(path, target_event_id)        # the version that write made
store.rollback(("facts", "user-1"), "acme-bank")   # a store entry; arollback for an async store
```

A rollback is a write, so the quarantine and the gate check it as one. It also raises `MemoryLocked` when the quarantine wouldn't serve the version it restores. A version that names no write, or doesn't match its own digest, raises `ValueError`, and nothing is restored. The rollback is recorded as a `memory_write` with `wake.memory.rollback_to` naming the write it restores. It returns the new label, which names the rollback and keeps the version it replaced, so a rollback can be rolled back too.

### Write gate

To vet what agents put in memory, give the adapter a gate. It's called before each write with the store, the entry, the content's text and the origins upstream of it, and before each delete with `None` for the content:

```python
def gate(store, entry, content, origins):
    if content is None:  # a delete
        return not entry.startswith("policy/")
    return "tool:read_email" not in origins or "account" not in content

memory = Memory(handler, gate=gate)
```

A rollback is asked about too, with the content and origins of the version it restores. A write, delete or rollback the gate refuses raises `MemoryLocked` and is recorded as a `denied` event under `write-gate-denied`. The gate fails closed: a gate that raises refuses, with its error in the event's payload, and so does a gate the adapter can't ask because it couldn't work out the write's origins. Without a gate, recording never holds up a write.

### Quarantine

When the brake quarantines memory, give the adapter the rules wake-proxy serves:

```python
from wake.quarantine import Quarantine

memory = Memory(handler, quarantine=Quarantine("http://wake-proxy:9003", agent="billing"))
```

It fetches them once as it starts, waiting at most a second, then every couple of seconds in the background. The fetches go straight to wake-proxy, whatever the app's proxy settings say.
- **A locked agent:** its writes and deletes raise `MemoryLocked` and don't happen.
- **A quarantined entry:** it reads as absent from `WakeStore`, drops out of searches, and makes `read_file` raise `MemoryQuarantined`. It can't be overwritten or deleted: it's kept, as evidence.
- **Every refusal** is recorded as a `denied` event.

One process can host several agents, as named runs (`handler.agent("email")`) or Claude subagents. The brake locks each by its own name, so the rules are fetched for each run's name too, from the run's first write, which waits at most a second for them. A run's writes are refused when its name, or the agent the `Quarantine` was given, is locked.

It fails closed. When a fetch fails the last rules stand for `ttl` seconds (30 by default). If no fetch has succeeded, or the rules are older than that, every write raises `MemoryLocked` and no entry is served, under the id `stale-rules`, until a fetch succeeds. `quarantine.health()` says whether the rules are fresh, and going stale is logged as a warning. A forked process starts fetching on its own, and each fetch names the process as well as the agent, and gives its `ttl` in whole seconds, rounded up. The proxy notes fetches by both, and holds a process's note back for its `ttl`, since until then the process may still act on the rules it last fetched. The brake confirms a quarantine, or a halt an integration stops in process, only once every process of the agent it still waits on has it. Once an integration that stops halted instances in process uses the `Quarantine` (the Claude Agent SDK integration, OpenAI Agents' `guard()`, or a LangChain handler given it), its fetches say so too, and the brake calls a halted instance stopped in process only when the agent's processes say they stop instances.

## MCP calls through the proxy

When an agent's MCP calls go through agentgateway, Wake's proxy records each one too. Link each call to the SDK's event for it, so the proxy's record joins the agent's trace and provenance. The link travels as two HTTP headers, `traceparent` and `wake-parents`. Install with the `mcp` extra.

With LangChain's MCP adapters, add the interceptor:

```python
from wake.mcp import link_interceptor

client = MultiServerMCPClient(connections, tool_interceptors=[link_interceptor])
```

The adapters apply it to tools from `client.get_tools()`, which open a connection per call. Tools loaded on a session you hold open don't get the headers.

With the MCP client directly, call it from inside a recorded tool, use Wake's HTTP client, and pass the link with each call:

```python
from wake import links
from wake.mcp import http_client

async with streamable_http_client(url, http_client=http_client()) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool(name, arguments, meta=links.meta())
```

For any other outgoing call, `wake.links.headers()` returns the headers for the current event, or `{}` outside a recorded tool call.

## Messages between agents (A2A)

When one agent sends another a message, link it so the receiver's steps join the sender's trace and descend from the step that sent it. With the a2a-sdk, send from inside a recorded tool call with Wake's HTTP client, and link the message:

```python
from wake import a2a as wake_a2a

client = await create_client(url, ClientConfig(httpx_client=wake_a2a.http_client()))
message = wake_a2a.link(new_text_message(question, role=Role.ROLE_USER))
async for reply in client.send_message(SendMessageRequest(message=message)):
    ...
```

On the receiving side, handle the message inside `received`, and link the reply there:

```python
class Researcher(AgentExecutor):
    async def execute(self, context, event_queue):
        with wake_a2a.received(context):
            answer = await agent.run(context.get_user_input())
            reply = wake_a2a.link(new_text_message(answer, context_id=context.context_id))
        await event_queue.enqueue_event(reply)
```

The reply names the receiver's last step, and the proxy names its record of the reply in the response's `wake-parents` header, which Wake's HTTP client reads. So the sender's next model call, the one that reads the tool's result, descends from the reply and from whatever the receiver read to produce it. Without the proxy, call `wake_a2a.replied(reply)` on each reply the client yields.

A run started inside the block is linked whatever records it: LangChain or LangGraph, the OpenAI Agents SDK or the Claude Agent SDK. Its first step's parent is the proxy's record of the message when the message went through agentgateway, or else the sender's tool call. For other servers, pass the request's headers and the message's metadata to `wake.links.received(headers=..., metadata=...)`. The same holds for a run started inside a recorded tool call: it descends from the tool call. Install with the `a2a` extra.

## A sub-agent's own AWS session

When each sub-agent assumes its agent's AWS role in a session of its own, name the session for it, and Wake's brake can deny that session alone, leaving the role's other sessions, its parent's and its siblings', be (`aws_sub_agent_sessions` in the brake's inventory):

```python
from wake.aws import session_name

name = session_name(instance)  # the sub-agent's identity, as the brake halts it
sts.assume_role(RoleArn=role, RoleSessionName=name, SourceIdentity=name)
```

The name is the sub-agent's identity when IAM takes it as it is (2 to 64 of `A-Z a-z 0-9 _ + = , . @ -`), and otherwise a readable form of it ending in a hash, the same name the brake gives it (checked against `sdk/testdata/aws_session_names.json`). Two identities can share a name, as when one reads like another's hashed name; halting either then denies the sessions of both, more than the halt named but never less. Prefer `SourceIdentity`: AWS never lets a session change it, and it carries over when the session assumes another role.

## When the collector is down

Events go out from a queue on a background thread, so the agent never waits on the collector.
- **The queue:** at most 2,048 events (`max_queue_size`) and 64 MiB (`max_queue_bytes`).
- **Batches:** each goes in at most 8 MiB, well under the collector's request limit.
- **Large payloads:** a payload over 4 MiB, such as a model's context late in a long run, is cut to its first 2 MiB and marked truncated, with the whole payload's size and SHA-256 (see [docs/events.md](../../docs/events.md)).

Events that can't be sent are dropped and counted:

```python
def page(count, reason):
    ...  # send to your alerting

recorder = Recorder(agent="email-agent", on_drop=page)
recorder.health()  # Health(sent=..., dropped=..., last_error=...)
```

The recorder also logs a warning on the `wake` logger when drops start and when the collector is reachable again. `on_drop` runs on the recorder's export thread, not the agent's, and a failing `on_drop` is logged and never reaches the agent.

The event format is in [docs/events.md](../../docs/events.md).

## Development

```bash
uv run pytest
```
