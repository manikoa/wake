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

For agents built from LangChain models and tools without LangGraph, install the handler once so every model and tool call in the process is recorded, and name the agent behind each run:

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
