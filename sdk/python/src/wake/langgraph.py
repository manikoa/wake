"""Records model calls and tool calls from LangGraph and LangChain runs.

    from wake import Recorder
    from wake.langgraph import WakeCallbackHandler

    handler = WakeCallbackHandler(Recorder(agent="email-agent"))
    graph.invoke(inputs, config={"callbacks": [handler]})

Causal edges follow the messages: a model call's parents are the events that
produced the AI and tool messages in its context, and a tool call's parent is
the model call that requested it. See docs/events.md.
"""

import logging
import threading
import uuid
from collections import OrderedDict
from functools import wraps
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import LLMResult
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from wake import events
from wake.recorder import Recorder

log = logging.getLogger("wake")


def _never_raise(callback):
    """Recording must never break the agent, so every callback swallows its own errors."""

    @wraps(callback)
    def wrapper(self, *args, **kwargs):
        try:
            callback(self, *args, **kwargs)
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

    # Structure: one invoke_agent span per top level run groups its events into a trace.

    @_never_raise
    def on_chain_start(self, serialized, inputs, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs) -> None:
        with self._lock:
            self._parent_run[run_id] = parent_run_id
        if parent_run_id is None:
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
                elif isinstance(m, HumanMessage) and m.id not in self._seen_user_messages:
                    # A user's message is new content the first time a model sees it.
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
        )

    @_never_raise
    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs) -> None:
        with self._lock:
            pending = self._pending.get(run_id)
        if pending is None:
            return
        outputs = []
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if message is None:
                    outputs.append({"text": generation.text})
                    continue
                outputs.append(_message(message))
                with self._lock:
                    if message.id:
                        self._event_by_message[message.id] = pending["event_id"]
                    for call in getattr(message, "tool_calls", None) or []:
                        if call.get("id"):
                            self._event_by_tool_call[call["id"]] = pending["event_id"]
        self._end_event(run_id, outputs)

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
        )

    @_never_raise
    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs) -> None:
        self._remember_tool_result(run_id, output)
        self._end_event(run_id, _tool_output(output))

    @_never_raise
    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs) -> None:
        self._remember_tool_result(run_id, None)
        self._end_event(run_id, None, error)

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
        tool_call_id: str | None = None,
    ) -> None:
        event_id = str(uuid.uuid4())
        attributes = {
            events.EVENT_ID: event_id,
            events.EVENT_KIND: kind,
            events.AGENT_ID: self._recorder.agent,
            events.CAPTURE: "sdk",
            events.PARENTS: list(dict.fromkeys(parents)),
            events.ORIGINS: origins,
            **attributes,
        }
        if metadata.get("langgraph_node"):
            attributes["wake.langgraph.node"] = metadata["langgraph_node"]
        span = self._recorder.tracer.start_span(
            name, context=self._context_for(parent_run_id), attributes=attributes
        )
        with self._lock:
            self._spans[run_id] = span
            self._pending[run_id] = {
                "event_id": event_id,
                "inputs": inputs,
                "tool_call_id": tool_call_id,
            }

    def _end_event(self, run_id: UUID, outputs: Any, error: BaseException | None = None) -> None:
        with self._lock:
            span = self._spans.pop(run_id, None)
            pending = self._pending.pop(run_id, None)
        if span is None or pending is None:
            return
        payload = {"input": pending["inputs"], "output": outputs}
        if error is not None:
            payload["error"] = repr(error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
        span.set_attributes(events.payload_attributes(payload))
        span.end()

    def _end_structure(self, run_id: UUID, error: BaseException | None = None) -> None:
        with self._lock:
            span = self._spans.pop(run_id, None)
        if span is None:
            return
        if error is not None:
            span.set_status(Status(StatusCode.ERROR, str(error)))
        span.end()

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
