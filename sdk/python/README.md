# wake-sdk

Records what every agent saw, said and did, and sends it to the Wake collector. It never signs anything and never blocks or breaks the agent: if the collector is down, events are dropped and the agent keeps working.

## LangGraph

```python
from wake import Recorder
from wake.langgraph import WakeCallbackHandler

handler = WakeCallbackHandler(Recorder(agent="email-agent"))
graph.invoke(inputs, config={"callbacks": [handler]})
```

The recorder sends to `WAKE_ENDPOINT`, or `http://localhost:4318` by default. Install with the `langgraph` extra.

The event format is in [docs/events.md](../../docs/events.md).

## Development

```bash
uv run pytest
```
