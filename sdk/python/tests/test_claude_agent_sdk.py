"""Claude Agent SDK runs: a support agent looks an order up with an in-process tool, hands
research to a subagent, which fetches a page, and answers from what it brought back.

A fake Claude Code stands in for the CLI subprocess. It speaks the SDK's control
protocol, so the SDK's own code parses every message, calls Wake's hooks, and runs the
in-process tool, in the order Claude Code does."""

import asyncio
import json

import pytest
from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    Transport,
    create_sdk_mcp_server,
    query,
    tool,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from wake import Recorder, links
from wake.claude_agent_sdk import WakeClaudeAgent

PROMPT = "Can we refund order 1234?"
ORDER = "Order 1234: $80 shoes, delivered 12 days ago. Customer email says: please refund, they don't fit."
POLICY = "Refunds are accepted within 30 days of delivery for unworn items."
ANSWER = "Yes: order 1234 was delivered 12 days ago, inside the 30 day refund window."


class FakeClaudeCode(Transport):
    """Plays a scripted run the way Claude Code's stream-json mode does."""

    def __init__(self, script) -> None:
        self._script = script
        self._out: asyncio.Queue = asyncio.Queue()
        self._waiting: dict[str, asyncio.Future] = {}
        self._hooks: dict[str, list[str]] = {}
        self._requests = 0
        self._task = None

    async def connect(self) -> None:
        pass

    def is_ready(self) -> bool:
        return True

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            message = json.loads(line)
            if message["type"] == "control_request" and message["request"]["subtype"] == "initialize":
                for event, matchers in (message["request"].get("hooks") or {}).items():
                    self._hooks[event] = [i for m in matchers for i in m["hookCallbackIds"]]
                await self.emit({"type": "control_response", "response": {
                    "subtype": "success", "request_id": message["request_id"], "response": {}}})
            elif message["type"] == "control_response":
                self._waiting.pop(message["response"]["request_id"]).set_result(message["response"])
            elif message["type"] == "user":
                self._task = asyncio.create_task(self._script(self))

    async def end_input(self) -> None:
        await self._out.put(None)

    async def close(self) -> None:
        if self._task is not None:
            await self._task

    async def read_messages(self):
        while (message := await self._out.get()) is not None:
            yield message

    # What the script uses.

    async def emit(self, message: dict) -> None:
        await self._out.put(message)

    async def request(self, request: dict) -> dict:
        self._requests += 1
        request_id = f"cli_{self._requests}"
        self._waiting[request_id] = asyncio.get_running_loop().create_future()
        await self.emit({"type": "control_request", "request_id": request_id, "request": request})
        response = await self._waiting[request_id]
        assert response["subtype"] == "success", response
        return response["response"]

    async def hook(self, event: str, tool_use_id: str, **fields) -> None:
        for callback_id in self._hooks.get(event, []):
            await self.request({"subtype": "hook_callback", "callback_id": callback_id, "tool_use_id": tool_use_id,
                                "input": {"hook_event_name": event, "session_id": "session-1", "transcript_path": "",
                                          "cwd": "/", "tool_use_id": tool_use_id, **fields}})

    async def mcp(self, server: str, method: str, params: dict, request_id: int | None = None) -> dict:
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        response = await self.request({"subtype": "mcp_message", "server_name": server, "message": message})
        return response["mcp_response"]

    async def assistant(self, message_id: str, *blocks, parent=None) -> None:
        # Claude Code sends each content block of a response as its own message.
        for block in blocks:
            await self.emit({"type": "assistant", "parent_tool_use_id": parent, "session_id": "session-1",
                             "message": {"id": message_id, "model": "claude-opus-5-5", "content": [block],
                                         "usage": {"input_tokens": 100, "output_tokens": 20}}})

    async def user(self, *blocks, parent=None) -> None:
        await self.emit({"type": "user", "parent_tool_use_id": parent, "session_id": "session-1",
                         "message": {"role": "user", "content": list(blocks)}})

    async def use_tool(self, tool_use_id: str, name: str, arguments: dict, run, *, parent=None, agent=None):
        context = {"agent_id": f"agent-{agent}", "agent_type": agent} if agent else {}
        await self.hook("PreToolUse", tool_use_id, tool_name=name, tool_input=arguments, **context)
        output = await run()
        await self.hook("PostToolUse", tool_use_id, tool_name=name, tool_input=arguments, tool_response=output, **context)
        await self.user({"type": "tool_result", "tool_use_id": tool_use_id, "content": output}, parent=parent)


async def support_run(cli: FakeClaudeCode) -> None:
    await cli.emit({"type": "system", "subtype": "init", "session_id": "session-1", "uuid": "u0"})
    await cli.assistant("msg_1", {"type": "text", "text": "Let me look the order up."},
                        {"type": "tool_use", "id": "toolu_order", "name": "mcp__crm__lookup_order",
                         "input": {"order": "1234"}})

    async def lookup():
        await cli.mcp("crm", "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "claude-code", "version": "2"}}, request_id=1)
        await cli.mcp("crm", "notifications/initialized", {})
        response = await cli.mcp("crm", "tools/call", {"name": "lookup_order", "arguments": {"order": "1234"}},
                                 request_id=2)
        return response["result"]["content"][0]["text"]

    await cli.use_tool("toolu_order", "mcp__crm__lookup_order", {"order": "1234"}, lookup)
    research = {"description": "Check the refund policy", "prompt": "What is our refund window?",
                "subagent_type": "researcher"}
    await cli.assistant("msg_2", {"type": "tool_use", "id": "toolu_agent", "name": "Agent", "input": research})
    await cli.hook("PreToolUse", "toolu_agent", tool_name="Agent", tool_input=research)
    await cli.user({"type": "text", "text": "What is our refund window?"}, parent="toolu_agent")
    fetch = {"url": "https://example.com/refunds", "prompt": "refund window"}
    await cli.assistant("msg_3", {"type": "tool_use", "id": "toolu_fetch", "name": "WebFetch", "input": fetch},
                        parent="toolu_agent")

    async def fetched():
        return POLICY

    await cli.use_tool("toolu_fetch", "WebFetch", fetch, fetched, parent="toolu_agent", agent="researcher")
    await cli.assistant("msg_4", {"type": "text", "text": f"The policy says: {POLICY}"}, parent="toolu_agent")
    await cli.user({"type": "tool_result", "tool_use_id": "toolu_agent",
                    "content": [{"type": "text", "text": f"The policy says: {POLICY}"}]})
    await cli.assistant("msg_5", {"type": "text", "text": ANSWER})
    await cli.emit({"type": "result", "subtype": "success", "duration_ms": 10, "duration_api_ms": 8, "is_error": False,
                    "num_turns": 5, "session_id": "session-1", "result": ANSWER})


seen_links = []


@tool("lookup_order", "Look an order up", {"order": str})
async def lookup_order(arguments):
    seen_links.append(links.current())
    return {"content": [{"type": "text", "text": ORDER}]}


async def run_support(claude: WakeClaudeAgent, script=support_run, tools=(lookup_order,)):
    crm = create_sdk_mcp_server("crm", tools=claude.tools(list(tools)))
    options = claude.options(ClaudeAgentOptions(
        mcp_servers={"crm": crm},
        agents={"researcher": AgentDefinition(description="Looks things up", prompt="Research carefully.")},
    ))
    return [m async for m in claude.record(query(prompt=PROMPT, options=options, transport=FakeClaudeCode(script)),
                                           prompt=PROMPT)]


@pytest.fixture
def recorded():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    seen_links.clear()
    messages = asyncio.run(run_support(WakeClaudeAgent(recorder)))
    recorder.flush()
    return messages, exporter.get_finished_spans()


def wake_events(spans):
    return sorted((s for s in spans if "wake.event.kind" in s.attributes), key=lambda s: s.start_time)


def summary(spans):
    return [(e.attributes["wake.event.kind"], e.attributes["wake.agent.id"], e.name) for e in wake_events(spans)]


def by_name(spans, name):
    (event,) = [e for e in wake_events(spans) if e.name == name]
    return event


def event_id(event):
    return event.attributes["wake.event.id"]


def test_records_model_calls_tool_calls_and_the_subagent(recorded):
    messages, spans = recorded
    assert messages[-1].result == ANSWER, "the messages pass through unchanged"
    assert summary(spans) == [
        ("model_call", "support", "chat claude-opus-5-5"),
        ("tool_call", "support", "execute_tool mcp__crm__lookup_order"),
        ("model_call", "support", "chat claude-opus-5-5"),
        ("agent_message", "support/researcher", "agent_message support -> support/researcher"),
        ("model_call", "support/researcher", "chat claude-opus-5-5"),
        ("tool_call", "support/researcher", "execute_tool WebFetch"),
        ("model_call", "support/researcher", "chat claude-opus-5-5"),
        ("agent_message", "support", "agent_message support/researcher -> support"),
        ("model_call", "support", "chat claude-opus-5-5"),
    ]
    first = wake_events(spans)[0]
    payload = json.loads(first.attributes["wake.payload"])
    assert payload["input"] == [{"role": "user", "content": PROMPT}]
    assert [b["type"] for b in payload["output"]] == ["text", "tool_use"], "one call for a response's blocks"
    assert first.attributes["wake.origins"] == ("user",)
    assert first.attributes["gen_ai.response.id"] == "msg_1"


def test_links_follow_the_sdks_ids(recorded):
    _, spans = recorded
    calls = [e for e in wake_events(spans) if e.attributes["wake.event.kind"] == "model_call"]
    lookup = by_name(spans, "execute_tool mcp__crm__lookup_order")
    fetch = by_name(spans, "execute_tool WebFetch")
    handoff = by_name(spans, "agent_message support -> support/researcher")
    back = by_name(spans, "agent_message support/researcher -> support")

    def parents(event):
        return event.attributes["wake.parents"]

    assert parents(lookup) == (event_id(calls[0]),)
    assert lookup.attributes["wake.origins"] == ("tool:mcp__crm__lookup_order",)
    assert lookup.attributes["wake.mcp.server"] == "crm"
    assert json.loads(lookup.attributes["wake.payload"])["output"] == ORDER
    assert parents(calls[1]) == (event_id(calls[0]), event_id(lookup))
    assert parents(handoff) == (event_id(calls[1]),)
    assert handoff.attributes["wake.origins"] == ("agent:support",)
    assert parents(calls[2]) == (event_id(handoff),), "the subagent starts from the handoff"
    assert json.loads(calls[2].attributes["wake.payload"])["input"] == [
        {"role": "user", "content": "What is our refund window?"}], "the echoed prompt isn't counted twice"
    assert parents(fetch) == (event_id(calls[2]),)
    assert parents(calls[3]) == (event_id(calls[2]), event_id(fetch))
    assert parents(back) == (event_id(calls[3]),)
    assert back.attributes["wake.origins"] == ("agent:support/researcher",)
    assert parents(calls[4]) == (event_id(calls[1]), event_id(back))


def test_one_trace_with_the_subagent_nested(recorded):
    _, spans = recorded
    root = next(s for s in spans if s.name == "invoke_agent support")
    sub = next(s for s in spans if s.name == "invoke_agent support/researcher")
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    assert root.attributes["wake.claude.session_id"] == "session-1"
    assert sub.parent.span_id == root.context.span_id
    calls = [e for e in wake_events(spans) if e.attributes["wake.agent.id"] == "support/researcher"
             and e.attributes["wake.event.kind"] == "model_call"]
    assert all(c.parent.span_id == sub.context.span_id for c in calls)


def identity(event):
    return event.attributes.get("wake.agent.instance"), event.attributes.get("wake.agent.lineage")


def test_the_subagent_is_an_instance_started_by_its_agent(recorded):
    _, spans = recorded
    for event in wake_events(spans):
        if event.attributes["wake.agent.id"] == "support/researcher":
            assert identity(event) == ("support/researcher#toolu_agent", ("support",)), event.name
        else:
            assert identity(event) == (None, None), f"{event.name} is the main agent's own"
    sub = next(s for s in spans if s.name == "invoke_agent support/researcher")
    assert identity(sub) == ("support/researcher#toolu_agent", ("support",))


async def two_researchers(cli: FakeClaudeCode) -> None:
    await cli.emit({"type": "system", "subtype": "init", "session_id": "session-1", "uuid": "u0"})
    asks = {"toolu_window": ("Check the refund window", "Thirty days."),
            "toolu_shipping": ("Check who pays return shipping", "We do.")}
    await cli.assistant("msg_1", *[{"type": "tool_use", "id": use, "name": "Agent",
                                    "input": {"description": ask, "prompt": ask, "subagent_type": "researcher"}}
                                   for use, (ask, _) in asks.items()])
    for use, (ask, answer) in asks.items():
        await cli.user({"type": "text", "text": ask}, parent=use)
        await cli.assistant(f"msg_{use}", {"type": "text", "text": answer}, parent=use)
    for use, (_, answer) in asks.items():
        await cli.user({"type": "tool_result", "tool_use_id": use, "content": [{"type": "text", "text": answer}]})
    await cli.assistant("msg_2", {"type": "text", "text": ANSWER})
    await cli.emit({"type": "result", "subtype": "success", "duration_ms": 10, "duration_api_ms": 8, "is_error": False,
                    "num_turns": 2, "session_id": "session-1", "result": ANSWER})


def test_two_subagents_of_one_type_are_two_instances():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    asyncio.run(run_support(WakeClaudeAgent(recorder), script=two_researchers))
    recorder.flush()
    spans = exporter.get_finished_spans()
    kinds: dict[str, list[str]] = {}
    for event in wake_events(spans):
        if event.attributes["wake.agent.id"] == "support/researcher":
            instance, lineage = identity(event)
            assert lineage == ("support",)
            kinds.setdefault(instance, []).append(event.attributes["wake.event.kind"])
    assert {i: sorted(k) for i, k in kinds.items()} == {
        "support/researcher#toolu_window": ["agent_message", "model_call"],
        "support/researcher#toolu_shipping": ["agent_message", "model_call"],
    }, "each started its own run, under one agent name"
    answers = [e for e in wake_events(spans) if e.name == "agent_message support/researcher -> support"]
    assert len(answers) == 2 and all(identity(a) == (None, None) for a in answers), "each answer reaches the main agent"


def test_an_in_process_tool_runs_with_its_event_current(recorded):
    _, spans = recorded
    lookup = by_name(spans, "execute_tool mcp__crm__lookup_order")
    (link,) = seen_links
    assert link is not None and link.event_id == event_id(lookup)
    assert link.span_id == lookup.context.span_id
    assert links.current() is None


def test_a_later_turn_continues_from_the_last():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    claude = WakeClaudeAgent(recorder)

    async def follow_up(cli):
        await cli.emit({"type": "system", "subtype": "init", "session_id": "session-1", "uuid": "u9"})
        await cli.assistant("msg_6", {"type": "text", "text": "Done."})
        await cli.emit({"type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1,
                        "is_error": False, "num_turns": 1, "session_id": "session-1"})

    async def both():
        await run_support(claude)
        await run_support(claude, follow_up)

    asyncio.run(both())
    recorder.flush()
    calls = [e for e in wake_events(exporter.get_finished_spans()) if e.attributes["wake.event.kind"] == "model_call"
             and e.attributes["wake.agent.id"] == "support"]
    assert calls[-1].attributes["wake.parents"][0] == event_id(calls[-2])


def test_a_run_inside_a_recorded_tool_links_to_it():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    claude = WakeClaudeAgent(recorder)
    outer = links.Link("outer-tool-call", 0x1234, 0x5678)

    async def run():
        frame = links._enter(outer)
        try:
            await run_support(claude)
        finally:
            links._exit(frame)

    asyncio.run(run())
    recorder.flush()
    spans = exporter.get_finished_spans()
    first = wake_events(spans)[0]
    assert first.attributes["wake.parents"] == ("outer-tool-call",)
    assert first.attributes["wake.origins"] == (), "the calling tool's lineage carries the origins"
    assert first.context.trace_id == 0x1234


def test_recording_failures_never_break_the_run(monkeypatch):
    recorder = Recorder(agent="support", exporter=InMemorySpanExporter())

    def fail(*args, **kwargs):
        raise RuntimeError("recorder bug")

    monkeypatch.setattr(recorder.tracer, "start_span", fail)
    seen_links.clear()
    messages = asyncio.run(run_support(WakeClaudeAgent(recorder)))
    assert messages[-1].result == ANSWER
    assert seen_links == [None], "the tool still ran"


def test_a_memory_write_in_a_tool_is_labeled_with_its_origins(tmp_path):
    from wake.memory import Memory

    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    claude = WakeClaudeAgent(recorder)
    memory = Memory(claude)

    @tool("lookup_order", "Look an order up", {"order": str})
    async def remembering_lookup(arguments):
        memory.write_file(tmp_path / "notes.md", ORDER)
        return {"content": [{"type": "text", "text": ORDER}]}

    async def run():
        crm = create_sdk_mcp_server("crm", tools=claude.tools([remembering_lookup]))
        options = claude.options(ClaudeAgentOptions(mcp_servers={"crm": crm}))
        async for _ in claude.record(query(prompt=PROMPT, options=options, transport=FakeClaudeCode(support_run)),
                                     prompt=PROMPT):
            pass

    asyncio.run(run())
    recorder.flush()
    spans = exporter.get_finished_spans()
    lookup = by_name(spans, "execute_tool mcp__crm__lookup_order")
    (write,) = [e for e in wake_events(spans) if e.attributes["wake.event.kind"] == "memory_write"]
    assert write.attributes["wake.parents"][0] == event_id(lookup)
    assert write.attributes["wake.agent.id"] == "support"
    assert identity(write) == (None, None), "the main agent wrote it"
    assert set(write.attributes["wake.memory.origins"]) == {"tool:mcp__crm__lookup_order", "user"}


async def researcher_remembers(cli: FakeClaudeCode) -> None:
    await cli.emit({"type": "system", "subtype": "init", "session_id": "session-1", "uuid": "u0"})
    research = {"description": "Look the order up", "prompt": "Look order 1234 up", "subagent_type": "researcher"}
    await cli.assistant("msg_1", {"type": "tool_use", "id": "toolu_agent", "name": "Agent", "input": research})
    await cli.hook("PreToolUse", "toolu_agent", tool_name="Agent", tool_input=research)
    await cli.user({"type": "text", "text": "Look order 1234 up"}, parent="toolu_agent")
    await cli.assistant("msg_2", {"type": "tool_use", "id": "toolu_order", "name": "mcp__crm__lookup_order",
                                  "input": {"order": "1234"}}, parent="toolu_agent")

    async def lookup():
        await cli.mcp("crm", "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "claude-code", "version": "2"}}, request_id=1)
        await cli.mcp("crm", "notifications/initialized", {})
        response = await cli.mcp("crm", "tools/call", {"name": "lookup_order", "arguments": {"order": "1234"}},
                                 request_id=2)
        return response["result"]["content"][0]["text"]

    await cli.use_tool("toolu_order", "mcp__crm__lookup_order", {"order": "1234"}, lookup, parent="toolu_agent",
                       agent="researcher")
    await cli.assistant("msg_3", {"type": "text", "text": ORDER}, parent="toolu_agent")
    await cli.user({"type": "tool_result", "tool_use_id": "toolu_agent", "content": [{"type": "text", "text": ORDER}]})
    await cli.assistant("msg_4", {"type": "text", "text": ANSWER})
    await cli.emit({"type": "result", "subtype": "success", "duration_ms": 10, "duration_api_ms": 8, "is_error": False,
                    "num_turns": 4, "session_id": "session-1", "result": ANSWER})


def test_a_memory_write_in_a_subagents_tool_names_the_instance(tmp_path):
    from wake.memory import Memory

    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    claude = WakeClaudeAgent(recorder)
    memory = Memory(claude)

    @tool("lookup_order", "Look an order up", {"order": str})
    async def remembering_lookup(arguments):
        memory.write_file(tmp_path / "notes.md", ORDER)
        return {"content": [{"type": "text", "text": ORDER}]}

    asyncio.run(run_support(claude, script=researcher_remembers, tools=[remembering_lookup]))
    recorder.flush()
    spans = exporter.get_finished_spans()
    lookup = by_name(spans, "execute_tool mcp__crm__lookup_order")
    (write,) = [e for e in wake_events(spans) if e.attributes["wake.event.kind"] == "memory_write"]
    assert write.attributes["wake.parents"][0] == event_id(lookup)
    assert write.attributes["wake.agent.id"] == "support/researcher"
    assert identity(lookup) == identity(write) == ("support/researcher#toolu_agent", ("support",))
