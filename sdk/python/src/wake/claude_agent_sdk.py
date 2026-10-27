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
named <agent>/<subagent type>: one agent_message hands it the task and another carries
its answer back. Links are exact, from the SDK's ids: a tool call's parent is the model
call that asked for it, and a model call's parents are the call before it and the tool
results and messages new in its context.

options() adds hooks that open each tool call before it runs, and tools() wraps
in-process tools so they run with their event current: an MCP or A2A call they make
through the proxy, or a memory write, links back to the tool call. Tools Claude Code runs
itself, such as Bash, WebFetch or an external MCP server, are recorded from the stream
but can't carry the link.
"""

import dataclasses
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator
from functools import wraps
from typing import Any

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

from wake import events, links
from wake._graph import EventGraph, _Recent
from wake.recorder import Recorder

log = logging.getLogger("wake")

# The tool Claude Code starts a subagent with; older versions call it Task.
SUBAGENT_TOOLS = frozenset({"Agent", "Task"})


def _never_raise(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:
            log.warning("wake: failed to record %s", method.__name__, exc_info=True)
            return None

    return wrapper


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


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

    def __init__(self, agent: str, span, parents: list[str], origins: list[str], given: list[dict]) -> None:
        self.agent = agent
        self.span = span  # its events nest under this span
        self.call: _Call | None = None  # the model call streaming now
        self.last_call: str | None = None
        self.parents = parents  # what feeds its next model call
        self.origins = origins
        self.given = given  # what's new in its next model call's context
        self.waiting_since = time.time_ns()  # when it last handed control to the model
        self.prompt = given[0]["content"] if given else None


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
        self.run = run
        self.parents: list[str] = []
        self.error: str | None = None
        self.ended_at: int | None = None


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

    def __init__(self, recorder: Recorder) -> None:
        super().__init__(recorder)
        self._tools: dict[str, _Tool] = _Recent()  # tool use id -> its open tool call
        self._waiting = _Recent()  # (tool name, arguments) -> tool use ids an in-process tool may be
        self._tool_agents = _Recent()  # tool call event id -> the agent making it
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
        return {}

    async def _post_tool_use(self, hook_input, tool_use_id, context) -> dict:
        self._hook_closed(hook_input, tool_use_id)
        return {}

    @_never_raise
    def _hook_opened(self, hook_input, tool_use_id) -> None:
        name = hook_input.get("tool_name") or ""
        if not tool_use_id or name in SUBAGENT_TOOLS:
            return
        run = self._run_for(hook_input.get("session_id"))
        if run is None:
            return  # the stream opens it instead
        agent_type = hook_input.get("agent_type") if hook_input.get("agent_id") else None
        agent = f"{self._recorder.agent}/{agent_type}" if agent_type else self._recorder.agent
        self._open_tool(run, tool_use_id, name, hook_input.get("tool_input"), agent, run.root)

    @_never_raise
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

    @_never_raise
    def _enter(self, name: str, arguments: Any):
        key = (name, _canonical(arguments))
        with self._lock:
            waiting = self._waiting.get(key)
            tool = None
            while waiting and tool is None:
                tool = self._tools.get(waiting.popleft())
        return links._enter(tool.link) if tool is not None else None

    # The message stream.

    @_never_raise
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

    @_never_raise
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
                    self._requested(run, thread, call, block.id, block.name, block.input)
            elif isinstance(block, ServerToolUseBlock):
                call.output.append({"type": "server_tool_use", "id": block.id, "name": block.name, "input": block.input})
                self._requested(run, thread, call, block.id, block.name, block.input, server=True)
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
        for block in blocks:
            if isinstance(block, ToolResultBlock):
                item = {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content}
                if block.is_error:
                    item["is_error"] = True
                thread.given.append(item)
                if block.tool_use_id in run.threads:
                    self._returned(run, thread, block)
                else:
                    tool = self._finish_tool(block.tool_use_id, block.content, block.is_error)
                    if tool is not None:
                        thread.parents.append(tool.event_id)
            elif isinstance(block, TextBlock):
                if block.text == thread.prompt:
                    continue  # the prompt it started with, echoed back
                thread.given.append({"role": "user", "content": block.text})
                if thread is run.threads[None] and events.ORIGIN_USER not in thread.origins:
                    thread.origins.append(events.ORIGIN_USER)
        thread.waiting_since = time.time_ns()

    def _thread(self, run: _Run, parent_tool_use_id: str | None) -> _Thread:
        thread = run.threads.get(parent_tool_use_id)
        if thread is None:
            # A subagent whose start Wake didn't see.
            thread = _Thread(f"{self._recorder.agent}/subagent", run.root, [], [], [])
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
                   call.parents, call.origins, attributes, payload, error=call.error)
        self._remember_output(call.span.get_span_context().trace_id, call.event_id, _text(call.output))
        call.span.end(end_time=call.ended_at)

    def _requested(self, run, thread, call, tool_use_id, name, arguments, *, server=False) -> None:
        tool = self._open_tool(run, tool_use_id, name, arguments, thread.agent, thread.span, server=server)
        tool.agent = thread.agent
        tool.parents = list(dict.fromkeys([call.event_id] + call.found))
        with self._lock:
            self._tool_agents[tool.event_id] = thread.agent
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
        self._emit(tool.span, tool.event_id, events.TOOL_CALL, f"execute_tool {tool.name}", tool.agent,
                   tool.parents, [f"tool:{tool.name}"], attributes, payload, error=error)
        self._remember_output(tool.span.get_span_context().trace_id, tool.event_id, _text(output))
        tool.span.end(end_time=tool.ended_at)
        return tool

    def _hand_off(self, run: _Run, thread: _Thread, call: _Call, block: ToolUseBlock) -> None:
        arguments = block.input if isinstance(block.input, dict) else {}
        target = f"{self._recorder.agent}/{arguments.get('subagent_type') or 'general-purpose'}"
        prompt = arguments.get("prompt")
        event_id = str(uuid.uuid4())
        parents = list(dict.fromkeys([call.event_id] + call.found))
        span = self._recorder.tracer.start_span(f"agent_message {thread.agent} -> {target}", context=_context(thread.span))
        self._emit(span, event_id, events.AGENT_MESSAGE, f"agent_message {thread.agent} -> {target}", target,
                   parents, [f"agent:{thread.agent}"], {"gen_ai.agent.name": target},
                   {"from": thread.agent, "to": target, "description": arguments.get("description"), "prompt": prompt})
        span.end()
        sub_span = self._recorder.tracer.start_span(
            f"invoke_agent {target}",
            context=_context(thread.span),
            attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": target, events.AGENT_ID: target},
        )
        given = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else []
        run.threads[block.id] = _Thread(target, sub_span, [event_id], [], given)
        run.handoffs[block.id] = event_id

    def _returned(self, run: _Run, thread: _Thread, block: ToolResultBlock) -> None:
        sub = run.threads.pop(block.tool_use_id)
        self._end_call(sub)
        sub.span.end()
        source = sub.last_call or run.handoffs.get(block.tool_use_id)
        event_id = str(uuid.uuid4())
        span = self._recorder.tracer.start_span(f"agent_message {sub.agent} -> {thread.agent}", context=_context(thread.span))
        self._emit(span, event_id, events.AGENT_MESSAGE, f"agent_message {sub.agent} -> {thread.agent}", thread.agent,
                   [source] if source else [], [f"agent:{sub.agent}"], {"gen_ai.agent.name": thread.agent},
                   {"from": sub.agent, "to": thread.agent, "output": block.content})
        self._remember_output(span.get_span_context().trace_id, event_id, _text(block.content))
        span.end()
        thread.parents.append(event_id)

    def _emit(self, span, event_id, kind, name, agent, parents, origins, attributes, payload, *, error=None) -> None:
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
            **attributes,
        })
        span.set_attributes(events.payload_attributes(json.loads(json.dumps(payload, default=str))))
        if error:
            span.set_status(Status(StatusCode.ERROR, str(error)))

    @_never_raise
    def _end_run(self, run: _Run | None) -> None:
        if run is None:
            return
        for key, thread in list(run.threads.items()):
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
        with self._lock:
            run = self._sessions.get(session_id) if session_id else None
            if run is None and self._active:
                run = self._active[-1]
        return run

    def _current_agent(self) -> str:
        link = links.current()
        if link is not None:
            with self._lock:
                agent = self._tool_agents.get(link.event_id)
            if agent:
                return agent
        return self._recorder.agent
