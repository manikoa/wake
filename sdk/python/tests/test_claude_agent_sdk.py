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
    AssistantMessage,
    ClaudeAgentOptions,
    SystemMessage,
    ToolUseBlock,
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
        self.refused: list[str] = []  # tool uses a PreToolUse hook denied

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

    async def hook(self, event: str, tool_use_id: str, **fields) -> list[dict]:
        answers = []
        for callback_id in self._hooks.get(event, []):
            answers.append(await self.request({
                "subtype": "hook_callback", "callback_id": callback_id, "tool_use_id": tool_use_id,
                "input": {"hook_event_name": event, "session_id": "session-1", "transcript_path": "",
                          "cwd": "/", "tool_use_id": tool_use_id, **fields}}))
        return answers

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

    async def user(self, *blocks, parent=None, result=None) -> None:
        message = {"type": "user", "parent_tool_use_id": parent, "session_id": "session-1",
                   "message": {"role": "user", "content": list(blocks)}}
        if result is not None:
            message["tool_use_result"] = result  # the tool's own output, beside the text the model reads
        await self.emit(message)

    async def allowed(self, tool_use_id: str, name: str, arguments: dict, context: dict, *, parent=None,
                      ask=None) -> bool:
        """Asks the PreToolUse hooks whether a tool may run. With ask, the message asking for it
        is read only after they have answered, as when the code reading the stream lags behind
        the hooks."""
        answers = await self.hook("PreToolUse", tool_use_id, tool_name=name, tool_input=arguments, **context)
        if ask is not None:
            await ask()
        refused = [(a.get("hookSpecificOutput") or {}) for a in answers]
        refused = [r["permissionDecisionReason"] for r in refused if r.get("permissionDecision") == "deny"]
        if not refused:
            return True
        # As Claude Code does: the tool doesn't run, and the model is told why.
        self.refused.append(tool_use_id)
        await self.user({"type": "tool_result", "tool_use_id": tool_use_id, "content": refused[0], "is_error": True},
                        parent=parent)
        return False

    async def use_tool(self, tool_use_id: str, name: str, arguments: dict, run, *, parent=None, agent=None,
                       agent_id=None, ask=None):
        context = caller(agent, agent_id)
        if not await self.allowed(tool_use_id, name, arguments, context, parent=parent, ask=ask):
            return
        output = await run()
        await self.hook("PostToolUse", tool_use_id, tool_name=name, tool_input=arguments, tool_response=output, **context)
        await self.user({"type": "tool_result", "tool_use_id": tool_use_id, "content": output}, parent=parent)

    async def spawn(self, tool_use_id: str, arguments: dict, work, *, subagent_id: str, background=False,
                    parent=None, agent=None, agent_id=None, ask=None) -> None:
        """Starts a subagent with the Agent tool, unless a hook refuses it. work plays the
        subagent, its hooks naming it subagent_id, and returns its answer, the tool's result.
        In the background, the result says it was launched, and a task_notification says when
        it has finished."""
        context = caller(agent, agent_id)
        if not await self.allowed(tool_use_id, "Agent", arguments, context, parent=parent, ask=ask):
            return
        if not background:
            answer = await work()
            done = {"status": "completed", "agentId": subagent_id, "content": [{"type": "text", "text": answer}],
                    "prompt": arguments["prompt"]}
            await self.hook("PostToolUse", tool_use_id, tool_name="Agent", tool_input=arguments, tool_response=done,
                            **context)
            await self.user({"type": "tool_result", "tool_use_id": tool_use_id,
                             "content": [{"type": "text", "text": answer}]}, parent=parent, result=done)
            return
        launched = {"status": "async_launched", "agentId": subagent_id, "description": arguments["description"],
                    "prompt": arguments["prompt"], "outputFile": f"/tmp/{subagent_id}.output"}
        await self.hook("PostToolUse", tool_use_id, tool_name="Agent", tool_input=arguments, tool_response=launched,
                        **context)
        await self.user({"type": "tool_result", "tool_use_id": tool_use_id, "content": [
            {"type": "text", "text": f"Async agent launched successfully. agentId: {subagent_id}"}]},
            parent=parent, result=launched)
        await work()
        await self.emit({"type": "system", "subtype": "task_notification", "task_id": subagent_id,
                         "tool_use_id": tool_use_id, "status": "completed", "output_file": launched["outputFile"],
                         "summary": f'Agent "{arguments["description"]}" completed', "uuid": f"{tool_use_id}-done",
                         "session_id": "session-1"})


def caller(agent, agent_id) -> dict:
    """What a hook says of the subagent making a call: nothing, for the main agent."""
    return {"agent_id": agent_id or f"agent-{agent}", "agent_type": agent} if agent else {}


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

    async def researcher():
        await cli.user({"type": "text", "text": "What is our refund window?"}, parent="toolu_agent")
        fetch = {"url": "https://example.com/refunds", "prompt": "refund window"}
        await cli.assistant("msg_3", {"type": "tool_use", "id": "toolu_fetch", "name": "WebFetch", "input": fetch},
                            parent="toolu_agent")

        async def fetched():
            return POLICY

        await cli.use_tool("toolu_fetch", "WebFetch", fetch, fetched, parent="toolu_agent", agent="researcher")
        await cli.assistant("msg_4", {"type": "text", "text": f"The policy says: {POLICY}"}, parent="toolu_agent")
        return f"The policy says: {POLICY}"

    await cli.spawn("toolu_agent", research, researcher, subagent_id="agent-researcher")
    await cli.assistant("msg_5", {"type": "text", "text": ANSWER})
    await cli.emit({"type": "result", "subtype": "success", "duration_ms": 10, "duration_api_ms": 8, "is_error": False,
                    "num_turns": 5, "session_id": "session-1", "result": ANSWER})


seen_links = []


@tool("lookup_order", "Look an order up", {"order": str})
async def lookup_order(arguments):
    seen_links.append(links.current())
    return {"content": [{"type": "text", "text": ORDER}]}


async def run_support(claude: WakeClaudeAgent, script=support_run, tools=(lookup_order,), read=None):
    """The messages the run streamed, put in read as the code reading the stream gets each."""
    crm = create_sdk_mcp_server("crm", tools=claude.tools(list(tools)))
    options = claude.options(ClaudeAgentOptions(
        mcp_servers={"crm": crm},
        agents={"researcher": AgentDefinition(description="Looks things up", prompt="Research carefully.")},
    ))
    read = [] if read is None else read
    async for message in claude.record(query(prompt=PROMPT, options=options, transport=FakeClaudeCode(script)),
                                       prompt=PROMPT):
        read.append(message)
    return read


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


def research(ask: str) -> dict:
    return {"description": ask, "prompt": ask, "subagent_type": "researcher"}


async def two_researchers(cli: FakeClaudeCode) -> None:
    await cli.emit({"type": "system", "subtype": "init", "session_id": "session-1", "uuid": "u0"})
    asks = {"toolu_window": ("Check the refund window", "Thirty days."),
            "toolu_shipping": ("Check who pays return shipping", "We do.")}
    await cli.assistant("msg_1", *[{"type": "tool_use", "id": use, "name": "Agent", "input": research(ask)}
                                   for use, (ask, _) in asks.items()])
    started = [use for use, (ask, _) in asks.items() if await cli.allowed(use, "Agent", research(ask), {})]
    for use in started:
        ask, answer = asks[use]
        await cli.user({"type": "text", "text": ask}, parent=use)
        await cli.assistant(f"msg_{use}", {"type": "text", "text": answer}, parent=use)
    for use in started:
        await cli.user({"type": "tool_result", "tool_use_id": use, "content": [{"type": "text", "text": asks[use][1]}]})
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


class Halts:
    """Stands in for the brake's rules (wake.quarantine.Quarantine): the halts in force."""

    def __init__(self) -> None:
        self.halts: list[dict] = []

    def halted(self, names):
        for h in self.halts:
            if h.get("fleet") or set(names) & set(h.get("agents", [])):
                return h["id"]
        return None


async def until(condition, what: str) -> None:
    for _ in range(400):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"never: {what}")


WINDOW, SHIPPING = "support/researcher#toolu_window", "support/researcher#toolu_shipping"


def two_researchers_working(claude: WakeClaudeAgent, halts: Halts):
    """Two researcher copies each fetch a page, then run a command. One copy is halted once
    both have fetched."""

    async def script(cli: FakeClaudeCode) -> None:
        await cli.emit({"type": "system", "subtype": "init", "session_id": "session-1", "uuid": "u0"})
        asks = {"toolu_window": "Check the refund window", "toolu_shipping": "Check who pays return shipping"}
        await cli.assistant("msg_1", *[{"type": "tool_use", "id": use, "name": "Agent", "input": research(ask)}
                                       for use, ask in asks.items()])
        asks = {use: ask for use, ask in asks.items() if await cli.allowed(use, "Agent", research(ask), {})}
        for use, ask in asks.items():
            await cli.user({"type": "text", "text": ask}, parent=use)
        run = lambda: claude._run_for("session-1")
        await until(lambda: run() is not None and {"toolu_window", "toolu_shipping"} <= set(run().threads), "both copies started")

        async def fetched():
            return POLICY

        for use in asks:
            fetch = {"url": f"https://example.com/{use}", "prompt": "policy"}
            await cli.assistant(f"msg_{use}_1", {"type": "tool_use", "id": f"{use}_fetch", "name": "WebFetch",
                                                 "input": fetch}, parent=use)
            await cli.use_tool(f"{use}_fetch", "WebFetch", fetch, fetched, parent=use, agent="researcher",
                               agent_id=f"agent-{use}")
        await until(lambda: len(claude._subagents) == 2, "each copy placed by its agent_id")
        halts.halts = [{"id": "halt-window", "agents": [WINDOW]}]

        async def ran():
            return "ok"

        for use in asks:
            command = {"command": f"echo {use}"}
            await cli.assistant(f"msg_{use}_2", {"type": "tool_use", "id": f"{use}_bash", "name": "Bash",
                                                 "input": command}, parent=use)
            await cli.use_tool(f"{use}_bash", "Bash", command, ran, parent=use, agent="researcher",
                               agent_id=f"agent-{use}")
        for use in asks:
            await cli.user({"type": "tool_result", "tool_use_id": use, "content": [{"type": "text", "text": "done"}]})
        # The main agent isn't halted: its own tool call runs.
        await cli.assistant("msg_2", {"type": "tool_use", "id": "toolu_main_bash", "name": "Bash",
                                      "input": {"command": "date"}})
        await cli.use_tool("toolu_main_bash", "Bash", {"command": "date"}, ran)
        await cli.assistant("msg_3", {"type": "text", "text": ANSWER})
        await cli.emit({"type": "result", "subtype": "success", "duration_ms": 10, "duration_api_ms": 8,
                        "is_error": False, "num_turns": 3, "session_id": "session-1", "result": ANSWER})
        cli_seen.append(cli)

    return script


cli_seen: list = []


def test_a_halted_subagent_is_stopped_in_process_and_its_sibling_goes_on():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    halts = Halts()
    claude = WakeClaudeAgent(recorder, quarantine=halts)
    cli_seen.clear()
    asyncio.run(run_support(claude, script=two_researchers_working(claude, halts)))
    recorder.flush()
    assert cli_seen[0].refused == ["toolu_window_bash"], "only the halted copy's command was refused"
    denied = [e for e in wake_events(exporter.get_finished_spans()) if e.attributes["wake.event.kind"] == "denied"]
    assert len(denied) == 1
    (d,) = denied
    assert d.attributes["wake.brake.halt_id"] == "halt-window"
    assert d.attributes["gen_ai.tool.name"] == "Bash"
    assert identity(d) == (WINDOW, ("support",)), "recorded as the halted instance's attempt"
    ran = [e for e in wake_events(exporter.get_finished_spans())
           if e.attributes["wake.event.kind"] == "tool_call" and e.attributes.get("gen_ai.tool.name") == "Bash"]
    assert sorted(identity(e)[0] or "main" for e in ran) == ["main", SHIPPING]


def test_while_wake_cant_tell_which_copy_is_calling_a_halt_that_may_cover_it_stops_it():
    # A hook can come before the stream says which copy asked for its tool, or even that the
    # copy started. Two copies of a type have started, neither yet placed by its agent_id, and
    # one is halted.
    halts = Halts()
    halts.halts = [{"id": "halt-window", "agents": [WINDOW]}]
    claude = WakeClaudeAgent(Recorder(agent="support", exporter=InMemorySpanExporter()), quarantine=halts)
    run = claude._start_run(PROMPT)
    claude._observe(run, SystemMessage(subtype="init", data={"session_id": "session-1"}))
    claude._observe(run, AssistantMessage(
        content=[ToolUseBlock(id=use, name="Agent", input=research("Check")) for use in ("toolu_window", "toolu_shipping")],
        model="claude-opus-5-5", message_id="msg_1", session_id="session-1"))

    def ask(agent_id, agent_type="researcher"):
        return claude._halting({"session_id": "session-1", "agent_id": agent_id, "agent_type": agent_type,
                                "tool_name": "Bash"}, None)

    halt, reason = ask("agent-1")
    assert halt == "halt-window" and "can't yet tell which copy of support/researcher is calling" in reason
    # Placing one copy doesn't place the other: an unknown agent_id may be a copy the stream
    # hasn't shown yet.
    claude._subagents["agent-2"] = (SHIPPING, "support")
    assert ask("agent-1")[0] == "halt-window"
    assert ask("agent-2") is None, "the sibling goes on"
    claude._subagents["agent-1"] = (WINDOW, "support")
    halt, reason = ask("agent-1")
    assert halt == "halt-window" and reason.startswith(f"Wake halted {WINDOW}")
    assert ask("agent-3", "writer")[0] == "halt-window", "a subagent the halted copy started, not yet shown"
    halts.halts = []
    assert ask("agent-1") is None and ask("agent-3", "writer") is None
    claude._end_run(run)


async def ok():
    return "ok"


LEAD = "support/lead#toolu_lead"


def test_a_subagent_the_stream_hasnt_shown_is_stopped_by_a_halt_on_what_started_it():
    # The halted lead started a writer, whose first call comes before the stream says so.
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    halts = Halts()
    claude = WakeClaudeAgent(recorder, quarantine=halts)
    seen, read = [], []

    async def script(cli: FakeClaudeCode) -> None:
        seen.append(cli)
        await cli.emit({"type": "system", "subtype": "init", "session_id": "session-1", "uuid": "u0"})
        lead = {"description": "Lead", "prompt": "Lead the research", "subagent_type": "lead"}
        write = {"description": "Write", "prompt": "Write it up", "subagent_type": "writer"}
        await cli.assistant("msg_1", {"type": "tool_use", "id": "toolu_lead", "name": "Agent", "input": lead})

        async def leading():
            await cli.user({"type": "text", "text": lead["prompt"]}, parent="toolu_lead")
            await until(lambda: any(getattr(m, "parent_tool_use_id", None) == "toolu_lead" for m in read), "the lead is read")
            assert await cli.allowed("toolu_writer", "Agent", write, caller("lead", None))
            halts.halts = [{"id": "halt-lead", "agents": [LEAD]}]
            command = {"command": "cat notes.md"}

            async def read_late():
                await cli.assistant("msg_lead_1", {"type": "tool_use", "id": "toolu_writer", "name": "Agent",
                                                   "input": write}, parent="toolu_lead")
                await cli.user({"type": "text", "text": write["prompt"]}, parent="toolu_writer")
                await cli.assistant("msg_writer_1", {"type": "tool_use", "id": "toolu_writer_bash", "name": "Bash",
                                                     "input": command}, parent="toolu_writer")

            await cli.use_tool("toolu_writer_bash", "Bash", command, ok, parent="toolu_writer", agent="writer",
                               ask=read_late)
            await cli.assistant("msg_writer_2", {"type": "text", "text": "Stopped."}, parent="toolu_writer")
            await cli.user({"type": "tool_result", "tool_use_id": "toolu_writer",
                            "content": [{"type": "text", "text": "Stopped."}]}, parent="toolu_lead")
            return "Stopped."

        await cli.spawn("toolu_lead", lead, leading, subagent_id="agent-lead")
        await cli.assistant("msg_2", {"type": "text", "text": ANSWER})
        await cli.emit({"type": "result", "subtype": "success", "duration_ms": 10, "duration_api_ms": 8,
                        "is_error": False, "num_turns": 2, "session_id": "session-1", "result": ANSWER})

    asyncio.run(run_support(claude, script=script, read=read))
    recorder.flush()
    assert seen[0].refused == ["toolu_writer_bash"], "refused while Wake couldn't tell who was calling"
    (denied,) = [e for e in wake_events(exporter.get_finished_spans()) if e.attributes["wake.event.kind"] == "denied"]
    assert denied.attributes["wake.brake.halt_id"] == "halt-lead"
    assert identity(denied) == ("support/writer#toolu_writer", ("support", LEAD)), "named once the stream said"


def test_a_halt_of_the_agent_stops_it_and_starts_no_subagent_and_no_halt_stops_nothing(recorded):
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    halts = Halts()
    halts.halts = [{"id": "halt-support", "agents": ["support", "0oa1support"]}]
    seen_links.clear()
    asyncio.run(run_support(WakeClaudeAgent(recorder, quarantine=halts)))
    recorder.flush()
    spans = exporter.get_finished_spans()
    assert summary(spans) == [
        ("model_call", "support", "chat claude-opus-5-5"),
        ("denied", "support", "execute_tool mcp__crm__lookup_order"),
        ("model_call", "support", "chat claude-opus-5-5"),
        ("denied", "support", "execute_tool Agent"),
        ("model_call", "support", "chat claude-opus-5-5"),
    ], "the subagent never started: nothing handed it a task, and it did nothing"
    denied = [e for e in wake_events(spans) if e.attributes["wake.event.kind"] == "denied"]
    assert {e.attributes["wake.brake.halt_id"] for e in denied} == {"halt-support"}
    assert not [s for s in spans if s.name.startswith("invoke_agent support/")]
    assert seen_links == [], "the in-process tool never ran"
    # Without a halt, the same run records as before: nothing refused.
    _, spans = recorded
    assert not [e for e in wake_events(spans) if e.attributes["wake.event.kind"] == "denied"]


@pytest.mark.parametrize("hook_first", [False, True], ids=["stream_first", "hook_first"])
def test_a_spawn_a_halt_refuses_is_recorded_as_a_denied_call_whichever_comes_first(hook_first):
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    halts = Halts()
    halts.halts = [{"id": "halt-support", "agents": ["support"]}]
    ask, read = research("Check the refund window"), []

    async def script(cli: FakeClaudeCode) -> None:
        await cli.emit({"type": "system", "subtype": "init", "session_id": "session-1", "uuid": "u0"})

        async def asked():
            await cli.assistant("msg_1", {"type": "tool_use", "id": "toolu_agent", "name": "Agent", "input": ask})

        async def never():
            raise AssertionError("a refused subagent never starts")

        if not hook_first:
            await asked()
            await until(lambda: any(isinstance(m, AssistantMessage) for m in read), "the Agent call is read")
        await cli.spawn("toolu_agent", ask, never, subagent_id="agent-researcher", ask=asked if hook_first else None)
        await cli.assistant("msg_2", {"type": "text", "text": "I was stopped."})
        await cli.emit({"type": "result", "subtype": "success", "duration_ms": 10, "duration_api_ms": 8,
                        "is_error": False, "num_turns": 2, "session_id": "session-1", "result": "I was stopped."})

    asyncio.run(run_support(WakeClaudeAgent(recorder, quarantine=halts), script=script, read=read))
    recorder.flush()
    spans = exporter.get_finished_spans()
    assert summary(spans) == [
        ("model_call", "support", "chat claude-opus-5-5"),
        ("denied", "support", "execute_tool Agent"),
        ("model_call", "support", "chat claude-opus-5-5"),
    ]
    calls = [e for e in wake_events(spans) if e.attributes["wake.event.kind"] == "model_call"]
    refused = by_name(spans, "execute_tool Agent")
    assert refused.attributes["wake.brake.halt_id"] == "halt-support"
    assert refused.attributes["wake.parents"] == (event_id(calls[0]),), "the call that asked for it"
    assert json.loads(refused.attributes["wake.payload"])["input"] == ask
    assert event_id(refused) in calls[1].attributes["wake.parents"], "the model read the refusal"
    assert not [s for s in spans if s.name.startswith("invoke_agent support/")]


def test_a_halt_of_the_agent_landing_while_a_subagent_runs_stops_the_subagent_too():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    halts = Halts()
    seen = []

    async def script(cli: FakeClaudeCode) -> None:
        seen.append(cli)
        await cli.emit({"type": "system", "subtype": "init", "session_id": "session-1", "uuid": "u0"})
        ask = research("What is our refund window?")
        await cli.assistant("msg_1", {"type": "tool_use", "id": "toolu_agent", "name": "Agent", "input": ask})

        async def researcher():
            await cli.user({"type": "text", "text": ask["prompt"]}, parent="toolu_agent")
            halts.halts = [{"id": "halt-support", "agents": ["support"]}]
            command = {"command": "curl https://example.com/refunds"}
            await cli.assistant("msg_r1", {"type": "tool_use", "id": "toolu_bash", "name": "Bash", "input": command},
                                parent="toolu_agent")
            await cli.use_tool("toolu_bash", "Bash", command, ok, parent="toolu_agent", agent="researcher")
            await cli.assistant("msg_r2", {"type": "text", "text": "I was stopped."}, parent="toolu_agent")
            return "I was stopped."

        await cli.spawn("toolu_agent", ask, researcher, subagent_id="agent-researcher")
        await cli.assistant("msg_2", {"type": "text", "text": ANSWER})
        await cli.emit({"type": "result", "subtype": "success", "duration_ms": 10, "duration_api_ms": 8,
                        "is_error": False, "num_turns": 2, "session_id": "session-1", "result": ANSWER})

    asyncio.run(run_support(WakeClaudeAgent(recorder, quarantine=halts), script=script))
    recorder.flush()
    assert seen[0].refused == ["toolu_bash"]
    (denied,) = [e for e in wake_events(exporter.get_finished_spans()) if e.attributes["wake.event.kind"] == "denied"]
    assert denied.attributes["wake.brake.halt_id"] == "halt-support"
    assert identity(denied) == ("support/researcher#toolu_agent", ("support",))


BACKGROUND = "support/researcher#toolu_bg"


def test_a_subagent_in_the_background_stays_its_instance_and_a_halt_on_it_holds():
    # The Agent tool starts a subagent in the background by default: the tool's result, that it
    # was launched, comes at once, and the subagent works on until Claude Code says it's done.
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    halts = Halts()
    claude = WakeClaudeAgent(recorder, quarantine=halts)
    seen, read = [], []

    async def script(cli: FakeClaudeCode) -> None:
        seen.append(cli)
        await cli.emit({"type": "system", "subtype": "init", "session_id": "session-1", "uuid": "u0"})
        ask = research("What is our refund window?")
        await cli.assistant("msg_1", {"type": "tool_use", "id": "toolu_bg", "name": "Agent", "input": ask})

        async def researcher():
            await cli.user({"type": "text", "text": ask["prompt"]}, parent="toolu_bg")
            await until(lambda: any(getattr(m, "tool_use_result", None) for m in read), "the launch is read")
            halts.halts = [{"id": "halt-bg", "agents": [BACKGROUND]}]  # the instance the log named
            command = {"command": "curl https://example.com/refunds"}

            async def asked():
                await cli.assistant("msg_bg_1", {"type": "tool_use", "id": "toolu_bg_bash", "name": "Bash",
                                                 "input": command}, parent="toolu_bg")

            # Its hook comes before the stream shows the call.
            await cli.use_tool("toolu_bg_bash", "Bash", command, ok, parent="toolu_bg", agent="researcher",
                               agent_id="agent-bg", ask=asked)
            await cli.assistant("msg_bg_2", {"type": "text", "text": f"The policy says: {POLICY}"}, parent="toolu_bg")
            return f"The policy says: {POLICY}"

        await cli.spawn("toolu_bg", ask, researcher, subagent_id="agent-bg", background=True)
        await cli.assistant("msg_2", {"type": "text", "text": ANSWER})
        await cli.emit({"type": "result", "subtype": "success", "duration_ms": 10, "duration_api_ms": 8,
                        "is_error": False, "num_turns": 2, "session_id": "session-1", "result": ANSWER})

    asyncio.run(run_support(claude, script=script, read=read))
    recorder.flush()
    assert seen[0].refused == ["toolu_bg_bash"], "the halt on the instance held"
    events = wake_events(exporter.get_finished_spans())
    (denied,) = [e for e in events if e.attributes["wake.event.kind"] == "denied"]
    assert denied.attributes["wake.brake.halt_id"] == "halt-bg"
    assert {e.attributes["wake.agent.id"] for e in events} == {"support", "support/researcher"}
    for event in events:
        if event.attributes["wake.agent.id"] == "support/researcher":
            assert identity(event) == (BACKGROUND, ("support",)), f"{event.name} is still the instance"
    (answer,) = [e for e in events if e.name == "agent_message support/researcher -> support"]
    calls = [e for e in events if e.attributes["wake.event.kind"] == "model_call"]
    *_, last = [c for c in calls if c.attributes["wake.agent.id"] == "support/researcher"]
    *_, after = [c for c in calls if c.attributes["wake.agent.id"] == "support"]
    assert answer.attributes["wake.parents"] == (event_id(last),), "it answers once it has finished"
    assert event_id(answer) in after.attributes["wake.parents"], "and the agent reads the answer"


def test_a_finished_runs_hooks_never_reach_another_sessions_run():
    # A subagent left running in the background can call after its session's run has ended,
    # while a run of another session goes on: the call isn't that run's.
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    claude = WakeClaudeAgent(recorder)

    async def stream(session_id, done=None):
        yield SystemMessage(subtype="init", data={"session_id": session_id})
        if done is not None:
            await done.wait()

    async def scenario():
        done = asyncio.Event()
        other = claude.record(stream("session-2", done))
        await anext(other)
        async for _ in claude.record(stream("session-1")):
            pass
        late = {"hook_event_name": "PreToolUse", "session_id": "session-1", "tool_name": "Bash",
                "tool_input": {"command": "date"}, "agent_id": "agent-bg", "agent_type": "researcher"}
        await claude._pre_tool_use(late, "toolu_late", None)
        done.set()
        async for _ in other:
            pass

    asyncio.run(scenario())
    recorder.flush()
    assert not [s for s in exporter.get_finished_spans() if s.name == "execute_tool Bash"]


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


async def call_lookup(cli: FakeClaudeCode, *, first: bool = True) -> str:
    """Calls the in-process lookup_order tool as Claude Code does, and returns its text. Only
    the first call initializes."""
    if first:
        await cli.mcp("crm", "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "claude-code", "version": "2"}}, request_id=1)
        await cli.mcp("crm", "notifications/initialized", {})
    response = await cli.mcp("crm", "tools/call", {"name": "lookup_order", "arguments": {"order": "1234"}},
                             request_id=2 if first else 3)
    return response["result"]["content"][0]["text"]


async def researcher_remembers(cli: FakeClaudeCode) -> None:
    await cli.emit({"type": "system", "subtype": "init", "session_id": "session-1", "uuid": "u0"})
    ask = {"description": "Look the order up", "prompt": "Look order 1234 up", "subagent_type": "researcher"}
    await cli.assistant("msg_1", {"type": "tool_use", "id": "toolu_agent", "name": "Agent", "input": ask})

    async def researcher():
        await cli.user({"type": "text", "text": "Look order 1234 up"}, parent="toolu_agent")
        await cli.assistant("msg_2", {"type": "tool_use", "id": "toolu_order", "name": "mcp__crm__lookup_order",
                                      "input": {"order": "1234"}}, parent="toolu_agent")
        await cli.use_tool("toolu_order", "mcp__crm__lookup_order", {"order": "1234"}, lambda: call_lookup(cli),
                           parent="toolu_agent", agent="researcher")
        await cli.assistant("msg_3", {"type": "text", "text": ORDER}, parent="toolu_agent")
        return ORDER

    await cli.spawn("toolu_agent", ask, researcher, subagent_id="agent-researcher")
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


def remembering(memory, path):
    @tool("lookup_order", "Look an order up", {"order": str})
    async def remembering_lookup(arguments):
        memory.write_file(path, ORDER)
        return {"content": [{"type": "text", "text": ORDER}]}

    return remembering_lookup


def test_a_memory_write_by_a_subagent_wake_cant_place_yet_names_no_instance(tmp_path):
    # A second researcher's in-process tool runs before the stream says the second started,
    # while the first is known: which copy wrote isn't guessed.
    from wake.memory import Memory

    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    claude = WakeClaudeAgent(recorder)
    read = []
    first, second, order = research("Look order 1234 up"), research("Look order 1234 up again"), {"order": "1234"}

    async def script(cli: FakeClaudeCode) -> None:
        await cli.emit({"type": "system", "subtype": "init", "session_id": "session-1", "uuid": "u0"})
        await cli.assistant("msg_1", {"type": "tool_use", "id": "toolu_r1", "name": "Agent", "input": first})
        assert await cli.allowed("toolu_r1", "Agent", first, {})
        await cli.user({"type": "text", "text": first["prompt"]}, parent="toolu_r1")
        await until(lambda: any(getattr(m, "parent_tool_use_id", None) == "toolu_r1" for m in read), "the first is read")
        assert await cli.allowed("toolu_r2", "Agent", second, {})
        await cli.hook("PreToolUse", "toolu_r2_order", tool_name="mcp__crm__lookup_order", tool_input=order,
                       **caller("researcher", "agent-r2"))
        output = await call_lookup(cli)
        # What the stream says of the second, read only now.
        await cli.assistant("msg_1", {"type": "tool_use", "id": "toolu_r2", "name": "Agent", "input": second})
        await cli.user({"type": "text", "text": second["prompt"]}, parent="toolu_r2")
        await cli.assistant("msg_r2", {"type": "tool_use", "id": "toolu_r2_order", "name": "mcp__crm__lookup_order",
                                       "input": order}, parent="toolu_r2")
        await cli.user({"type": "tool_result", "tool_use_id": "toolu_r2_order", "content": output}, parent="toolu_r2")
        for use in ("toolu_r1", "toolu_r2"):
            await cli.user({"type": "tool_result", "tool_use_id": use, "content": [{"type": "text", "text": ORDER}]})
        await cli.assistant("msg_2", {"type": "text", "text": ANSWER})
        await cli.emit({"type": "result", "subtype": "success", "duration_ms": 10, "duration_api_ms": 8,
                        "is_error": False, "num_turns": 2, "session_id": "session-1", "result": ANSWER})

    asyncio.run(run_support(claude, script=script, tools=[remembering(Memory(claude), tmp_path / "notes.md")],
                            read=read))
    recorder.flush()
    spans = exporter.get_finished_spans()
    lookup = by_name(spans, "execute_tool mcp__crm__lookup_order")
    (write,) = [e for e in wake_events(spans) if e.attributes["wake.event.kind"] == "memory_write"]
    assert write.attributes["wake.parents"][0] == event_id(lookup)
    assert write.attributes["wake.agent.id"] == "support/researcher"
    assert identity(write) == (None, None), "not guessed to be the copy the stream had shown"
    assert identity(lookup) == ("support/researcher#toolu_r2", ("support",)), "named once the stream said"


def test_calls_alike_by_two_copies_at_once_name_no_instance_in_what_they_write(tmp_path):
    # Two researchers call the in-process tool with the same arguments at once, and the second's
    # call runs first. Nothing tells the tool which call it's running.
    from wake.memory import Memory

    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    claude = WakeClaudeAgent(recorder)
    asks, order = {"toolu_r1": research("Look order 1234 up"), "toolu_r2": research("Look it up too")}, {"order": "1234"}

    async def script(cli: FakeClaudeCode) -> None:
        await cli.emit({"type": "system", "subtype": "init", "session_id": "session-1", "uuid": "u0"})
        await cli.assistant("msg_1", *[{"type": "tool_use", "id": use, "name": "Agent", "input": ask}
                                       for use, ask in asks.items()])
        for use, ask in asks.items():
            assert await cli.allowed(use, "Agent", ask, {})
            await cli.user({"type": "text", "text": ask["prompt"]}, parent=use)
        for use in asks:
            await cli.assistant(f"msg_{use}", {"type": "tool_use", "id": f"{use}_order", "name": "mcp__crm__lookup_order",
                                               "input": order}, parent=use)
            await cli.hook("PreToolUse", f"{use}_order", tool_name="mcp__crm__lookup_order", tool_input=order,
                           **caller("researcher", f"agent-{use}"))
        outputs = [await call_lookup(cli), await call_lookup(cli, first=False)]
        for use, output in zip(reversed(list(asks)), outputs):
            await cli.user({"type": "tool_result", "tool_use_id": f"{use}_order", "content": output}, parent=use)
        for use in asks:
            await cli.user({"type": "tool_result", "tool_use_id": use, "content": [{"type": "text", "text": ORDER}]})
        await cli.assistant("msg_2", {"type": "text", "text": ANSWER})
        await cli.emit({"type": "result", "subtype": "success", "duration_ms": 10, "duration_api_ms": 8,
                        "is_error": False, "num_turns": 2, "session_id": "session-1", "result": ANSWER})

    asyncio.run(run_support(claude, script=script, tools=[remembering(Memory(claude), tmp_path / "notes.md")]))
    recorder.flush()
    writes = [e for e in wake_events(exporter.get_finished_spans()) if e.attributes["wake.event.kind"] == "memory_write"]
    assert len(writes) == 2
    assert all(w.attributes["wake.agent.id"] == "support/researcher" for w in writes)
    assert all(identity(w) == (None, None) for w in writes), "either could be either copy's"
