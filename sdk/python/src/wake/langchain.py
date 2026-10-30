"""Records model calls, tool calls and handoffs between agents from LangChain and LangGraph runs.

    from wake import Recorder
    from wake.langchain import WakeCallbackHandler

    handler = WakeCallbackHandler(Recorder(agent="email-agent"))
    graph.invoke(inputs, config={"callbacks": [handler]})

Or record every LangChain call in the process, and name the agent behind each run:

    handler = WakeCallbackHandler(Recorder(agent="clavr")).install()

    with handler.agent("supervisor", input=query, origin="user"):
        ...
        with handler.agent("email", input=step_query) as run:  # a handoff
            answer = await email_agent.run(step_query)
            run.reply(answer)

A run started inside another agent's run is an instance of its own, <name>#<its span id>,
started by that run (docs/events.md), so two runs of one agent at once can be told apart.

Causal edges follow the messages: a model call's parents are the events that
produced the AI and tool messages in its context, and a tool call's parent is
the model call that requested it. A message from one run to another descends from the
sender's latest event, however the app words it. When an app builds its prompts from
strings, so no message ids link them, an event's parents are the earlier events in the
same trace whose output appears in its input. See docs/events.md.

Given the brake's rules (WakeCallbackHandler(recorder, quarantine=Quarantine(...))), the
handler also stops an agent, or one run of it, in its own process: agent() refuses to
start a run a halt covers, raising AgentHalted, and a LangGraph ToolNode given the
handler's wrap_tool_call and awrap_tool_call refuses a tool call a halt covers before it
runs, telling the model why. Any other tool call a halt covers, as from
create_react_agent's ToolNode, a node of the app's own or a direct invoke, is refused as
it starts: the handler raises AgentHalted from its tool start callback, and its
raise_error has LangChain pass that on, so the tool never runs. A ToolNode then raises it,
ending the graph's run, unless its handle_tool_errors catches every error, when the model
gets it as an error message. Each refusal is recorded as denied, once. It's a cooperative
stop: it holds only while the agent's code does.
"""

import asyncio
import hashlib
import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import LLMResult
from langchain_core.tracers.context import register_configure_hook
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from wake import events, links, stop
from wake._graph import _MIN_MATCH, EventGraph, _Recent, failed, never_raise
from wake.recorder import Recorder
from wake.stop import AgentHalted

if TYPE_CHECKING:
    from wake.quarantine import Quarantine

__all__ = ["AgentHalted", "AgentRun", "WakeCallbackHandler"]

log = logging.getLogger("wake")

_current_run: ContextVar["AgentRun | None"] = ContextVar("wake_agent_run", default=None)


class _Installed:
    """What install() registers with LangChain. LangChain asks it for the handler to add
    to every run; a ContextVar would do, but couldn't be turned off again."""

    def __init__(self, handler: "WakeCallbackHandler") -> None:
        self.handler: WakeCallbackHandler | None = handler

    def get(self) -> "WakeCallbackHandler | None":
        return self.handler


class AgentRun:
    """One run of one agent. Events recorded inside it carry its agent id and share its trace."""

    def __init__(self, handler: "WakeCallbackHandler", agent: str, span: Span | None, parent: "AgentRun | None") -> None:
        self.agent = agent
        self._handler = handler
        self._span = span
        self._parent = parent
        # Started inside another agent's run, it's an instance of its own, started by that one.
        self.instance: str | None = None
        self.lineage: tuple[str, ...] = ()
        if parent is not None:
            suffix = format(span.get_span_context().span_id, "016x") if span is not None else uuid.uuid4().hex[:16]
            self.instance = f"{agent}#{suffix}"
            self.lineage = (*parent.lineage, parent.instance or parent.agent)
        self._lock = threading.Lock()
        # The message that started this run feeds its next event.
        self._next_parents: list[str] = []
        self._next_origins: list[str] = []
        # The latest event recorded in this run, which a message it sends descends from.
        self._latest: str | None = None

    def reply(self, content: Any) -> None:
        """Record this agent's answer going back to the agent that handed off to it."""
        if self._parent is None:
            return
        try:
            self._handler._record_message(sender=self, receiver=self._parent, content=content)
        except Exception:
            log.warning("wake: failed to record reply", exc_info=True)
            failed(self._handler._recorder, "reply")

    def _take_next(self) -> tuple[list[str], list[str]]:
        with self._lock:
            parents, origins = self._next_parents, self._next_origins
            self._next_parents, self._next_origins = [], []
        return parents, origins


class WakeCallbackHandler(EventGraph, BaseCallbackHandler):
    # Run callbacks in order, even in async graphs, so a tool call can find
    # the model call that requested it. The work done here is small and
    # never waits on the network.
    run_inline = True
    # LangChain passes on what a callback raises. Recording never raises, so the only
    # error that reaches it is a refused tool call's AgentHalted. The callbacks are
    # synchronous, so it reaches sync and async runs alike.
    raise_error = True

    def __init__(self, recorder: Recorder, quarantine: "Quarantine | None" = None) -> None:
        """With quarantine, the brake's rules for the agent (wake.quarantine), agent(), the
        ToolNode wrappers and every tool call stop what a halt covers."""
        EventGraph.__init__(self, recorder)
        self._quarantine = quarantine
        stop.stops_with(quarantine)
        self._call_context = _Recent()  # tool call id -> the span context of the model call that requested it
        self._parent_run = _Recent()  # run id -> parent run id
        self._spans = _Recent()  # open spans by run id
        self._pending = _Recent()  # open events by run id
        self._event_by_message = _Recent()  # AI message id -> model call event id
        self._event_by_tool_call = _Recent()  # tool call id -> model call that requested it
        self._result_by_tool_call = _Recent()  # tool call id -> tool call event id
        self._returned_by_event = _Recent()  # tool call event id -> events its result came from
        self._seen_user_messages = _Recent()  # message id, or (trace id, digest) for one with no id
        self._linked = _Recent()  # top level run id -> the link it started under, for its first event
        self._under_tool = _Recent()  # tool call run id whose nested run's first event is recorded
        self._installed: _Installed | None = None

    def install(self) -> "WakeCallbackHandler":
        """Record every LangChain run in this process, without passing callbacks at each call."""
        if self._installed is None:
            self._installed = _Installed(self)
            register_configure_hook(self._installed, inheritable=True)  # type: ignore[arg-type]
        self._installed.handler = self
        return self

    def uninstall(self) -> None:
        """Stop recording runs that don't pass this handler explicitly."""
        if self._installed is not None:
            self._installed.handler = None

    @contextmanager
    def agent(self, name: str, *, input: Any = None, origin: str | None = None) -> Iterator[AgentRun]:
        """Name the agent behind the events recorded inside this block.

        Inside another agent's run, `input` is a message from that agent: it's
        recorded as an agent_message event that feeds this run's next event.
        At the top, `origin` labels where `input` came from, such as "user".
        Call reply() on the run to record the answer going back.

        With the brake's rules, a run a halt covers, by its name or what starts it, isn't
        started: this raises AgentHalted, and the attempt is recorded as denied.
        """
        self._refuse_start(name, input)
        run = self._start_run(name, input, origin)
        token = _current_run.set(run)
        error: BaseException | None = None
        try:
            yield run
        except BaseException as e:
            error = e
            raise
        finally:
            try:
                _current_run.reset(token)
            except ValueError:
                # The block ended in another context, such as an async generator
                # finished by a different task. That context never saw the run.
                pass
            self._end_span(run._span, error)

    # Structure: one invoke_agent span per top level run groups its events into a trace.

    @never_raise
    def on_chain_start(self, serialized, inputs, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs) -> None:
        with self._lock:
            self._parent_run[run_id] = parent_run_id
        if parent_run_id is None and _current_run.get() is None:
            # A run inside a recorded tool call, or for a message from another agent, joins
            # that trace, and its first event descends from it.
            agent = self._recorder.agent
            link = links.current()
            span = self._recorder.tracer.start_span(
                f"invoke_agent {agent}",
                context=EventGraph._event_context(self),
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": agent,
                    events.AGENT_ID: agent,
                },
            )
            with self._lock:
                self._spans[run_id] = span
                if link is not None:
                    self._linked[run_id] = {"parents": list(link.parents), "taken": False}

    @never_raise
    def on_chain_end(self, outputs, *, run_id: UUID, **kwargs) -> None:
        self._end_structure(run_id)

    @never_raise
    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs) -> None:
        self._end_structure(run_id, error)

    # Model calls.

    @never_raise
    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs,
    ) -> None:
        metadata = metadata or {}
        context = [m for batch in messages for m in batch]
        parents: list[str] = []
        origins: list[str] = []
        linked = self._is_linked(parent_run_id)
        # The trace this call joins, if it doesn't start one of its own.
        trace_context = self._context_for(parent_run_id) or EventGraph._event_context(self)
        trace_id = trace.get_current_span(trace_context).get_span_context().trace_id if trace_context else 0
        with self._lock:
            self._parent_run[run_id] = parent_run_id
            for m in context:
                if isinstance(m, AIMessage) and m.id in self._event_by_message:
                    parents.append(self._event_by_message[m.id])
                elif isinstance(m, ToolMessage) and m.tool_call_id in self._result_by_tool_call:
                    tool_event = self._result_by_tool_call[m.tool_call_id]
                    parents.append(tool_event)
                    parents += self._returned_by_event.get(tool_event, [])
                elif isinstance(m, HumanMessage) and _current_run.get() is None and not linked:
                    # A user's message is new content the first time a model sees it.
                    # Inside a named agent run, a human message is usually a prompt the
                    # app built, so the run's input says where content came from instead;
                    # in a linked run, or one inside a tool call, what started it does. A
                    # message with no id is known by its text within its trace.
                    key = m.id
                    if key is None and trace_id:
                        key = (trace_id, hashlib.sha256(_text(m.content).encode()).hexdigest())
                    if key is not None and key in self._seen_user_messages:
                        continue
                    if key is not None:
                        self._seen_user_messages[key] = True
                    if events.ORIGIN_USER not in origins:
                        origins.append(events.ORIGIN_USER)
        model = metadata.get("ls_model_name") or (serialized or {}).get("name") or ""
        self._start_event(
            run_id,
            parent_run_id,
            name=f"chat {model}".strip(),
            kind=events.MODEL_CALL,
            parents=parents,
            origins=origins,
            metadata=metadata,
            attributes={"gen_ai.operation.name": "chat", "gen_ai.request.model": model},
            inputs={"model": model, "messages": [_message(m) for m in context]},
            text="\n".join(_text(m.content) for m in context),
        )

    @never_raise
    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs) -> None:
        with self._lock:
            pending = self._pending.get(run_id)
        if pending is None:
            return
        outputs = []
        texts = []
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if message is None:
                    outputs.append({"text": generation.text})
                    texts.append(generation.text)
                    continue
                outputs.append(_message(message))
                texts.append(_text(message.content))
                with self._lock:
                    span = self._spans.get(run_id)
                    if message.id:
                        self._event_by_message[message.id] = pending["event_id"]
                    for call in getattr(message, "tool_calls", None) or []:
                        if call.get("id"):
                            self._event_by_tool_call[call["id"]] = pending["event_id"]
                            if self._quarantine is not None and span is not None:
                                # Where a refusal of the call is recorded, if it's refused.
                                self._call_context[call["id"]] = span.get_span_context()
        self._end_event(run_id, outputs, text="\n".join(texts))

    @never_raise
    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs) -> None:
        self._end_event(run_id, [], error)

    # Tool calls.

    @never_raise
    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
        **kwargs,
    ) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "tool"
        # The stop for any tool call a ToolNode's wrappers didn't refuse: raised here, before
        # the tool's run is opened, it never runs, and nothing but the refusal is recorded.
        refused = self._refuse_tool({"name": name, "id": tool_call_id, "args": inputs if inputs is not None else input_str})
        if refused is not None:
            raise AgentHalted(refused)
        with self._lock:
            self._parent_run[run_id] = parent_run_id
            requested_by = self._event_by_tool_call.get(tool_call_id) if tool_call_id else None
        attributes = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name}
        if tool_call_id:
            attributes["gen_ai.tool.call.id"] = tool_call_id
        self._start_event(
            run_id,
            parent_run_id,
            name=f"execute_tool {name}",
            kind=events.TOOL_CALL,
            parents=[requested_by] if requested_by else [],
            # The tool's result is new content entering the system here.
            origins=[f"tool:{name}"],
            metadata=metadata or {},
            attributes=attributes,
            inputs=inputs if inputs is not None else input_str,
            tool_call_id=tool_call_id,
            # Without a tool call id, the arguments themselves point at the model call that chose them.
            arguments=None if requested_by else _argument_texts(inputs if inputs is not None else input_str),
        )
        self._end_if_cancelled(run_id)

    def _end_if_cancelled(self, run_id: UUID) -> None:
        """LangChain ends a tool call that raises with on_tool_error, but it only catches
        Exception and KeyboardInterrupt, so an async tool that's cancelled ends with no
        callback: its event never sent and its link still current. So when the task running
        the tool is done with the call still open, it's ended as cancelled. A sync tool
        can't be cancelled, and runs with no event loop of its own here."""
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return
        if task is None:
            return

        def done(_: asyncio.Task) -> None:
            self._cancelled(run_id)

        task.add_done_callback(done)
        with self._lock:
            pending = self._pending.get(run_id)
            if pending is not None:
                pending["task"] = (task, done)

    @never_raise
    def _cancelled(self, run_id: UUID) -> None:
        self._end_event(run_id, None, asyncio.CancelledError())

    @never_raise
    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs) -> None:
        self._remember_tool_result(run_id, output)
        self._end_event(run_id, _tool_output(output), text=_text(output))

    @never_raise
    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs) -> None:
        self._remember_tool_result(run_id, None)
        self._end_event(run_id, None, error)

    # Cooperative stops.

    def wrap_tool_call(self, request, execute):
        """For a LangGraph ToolNode (ToolNode(tools, wrap_tool_call=handler.wrap_tool_call,
        awrap_tool_call=handler.awrap_tool_call)), or a create_agent middleware's hook of the
        same name: refuses a tool call a halt covers before it runs, answering the model with
        why, and records it as denied. Any other call runs."""
        refused = self._tool_refusal(request)
        return execute(request) if refused is None else refused

    async def awrap_tool_call(self, request, execute):
        """wrap_tool_call(), for a graph run asynchronously."""
        refused = self._tool_refusal(request)
        return await execute(request) if refused is None else refused

    def _tool_refusal(self, request) -> ToolMessage | None:
        """The answer to a tool call a halt covers, recorded as denied; None for a call to run."""
        call = getattr(request, "tool_call", None)
        call = call if isinstance(call, dict) else {}
        refused = self._refuse_tool(call)
        if refused is None:
            return None
        return ToolMessage(content=refused.reason, tool_call_id=call.get("id") or "", name=call.get("name") or "tool",
                           status="error")

    def _refuse_tool(self, call: dict) -> "stop.Refusal | None":
        """The refusal of a tool call about to run, if a halt covers the agent making it, the
        run it's in or what started that run, recorded as denied; None for a call to run. Never
        raises: a check that fails refuses."""
        if self._quarantine is None:
            return None
        run, who = _current_run.get(), self._recorder.agent
        try:
            names = {self._recorder.agent}
            if run is not None:
                names.update((run.agent, *run.lineage))
                if run.instance:
                    names.add(run.instance)
                who = run.instance or run.agent
            refused = stop.refusal(self._quarantine, who, names)
        except Exception:
            log.warning("wake: couldn't check the halts for a tool call; refusing it", exc_info=True)
            refused = stop.unchecked()
        if refused is None:
            return None
        name = call.get("name") or "tool"
        self._refused_call(run, name, call, refused)
        log.warning("wake: refused a %s call of %s: %s", name, who, refused.halt)
        return refused

    def _refuse_start(self, name: str, input: Any) -> None:
        """Raises AgentHalted, recording the attempt, if a halt covers the run of name about
        to start here: by its name, or what starts it."""
        if self._quarantine is None:
            return
        enclosing = _current_run.get()
        try:
            lineage = (*enclosing.lineage, enclosing.instance or enclosing.agent) if enclosing is not None else ()
            refused = stop.refusal(self._quarantine, name, {self._recorder.agent, name, *lineage},
                                   outcome=f"this run of {name} wasn't started")
        except Exception:
            log.warning("wake: couldn't check the halts for a run of %s; refusing it", name, exc_info=True)
            refused = stop.unchecked(f"this run of {name} wasn't started")
        if refused is None:
            return
        self._refused_start(enclosing, name, input, refused)
        log.warning("wake: refused to start a run of %s: %s", name, refused.halt)
        raise AgentHalted(refused)

    @never_raise
    def _refused_call(self, run: AgentRun | None, name: str, call: dict, refused: stop.Refusal) -> None:
        """Records a tool call a halt refused before it ran: what the agent tried, under the
        model call that asked for it. The tool never ran, so it has no origins. The model's
        next call reads the refusal."""
        call_id = call.get("id")
        with self._lock:
            requested_by = self._event_by_tool_call.get(call_id) if call_id else None
            requester = self._call_context.get(call_id) if call_id else None
        if requester is not None:
            context = trace.set_span_in_context(trace.NonRecordingSpan(requester))
        elif run is not None and run._span is not None:
            context = trace.set_span_in_context(run._span)
        else:
            context = EventGraph._event_context(self)
        attributes = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "wake.brake.halt_id": refused.halt}
        if call_id:
            attributes["gen_ai.tool.call.id"] = call_id
        event_id = self._denied(f"execute_tool {name}", run, [requested_by] if requested_by else [], [],
                                attributes, {"input": call.get("args"), "output": refused.reason}, context)
        if call_id:
            with self._lock:
                self._result_by_tool_call[call_id] = event_id

    @never_raise
    def _refused_start(self, sender: AgentRun | None, name: str, content: Any, refused: stop.Refusal) -> None:
        """Records a run a halt refused to start: the attempt to hand it its task, in the name
        of what tried, descending from what tried's latest event. Nothing entered the run, so
        it has no origins."""
        if sender is not None and sender._span is not None:
            context = trace.set_span_in_context(sender._span)
        else:
            context = EventGraph._event_context(self)
        trace_id = trace.get_current_span(context).get_span_context().trace_id if context is not None else 0
        parents = self._latest_of(sender)
        parents += self._matching_outputs(trace_id, _text(content)) if trace_id and content is not None else []
        agent = sender.agent if sender is not None else self._recorder.agent
        self._denied(f"agent_message {agent} -> {name}", sender, parents, [],
                     {"gen_ai.agent.name": name, "wake.brake.halt_id": refused.halt, "wake.brake.receiver": name},
                     {"from": agent, "to": name, "content": content}, context)

    def _denied(self, span_name: str, run: AgentRun | None, parents: list[str], origins: list[str],
                attributes: dict[str, Any], payload: Any, context) -> str:
        """Records a denied event, ended as soon as it starts, and returns its id."""
        event_id = str(uuid.uuid4())
        parents = list(dict.fromkeys(parents))
        self._remember_lineage(event_id, parents, origins)
        self._latest_in(run, event_id)
        span = self._recorder.tracer.start_span(span_name, context=context, attributes={
            events.EVENT_ID: event_id,
            events.EVENT_KIND: events.DENIED,
            events.AGENT_ID: run.agent if run is not None else self._recorder.agent,
            **(events.instance_attributes(run.instance, run.lineage) if run is not None else {}),
            events.CAPTURE: "sdk",
            events.PARENTS: parents,
            events.ORIGINS: origins,
            **attributes,
            **events.payload_attributes(payload),
        })
        span.end()
        ctx = span.get_span_context()
        links._note(links.Link(event_id, ctx.trace_id, ctx.span_id))
        return event_id

    # Agent runs and the messages between them.

    def _start_run(self, name: str, input: Any, origin: str | None) -> AgentRun:
        enclosing = _current_run.get()
        link = links.current() if enclosing is None else None
        try:
            if enclosing is not None:
                context = trace.set_span_in_context(enclosing._span) if enclosing._span else None
            else:
                context = EventGraph._event_context(self)
            span = self._recorder.tracer.start_span(
                f"invoke_agent {name}",
                context=context,
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": name,
                    events.AGENT_ID: name,
                },
            )
        except Exception:
            log.warning("wake: failed to record agent run", exc_info=True)
            failed(self._recorder, "agent run")
            return AgentRun(self, name, None, enclosing)
        run = AgentRun(self, name, span, enclosing)
        span.set_attributes(events.instance_attributes(run.instance, run.lineage))
        try:
            if enclosing is not None and input is not None:
                message_id = self._record_message(sender=enclosing, receiver=run, content=input)
                if message_id:
                    run._next_parents.append(message_id)
            else:
                if link is not None:
                    run._next_parents += link.parents
                if origin is not None:
                    run._next_origins.append(origin)
        except Exception:
            log.warning("wake: failed to record handoff", exc_info=True)
            failed(self._recorder, "handoff")
        return run

    def _record_message(self, *, sender: AgentRun, receiver: AgentRun, content: Any) -> str | None:
        """Record a message from one agent to another as an event in the receiver's run.

        Its parents are the sender's latest event, since an app may word the message
        its own way, and the events whose output it carries. Its origin is the sender:
        this is where the content enters the receiver.
        """
        if receiver._span is None:
            return None
        text = _text(content)
        trace_id = receiver._span.get_span_context().trace_id
        event_id = str(uuid.uuid4())
        parents = list(dict.fromkeys(self._latest_of(sender) + self._matching_outputs(trace_id, text)))
        self._remember_lineage(event_id, parents, [f"agent:{sender.agent}"])
        self._latest_in(receiver, event_id)
        attributes = {
            events.EVENT_ID: event_id,
            events.EVENT_KIND: events.AGENT_MESSAGE,
            events.AGENT_ID: receiver.agent,
            **events.instance_attributes(receiver.instance, receiver.lineage),
            events.CAPTURE: "sdk",
            events.PARENTS: parents,
            events.ORIGINS: [f"agent:{sender.agent}"],
            "gen_ai.agent.name": receiver.agent,
            **events.payload_attributes({"from": sender.agent, "to": receiver.agent, "content": content}),
        }
        span = self._recorder.tracer.start_span(
            f"agent_message {sender.agent} -> {receiver.agent}",
            context=trace.set_span_in_context(receiver._span),
            attributes=attributes,
        )
        span.end()
        ctx = span.get_span_context()
        links._note(links.Link(event_id, ctx.trace_id, ctx.span_id))
        self._remember_output(trace_id, event_id, text)
        return event_id

    # Helpers.

    def _start_event(
        self,
        run_id: UUID,
        parent_run_id: UUID | None,
        *,
        name: str,
        kind: str,
        parents: list[str],
        origins: list[str],
        metadata: dict[str, Any],
        attributes: dict[str, Any],
        inputs: Any,
        text: str | None = None,
        arguments: list[str] | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        run = _current_run.get()
        agent = run.agent if run else self._recorder.agent
        # A run inside a recorded tool call descends from it (docs/linking.md).
        under_tool = self._take_tool_parent(parent_run_id)
        if run is not None:
            next_parents, next_origins = run._take_next()
            parents = under_tool + next_parents + parents
            origins = origins + [o for o in next_origins if o not in origins]
        else:
            parents = (under_tool or self._take_linked(parent_run_id)) + parents
        context = self._context_for(parent_run_id)
        if context is None and run is not None and run._span is not None:
            context = trace.set_span_in_context(run._span)
        if context is None:
            context = EventGraph._event_context(self)
        trace_id = trace.get_current_span(context).get_span_context().trace_id if context else None
        if trace_id:
            if text:
                parents = parents + self._matching_outputs(trace_id, text)
            if arguments:
                parents = parents + self._outputs_containing(trace_id, arguments)
        event_id = str(uuid.uuid4())
        parents = list(dict.fromkeys(parents))
        self._remember_lineage(event_id, parents, origins)
        self._latest_in(run, event_id)
        attributes = {
            events.EVENT_ID: event_id,
            events.EVENT_KIND: kind,
            events.AGENT_ID: agent,
            **(events.instance_attributes(run.instance, run.lineage) if run else {}),
            events.CAPTURE: "sdk",
            events.PARENTS: parents,
            events.ORIGINS: origins,
            **attributes,
        }
        if metadata.get("langgraph_node"):
            attributes["wake.langgraph.node"] = metadata["langgraph_node"]
        span = self._recorder.tracer.start_span(name, context=context, attributes=attributes)
        ctx = span.get_span_context()
        links._note(links.Link(event_id, ctx.trace_id, ctx.span_id))
        link = None
        if kind == events.TOOL_CALL:
            # Calls the tool makes, such as to an MCP server through the proxy, link back
            # to this event. LangChain runs the tool in the context this callback runs in.
            link = links._enter(links.Link(event_id, ctx.trace_id, ctx.span_id))
        with self._lock:
            self._spans[run_id] = span
            self._pending[run_id] = {
                "event_id": event_id,
                "inputs": inputs,
                "tool_call_id": tool_call_id,
                "link": link,
            }

    def _end_event(self, run_id: UUID, outputs: Any, error: BaseException | None = None, *, text: str = "") -> None:
        with self._lock:
            span = self._spans.pop(run_id, None)
            pending = self._pending.pop(run_id, None)
        if span is None or pending is None:
            return
        if "task" in pending:
            task, done = pending["task"]
            task.remove_done_callback(done)
        try:
            returned = links._returned(pending.get("link"))
            links._exit(pending.get("link"))
            if returned:
                # What the tool got back came from these events, such as an A2A reply: they
                # feed the step that reads its result.
                with self._lock:
                    self._returned_by_event[pending["event_id"]] = returned
            payload = {"input": pending["inputs"], "output": outputs}
            if error is not None:
                payload["error"] = repr(error)
                span.set_status(Status(StatusCode.ERROR, str(error)))
            span.set_attributes(events.payload_attributes(payload))
        finally:
            # Whatever failed above, the event is still sent, and the failure counted.
            span.end()
        self._remember_output(span.get_span_context().trace_id, pending["event_id"], text)

    def _end_structure(self, run_id: UUID, error: BaseException | None = None) -> None:
        with self._lock:
            span = self._spans.pop(run_id, None)
        self._end_span(span, error)

    def _end_span(self, span: Span | None, error: BaseException | None) -> None:
        if span is None:
            return
        try:
            if error is not None:
                span.set_status(Status(StatusCode.ERROR, str(error)))
            span.end()
        except Exception:
            log.warning("wake: failed to end span", exc_info=True)
            failed(self._recorder, "span end")

    def _event_context(self):
        """Under the tool call making it, else the agent run."""
        context = super()._event_context()
        if context is not None:
            return context
        run = _current_run.get()
        if run is not None and run._span is not None:
            return trace.set_span_in_context(run._span)
        return None

    def _latest_in(self, run: AgentRun | None, event_id: str) -> None:
        """Notes the run's latest event, if it's one of this handler's runs: another handler
        recording the same calls keeps its own."""
        if run is not None and run._handler is self:
            run._latest = event_id

    def _latest_of(self, run: AgentRun | None) -> list[str]:
        """The run's latest event, as a parent, if it's one of this handler's runs."""
        return [run._latest] if run is not None and run._handler is self and run._latest else []

    def _current_agent(self) -> str:
        run = _current_run.get()
        return run.agent if run else self._recorder.agent

    def _current_instance(self) -> "tuple[str | None, tuple[str, ...]]":
        run = _current_run.get()
        return (run.instance, run.lineage) if run else (None, ())

    def _remember_tool_result(self, run_id: UUID, output: Any) -> None:
        with self._lock:
            pending = self._pending.get(run_id)
            if pending is None:
                return
            tool_call_id = pending["tool_call_id"] or getattr(output, "tool_call_id", None)
            if tool_call_id:
                self._result_by_tool_call[tool_call_id] = pending["event_id"]

    def _root_of(self, run_id: UUID) -> UUID:
        """The top level run this one is part of. Call with the lock held."""
        for _ in range(1000):
            parent = self._parent_run.get(run_id)
            if parent is None:
                break
            run_id = parent
        return run_id

    def _enclosing_tool(self, parent_run_id: UUID | None) -> tuple[UUID, str] | None:
        """The recorded tool call a run runs inside, in this process, such as a graph a
        tool invokes: its run id and event id. Call with the lock held."""
        run = parent_run_id
        for _ in range(1000):
            if run is None:
                return None
            pending = self._pending.get(run)
            if pending is not None and pending.get("link") is not None:
                return run, pending["event_id"]
            run = self._parent_run.get(run)
        return None

    def _take_tool_parent(self, parent_run_id: UUID | None) -> list[str]:
        """The enclosing tool call, if this is the first event of a run inside it."""
        with self._lock:
            tool = self._enclosing_tool(parent_run_id)
            if tool is None or tool[0] in self._under_tool:
                return []
            self._under_tool[tool[0]] = True
            return [tool[1]]

    def _is_linked(self, parent_run_id: UUID | None) -> bool:
        if parent_run_id is None:
            return links.current() is not None
        with self._lock:
            return self._enclosing_tool(parent_run_id) is not None or self._root_of(parent_run_id) in self._linked

    def _take_linked(self, parent_run_id: UUID | None) -> list[str]:
        """What started this event's top level run, if it's the run's first event."""
        if parent_run_id is None:
            # A call with no run around it is a top level run of its own.
            link = links.current()
            return list(link.parents) if link is not None else []
        with self._lock:
            entry = self._linked.get(self._root_of(parent_run_id))
            if entry is None or entry["taken"]:
                return []
            entry["taken"] = True
            return list(entry["parents"])

    def _context_for(self, parent_run_id: UUID | None):
        """Nest an event under the closest enclosing span this handler opened."""
        with self._lock:
            run = parent_run_id
            while run is not None:
                span = self._spans.get(run)
                if span is not None:
                    return trace.set_span_in_context(span)
                run = self._parent_run.get(run)
        return None


def _text(content: Any) -> str:
    """The plain text in a message's content, a tool's output or a handoff."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, BaseMessage):
        return _text(content.content)
    if isinstance(content, list):
        parts = [c if isinstance(c, str) else c.get("text", "") if isinstance(c, dict) else "" for c in content]
        return "\n".join(p for p in parts if p)
    return str(content)


def _argument_texts(inputs: Any) -> list[str]:
    """The string arguments of a tool call long enough to be matched against model output."""
    values = inputs.values() if isinstance(inputs, dict) else [inputs]
    return [v.strip() for v in values if isinstance(v, str) and len(v.strip()) >= _MIN_MATCH]


def _message(m: BaseMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"type": m.type, "content": m.content}
    if m.id:
        out["id"] = m.id
    if m.name:
        out["name"] = m.name
    if getattr(m, "tool_calls", None):
        out["tool_calls"] = m.tool_calls
    if isinstance(m, ToolMessage):
        out["tool_call_id"] = m.tool_call_id
    return out


def _tool_output(output: Any) -> Any:
    return _message(output) if isinstance(output, BaseMessage) else output
