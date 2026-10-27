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

Causal edges follow the messages: a model call's parents are the events that
produced the AI and tool messages in its context, and a tool call's parent is
the model call that requested it. When an app builds its prompts from strings,
so no message ids link them, an event's parents are the earlier events in the
same trace whose output appears in its input. See docs/events.md.
"""

import logging
import threading
import uuid
from collections import OrderedDict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import LLMResult
from langchain_core.tracers.context import register_configure_hook
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from wake import events, links
from wake.recorder import Recorder

log = logging.getLogger("wake")

# Content matching: an output is findable by its first characters, and only
# outputs long enough that a match is unlikely to be chance are indexed.
_SNIPPET = 160
_MIN_MATCH = 24
_OUTPUTS_PER_TRACE = 64
_KEPT_OUTPUT = 2000

_current_run: ContextVar["AgentRun | None"] = ContextVar("wake_agent_run", default=None)


def _never_raise(callback):
    """Recording must never break the agent, so every callback swallows its own errors."""

    @wraps(callback)
    def wrapper(self, *args, **kwargs):
        try:
            return callback(self, *args, **kwargs)
        except Exception:
            log.warning("wake: failed to record %s", callback.__name__, exc_info=True)

    return wrapper


class _Recent(OrderedDict):
    """A dict that forgets its oldest entries, so a long running agent's memory stays bounded."""

    def __init__(self, limit: int = 10_000) -> None:
        super().__init__()
        self._limit = limit

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        if len(self) > self._limit:
            self.popitem(last=False)


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
        self._lock = threading.Lock()
        # The message that started this run feeds its next event.
        self._next_parents: list[str] = []
        self._next_origins: list[str] = []

    def reply(self, content: Any) -> None:
        """Record this agent's answer going back to the agent that handed off to it."""
        if self._parent is None:
            return
        try:
            self._handler._record_message(sender=self, receiver=self._parent, content=content)
        except Exception:
            log.warning("wake: failed to record reply", exc_info=True)

    def _take_next(self) -> tuple[list[str], list[str]]:
        with self._lock:
            parents, origins = self._next_parents, self._next_origins
            self._next_parents, self._next_origins = [], []
        return parents, origins


class WakeCallbackHandler(BaseCallbackHandler):
    # Run callbacks in order, even in async graphs, so a tool call can find
    # the model call that requested it. The work done here is small and
    # never waits on the network.
    run_inline = True

    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder
        self._lock = threading.Lock()
        self._parent_run = _Recent()  # run id -> parent run id
        self._spans = _Recent()  # open spans by run id
        self._pending = _Recent()  # open events by run id
        self._event_by_message = _Recent()  # AI message id -> model call event id
        self._event_by_tool_call = _Recent()  # tool call id -> model call that requested it
        self._result_by_tool_call = _Recent()  # tool call id -> tool call event id
        self._seen_user_messages = _Recent()
        self._outputs = _Recent(limit=256)  # trace id -> recent (event id, output text)
        self._lineage = _Recent()  # event id -> (parents, origins), for memory labels
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
        """
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

    @_never_raise
    def on_chain_start(self, serialized, inputs, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs) -> None:
        with self._lock:
            self._parent_run[run_id] = parent_run_id
        if parent_run_id is None and _current_run.get() is None:
            agent = self._recorder.agent
            span = self._recorder.tracer.start_span(
                f"invoke_agent {agent}",
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": agent,
                    events.AGENT_ID: agent,
                },
            )
            with self._lock:
                self._spans[run_id] = span

    @_never_raise
    def on_chain_end(self, outputs, *, run_id: UUID, **kwargs) -> None:
        self._end_structure(run_id)

    @_never_raise
    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs) -> None:
        self._end_structure(run_id, error)

    # Model calls.

    @_never_raise
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
        with self._lock:
            self._parent_run[run_id] = parent_run_id
            for m in context:
                if isinstance(m, AIMessage) and m.id in self._event_by_message:
                    parents.append(self._event_by_message[m.id])
                elif isinstance(m, ToolMessage) and m.tool_call_id in self._result_by_tool_call:
                    parents.append(self._result_by_tool_call[m.tool_call_id])
                elif isinstance(m, HumanMessage) and _current_run.get() is None and m.id not in self._seen_user_messages:
                    # A user's message is new content the first time a model sees it.
                    # Inside a named agent run, a human message is usually a prompt the
                    # app built, so the run's input says where content came from instead.
                    if m.id is not None:
                        self._seen_user_messages[m.id] = True
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

    @_never_raise
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
                    if message.id:
                        self._event_by_message[message.id] = pending["event_id"]
                    for call in getattr(message, "tool_calls", None) or []:
                        if call.get("id"):
                            self._event_by_tool_call[call["id"]] = pending["event_id"]
        self._end_event(run_id, outputs, text="\n".join(texts))

    @_never_raise
    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs) -> None:
        self._end_event(run_id, [], error)

    # Tool calls.

    @_never_raise
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

    @_never_raise
    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs) -> None:
        self._remember_tool_result(run_id, output)
        self._end_event(run_id, _tool_output(output), text=_text(output))

    @_never_raise
    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs) -> None:
        self._remember_tool_result(run_id, None)
        self._end_event(run_id, None, error)

    # Agent runs and the messages between them.

    def _start_run(self, name: str, input: Any, origin: str | None) -> AgentRun:
        enclosing = _current_run.get()
        try:
            context = trace.set_span_in_context(enclosing._span) if enclosing and enclosing._span else None
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
            return AgentRun(self, name, None, enclosing)
        run = AgentRun(self, name, span, enclosing)
        try:
            if enclosing is not None and input is not None:
                message_id = self._record_message(sender=enclosing, receiver=run, content=input)
                if message_id:
                    run._next_parents.append(message_id)
            elif origin is not None:
                run._next_origins.append(origin)
        except Exception:
            log.warning("wake: failed to record handoff", exc_info=True)
        return run

    def _record_message(self, *, sender: AgentRun, receiver: AgentRun, content: Any) -> str | None:
        """Record a message from one agent to another as an event in the receiver's run.

        Its parents are the sender's events whose output it carries, and its
        origin is the sender: this is where the content enters the receiver.
        """
        if receiver._span is None:
            return None
        text = _text(content)
        trace_id = receiver._span.get_span_context().trace_id
        event_id = str(uuid.uuid4())
        parents = self._matching_outputs(trace_id, text)
        self._remember_lineage(event_id, parents, [f"agent:{sender.agent}"])
        attributes = {
            events.EVENT_ID: event_id,
            events.EVENT_KIND: events.AGENT_MESSAGE,
            events.AGENT_ID: receiver.agent,
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
        if run is not None:
            next_parents, next_origins = run._take_next()
            parents = next_parents + parents
            origins = origins + [o for o in next_origins if o not in origins]
        context = self._context_for(parent_run_id)
        if context is None and run is not None and run._span is not None:
            context = trace.set_span_in_context(run._span)
        trace_id = trace.get_current_span(context).get_span_context().trace_id if context else None
        if trace_id:
            if text:
                parents = parents + self._matching_outputs(trace_id, text)
            if arguments:
                parents = parents + self._outputs_containing(trace_id, arguments)
        event_id = str(uuid.uuid4())
        parents = list(dict.fromkeys(parents))
        self._remember_lineage(event_id, parents, origins)
        attributes = {
            events.EVENT_ID: event_id,
            events.EVENT_KIND: kind,
            events.AGENT_ID: agent,
            events.CAPTURE: "sdk",
            events.PARENTS: parents,
            events.ORIGINS: origins,
            **attributes,
        }
        if metadata.get("langgraph_node"):
            attributes["wake.langgraph.node"] = metadata["langgraph_node"]
        span = self._recorder.tracer.start_span(name, context=context, attributes=attributes)
        link = None
        if kind == events.TOOL_CALL:
            # Calls the tool makes, such as to an MCP server through the proxy, link back
            # to this event. LangChain runs the tool in the context this callback runs in.
            ctx = span.get_span_context()
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
        links._exit(pending.get("link"))
        payload = {"input": pending["inputs"], "output": outputs}
        if error is not None:
            payload["error"] = repr(error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
        span.set_attributes(events.payload_attributes(payload))
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

    def _remember_lineage(self, event_id: str, parents: list[str], origins: list[str]) -> None:
        with self._lock:
            self._lineage[event_id] = (tuple(parents), tuple(origins))

    def _origins_upstream(self, event_ids: list[str], limit: int = 2000) -> list[str]:
        """The origins of these events and everything upstream of them that this handler
        recorded. Coarse: an event's origins are the union over its ancestors."""
        seen: set[str] = set()
        out: list[str] = []
        queue = list(event_ids)
        with self._lock:
            while queue and len(seen) < limit:
                event_id = queue.pop()
                if event_id in seen:
                    continue
                seen.add(event_id)
                parents, origins = self._lineage.get(event_id, ((), ()))
                for o in origins:
                    if o not in out:
                        out.append(o)
                queue.extend(parents)
        return out

    def _event_context(self):
        """The trace context for an event recorded outside a LangChain callback, such as a
        memory read or write: under the tool call making it, else the agent run."""
        link = links.current()
        if link is not None:
            return trace.set_span_in_context(
                trace.NonRecordingSpan(
                    trace.SpanContext(link.trace_id, link.span_id, is_remote=False, trace_flags=trace.TraceFlags(1))
                )
            )
        run = _current_run.get()
        if run is not None and run._span is not None:
            return trace.set_span_in_context(run._span)
        return None

    def _current_agent(self) -> str:
        run = _current_run.get()
        return run.agent if run else self._recorder.agent

    def _remember_output(self, trace_id: int, event_id: str, text: str) -> None:
        text = text.strip()
        if len(text) < _MIN_MATCH:
            return
        with self._lock:
            outputs = self._outputs.get(trace_id)
            if outputs is None:
                outputs = deque(maxlen=_OUTPUTS_PER_TRACE)
                self._outputs[trace_id] = outputs
            outputs.append((event_id, text[:_KEPT_OUTPUT]))

    def _matching_outputs(self, trace_id: int, text: str) -> list[str]:
        """Earlier events in this trace whose output appears in this text, or that contain all of it."""
        text = text.strip()
        whole = len(text) >= _MIN_MATCH
        with self._lock:
            outputs = list(self._outputs.get(trace_id, ()))
        return [
            event_id
            for event_id, output in outputs
            if output[:_SNIPPET] in text or (whole and text in output)
        ]

    def _outputs_containing(self, trace_id: int, arguments: list[str]) -> list[str]:
        """Earlier events in this trace whose output contains one of these arguments."""
        with self._lock:
            outputs = list(self._outputs.get(trace_id, ()))
        return [event_id for event_id, output in outputs if any(a in output for a in arguments)]

    def _remember_tool_result(self, run_id: UUID, output: Any) -> None:
        with self._lock:
            pending = self._pending.get(run_id)
            if pending is None:
                return
            tool_call_id = pending["tool_call_id"] or getattr(output, "tool_call_id", None)
            if tool_call_id:
                self._result_by_tool_call[tool_call_id] = pending["event_id"]

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
