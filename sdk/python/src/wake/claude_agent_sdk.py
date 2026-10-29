"""Records Claude Agent SDK runs: model calls, tool calls and subagents.

    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, query
    from wake import Recorder
    from wake.claude_agent_sdk import WakeClaudeAgent

    claude = WakeClaudeAgent(Recorder(agent="support"))
    crm = create_sdk_mcp_server("crm", tools=claude.tools([lookup_order]))
    options = claude.options(ClaudeAgentOptions(mcp_servers={"crm": crm}))
    async for message in claude.record(query(prompt=prompt, options=options), prompt=prompt):
        ...

The SDK runs Claude Code in a subprocess and streams its messages back, and Wake reads
that stream as it passes. Each assistant message is a model call, and each tool use and
its result a tool call. A subagent started with the Agent tool is an agent of its own,
named <agent>/<subagent type>: one agent_message hands it the task once it has started,
and another carries its answer back, when the tool returns or, for a subagent left
running in the background, when Claude Code says it has finished. Several of one type can
run at once, so each is also an instance, <agent>/<subagent type>#<the Agent tool use that
started it>, and its events name the instance and the lineage that started it. Links are
exact, from the SDK's ids: a tool call's parent is the model call that asked for it, and a
model call's parents are the call before it and the tool results and messages new in its
context.

options() adds hooks that open each tool call before it runs, and tools() wraps
in-process tools so they run with their event current: an MCP or A2A call they make
through the proxy, or a memory write, links back to the tool call. Tools Claude Code runs
itself, such as Bash, WebFetch or an external MCP server, are recorded from the stream
but can't carry the link.

Given the brake's rules (WakeClaudeAgent(recorder, quarantine=Quarantine(...))), the
hooks also stop the agent, or one subagent instance of it, in its own process: a tool
call a halt covers is refused before it runs, and recorded as denied, an Agent tool call
too, which then starts no subagent. That reaches what the proxy deny can't, such as Bash,
and subagents that call with their parent's credentials, which the proxy can't tell from
their parent. It's a cooperative stop: it holds only while the agent's code does, and a
compromised agent can leave it out.
"""

import dataclasses
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator
from functools import wraps
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from wake import events, links, stop
from wake._graph import EventGraph, _Recent, failed, never_raise
from wake.recorder import Recorder

if TYPE_CHECKING:
    from wake.quarantine import Quarantine

log = logging.getLogger("wake")

# The tool Claude Code starts a subagent with; older versions call it Task.
SUBAGENT_TOOLS = frozenset({"Agent", "Task"})

# What the Agent tool's result says of a subagent it left running in the background.
LAUNCHED = frozenset({"async_launched", "remote_launched"})


def _canonical(value: Any) -> str:
    return events.dumps(value)


def _short_name(name: str) -> str:
    """An in-process tool's own name: Claude sees it as mcp__<server>__<name>."""
    parts = name.split("__", 2)
    return parts[2] if len(parts) == 3 and parts[0] == "mcp" else name


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(t for t in (_text(c) for c in content) if t)
    if isinstance(content, dict):
        return content["text"] if isinstance(content.get("text"), str) else ""
    return getattr(content, "text", "") or ""


def _context(span) -> Any:
    return trace.set_span_in_context(span) if span is not None else None


class _Thread:
    """One agent's conversation in a run: the main agent's, or a subagent's."""

    def __init__(self, agent: str, span, parents: list[str], origins: list[str], given: list[dict],
                 instance: str | None = None, lineage: tuple[str, ...] = ()) -> None:
        self.agent = agent
        self.instance = instance  # a subagent's own instance; the main agent has none
        self.lineage = lineage  # the agent and instances that started it, outermost first
        self.span = span  # its events nest under this span
        self.call: _Call | None = None  # the model call streaming now
        self.last_call: str | None = None
        self.parents = parents  # what feeds its next model call
        self.origins = origins
        self.given = given  # what's new in its next model call's context
        self.waiting_since = time.time_ns()  # when it last handed control to the model
        self.prompt = given[0]["content"] if given else None
        self.spawn: _Spawn | None = None  # a subagent's start, until it's known to have started
        self.background = False  # a subagent the Agent tool left running in the background

    def started(self, agent: str, instance: str, span, parents: list[str], given: list[dict]) -> "_Thread":
        """The thread of an instance this one starts."""
        return _Thread(agent, span, parents, [], given, instance, (*self.lineage, self.instance or self.agent))


class _Spawn:
    """An Agent tool use, until its subagent is known to have started. One refused before
    then, as by a halt, starts no subagent, and is recorded as the refused tool call it is."""

    def __init__(self, starter: _Thread, tool: str, arguments: Any, parents: list[str]) -> None:
        self.starter = starter  # the thread that asked for it
        self.tool = tool
        self.arguments = arguments
        self.parents = parents  # the model call that asked for it, and what fed it
        self.event_id = str(uuid.uuid4())  # the agent_message handing the subagent its task
        self.at = time.time_ns()


class _Call:
    def __init__(self, event_id: str, message_id: str | None, span) -> None:
        self.event_id = event_id
        self.message_id = message_id
        self.span = span
        self.model = ""
        self.output: list[dict] = []
        self.usage: dict | None = None
        self.parents: list[str] = []
        self.origins: list[str] = []
        self.given: list[dict] = []
        self.found: list[str] = []  # server tool events earlier in this response
        self.server_tools: list[str] = []  # server tool uses still waiting for a result
        self.error: str | None = None
        self.ended_at = time.time_ns()


class _Tool:
    def __init__(self, event_id: str, span, name: str, arguments: Any, agent: str, run: "_Run | None") -> None:
        self.event_id = event_id
        self.span = span
        ctx = span.get_span_context()
        self.link = links.Link(event_id, ctx.trace_id, ctx.span_id)
        self.name = name
        self.arguments = arguments
        self.agent = agent
        self.instance: str | None = None
        self.lineage: tuple[str, ...] = ()
        self.run = run
        self.parents: list[str] = []
        self.error: str | None = None
        self.ended_at: int | None = None
        self.frame = None  # the in-process tool's link frame, once it runs
        self.halted: str | None = None  # the halt its call was refused under


class _Run:
    """One record() call: a prompt and everything the agent and its subagents did for it."""

    def __init__(self, root, main: _Thread) -> None:
        self.root = root
        self.threads: dict[str | None, _Thread] = {None: main}  # by the Agent tool use that started each
        self.handoffs: dict[str, str] = {}  # Agent tool use id -> its handoff event
        self.tools: set[str] = set()  # tool use ids opened in this run
        self.session_id: str | None = None


class WakeClaudeAgent(EventGraph):
    """Records one Claude agent's runs, and the subagents it starts."""

    def __init__(self, recorder: Recorder, quarantine: "Quarantine | None" = None) -> None:
        """With quarantine, the brake's rules for this agent (wake.quarantine), a tool call by
        the agent, or by a subagent instance of it, that a halt covers is refused before it
        runs, and recorded as denied. It trusts the agent's code: see cooperative stops in
        the README."""
        super().__init__(recorder)
        self._quarantine = quarantine
        self._tools: dict[str, _Tool] = _Recent()  # tool use id -> its open tool call
        self._waiting = _Recent()  # (tool name, arguments) -> tool use ids an in-process tool may be
        self._tool_agents = _Recent()  # tool call event id -> the agent making it
        self._tool_instances = _Recent()  # tool call event id -> the subagent instance making it, and its lineage
        self._agent_ids = _Recent()  # tool use id -> the agent_id its hook named, for a subagent's
        self._subagents = _Recent()  # a hook's agent_id -> the names of the instance it is: itself, then its lineage
        self._spawned = _Recent()  # Agent tool use id -> its session, and the agent, instance and lineage it started
        self._halted = _Recent()  # tool use id -> the halt a hook refused it under
        self._ambiguous = _Recent()  # tool call event id -> True: an in-process call Wake can't tell from another
        self._sessions = _Recent(limit=1024)  # session id -> its run now
        self._last_calls = _Recent(limit=1024)  # session id -> its last model call, for the next turn
        self._active: list[_Run] = []

    # Setup.

    def options(self, options=None):
        """These options, with hooks that open each tool call before it runs."""
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

        options = options if options is not None else ClaudeAgentOptions()
        hooks = {event: list(matchers) for event, matchers in (options.hooks or {}).items()}
        for event, callback in (
            ("PreToolUse", self._pre_tool_use),
            ("PostToolUse", self._post_tool_use),
            ("PostToolUseFailure", self._post_tool_use),
        ):
            hooks.setdefault(event, []).append(HookMatcher(hooks=[callback]))
        return dataclasses.replace(options, hooks=hooks)

    def tools(self, tools: list) -> list:
        """These in-process tools, made to run with their tool call's event current."""
        return [dataclasses.replace(t, handler=self._linked(t.name, t.handler)) for t in tools]

    async def record(self, messages: AsyncIterable, *, prompt: str | None = None) -> AsyncIterator:
        """Yields the messages unchanged, recording them as they pass. Pass the prompt
        that started them so the first model call knows what it was given."""
        run = self._start_run(prompt)
        try:
            async for message in messages:
                self._observe(run, message)
                yield message
        finally:
            self._end_run(run)

    # Hooks.

    async def _pre_tool_use(self, hook_input, tool_use_id, context) -> dict:
        self._hook_opened(hook_input, tool_use_id)
        refused = self._halting(hook_input, tool_use_id)
        if refused is None:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": refused.reason}}

    async def _post_tool_use(self, hook_input, tool_use_id, context) -> dict:
        self._hook_closed(hook_input, tool_use_id)
        return {}

    @never_raise
    def _hook_opened(self, hook_input, tool_use_id) -> None:
        name = hook_input.get("tool_name") or ""
        agent_id = hook_input.get("agent_id")
        if tool_use_id and agent_id:
            with self._lock:
                self._agent_ids[tool_use_id] = agent_id
        if not tool_use_id or name in SUBAGENT_TOOLS:
            return
        run = self._run_for(hook_input.get("session_id"))
        if run is None:
            return  # the stream opens it instead
        agent_type = hook_input.get("agent_type") if agent_id else None
        agent = f"{self._recorder.agent}/{agent_type}" if agent_type else self._recorder.agent
        tool = self._open_tool(run, tool_use_id, name, hook_input.get("tool_input"), agent, run.root)
        if not agent_id:
            return
        with self._lock:
            if tool.instance:
                # The stream said first which instance asked for it.
                self._subagents[agent_id] = (tool.instance, *tool.lineage)
            elif known := self._subagents.get(agent_id):
                # Its agent_id is placed already, by its earlier calls or the Agent tool's result.
                tool.instance, tool.lineage = known[0], tuple(known[1:])
                self._tool_instances[tool.event_id] = (tool.instance, tool.lineage)

    # Cooperative stops.

    def _halting(self, hook_input, tool_use_id) -> "stop.Refusal | None":
        """The halt that stops this tool call, and why, if one covers who's making it: the
        agent, the subagent type it runs as, and the instance it is and those that started it.
        A subagent's instance is known by the agent_id its hooks name, from its earlier calls
        or the Agent tool's result. Before that, Wake can't tell which instance is calling: it
        could be any the session started, or one started by any of them that the stream hasn't
        shown yet, since hooks can run ahead of the code reading it. So the call is refused if a
        halt covers any of them. A check that fails refuses the call too."""
        if self._quarantine is None:
            return None
        who = self._recorder.agent
        try:
            agent_id = hook_input.get("agent_id")
            agent_type = hook_input.get("agent_type") if agent_id else None
            agent = f"{self._recorder.agent}/{agent_type}" if agent_type else self._recorder.agent
            names = {self._recorder.agent, agent}
            who = agent
            with self._lock:
                known = self._subagents.get(agent_id) if agent_id else None
            if known:
                names.update(known)
                who = known[0]
            unplaced = self._instances(hook_input.get("session_id")) if agent_id and not known else ()
            refused = stop.refusal(self._quarantine, who, names, unplaced=unplaced)
        except Exception:
            log.warning("wake: couldn't check the halts for a tool call; refusing it", exc_info=True)
            refused = stop.unchecked()
        if refused is None:
            return None
        try:
            with self._lock:
                if tool_use_id:
                    self._halted[tool_use_id] = refused.halt
                tool = self._tools.get(tool_use_id) if tool_use_id else None
            if tool is not None:
                tool.halted = refused.halt
        except Exception:
            # Refused all the same; only its record may say less.
            failed(self._recorder, "_halting")
        log.warning("wake: refused a tool call of %s: %s", who, refused.halt)
        return refused

    def _instances(self, session_id: str | None) -> set[str]:
        """Every instance Wake has seen start in this session, and what started each."""
        with self._lock:
            return {name for session, _, instance, lineage in self._spawned.values()
                    if session in (session_id, None) for name in (instance, *lineage)}

    @never_raise
    def _hook_closed(self, hook_input, tool_use_id) -> None:
        with self._lock:
            tool = self._tools.get(tool_use_id) if tool_use_id else None
        if tool is not None:
            tool.ended_at = time.time_ns()
            if hook_input.get("hook_event_name") == "PostToolUseFailure":
                tool.error = str(hook_input.get("error") or "error")

    # In-process tools.

    def _linked(self, name: str, handler):
        @wraps(handler)
        async def run_with_link(arguments):
            frame = self._enter(name, arguments)
            try:
                return await handler(arguments)
            finally:
                links._exit(frame)

        return run_with_link

    @never_raise
    def _enter(self, name: str, arguments: Any):
        key = (name, _canonical(arguments))
        with self._lock:
            waiting = self._waiting.get(key) or deque()
            alike = [t for t in (self._tools.get(i) for i in waiting) if t is not None and not t.halted]
            if len(alike) > 1 and not (alike[0].instance and all(t.instance == alike[0].instance for t in alike)):
                # Calls alike, by instances that differ or aren't known yet, and Claude Code may
                # run them in either order: which is which is a guess, so none names an instance.
                for t in alike:
                    self._ambiguous[t.event_id] = True
            tool = None
            while waiting and tool is None:
                tool = self._tools.get(waiting.popleft())
                if tool is not None and tool.halted:
                    tool = None  # refused, so it never runs
        if tool is None:
            return None
        tool.frame = links._enter(tool.link)
        return tool.frame

    # The message stream.

    @never_raise
    def _start_run(self, prompt: str | None) -> _Run:
        agent = self._recorder.agent
        link = links.current()
        root = self._recorder.tracer.start_span(
            f"invoke_agent {agent}",
            context=self._event_context(),
            attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent, events.AGENT_ID: agent},
        )
        parents: list[str] = []
        origins: list[str] = []
        if link is not None:
            # Started inside a recorded tool call, or for a message from another agent,
            # which gave it the prompt.
            parents += link.parents
        elif prompt is not None:
            origins.append(events.ORIGIN_USER)
        if prompt:
            parents += self._matching_outputs(root.get_span_context().trace_id, prompt)
        given = [{"role": "user", "content": prompt}] if prompt is not None else []
        run = _Run(root, _Thread(agent, root, parents, origins, given))
        with self._lock:
            self._active.append(run)
        return run

    @never_raise
    def _observe(self, run: _Run | None, message) -> None:
        if run is None:
            return
        session_id = getattr(message, "session_id", None)
        if isinstance(message, SystemMessage):
            session_id = message.data.get("session_id")
        if session_id and run.session_id is None:
            self._join_session(run, session_id)
        if isinstance(message, AssistantMessage):
            self._assistant(run, message)
        elif isinstance(message, UserMessage):
            self._user(run, message)
        elif isinstance(message, ResultMessage):
            for thread in run.threads.values():
                self._end_call(thread)
            run.root.set_attributes({"wake.claude.session_id": message.session_id, "wake.claude.turns": message.num_turns})
            if message.is_error:
                run.root.set_status(Status(StatusCode.ERROR, message.subtype))
        elif isinstance(message, SystemMessage) and message.subtype == "task_notification":
            self._finished(run, message.data)

    def _join_session(self, run: _Run, session_id: str) -> None:
        run.session_id = session_id
        run.root.set_attribute("wake.claude.session_id", session_id)
        with self._lock:
            self._sessions[session_id] = run
            previous = self._last_calls.get(session_id)
        main = run.threads[None]
        if previous and main.last_call is None and main.call is None:
            # A later turn of the same conversation: the earlier turns are in its context.
            main.parents.insert(0, previous)

    def _assistant(self, run: _Run, message: AssistantMessage) -> None:
        thread = self._thread(run, message.parent_tool_use_id)
        call = thread.call
        if call is not None and (message.message_id is None or call.message_id != message.message_id):
            self._end_call(thread)
            call = None
        if call is None:
            call = self._start_call(thread, message.message_id)
        call.model = message.model or call.model
        call.usage = message.usage or call.usage
        call.ended_at = time.time_ns()
        if message.error:
            call.error = message.error
        for block in message.content:
            if isinstance(block, TextBlock):
                call.output.append({"type": "text", "text": block.text})
            elif isinstance(block, ThinkingBlock):
                call.output.append({"type": "thinking", "thinking": block.thinking})
            elif isinstance(block, ToolUseBlock):
                call.output.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
                if block.name in SUBAGENT_TOOLS:
                    self._hand_off(run, thread, call, block)
                else:
                    self._requested(run, thread, [call.event_id, *call.found], block.id, block.name, block.input)
            elif isinstance(block, ServerToolUseBlock):
                call.output.append({"type": "server_tool_use", "id": block.id, "name": block.name, "input": block.input})
                self._requested(run, thread, [call.event_id, *call.found], block.id, block.name, block.input,
                                server=True)
                call.server_tools.append(block.id)
            elif isinstance(block, (ServerToolResultBlock, ToolResultBlock)):
                call.output.append({"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content})
                tool = self._finish_tool(block.tool_use_id, block.content, getattr(block, "is_error", None))
                if tool is not None:
                    # Run inside this response: it feeds the rest of it, and the next call.
                    call.found.append(tool.event_id)
                    thread.parents.append(tool.event_id)

    def _user(self, run: _Run, message: UserMessage) -> None:
        thread = self._thread(run, message.parent_tool_use_id)
        self._end_call(thread)
        content = message.content
        blocks = [TextBlock(text=content)] if isinstance(content, str) else content
        # The tool's own output, beside the text the model reads, when the message answers one
        # tool use, as Claude Code's do.
        results = [b for b in blocks if isinstance(b, ToolResultBlock)]
        output = getattr(message, "tool_use_result", None) if len(results) == 1 else None
        for block in blocks:
            if isinstance(block, ToolResultBlock):
                item = {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content}
                if block.is_error:
                    item["is_error"] = True
                thread.given.append(item)
                sub = run.threads.get(block.tool_use_id)
                if sub is not None and sub.spawn is not None:
                    with self._lock:
                        halted = block.tool_use_id in self._halted
                    if halted or block.is_error:
                        # Refused before the subagent started, as by a halt: it never ran.
                        self._never_started(run, block.tool_use_id)
                        sub = None
                if sub is not None:
                    self._answered(run, thread, sub, block, output)
                    continue
                tool = self._finish_tool(block.tool_use_id, block.content, block.is_error)
                if tool is not None:
                    # The tool, and whatever its result came from, such as an A2A reply.
                    thread.parents.append(tool.event_id)
                    thread.parents += links._returned(tool.frame)
            elif isinstance(block, TextBlock):
                if block.text == thread.prompt:
                    continue  # the prompt it started with, echoed back
                thread.given.append({"role": "user", "content": block.text})
                if thread is run.threads[None] and events.ORIGIN_USER not in thread.origins:
                    thread.origins.append(events.ORIGIN_USER)
        thread.waiting_since = time.time_ns()

    def _thread(self, run: _Run, parent_tool_use_id: str | None) -> _Thread:
        thread = run.threads.get(parent_tool_use_id)
        if thread is not None:
            if thread.spawn is not None:
                self._deliver(thread)  # its first message: it has started
            return thread
        with self._lock:
            spawned = self._spawned.get(parent_tool_use_id) if parent_tool_use_id else None
        if spawned is not None:
            # A subagent started earlier, such as in an earlier turn, and running on in the
            # background: still the same instance.
            _, agent, instance, lineage = spawned
            thread = _Thread(agent, None, [], [], [], instance, lineage)
            thread.background = True
        else:
            # A subagent whose start Wake didn't see.
            agent = f"{self._recorder.agent}/subagent"
            thread = run.threads[None].started(agent, f"{agent}#{parent_tool_use_id}", None, [], [])
        thread.span = self._invoke(thread, run.root)
        run.threads[parent_tool_use_id] = thread
        return thread

    # Events.

    def _start_call(self, thread: _Thread, message_id: str | None) -> _Call:
        event_id = str(uuid.uuid4())
        span = self._recorder.tracer.start_span("chat", context=_context(thread.span), start_time=thread.waiting_since)
        call = _Call(event_id, message_id, span)
        call.parents = list(dict.fromkeys(([thread.last_call] if thread.last_call else []) + thread.parents))
        call.origins, call.given = thread.origins, thread.given
        thread.parents, thread.origins, thread.given = [], [], []
        thread.call = call
        self._remember_lineage(event_id, call.parents, call.origins)
        return call

    def _end_call(self, thread: _Thread) -> None:
        call = thread.call
        if call is None:
            return
        thread.call = None
        thread.last_call = call.event_id
        for tool_use_id in call.server_tools:
            # The SDK passes on few server tool results; the response's end is the tool's.
            tool = self._finish_tool(tool_use_id, None, None)
            if tool is not None:
                thread.parents.append(tool.event_id)
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": call.model,
            "gen_ai.response.model": call.model,
        }
        if call.message_id:
            attributes["gen_ai.response.id"] = call.message_id
        usage = call.usage or {}
        for key, attribute in (("input_tokens", "gen_ai.usage.input_tokens"), ("output_tokens", "gen_ai.usage.output_tokens")):
            if isinstance(usage.get(key), int):
                attributes[attribute] = usage[key]
        payload: dict[str, Any] = {"model": call.model, "input": call.given, "output": call.output}
        if call.error:
            payload["error"] = call.error
        self._emit(call.span, call.event_id, events.MODEL_CALL, f"chat {call.model}".strip(), thread.agent,
                   call.parents, call.origins, attributes, payload, error=call.error, who=thread)
        self._remember_output(call.span.get_span_context().trace_id, call.event_id, _text(call.output))
        call.span.end(end_time=call.ended_at)

    def _requested(self, run, thread, parents, tool_use_id, name, arguments, *, server=False) -> None:
        tool = self._open_tool(run, tool_use_id, name, arguments, thread.agent, thread.span, server=server)
        tool.agent, tool.instance, tool.lineage = thread.agent, thread.instance, thread.lineage
        tool.parents = list(dict.fromkeys(parents))
        with self._lock:
            self._tool_agents[tool.event_id] = thread.agent
            if thread.instance:
                self._tool_instances[tool.event_id] = (thread.instance, thread.lineage)
                agent_id = self._agent_ids.get(tool_use_id)
                if agent_id:
                    # Its hook came first, naming the subagent by agent_id: now it's placed.
                    self._subagents[agent_id] = (thread.instance, *thread.lineage)
        self._remember_lineage(tool.event_id, tool.parents, [f"tool:{name}"])

    def _open_tool(self, run, tool_use_id, name, arguments, agent, parent_span, *, server=False) -> _Tool:
        with self._lock:
            tool = self._tools.get(tool_use_id)
        if tool is not None:
            return tool  # the hook opened it before the stream got here
        event_id = str(uuid.uuid4())
        attributes = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "gen_ai.tool.call.id": tool_use_id}
        span = self._recorder.tracer.start_span(f"execute_tool {name}", context=_context(parent_span), attributes=attributes)
        tool = _Tool(event_id, span, name, arguments, agent, run)
        self._remember_lineage(event_id, [], [f"tool:{name}"])
        with self._lock:
            tool.halted = self._halted.get(tool_use_id)  # its hook refused it before the stream got here
            self._tools[tool_use_id] = tool
            self._tool_agents[event_id] = agent
            if not server:
                key = (_short_name(name), _canonical(arguments))
                waiting = self._waiting.get(key)
                if waiting is None:
                    waiting = self._waiting[key] = deque(maxlen=64)
                waiting.append(tool_use_id)
        if run is not None:
            run.tools.add(tool_use_id)
        return tool

    def _finish_tool(self, tool_use_id: str, output: Any, is_error: bool | None) -> _Tool | None:
        with self._lock:
            tool = self._tools.pop(tool_use_id, None)
        if tool is None:
            return None
        error = tool.error or ("error" if is_error else None)
        attributes = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool.name}
        if tool.name.startswith("mcp__") and tool.name.count("__") >= 2:
            attributes["wake.mcp.server"] = tool.name.split("__", 2)[1]
        payload: dict[str, Any] = {"input": tool.arguments, "output": output}
        if error:
            payload["error"] = tool.error or _text(output) or error
        kind, origins = events.TOOL_CALL, [f"tool:{tool.name}"]
        if tool.halted:
            # Refused in the agent's own process, under the halt: what the agent tried. The
            # tool never ran, so nothing of it entered.
            kind, origins, attributes["wake.brake.halt_id"] = events.DENIED, [], tool.halted
        self._emit(tool.span, tool.event_id, kind, f"execute_tool {tool.name}", tool.agent,
                   tool.parents, origins, attributes, payload, error=error, who=tool)
        self._remember_output(tool.span.get_span_context().trace_id, tool.event_id, _text(output))
        tool.span.end(end_time=tool.ended_at)
        return tool

    def _hand_off(self, run: _Run, thread: _Thread, call: _Call, block: ToolUseBlock) -> None:
        parents = list(dict.fromkeys([call.event_id] + call.found))
        with self._lock:
            refused = block.id in self._halted
        if refused:
            # Its hook refused it before the stream got here: no subagent starts.
            self._requested(run, thread, parents, block.id, block.name, block.input)
            return
        arguments = block.input if isinstance(block.input, dict) else {}
        target = f"{self._recorder.agent}/{arguments.get('subagent_type') or 'general-purpose'}"
        prompt = arguments.get("prompt")
        spawn = _Spawn(thread, block.name, block.input, parents)
        given = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else []
        sub = thread.started(target, f"{target}#{block.id}", None, [spawn.event_id], given)
        # The handoff is recorded once the subagent has started, by its first message or the
        # tool's result: a hook may yet refuse it.
        sub.spawn = spawn
        run.threads[block.id] = sub
        run.handoffs[block.id] = spawn.event_id
        with self._lock:
            self._spawned[block.id] = (run.session_id, target, sub.instance, sub.lineage)

    def _deliver(self, sub: _Thread) -> None:
        """Records the agent_message handing a subagent its task, now that it has started."""
        spawn, sub.spawn = sub.spawn, None
        if spawn is None:
            return
        starter, arguments = spawn.starter, spawn.arguments if isinstance(spawn.arguments, dict) else {}
        name = f"agent_message {starter.agent} -> {sub.agent}"
        span = self._recorder.tracer.start_span(name, context=_context(starter.span), start_time=spawn.at)
        self._emit(span, spawn.event_id, events.AGENT_MESSAGE, name, sub.agent, spawn.parents,
                   [f"agent:{starter.agent}"], {"gen_ai.agent.name": sub.agent},
                   {"from": starter.agent, "to": sub.agent, "description": arguments.get("description"),
                    "prompt": arguments.get("prompt")},
                   who=sub)
        span.end(end_time=spawn.at)
        sub.span = self._invoke(sub, starter.span, start_time=spawn.at)

    def _invoke(self, sub: _Thread, parent_span, start_time: int | None = None):
        """The span a subagent's events nest under."""
        return self._recorder.tracer.start_span(
            f"invoke_agent {sub.agent}",
            context=_context(parent_span),
            start_time=start_time,
            attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": sub.agent,
                        events.AGENT_ID: sub.agent, **events.instance_attributes(sub.instance, sub.lineage)},
        )

    def _never_started(self, run: _Run, tool_use_id: str) -> None:
        """Opens an Agent tool call whose subagent never started, refused before it did, as the
        tool call it was: no handoff, and a denied event when a halt refused it."""
        sub = run.threads.pop(tool_use_id)
        run.handoffs.pop(tool_use_id, None)
        with self._lock:
            self._spawned.pop(tool_use_id, None)
        spawn = sub.spawn
        self._requested(run, spawn.starter, spawn.parents, tool_use_id, spawn.tool, spawn.arguments)

    def _answered(self, run: _Run, thread: _Thread, sub: _Thread, block: ToolResultBlock, output: Any) -> None:
        """The Agent tool's result: the subagent's answer, or that it runs on in the background."""
        output = output if isinstance(output, dict) else {}
        agent_id = output.get("agentId")
        if isinstance(agent_id, str) and agent_id:
            # The id its hooks name it by: its calls are placed from here on, whatever the
            # stream has shown.
            with self._lock:
                self._subagents[agent_id] = (sub.instance, *sub.lineage)
        self._deliver(sub)
        if output.get("status") in LAUNCHED:
            # It works on, and answers when Claude Code says it has finished.
            sub.background = True
            return
        self._returned(run, thread, block.tool_use_id, block.content)

    def _finished(self, run: _Run, data: dict) -> None:
        """A task Claude Code ran in the background has ended: for a subagent, its answer."""
        tool_use_id = data.get("tool_use_id")
        sub = run.threads.get(tool_use_id) if tool_use_id else None
        if sub is None or not sub.background:
            return
        # The answer goes to what started it, or to the agent once that's gone.
        starter = next((t for t in list(run.threads.values()) if t.instance and t.instance == sub.lineage[-1]),
                       run.threads[None])
        self._returned(run, starter, tool_use_id, data.get("summary"))

    def _returned(self, run: _Run, thread: _Thread, tool_use_id: str, content: Any) -> None:
        sub = run.threads.pop(tool_use_id)
        self._end_call(sub)
        sub.span.end()
        source = sub.last_call or run.handoffs.get(tool_use_id)
        event_id = str(uuid.uuid4())
        span = self._recorder.tracer.start_span(f"agent_message {sub.agent} -> {thread.agent}", context=_context(thread.span))
        self._emit(span, event_id, events.AGENT_MESSAGE, f"agent_message {sub.agent} -> {thread.agent}", thread.agent,
                   [source] if source else [], [f"agent:{sub.agent}"], {"gen_ai.agent.name": thread.agent},
                   {"from": sub.agent, "to": thread.agent, "output": content}, who=thread)
        self._remember_output(span.get_span_context().trace_id, event_id, _text(content))
        span.end()
        thread.parents.append(event_id)

    def _emit(self, span, event_id, kind, name, agent, parents, origins, attributes, payload, *, error=None,
              who: "_Thread | _Tool | None" = None) -> None:
        parents = list(dict.fromkeys(parents))
        self._remember_lineage(event_id, parents, origins)
        span.update_name(name)
        span.set_attributes({
            events.EVENT_ID: event_id,
            events.EVENT_KIND: kind,
            events.AGENT_ID: agent,
            events.CAPTURE: "sdk",
            events.PARENTS: parents,
            events.ORIGINS: origins,
            **(events.instance_attributes(who.instance, who.lineage) if who is not None else {}),
            **attributes,
        })
        span.set_attributes(events.payload_attributes(payload))
        if error:
            span.set_status(Status(StatusCode.ERROR, str(error)))
        ctx = span.get_span_context()
        links._note(links.Link(event_id, ctx.trace_id, ctx.span_id))

    @never_raise
    def _end_run(self, run: _Run | None) -> None:
        if run is None:
            return
        for key, thread in list(run.threads.items()):
            if thread.spawn is not None:
                with self._lock:
                    refused = key in self._halted
                if refused:
                    self._never_started(run, key)  # finished below, with the run's other open calls
                    continue
                self._deliver(thread)
            self._end_call(thread)
            if key is not None:
                thread.span.end()
        for tool_use_id in list(run.tools):
            with self._lock:
                tool = self._tools.get(tool_use_id)
            if tool is not None and tool.run is run:
                self._finish_tool(tool_use_id, None, None)
        main = run.threads[None]
        with self._lock:
            if run in self._active:
                self._active.remove(run)
            if run.session_id:
                if main.last_call:
                    self._last_calls[run.session_id] = main.last_call
                if self._sessions.get(run.session_id) is run:
                    del self._sessions[run.session_id]
        run.root.end()

    # Helpers.

    def _run_for(self, session_id: str | None) -> _Run | None:
        """The run a hook's session is in: before a run has read its first message, the newest
        that hasn't. Never a run of another session, as one still going after this session's
        has ended, while a subagent of this one runs on."""
        with self._lock:
            run = self._sessions.get(session_id) if session_id else None
            if run is None:
                run = next((r for r in reversed(self._active) if r.session_id is None), None)
        return run

    def _current_agent(self) -> str:
        link = links.current()
        if link is not None:
            with self._lock:
                agent = self._tool_agents.get(link.event_id)
            if agent:
                return agent
        return self._recorder.agent

    def _current_instance(self) -> "tuple[str | None, tuple[str, ...]]":
        link = links.current()
        if link is None:
            return None, ()
        with self._lock:
            if link.event_id in self._ambiguous:
                return None, ()
            # Known from the stream, or from the agent_id its hook named. Before either, the
            # instance is left out rather than guessed: one the stream hasn't shown may be calling.
            return self._tool_instances.get(link.event_id) or (None, ())
