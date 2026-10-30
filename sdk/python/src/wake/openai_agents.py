"""Records OpenAI Agents SDK runs: model responses, tool calls and handoffs between agents.

    from wake import Recorder
    from wake.openai_agents import install

    install(Recorder(agent="support"))   # then run agents as usual
    await Runner.run(triage_agent, "Refund order 1234")

The Agents SDK traces every run, and Wake adds itself as one of its trace processors. Each
agent's run groups its events, named for the agent. Links are exact where the SDK gives
ids: a tool call's parent is the response that requested it, and a response's parents
are the earlier responses and tool results in its input, as Responses items or as Chat
Completions messages. A handoff is an agent_message in the receiving agent's name, fed by
the response that handed off, and it feeds that agent's next step. Each run, by its first
agent span, is linked within itself, as a workflow of its own, so two copies of one agent
running at once never take each other's steps as parents. An agent run inside a recorded
tool call, such as an agent used as a tool, descends from that tool call, and what it said
last feeds the response that reads the tool's result. It's an instance of its agent of its
own, <agent>#<its first agent span>, since a tool call can run the same agent more than
once, and a response can call it as a tool more than once at a time (docs/events.md).
Where nothing else links events, content does, as for LangChain, within each run.

Inputs and outputs are only in the trace when the SDK's trace_include_sensitive_data is
on, as it is by default. Without them, events are recorded with empty payloads, and the
SDK's loop links them instead: a tool call comes from its agent's latest response, and a
response reads that response and the tool results since.

Given the brake's rules (install(recorder, quarantine=Quarantine(...))), guard(agent) also
stops an agent, or one instance of it, in its own process. A call a halt covers, to a
function tool, an agent used as a tool or an MCP server's tool, is refused before it runs,
by a tool input guardrail. One to a tool the agent runs itself, a shell, an editor, a
computer or a custom tool, is refused as it starts. Either way it's recorded as denied, and
a refused agent tool starts no run. A handoff is offered only while neither the agent
offering it nor the one it hands off to is halted, and one the model picked before a halt
landed is refused as it starts. It's a cooperative stop: it holds only while the agent's
code does.

Hosted tools run at OpenAI, inside the model call, so they're checked around it. Before each
model call of an agent that offers hosted tools other than hosted MCP (web search, file
search, code interpreter, image generation, tool search, programmatic tool calling, a shell in
a hosted container), each is checked as a call by the agent to a tool of its name: if any is
held back, the model call isn't made, each held back is recorded as denied, and the run ends
with AgentHalted. A hosted MCP tool is made to ask before every call, and Wake answers each:
a call held back is rejected with why, and recorded as denied. One allowed gets the answer of
the tool's own approval callback when its own setting would have asked, and is approved
otherwise. A hosted MCP tool whose calls the app approves itself, through run interruptions,
is left as it is: Wake can't answer for a person.

The same checks apply the flow policy wake-proxy serves with those rules (wake.flow), once
no halt refuses the call: a tool call is refused when a rule holds its agent back for what
entered it, a call to an agent used as a tool when one holds it back from reaching that
agent, and a handoff likewise, which is then neither offered nor made. A tool's result
enters the agent that called it as tool:<name>, an MCP server's tool by its MCP name. A
hosted tool's call enters as tool:<name> as the response holding it comes in, before
anything that follows from that response is checked, a hosted MCP call by its MCP tool's own
name. Without sensitive data the trace doesn't say which hosted tools ran, so every hosted
tool the agent offered that call enters, but hosted MCP, which enters as each call Wake
answers for is approved. A handoff brings what had entered the agent handing off into the one it hands off to,
as agent:<that agent> with the rest. So does a call to an agent used as a tool, or any run
started inside a tool call, and what the run's agent said last brings what entered it back.
"""

import dataclasses
import functools
import hashlib
import inspect
import logging
import types
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, NamedTuple

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from wake import events, flow, links, stop
from wake._graph import EventGraph, _Recent, failed, never_raise
from wake.recorder import Recorder

if TYPE_CHECKING:
    from wake.quarantine import Quarantine

log = logging.getLogger("wake")

# Tools that run at OpenAI, inside the model call, where nothing in the agent's process can
# refuse one call at a time. A shell runs there too when its environment isn't local.
_HOSTED = ("FileSearchTool", "WebSearchTool", "HostedMCPTool", "ImageGenerationTool", "CodeInterpreterTool",
           "ToolSearchTool", "ProgrammaticToolCallingTool")
# A hosted tool's call in a response, and the name of the tool that made it, as agents offer
# it. A hosted MCP call is by its MCP tool's own name, and a hosted shell's by its tool's.
_HOSTED_CALLS = {"web_search_call": "web_search", "file_search_call": "file_search",
                 "code_interpreter_call": "code_interpreter", "image_generation_call": "image_generation",
                 "tool_search_call": "tool_search", "program": "programmatic_tool_calling"}

# What the SDK asks of a computer while it carries out a computer call.
_COMPUTER_ACTIONS = ("click", "double_click", "drag", "keypress", "move", "screenshot", "scroll", "type", "wait")


def install(recorder: Recorder, quarantine: "Quarantine | None" = None) -> "WakeTracingProcessor":
    """Adds Wake as a trace processor for every OpenAI Agents SDK run in this process. With
    quarantine, the brake's rules for the agent (wake.quarantine), guard() stops what a halt
    covers."""
    from agents.tracing import add_trace_processor

    processor = WakeTracingProcessor(recorder, quarantine=quarantine)
    add_trace_processor(processor)
    return processor


def _plain(value: Any) -> Any:
    """Pydantic models and the SDK's items as plain JSON values."""
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _text(value: Any) -> str:
    """The text in an item or a list of them, for the content index."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(t for t in (_text(v) for v in value) if t)
    content = _field(value, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(t for t in (_field(c, "text") or "" for c in content) if t)
    for key in ("text", "output", "arguments"):
        v = _field(value, key)
        if isinstance(v, str):
            return v
    return ""


def _is_result(item_type: Any) -> bool:
    """Whether an input item is a tool's result, as function_call_output or shell_call_output."""
    return isinstance(item_type, str) and item_type.endswith(("_output", "_result"))


def _current_span():
    """The span the SDK has current here, if any."""
    try:
        from agents.tracing import get_current_span

        return get_current_span()
    except Exception:
        return None


def _kind(tool: Any, *names: str) -> bool:
    """Whether tool is one of the SDK's tool classes by these names, as far as this version
    of the SDK has them."""
    import agents

    return any(isinstance(k, type) and isinstance(tool, k) for k in (getattr(agents, n, None) for n in names))


def _hosted(tool: Any) -> bool:
    """Whether a tool runs at OpenAI rather than in the agent's process."""
    if _kind(tool, *_HOSTED):
        return True
    environment = getattr(tool, "environment", None)
    return _kind(tool, "ShellTool") and isinstance(environment, dict) and environment.get("type", "local") != "local"


def _asks(setting: Any, name: str) -> bool:
    """Whether a hosted MCP tool's own require_approval would ask before a call to the MCP
    tool name, as OpenAI applies it: always unless it's "never", and under a filter unless the
    filter's never names the tool. Left out, OpenAI asks. A filter on tools' read-only hints
    can't be applied here, so a call it would cover counts as asking."""
    if setting == "never":
        return False
    if not isinstance(setting, Mapping):
        return True
    always, never = setting.get("always"), setting.get("never")
    if isinstance(always, Mapping) and name in (always.get("tool_names") or ()):
        return True
    if not isinstance(never, Mapping) or never.get("read_only") is not None:
        return True
    return name not in (never.get("tool_names") or ())


class _Stopped(NamedTuple):
    """A tool call or handoff refused as it started: the refusal, the call's id when the SDK
    gives it, and the agent a handoff would have started."""

    refusal: stop.Refusal
    call: str | None = None
    receiver: str | None = None


class _RefusingEditor:
    """An apply_patch editor that refuses each change a halt covers, answering with why."""

    def __init__(self, processor: "WakeTracingProcessor", editor: Any, owner: str, name: str) -> None:
        self.editor = editor
        for operation in ("create_file", "update_file", "delete_file"):
            change = getattr(editor, operation, None)
            if callable(change):
                setattr(self, operation, processor._refusing(
                    change, owner, name,
                    lambda op, *_, **__: ({"type": getattr(op, "type", None), "path": getattr(op, "path", None),
                                           "diff": getattr(op, "diff", None)}, None),
                    lambda refused: {"status": "failed", "output": refused.reason}))

    def __getattr__(self, name: str) -> Any:
        if name == "editor":
            raise AttributeError(name)
        return getattr(self.editor, name)


class WakeTracingProcessor(EventGraph):
    """An OpenAI Agents SDK TracingProcessor that records Wake events."""

    def __init__(self, recorder: Recorder, quarantine: "Quarantine | None" = None) -> None:
        super().__init__(recorder)
        self._quarantine = quarantine
        self._guardrail = None  # the tool input guardrail guard() adds, made once
        self._halted = _Recent()  # sdk span id of a tool call or handoff -> what refused it as it started
        self._instances = _Recent()  # instance -> its agent and lineage, for each Wake has seen start
        self._agent_tools = _Recent()  # (agent, tool name) -> the agent a call to that agent tool runs
        self._run_agent = _Recent()  # run -> the agent it has reached, which answers for it
        self._parent = _Recent()  # sdk span id -> its parent's sdk span id
        self._agent_span = _Recent()  # sdk span id of an agent span -> agent name
        # A run is told apart from other runs of its agent, and from other copies of it, by
        # its first agent span: the agent spans of one run, after handoffs too, share a
        # parent, the task span the SDK opens for each run. Its steps are linked, and their
        # content matched, within the run.
        self._runs = _Recent()  # sdk span id of an agent span -> its run, the run's first agent span id
        self._firsts = _Recent()  # (sdk trace id, parent span id) -> the first agent span under it
        self._runs_in = _Recent()  # sdk span id of a tool call -> the runs inside it
        self._run_last = _Recent()  # run -> its latest response event id
        self._begun = _Recent()  # (trace, run) whose first response is recorded
        self._pending = _Recent()  # sdk span id -> what on_span_start set up
        self._call = _Recent()  # (trace, run, tool name, arguments) -> (call id, response event id)
        self._event_by_call = _Recent()  # call id -> tool call event id
        self._returned_by_event = _Recent()  # tool call event id -> events its output came from
        self._event_by_item = _Recent()  # response or output item id -> response event id
        self._last_response = _Recent()  # (trace, run, agent) -> response event id
        self._handed_off = _Recent()  # (trace, run, agent) -> handoff event id, feeding its next step
        self._seen_user = _Recent()  # (trace, run, digest) for user input already seen
        self._roots = _Recent(limit=1024)  # sdk trace id -> the workflow's root span
        self._linked = _Recent(limit=1024)  # sdk trace id -> what started it, feeding each of its runs' first response
        self._spans = _Recent(limit=10_000)  # sdk span id -> its open span
        self._tool_agents = _Recent()  # tool call event id -> the agent making it
        self._tool_instances = _Recent()  # tool call event id -> the instance making it, and its lineage
        self._tool_runs = _Recent()  # tool call event id -> the run making it
        self._tools_since = _Recent()  # (trace, run, agent) -> tool calls since its last response, when inputs are hidden
        self._agent_data = _Recent()  # sdk span id of an agent span -> its data, which names the tools offered each call
        self._hosted_names = _Recent()  # agent guard() covers -> its hosted tools but hosted MCP, by name
        self._shells = _Recent()  # agent guard() covers -> its shell tool's name, and whether it runs at OpenAI

    # TracingProcessor.

    @never_raise
    def on_trace_start(self, trace_obj) -> None:
        # One root per workflow, so every agent in it, handoffs included, shares a trace. A
        # workflow run inside a recorded tool call, or for a message from another agent,
        # joins that trace, and its first response descends from it.
        link = links.current()
        root = self._recorder.tracer.start_span(
            f"workflow {trace_obj.name}",
            context=self._event_context(),
            attributes={"wake.openai_agents.workflow": trace_obj.name or ""},
        )
        with self._lock:
            self._roots[trace_obj.trace_id] = root
            if link is not None:
                self._linked[trace_obj.trace_id] = list(link.parents)

    @never_raise
    def on_trace_end(self, trace_obj) -> None:
        with self._lock:
            root = self._roots.pop(trace_obj.trace_id, None)
        if root is not None:
            root.end()

    @never_raise
    def on_span_start(self, span) -> None:
        with self._lock:
            self._parent[span.span_id] = span.parent_id
        data = span.span_data
        kind = data.type
        if kind not in ("agent", "response", "generation", "function", "handoff"):
            return
        attributes = {}
        if kind == "agent":
            caller = None
            with self._lock:
                self._agent_span[span.span_id] = data.name
                self._agent_data[span.span_id] = data
                # The first agent span under a parent starts a run; the rest are its next
                # agents, after a handoff.
                run = self._firsts.get((span.trace_id, span.parent_id))
                if run is None:
                    run = self._firsts[(span.trace_id, span.parent_id)] = span.span_id
                    tool = self._tool_above(span.parent_id)
                    if tool is not None:
                        self._runs_in[tool[0]] = [*(self._runs_in.get(tool[0]) or []), run]
                        caller = self._tool_agents.get(tool[1])
                self._runs[span.span_id] = run
                self._run_agent[run] = data.name
                instance, lineage = self._identity(span.span_id)
                if instance:
                    self._instances[instance] = (data.name, lineage)
            if caller:
                # A run started inside a tool call, such as an agent used as a tool: what had
                # entered the agent calling it enters it, with its input (wake.flow).
                flow.message(caller, data.name)
            attributes = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": data.name,
                          events.AGENT_ID: data.name, **events.instance_attributes(instance, lineage)}
        otel = self._recorder.tracer.start_span(f"{kind} {getattr(data, 'name', '') or ''}".strip(), context=self._context(span), attributes=attributes)
        with self._lock:
            self._spans[span.span_id] = otel
        if kind == "function":
            # Calls the tool makes, such as memory writes, link back to this event. The SDK
            # runs the tool in the context this callback runs in.
            event_id = str(uuid.uuid4())
            ctx = otel.get_span_context()
            link = links._enter(links.Link(event_id, ctx.trace_id, ctx.span_id))
            # Its arguments aren't set yet, so the responses that asked for a tool of this
            # name stand in as its parents until it ends, for a memory write it makes.
            agent = self._agent_for(span)
            with self._lock:
                run = self._run_of(span)
                instance = self._identity(self._agent_above(span.parent_id))
                self._pending[span.span_id] = {"event_id": event_id, "link": link}
                self._tool_agents[event_id] = agent
                self._tool_runs[event_id] = run
                if instance[0]:
                    self._tool_instances[event_id] = instance
                requested_by = [r for (t, ru, n, _), (_, r) in self._call.items()
                                if t == span.trace_id and ru == run and n == data.name]
                if not requested_by and self._last_response.get((span.trace_id, run, agent)):
                    # Without sensitive data no call names it: the agent's latest response did.
                    requested_by = [self._last_response[(span.trace_id, run, agent)]]
            self._remember_lineage(event_id, list(dict.fromkeys(requested_by)), [f"tool:{data.name}"])

    @never_raise
    def on_span_end(self, span) -> None:
        data = span.span_data
        handler = {
            "agent": self._agent_ended,
            "response": self._response_ended,
            "generation": self._generation_ended,
            "function": self._function_ended,
            "handoff": self._handoff_ended,
        }.get(data.type)
        if handler is not None:
            handler(span, data)

    def shutdown(self) -> None:
        self._recorder.flush()

    def force_flush(self) -> None:
        self._recorder.flush()

    # Cooperative stops.

    def guard(self, agent):
        """Has the brake's halts stop this agent, and every agent it reaches as a tool or by a
        handoff, in this process, and returns it. Each function tool, agent used as a tool and
        MCP server gets a tool input guardrail that refuses a call a halt covers before it
        runs. Each tool the agent runs in its own process refuses such a call as it starts: a
        shell, a custom tool or an editor answers with the refusal, and a computer, whose
        output is a screenshot that can't carry it, raises AgentHalted, which the SDK ends the
        run with. Each handoff is offered only while neither the agent offering it nor the one
        it hands off to is halted, and if a halt lands after the model picked it, it raises
        AgentHalted as it starts. Bare agents in handoffs become the handoff the SDK would make
        of them. A tool the agent would run itself that Wake can't make refuse raises
        TypeError, rather than run unstopped. The flow policy served with the halts refuses
        the same calls and handoffs the same ways (wake.flow).

        Hosted tools run at OpenAI, inside the model call. Each model call of an agent that
        offers hosted tools other than hosted MCP is checked first, by the agent's hooks
        (on_llm_start, the agent's own still called): when a halt or the flow policy holds back
        any of them, the call isn't made, and AgentHalted ends the run. Each hosted MCP tool is
        made to ask before every call, and Wake answers: see _answer_mcp. One whose calls the
        app approves itself, through run interruptions, is left as it is."""
        from agents import Agent, FunctionTool, Handoff, handoff
        from agents.tool_guardrails import ToolInputGuardrail

        if self._quarantine is None:
            raise ValueError("guard() needs the brake's rules: install(recorder, quarantine=Quarantine(...))")
        if self._guardrail is None:
            self._guardrail = ToolInputGuardrail(guardrail_function=self._check_tool_call, name="wake_halts")
        stop.stops_with(self._quarantine)
        seen: set[int] = set()
        todo = [agent]
        while todo:
            current = todo.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            tools, hosted = [], []
            for tool in current.tools:
                if isinstance(tool, FunctionTool):
                    if self._guardrail not in (tool.tool_input_guardrails or []):
                        tool.tool_input_guardrails = [*(tool.tool_input_guardrails or []), self._guardrail]
                    if getattr(tool, "_is_agent_tool", False) and getattr(tool, "_agent_instance", None) is not None:
                        todo.append(tool._agent_instance)
                        # A call to it starts that agent's run, which the flow policy checks too.
                        self._agent_tools[(current.name, tool.name)] = tool._agent_instance.name
                elif _kind(tool, "HostedMCPTool"):
                    self._guard_hosted_mcp(tool, current.name)
                elif _hosted(tool):
                    hosted.append(tool)
                else:
                    tool = self._guard_local(tool, current.name)
                tools.append(tool)
            current.tools = tools
            shell = next((t for t in tools if _kind(t, "ShellTool")), None)
            with self._lock:
                # What a model call of it may run at OpenAI, for when the trace doesn't say, by
                # any agent of its name.
                self._hosted_names[current.name] = self._hosted_names.get(current.name, frozenset()) | {
                    t.name for t in hosted}
                if shell is not None:
                    self._shells[current.name] = (shell.name, _hosted(shell))
            if hosted:
                self._guard_model_calls(current)
            for server in getattr(current, "mcp_servers", None) or []:
                if hasattr(server, "tool_input_guardrails") and self._guardrail not in (server.tool_input_guardrails or []):
                    server.tool_input_guardrails = [*(server.tool_input_guardrails or []), self._guardrail]
            handoffs = []
            for item in current.handoffs:
                if isinstance(item, Agent):
                    todo.append(item)
                    item = handoff(item)
                elif isinstance(item, Handoff) and item._agent_ref is not None and item._agent_ref() is not None:
                    todo.append(item._agent_ref())
                handoffs.append(self._guard_handoff(item, current.name))
            current.handoffs = handoffs
        return agent

    def _guard_local(self, tool, owner: str):
        """A tool the agent runs in its own process, made to refuse a call a halt covers as it
        starts. owner is the agent guard() found it on, which the call is checked as when no
        span says who's calling."""
        if _kind(tool, "LocalShellTool"):
            tool.executor = self._refusing(
                tool.executor, owner, "local_shell", lambda request, *_, **__: _shell_call(request), lambda refused: refused.reason)
        elif _kind(tool, "ShellTool"):
            tool.executor = self._refusing(tool.executor, owner, tool.name, lambda request, *_, **__: _shell_call(request), _refused_shell)
        elif _kind(tool, "CustomTool"):
            tool.on_invoke_tool = self._refusing(
                tool.on_invoke_tool, owner, tool.name,
                lambda context, text, *_, **__: (text, getattr(context, "tool_call_id", None)), lambda refused: refused.reason)
        elif _kind(tool, "ApplyPatchTool"):
            if not isinstance(tool.editor, _RefusingEditor):
                tool.editor = _RefusingEditor(self, tool.editor, owner, tool.name)
        elif _kind(tool, "ComputerTool"):
            return self._guard_computer(tool, owner)
        else:
            raise TypeError(f"guard() can't refuse calls to {type(tool).__name__}, which would run in the agent's "
                            "process, so it can't stop this agent")
        return tool

    def _guard_computer(self, tool, owner: str):
        """A computer tool whose computer refuses each action a halt covers, raising
        AgentHalted. One made for each run, by a function or a provider, is made refusing, in
        a new tool, since the SDK keeps the one it was given."""
        from agents import AsyncComputer, Computer

        computer = tool.computer
        if isinstance(computer, (Computer, AsyncComputer)):
            self._refusing_computer(computer, owner, tool.name)
            return tool
        if getattr(computer, "_wake", False):
            return tool
        provider = callable(getattr(computer, "create", None))
        create = computer.create if provider else computer
        if not callable(create):
            raise TypeError("guard() can't refuse this computer tool's actions: it has no computer it can wrap")

        def made(*args, **kwargs):
            new = create(*args, **kwargs)
            if inspect.isawaitable(new):
                async def refusing():
                    return self._refusing_computer(await new, owner, tool.name)

                return refusing()
            return self._refusing_computer(new, owner, tool.name)

        wrapped = types.SimpleNamespace(create=made, dispose=getattr(computer, "dispose", None)) if provider else made
        wrapped._wake = True
        return dataclasses.replace(tool, computer=wrapped)

    def _refusing_computer(self, computer, owner: str, name: str):
        """The computer, each action it carries out for the SDK refused while a halt covers
        whoever is calling: it raises AgentHalted instead."""
        if getattr(computer, "_wake", False):
            return computer
        try:
            for action in _COMPUTER_ACTIONS:
                method = getattr(computer, action, None)
                if callable(method):
                    setattr(computer, action, self._refusing(
                        method, owner, name, lambda *_, action=action, **__: ({"action": action}, None), _raise))
            computer._wake = True
        except (AttributeError, TypeError) as e:
            raise TypeError(f"guard() can't refuse the actions of {type(computer).__name__}: {e}") from e
        return computer

    def _refusing(self, run, owner: str, name: str, describe, answer):
        """run, refusing a call a halt covers as it starts: answer(refusal) is what it returns
        instead. describe(*args) gives the call's arguments and id, for its record."""
        if getattr(run, "_wake", False):
            return run

        @functools.wraps(run)
        def refusing(*args, **kwargs):
            try:
                arguments, call_id = describe(*args, **kwargs)
            except Exception:
                arguments, call_id = None, None
            refused = self._refuse_call(None, name, arguments, call_id, owner=owner)
            if refused is not None:
                return answer(refused)
            # What it returns enters the agent calling it (wake.flow). The SDK opens no span for
            # some of these, such as a local shell, so it's noted here.
            flow.note(self._caller(None, owner), f"tool:{name}")
            return run(*args, **kwargs)

        refusing._wake = True  # type: ignore[attr-defined]
        return refusing

    def _guard_handoff(self, item, owner: str):
        """The handoff, offered only while neither the agent offering it nor the one it hands
        off to is halted, and refused as it starts, raising AgentHalted, if a halt has landed
        since."""
        enabled = item.is_enabled
        if not getattr(enabled, "_wake", False):
            async def is_enabled(context, agent) -> bool:
                source = getattr(agent, "name", None) or self._recorder.agent
                if self._refusal(source, target=item.agent_name) is not None:
                    return False
                if isinstance(enabled, bool):
                    return enabled
                result = enabled(context, agent)
                return bool(await result) if inspect.isawaitable(result) else bool(result)

            is_enabled._wake = True  # type: ignore[attr-defined]
            item.is_enabled = is_enabled
        invoke = item.on_invoke_handoff
        if not getattr(invoke, "_wake", False):
            async def on_invoke_handoff(context, arguments):
                self._refuse_handoff(item.agent_name, owner)
                return await invoke(context, arguments)

            on_invoke_handoff._wake = True  # type: ignore[attr-defined]
            item.on_invoke_handoff = on_invoke_handoff
        return item

    def _guard_model_calls(self, agent) -> None:
        """Has each model call of agent check first the hosted tools it offers, other than
        hosted MCP, through the agent's hooks: on_llm_start raises AgentHalted, before the
        call is made, when any is held back (_check_model_call), then calls the agent's own
        on_llm_start. Hooks that can't be given that raise TypeError, rather than leave the
        model calls unchecked."""
        from agents import AgentHooks

        hooks = agent.hooks if agent.hooks is not None else AgentHooks()
        start = getattr(hooks, "on_llm_start", None)
        if getattr(start, "_wake", False):
            agent.hooks = hooks
            return
        owner = agent.name

        async def on_llm_start(context, agent, system_prompt, input_items):
            self._check_model_call(agent, owner)
            if callable(start):
                started = start(context, agent, system_prompt, input_items)
                if inspect.isawaitable(started):
                    await started

        on_llm_start._wake = True  # type: ignore[attr-defined]
        try:
            hooks.on_llm_start = on_llm_start
        except (AttributeError, TypeError) as e:
            raise TypeError(f"guard() can't check the model calls of {owner}, whose hosted tools run at OpenAI: "
                            f"its hooks, {type(hooks).__name__}, can't be wrapped ({e})") from e
        agent.hooks = hooks

    def _check_model_call(self, agent, owner: str) -> None:
        """Before a model call of agent: checks each hosted tool it offers, other than hosted
        MCP, as a call by the agent to a tool of its name, halts first, then the flow policy.
        They run inside the call, with no check of their own, so if any is held back, this
        raises AgentHalted and the call isn't made. Each held back is recorded as denied, under
        the agent's run. A check that fails refuses."""
        name = getattr(agent, "name", None) or owner
        span = _current_span()
        held = None
        for tool in list(getattr(agent, "tools", None) or []):
            if not _hosted(tool) or _kind(tool, "HostedMCPTool"):
                continue
            tool_name = getattr(tool, "name", None) or type(tool).__name__
            refused = self._refusal(name, span, tool=tool_name,
                                    outcome=f"its model call, which could run {tool_name} at OpenAI, wasn't made")
            if refused is None:
                continue
            try:
                if getattr(span, "span_id", None) is not None:
                    self._refused_alone(span, name, tool_name, None, None, refused)
            except Exception:
                # Refused all the same; only its record may say less.
                log.warning("wake: failed to record a refused model call", exc_info=True)
                failed(self._recorder, "a refused model call")
            log.warning("wake: refused a model call of %s, which could run %s at OpenAI: %s", name, tool_name,
                        refused.halt)
            held = held or refused
        if held is not None:
            raise stop.AgentHalted(held)

    def _guard_hosted_mcp(self, tool, owner: str) -> None:
        """A hosted MCP tool, made to ask before every call so that Wake answers each
        (_answer_mcp). owner is the agent guard() found it on, which a call is checked as when
        no span says who's calling. A tool whose own setting asks before some calls and that
        has no callback is approved by the app, through run interruptions: Wake can't answer
        for a person, or interrupt, so it's left as it is."""
        callback = tool.on_approval_request
        if getattr(callback, "_wake", False):
            return
        config = tool.tool_config if isinstance(tool.tool_config, Mapping) else {}
        setting = config.get("require_approval")
        if callback is None and setting != "never":
            return

        async def on_approval_request(request):
            return await self._answer_mcp(request, owner, setting, callback)

        on_approval_request._wake = True  # type: ignore[attr-defined]
        tool.tool_config = {**config, "require_approval": "always"}
        tool.on_approval_request = on_approval_request

    async def _answer_mcp(self, request, owner: str, setting: Any, callback) -> dict[str, Any]:
        """The answer to a hosted MCP tool's request to make a call: rejected, with why, when a
        halt covers the agent making it or the flow policy holds the agent back from the MCP
        tool, by its own name, and recorded as denied. Otherwise the tool's own callback's
        answer, when its own setting would have asked before this call, or else approved. Once
        approved, tool:<MCP tool> enters the agent: the call's result comes back in the next
        response, and noting it now doesn't depend on the trace."""
        data = getattr(request, "data", None)
        name = _field(data, "name") or "mcp"
        agent = self._caller(None, owner)
        refused = self._refuse_call(agent, name, _field(data, "arguments"), _field(data, "id"))
        if refused is not None:
            return {"approve": False, "reason": refused.reason}
        if callback is not None and _asks(setting, name):
            answer = callback(request)
            if inspect.isawaitable(answer):
                answer = await answer
        else:
            answer = {"approve": True}
        if _field(answer, "approve"):
            flow.note(agent, f"tool:{name}")
        return answer

    def _check_tool_call(self, data):
        """The guardrail: refuses a call a halt covers, telling the model why."""
        from agents.tool_guardrails import ToolGuardrailFunctionOutput

        context = getattr(data, "context", None)
        refused = self._refuse_call(getattr(getattr(data, "agent", None), "name", None) or self._recorder.agent,
                                    getattr(context, "tool_name", None) or "tool", getattr(context, "tool_arguments", None),
                                    getattr(context, "tool_call_id", None))
        if refused is None:
            return ToolGuardrailFunctionOutput.allow()
        return ToolGuardrailFunctionOutput.reject_content(refused.reason, output_info={"halt": refused.halt})

    def _refuse_call(self, agent: str | None, name: str, arguments: Any, call_id: str | None, *,
                     owner: str | None = None) -> "stop.Refusal | None":
        """The refusal of a tool call about to run, if a halt covers who's making it: agent, as
        the SDK says, or else the agent whose run the SDK's current span is in, or owner. Or if
        the flow policy holds them back from it, or, for an agent used as a tool, from
        reaching that agent. It's noted on the call's span, to be recorded as denied when that
        ends, or recorded as denied now if the SDK opened none for it, as for a local shell.
        Never raises: a check that fails refuses."""
        if self._quarantine is None:
            return None
        span = _current_span()
        try:
            agent = agent or self._agent_at(getattr(span, "span_id", None)) or owner or self._recorder.agent
            with self._lock:
                starts = self._agent_tools.get((agent, name))
            refused = self._refusal(agent, span, tool=name, starts=starts)
        except Exception:
            log.warning("wake: couldn't check the halts for a tool call; refusing it", exc_info=True)
            agent, refused = agent or owner or self._recorder.agent, stop.unchecked()
        if refused is None:
            return None
        try:
            sid = getattr(span, "span_id", None)
            with self._lock:
                noted = sid is not None and sid in self._pending
                if noted:
                    self._halted[sid] = _Stopped(refused, call_id)
            if not noted and sid is not None:
                self._refused_alone(span, agent, name, arguments, call_id, refused)
        except Exception:
            # Refused all the same; only its record may say less.
            log.warning("wake: failed to record a refused tool call", exc_info=True)
            failed(self._recorder, "a refused tool call")
        log.warning("wake: refused a %s call of %s: %s", name, agent, refused.halt)
        return refused

    def _refuse_handoff(self, target: str, owner: str) -> None:
        """Raises AgentHalted, noting the refusal on the handoff's span to be recorded as
        denied, if a halt covers the agent handing off or the one it would hand off to."""
        span = _current_span()
        data = getattr(span, "span_data", None)
        source = getattr(data, "from_agent", None) or owner
        refused = self._refusal(source, span, target=target)
        if refused is None:
            return
        try:
            sid = getattr(span, "span_id", None)
            with self._lock:
                if getattr(data, "type", None) == "handoff" and sid in self._spans:
                    self._halted[sid] = _Stopped(refused, receiver=target)
        except Exception:
            failed(self._recorder, "a refused handoff")
        log.warning("wake: refused a handoff of %s to %s: %s", source, target, refused.halt)
        raise stop.AgentHalted(refused)

    def _refusal(self, agent: str, span: Any = None, *, tool: str | None = None, target: str | None = None,
                 starts: str | None = None, outcome: str | None = None) -> "stop.Refusal | None":
        """The refusal of what a run of agent is about to do, if a halt covers it: by its name,
        the instance the run is and those that started it. With target, a handoff to target is
        refused too when a halt covers it, or the instance of it the handoff would start in
        the same run. The run is the one span is in, by default the SDK's current span, as for
        its events. While Wake can't place it, a halt on any run of the agent Wake has seen
        refuses it.

        Then the flow policy (wake.flow), on what entered agent: a call to tool, and the run of
        starts it begins, for an agent used as a tool; or a handoff to target. A rule for any
        agent a halt here could name applies. outcome is what the refusal says didn't happen,
        by default the tool call or the handoff. Never raises: a check that fails refuses."""
        if self._quarantine is None:
            return None
        outcome = outcome or (f"the handoff to {target} wasn't made" if target else "this tool call wasn't run")
        try:
            span = span if span is not None else _current_span()
            sid = getattr(span, "span_id", None)
            names, unplaced, who, run = {self._recorder.agent, agent}, set(), agent, None
            with self._lock:
                agent_sid = self._agent_above(sid) if sid is not None else None
                if agent_sid is not None and self._agent_span.get(agent_sid) == agent:
                    instance, lineage = self._identity(agent_sid)
                    names.update(lineage)
                    if instance:
                        names.add(instance)
                        who, run = instance, self._runs.get(agent_sid)
                else:
                    agent_sid = None
                    for instance, (name, lineage) in self._instances.items():
                        if name == agent:
                            unplaced.update((instance, *lineage))
                if target:
                    # A handoff's target runs where its source does: in a tool call's run, as
                    # an instance of that run.
                    target_names = {target, f"{target}#{run}"} if run else {target}
                    target_unplaced = {i for i, (name, _) in self._instances.items() if name == target} if agent_sid is None else set()
            refused = stop.refusal(self._quarantine, who, names, unplaced=unplaced, outcome=outcome)
            if refused is None and target:
                refused = stop.refusal(self._quarantine, f"{target}#{run}" if run else target, target_names,
                                       unplaced=target_unplaced, outcome=outcome)
            if refused is None and target:
                refused = flow.refusal(self._quarantine, who, agent, names | unplaced, receiver=target, outcome=outcome)
            elif refused is None and tool:
                refused = flow.refusal(self._quarantine, who, agent, names | unplaced, tool=tool, outcome=outcome)
                if refused is None and starts:
                    refused = flow.refusal(self._quarantine, who, agent, names | unplaced, receiver=starts,
                                           outcome=outcome)
        except Exception:
            log.warning("wake: couldn't check the halts for %s; refusing", agent, exc_info=True)
            refused = stop.unchecked(outcome)
        return refused

    def _refused_alone(self, span, agent: str, name: str, arguments: Any, call_id: str | None, refused) -> None:
        """Records a tool call refused where the SDK opened no span for it, such as a local
        shell's: denied, under its agent's run, the response that asked for it its parent."""
        sid = span.span_id
        with self._lock:
            run = self._run_at(sid)
            requested = self._call.get((span.trace_id, run, name, arguments)) if isinstance(arguments, str) else None
            parent = requested[1] if requested else self._last_response.get((span.trace_id, run, agent))
            context = self._context_at(sid, span.trace_id)
        otel = self._recorder.tracer.start_span(f"execute_tool {name}", context=context)
        event_id = str(uuid.uuid4())
        self._event(otel, types.SimpleNamespace(parent_id=sid, error=None), event_id, events.DENIED,
                    f"execute_tool {name}", agent, [parent] if parent else [], [],
                    {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "wake.brake.halt_id": refused.halt},
                    {"input": _plain(arguments), "output": refused.reason, **flow.payload(refused)})
        with self._lock:
            if call_id:
                self._event_by_call[call_id] = event_id
        self._remember_output((otel.get_span_context().trace_id, run), event_id, refused.reason)
        otel.end()

    # Spans.

    def _agent_ended(self, span, data) -> None:
        otel = self._take(span)
        if otel is not None:
            otel.update_name(f"invoke_agent {data.name}")
            otel.end()

    def _response_ended(self, span, data) -> None:
        # The SDK ends the response span before any tool from that response runs, so what its
        # hosted tools brought in is noted before anything that follows from it is checked.
        self._note_hosted(span, data)
        response = data.response
        output = list(getattr(response, "output", None) or [])
        self._model_call(
            span,
            model=getattr(response, "model", "") or "",
            input_items=data.input,
            output_items=output,
            response_id=getattr(response, "id", None),
        )

    def _note_hosted(self, span, data) -> None:
        """Notes that each hosted tool call in a response brought its result into the agent
        whose response it is, as tool:<name>, now (wake.flow): a hosted MCP call by its MCP
        tool's own name. Without sensitive data the response isn't on the span, so every hosted
        tool the agent offered that call, but hosted MCP, is noted instead. Never raises: when
        the response can't be read, that's what's noted too."""
        agent = self._agent_for(span)
        try:
            response = getattr(data, "response", None)
            names = self._offered_hosted(span, agent) if response is None else self._hosted_calls(
                agent, list(getattr(response, "output", None) or []))
        except Exception:
            log.warning("wake: couldn't read which hosted tools a response ran; noting every one offered",
                        exc_info=True)
            try:
                names = self._offered_hosted(span, agent)
            except Exception:
                failed(self._recorder, "hosted tool results")
                return
        for name in dict.fromkeys(names):
            flow.note(agent, f"tool:{name}")

    def _hosted_calls(self, agent: str, output: list) -> list[str]:
        """The hosted tools a response's output items say ran, by name. A shell call ran at
        OpenAI when the agent's shell is hosted, as guard() found it, or, for an agent it
        didn't, when the call names a hosted environment or the response holds its output."""
        with self._lock:
            shell = self._shells.get(agent)
        ran_there = {_field(i, "call_id") for i in output if _field(i, "type") == "shell_call_output"}
        names = []
        for item in output:
            kind = _field(item, "type")
            if kind in _HOSTED_CALLS:
                names.append(_HOSTED_CALLS[kind])
            elif kind == "mcp_call" and _field(item, "name"):
                names.append(_field(item, "name"))
            elif kind == "shell_call":
                environment = _field(_field(item, "environment"), "type")
                hosted = shell[1] if shell is not None else (
                    environment not in (None, "local") or _field(item, "call_id") in ran_there)
                if hosted:
                    names.append(shell[0] if shell is not None else "shell")
        return names

    def _offered_hosted(self, span, agent: str) -> list[str]:
        """The hosted tools, but hosted MCP, the agent offered the model call span is, by name:
        those guard() found on it, or for an agent it didn't, those of the SDK's own hosted
        tools' names the agent's span says it offered."""
        with self._lock:
            agent_sid = self._agent_above(span.parent_id)
            data = self._agent_data.get(agent_sid) if agent_sid is not None else None
            known = self._hosted_names.get(agent)
        offered = getattr(data, "tools", None)
        offered = set(offered) if isinstance(offered, list) else None
        if known is not None:
            return sorted(n for n in known if offered is None or n in offered)
        return [n for n in _HOSTED_CALLS.values() if offered is not None and n in offered]

    def _generation_ended(self, span, data) -> None:
        # Chat Completions models report generations rather than responses.
        self._model_call(span, model=data.model or "", input_items=data.input, output_items=list(data.output or []), response_id=None)

    def _model_call(self, span, *, model, input_items, output_items, response_id) -> None:
        trace_key = span.trace_id
        agent = self._agent_for(span)
        # The SDK leaves the input out without sensitive data, and the output with it.
        hidden = input_items is None
        items = input_items if isinstance(input_items, list) else ([{"role": "user", "content": input_items}] if input_items else [])
        parents: list[str] = []
        origins: list[str] = []
        with self._lock:
            run = self._run_of(span)
            first = (trace_key, run) not in self._begun
            self._begun[(trace_key, run)] = True
            # A run inside a recorded tool call, such as an agent used as a tool, shares the
            # outer trace, so no trace starts for it. It descends from the tool call: its
            # first response has the call as a parent, and its input isn't the user's. A run
            # of the workflow's own descends from what started the workflow, if anything did.
            tool = self._enclosing_tool(span)
            linked = self._linked.get(trace_key) if first and tool is None else None
            if linked:
                parents += linked
            if first and tool is not None:
                parents.append(tool[1])
            handoff = self._handed_off.pop((trace_key, run, agent), None)
            if handoff:
                parents.append(handoff)
            if hidden:
                # The SDK's loop says what this response read: the agent's last response
                # and the tool results since, and what they came from. Its first reads the
                # workflow's input.
                last = self._last_response.get((trace_key, run, agent))
                if last:
                    parents.append(last)
                for tool_event in self._tools_since.pop((trace_key, run, agent), None) or []:
                    parents += [tool_event, *self._returned_by_event.get(tool_event, [])]
                if not (linked or tool or handoff or last):
                    origins.append(events.ORIGIN_USER)
            for item in items:
                item_type = _field(item, "type")
                call_id = _field(item, "call_id") or (_field(item, "tool_call_id") if _field(item, "role") == "tool" else None)
                if (_is_result(item_type) or _field(item, "role") == "tool") and call_id in self._event_by_call:
                    tool_event = self._event_by_call[call_id]
                    parents.append(tool_event)
                    parents += self._returned_by_event.get(tool_event, [])
                elif _field(item, "id") in self._event_by_item:
                    parents.append(self._event_by_item[_field(item, "id")])
                elif _field(item, "role") == "user":
                    # A user's message is new content the first time a model sees it in a run.
                    # In a linked workflow, or a run inside a tool call, the input came from
                    # what started it.
                    digest = hashlib.sha256(_text(item).encode()).hexdigest()
                    if (trace_key, run, digest) not in self._seen_user:
                        self._seen_user[(trace_key, run, digest)] = True
                        if not linked and tool is None and events.ORIGIN_USER not in origins:
                            origins.append(events.ORIGIN_USER)
        otel = self._take(span)
        if otel is None:
            return
        within = (otel.get_span_context().trace_id, run)
        input_text = _text(items)
        parents += self._matching_outputs(within, input_text) if input_text else []
        event_id = str(uuid.uuid4())
        self._event(otel, span, event_id, events.MODEL_CALL, f"chat {model}".strip(), agent, parents, origins, {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model,
        }, {"input": _plain(items), "output": _plain(output_items)})
        with self._lock:
            self._last_response[(trace_key, run, agent)] = event_id
            self._run_last[run] = event_id
            if response_id:
                self._event_by_item[response_id] = event_id
            for item in output_items:
                if _field(item, "id"):
                    self._event_by_item[_field(item, "id")] = event_id
                if _field(item, "type") == "function_call":
                    key = (trace_key, run, _field(item, "name"), _field(item, "arguments"))
                    self._call[key] = (_field(item, "call_id"), event_id)
                # Chat Completions models, LiteLLM's included, report an assistant message
                # with its tool calls.
                for requested in _field(item, "tool_calls") or []:
                    function = _field(requested, "function") or {}
                    key = (trace_key, run, _field(function, "name"), _field(function, "arguments"))
                    self._call[key] = (_field(requested, "id"), event_id)
        self._remember_output(within, event_id, _text(output_items))
        otel.end()

    def _function_ended(self, span, data) -> None:
        with self._lock:
            pending = self._pending.pop(span.span_id, None) or {}
            # What the runs inside the tool call, such as an agent used as a tool, said last is
            # what the tool returns.
            runs = self._runs_in.pop(span.span_id, None) or []
            ran = [self._run_last[r] for r in runs if r in self._run_last]
            answering = [self._run_agent[r] for r in runs if r in self._run_agent]
            refused = span.span_id in self._halted
        returned = [*links._returned(pending.get("link")), *ran]
        links._exit(pending.get("link"))
        event_id = pending.get("event_id") or str(uuid.uuid4())
        if returned:
            # What the tool got back came from these events, such as an A2A reply: they
            # feed the response that reads its output.
            with self._lock:
                self._returned_by_event[event_id] = returned
        agent = self._agent_for(span)
        if not refused:
            # What it returned enters the agent that called it (wake.flow), and what each run
            # inside it said last brings what entered that run's agent. A hosted tool, such as
            # web search, runs at OpenAI with no span of its own: its result is noted as the
            # response holding it comes in (_note_hosted).
            flow.note(agent, f"tool:{data.name}")
            for answered in answering:
                flow.message(answered, agent)
        otel = self._take(span)
        if otel is None:
            return
        parents: list[str] = []
        with self._lock:
            run = self._run_of(span)
            call = self._call.pop((span.trace_id, run, data.name, data.input), None)
            handoff = self._handed_off.pop((span.trace_id, run, agent), None)
            stopped = self._halted.pop(span.span_id, None)
        within = (otel.get_span_context().trace_id, run)
        if call:
            call_id, requested_by = call
            parents.append(requested_by)
            with self._lock:
                if call_id:
                    self._event_by_call[call_id] = event_id
        elif isinstance(data.input, str) and data.input:
            # No exact match: the arguments themselves point at the response that chose them.
            parents += self._outputs_containing(within, [data.input])
        elif data.input is None:
            # Without sensitive data the SDK leaves the arguments out, but a tool only runs
            # from its agent's latest response, and its result feeds the agent's next one.
            with self._lock:
                since = self._tools_since.get((span.trace_id, run, agent)) or []
                self._tools_since[(span.trace_id, run, agent)] = [*since, event_id]
        if not parents:
            # A call no response names, as a shell's: it ran from its agent's latest response.
            with self._lock:
                last = self._last_response.get((span.trace_id, run, agent))
            parents += [last] if last else []
        if handoff:
            parents.insert(0, handoff)
        mcp = data.mcp_data if isinstance(getattr(data, "mcp_data", None), dict) else None
        attributes = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": data.name}
        if mcp and mcp.get("server"):
            attributes["wake.mcp.server"] = str(mcp["server"])
        kind, origins, output = events.TOOL_CALL, [f"tool:{data.name}"], _plain(data.output)
        refusal: dict[str, Any] = {}
        if stopped:
            # Refused in the agent's own process, under the halt or the flow policy: what the
            # agent tried. The tool never ran, so nothing of it entered.
            kind, origins, attributes["wake.brake.halt_id"] = events.DENIED, [], stopped.refusal.halt
            output = stopped.refusal.reason if output is None else output
            refusal = flow.payload(stopped.refusal)
            with self._lock:
                if stopped.call:
                    self._event_by_call[stopped.call] = event_id
        self._event(otel, span, event_id, kind, f"execute_tool {data.name}", agent, parents,
                    origins, attributes, {"input": data.input, "output": output, **refusal})
        self._remember_output(within, event_id, output if isinstance(output, str) else _text(output))
        otel.end()

    def _handoff_ended(self, span, data) -> None:
        source = data.from_agent or "unknown"
        with self._lock:
            run = self._run_of(span)
            requested_by = self._last_response.get((span.trace_id, run, source))
            stopped = self._halted.pop(span.span_id, None)
        if not stopped and data.from_agent and data.to_agent:
            # What had entered the agent handing off enters the one it hands off to (wake.flow).
            flow.message(data.from_agent, data.to_agent)
        otel = self._take(span)
        if otel is None:
            return
        event_id = str(uuid.uuid4())
        if stopped:
            # Refused as it started, under the halt or the flow policy: the attempt to hand the
            # target its task, in the name of the agent that tried. Nothing reached the target.
            target = stopped.receiver or data.to_agent or "unknown"
            self._event(otel, span, event_id, events.DENIED, f"agent_message {source} -> {target}", source,
                        [requested_by] if requested_by else [], [],
                        {"gen_ai.agent.name": target, "wake.brake.halt_id": stopped.refusal.halt, "wake.brake.receiver": target},
                        {"from": source, "to": target, "output": stopped.refusal.reason, **flow.payload(stopped.refusal)})
            otel.end()
            return
        target = data.to_agent or "unknown"
        self._event(otel, span, event_id, events.AGENT_MESSAGE, f"agent_message {source} -> {target}", target,
                    [requested_by] if requested_by else [], [f"agent:{source}"],
                    {"gen_ai.agent.name": target}, {"from": source, "to": target})
        with self._lock:
            self._handed_off[(span.trace_id, run, target)] = event_id
        otel.end()

    # Content matching, within a run: what each output is kept under says which run it was
    # made in, so another run's steps never match it. A memory read or write is matched in the
    # run whose tool call made it.

    def _remember_output(self, trace_id, event_id: str, text: str) -> None:
        super()._remember_output(self._within(trace_id), event_id, text)

    def _matching_outputs(self, trace_id, text: str) -> list[str]:
        return super()._matching_outputs(self._within(trace_id), text)

    def _outputs_containing(self, trace_id, arguments: list[str]) -> list[str]:
        return super()._outputs_containing(self._within(trace_id), arguments)

    def _within(self, key) -> tuple:
        """The trace id the memory adapter gives, with the run whose tool call is running."""
        if isinstance(key, tuple):
            return key
        link = links.current()
        with self._lock:
            run = self._tool_runs.get(link.event_id) if link is not None else None
        return key, run

    # Helpers.

    def _enclosing_tool(self, span) -> tuple[str, str] | None:
        """The recorded tool call a span runs inside, as its sdk span id and event id, if
        any. Call with the lock held."""
        return self._tool_above(span.parent_id)

    def _tool_above(self, sid) -> tuple[str, str] | None:
        """The recorded tool call at or above sdk span sid, as its sdk span id and event id.
        Call with the lock held."""
        while sid is not None:
            pending = self._pending.get(sid)
            if pending is not None:
                return sid, pending["event_id"]
            sid = self._parent.get(sid)
        return None

    def _run_of(self, span) -> str | None:
        """The run a span is a step of, by its first agent span, or None outside any agent's
        run. Call with the lock held."""
        return self._run_at(span.parent_id)

    def _run_at(self, sid) -> str | None:
        """The run of the agent span at or above sdk span sid. Call with the lock held."""
        agent_sid = self._agent_above(sid)
        return self._runs.get(agent_sid) if agent_sid is not None else None

    def _agent_above(self, sid) -> str | None:
        """The agent span at or above sdk span sid. Call with the lock held."""
        while sid is not None:
            if sid in self._agent_span:
                return sid
            sid = self._parent.get(sid)
        return None

    def _agent_at(self, sid) -> str | None:
        """The agent whose run sdk span sid is in, if Wake has seen it start."""
        with self._lock:
            agent_sid = self._agent_above(sid)
            return self._agent_span.get(agent_sid) if agent_sid is not None else None

    def _caller(self, agent: str | None, owner: str | None = None) -> str:
        """Who is making a call: agent, as the SDK says, or else the agent whose run the SDK's
        current span is in, or owner. Never raises."""
        try:
            return agent or self._agent_at(getattr(_current_span(), "span_id", None)) or owner or self._recorder.agent
        except Exception:
            return agent or owner or self._recorder.agent

    def _run(self, agent_sid) -> tuple[str | None, tuple[str, ...]]:
        """The run an agent span is a step of, by its first agent span, when it runs inside a
        recorded tool call, and the lineage that started it: the agent that made the call,
        after its own lineage. None for a run of the workflow's own. Call with the lock held."""
        if agent_sid is None:
            return None, ()
        tool = self._tool_above(self._parent.get(agent_sid))
        if tool is None:
            return None, ()
        outer = self._agent_above(self._parent.get(tool[0]))
        outer_instance, outer_lineage = self._identity(outer)
        outer_name = self._agent_span.get(outer) if outer is not None else self._recorder.agent
        return self._runs.get(agent_sid, agent_sid), (*outer_lineage, outer_instance or outer_name)

    def _identity(self, agent_sid) -> tuple[str | None, tuple[str, ...]]:
        """The instance an agent span's run is, <agent>#<the run's first agent span id>, and
        the lineage that started it. Call with the lock held."""
        run, lineage = self._run(agent_sid)
        if run is None:
            return None, ()
        return f"{self._agent_span[agent_sid]}#{run}", lineage

    def _agent_for(self, span) -> str:
        with self._lock:
            sid = span.parent_id
            while sid is not None:
                if sid in self._agent_span:
                    return self._agent_span[sid]
                sid = self._parent.get(sid)
        return self._recorder.agent

    def _current_agent(self) -> str:
        """The agent whose tool call is running, for a memory write it makes."""
        link = links.current()
        if link is not None:
            with self._lock:
                agent = self._tool_agents.get(link.event_id)
            if agent:
                return agent
        return self._recorder.agent

    def _current_instance(self) -> "tuple[str | None, tuple[str, ...]]":
        """The instance whose tool call is running, for a memory write it makes."""
        link = links.current()
        if link is not None:
            with self._lock:
                instance = self._tool_instances.get(link.event_id)
            if instance:
                return instance
        return None, ()

    def _context(self, span):
        """Under the closest enclosing span Wake opened, else the workflow's root."""
        with self._lock:
            return self._context_at(span.parent_id, span.trace_id)

    def _context_at(self, sid, trace_id):
        """Under the span Wake opened for sdk span sid or the closest above it, else the
        workflow's root. Call with the lock held."""
        while sid is not None:
            if sid in self._spans:
                return trace.set_span_in_context(self._spans[sid])
            sid = self._parent.get(sid)
        root = self._roots.get(trace_id)
        return trace.set_span_in_context(root) if root is not None else None

    def _take(self, span):
        with self._lock:
            return self._spans.pop(span.span_id, None)

    def _event(self, otel, span, event_id, kind, name, agent, parents, origins, attributes, payload) -> None:
        parents = list(dict.fromkeys(parents))
        self._remember_lineage(event_id, parents, origins)
        with self._lock:
            # In the run the span is in. A handoff is recorded in its target's name, and the
            # target runs where its source did: the same run, as an instance of its own.
            run, lineage = self._run(self._agent_above(span.parent_id))
        otel.update_name(name)
        otel.set_attributes({
            events.EVENT_ID: event_id,
            events.EVENT_KIND: kind,
            events.AGENT_ID: agent,
            events.CAPTURE: "sdk",
            events.PARENTS: parents,
            events.ORIGINS: origins,
            **events.instance_attributes(f"{agent}#{run}" if run else None, lineage),
            **attributes,
        })
        if span.error:
            payload = {**payload, "error": _plain(span.error)}
            otel.set_status(Status(StatusCode.ERROR, str(_field(span.error, "message") or "error")))
        otel.set_attributes(events.payload_attributes(payload))
        ctx = otel.get_span_context()
        links._note(links.Link(event_id, ctx.trace_id, ctx.span_id))


def _shell_call(request: Any) -> tuple[Any, str | None]:
    """A shell call's arguments and id, from the request the SDK hands a shell's executor."""
    data = getattr(request, "data", None)
    action = getattr(data, "action", None)
    return _plain(dataclasses.asdict(action) if dataclasses.is_dataclass(action) else action), getattr(data, "call_id", None)


def _refused_shell(refused: stop.Refusal):
    """What a refused shell call answers, as the SDK answers one its approval rejects."""
    from agents import ShellCallOutcome, ShellCommandOutput, ShellResult

    return ShellResult(output=[ShellCommandOutput(stderr=refused.reason, outcome=ShellCallOutcome(type="exit", exit_code=1))])


def _raise(refused: stop.Refusal):
    raise stop.AgentHalted(refused)
