# wake-sdk

Records what every agent saw, said and did, and sends it to the Wake collector. It never signs anything and never blocks or breaks the agent: if the collector is down, events are dropped and the agent keeps working.

## LangGraph

```python
from wake import Recorder
from wake.langchain import WakeCallbackHandler

handler = WakeCallbackHandler(Recorder(agent="email-agent"))
graph.invoke(inputs, config={"callbacks": [handler]})
```

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

A handoff is recorded as an `agent_message` event in the receiving agent's run, and it feeds that agent's next event. When prompts are built from strings, so no message ids link one call to the next, an event's parents are the earlier events in the same trace whose output appears in its input.

The recorder sends to `WAKE_ENDPOINT`, or `http://localhost:4318` by default. Install with the `langchain` or `langgraph` extra.

## Memory

The memory adapter records each write to agent memory, and each read from it. It labels each entry with where its content came from before it lands, so a later read, by any agent in any session, links back to the write. Entries with no label, such as ones planted or written outside Wake, or changed since they were written, aren't linked, so whatever follows from them is flagged.

For LangGraph's long-term memory store, which langmem uses too, wrap the store:

```python
from wake.langgraph import WakeStore
from wake.memory import Memory

store = WakeStore(PostgresStore(...), Memory(handler))
graph = builder.compile(store=store)
```

The label is kept in each value under `__wake__` and taken out before values reach the app.

For memory files, such as an agent's notes file, use `Memory(handler).write_file(path, text)` and `read_file(path)`. The label goes next to the file as `<file>.wake.json`. For any other store, call `memory.write(store, entry, content)` before writing, save the label it returns with the entry, and call `memory.read(store, entry, content, label)` after reading.

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

## When the collector is down

Events go out from a bounded queue on a background thread, so the agent never waits on the collector. Events that can't be sent are dropped and counted:

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
