"""Records OpenAI Agents SDK runs: model responses, tool calls and handoffs between agents.

    from wake import Recorder
    from wake.openai_agents import install

    install(Recorder(agent="support"))   # then run agents as usual
    await Runner.run(triage_agent, "Refund order 1234")

The Agents SDK traces every run, and Wake adds itself as one of its trace processors. Each
agent's run groups its events, named for the agent. Links are exact where the SDK gives
ids: a tool call's parent is the response that requested it, and a response's parents
are the earlier responses and tool results in its input. A handoff is an agent_message in
the receiving agent's name, fed by the response that handed off, and it feeds that
agent's next step. Where nothing else links events, content does, as for LangChain.

Inputs and outputs are only in the trace when the SDK's trace_include_sensitive_data is
on, as it is by default; without them, events are recorded with empty payloads.
"""

import hashlib
import json
import logging
import uuid
from functools import wraps
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from wake import events, links
from wake._graph import EventGraph, _Recent
from wake.recorder import Recorder

log = logging.getLogger("wake")


def install(recorder: Recorder) -> "WakeTracingProcessor":
    """Adds Wake as a trace processor for every OpenAI Agents SDK run in this process."""
    from agents.tracing import add_trace_processor

    processor = WakeTracingProcessor(recorder)
    add_trace_processor(processor)
    return processor


def _never_raise(callback):
    @wraps(callback)
    def wrapper(self, *args, **kwargs):
        try:
            return callback(self, *args, **kwargs)
        except Exception:
            log.warning("wake: failed to record %s", callback.__name__, exc_info=True)

    return wrapper


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


class WakeTracingProcessor(EventGraph):
    """An OpenAI Agents SDK TracingProcessor that records Wake events."""

    def __init__(self, recorder: Recorder) -> None:
        super().__init__(recorder)
        self._parent = _Recent()  # sdk span id -> its parent's sdk span id
        self._agent_span = _Recent()  # sdk span id of an agent span -> agent name
        self._pending = _Recent()  # sdk span id -> what on_span_start set up
        self._call = _Recent()  # (trace, tool name, arguments) -> (call id, response event id)
        self._event_by_call = _Recent()  # call id -> tool call event id
        self._returned_by_event = _Recent()  # tool call event id -> events its output came from
        self._event_by_item = _Recent()  # response or output item id -> response event id
        self._last_response = _Recent()  # (trace, agent) -> response event id
        self._handed_off = _Recent()  # (trace, agent) -> handoff event id, feeding its next step
        self._seen_user = _Recent()  # (trace, digest) for user input already seen
        self._roots = _Recent(limit=1024)  # sdk trace id -> the workflow's root span
        self._linked = _Recent(limit=1024)  # sdk trace id -> what started it, feeding its first response
        self._spans = _Recent(limit=10_000)  # sdk span id -> its open span

    # TracingProcessor.

    @_never_raise
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

    @_never_raise
    def on_trace_end(self, trace_obj) -> None:
        with self._lock:
            root = self._roots.pop(trace_obj.trace_id, None)
        if root is not None:
            root.end()

    @_never_raise
    def on_span_start(self, span) -> None:
        with self._lock:
            self._parent[span.span_id] = span.parent_id
        data = span.span_data
        kind = data.type
        if kind not in ("agent", "response", "generation", "function", "handoff"):
            return
        attributes = {}
        if kind == "agent":
            with self._lock:
                self._agent_span[span.span_id] = data.name
            attributes = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": data.name, events.AGENT_ID: data.name}
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
            with self._lock:
                self._pending[span.span_id] = {"event_id": event_id, "link": link}
                requested_by = [r for (t, n, _), (_, r) in self._call.items() if t == span.trace_id and n == data.name]
            self._remember_lineage(event_id, list(dict.fromkeys(requested_by)), [f"tool:{data.name}"])

    @_never_raise
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

    # Spans.

    def _agent_ended(self, span, data) -> None:
        otel = self._take(span)
        if otel is not None:
            otel.update_name(f"invoke_agent {data.name}")
            otel.end()

    def _response_ended(self, span, data) -> None:
        response = data.response
        output = list(getattr(response, "output", None) or [])
        self._model_call(
            span,
            model=getattr(response, "model", "") or "",
            input_items=data.input,
            output_items=output,
            response_id=getattr(response, "id", None),
        )

    def _generation_ended(self, span, data) -> None:
        # Chat Completions models report generations rather than responses.
        self._model_call(span, model=data.model or "", input_items=data.input, output_items=list(data.output or []), response_id=None)

    def _model_call(self, span, *, model, input_items, output_items, response_id) -> None:
        trace_key = span.trace_id
        agent = self._agent_for(span)
        items = input_items if isinstance(input_items, list) else ([{"role": "user", "content": input_items}] if input_items else [])
        parents: list[str] = []
        origins: list[str] = []
        with self._lock:
            linked = self._linked.pop(trace_key, None)
            if linked:
                parents += linked
            handoff = self._handed_off.pop((trace_key, agent), None)
            if handoff:
                parents.append(handoff)
            for item in items:
                item_type = _field(item, "type")
                call_id = _field(item, "call_id")
                if item_type == "function_call_output" and call_id in self._event_by_call:
                    tool_event = self._event_by_call[call_id]
                    parents.append(tool_event)
                    parents += self._returned_by_event.get(tool_event, [])
                elif _field(item, "id") in self._event_by_item:
                    parents.append(self._event_by_item[_field(item, "id")])
                elif _field(item, "role") == "user":
                    # A user's message is new content the first time a model sees it. In a
                    # linked workflow, the input came from what started it.
                    digest = hashlib.sha256(_text(item).encode()).hexdigest()
                    if (trace_key, digest) not in self._seen_user:
                        self._seen_user[(trace_key, digest)] = True
                        if not linked and events.ORIGIN_USER not in origins:
                            origins.append(events.ORIGIN_USER)
        otel = self._take(span)
        if otel is None:
            return
        trace_id = otel.get_span_context().trace_id
        input_text = _text(items)
        parents += self._matching_outputs(trace_id, input_text) if input_text else []
        event_id = str(uuid.uuid4())
        self._event(otel, span, event_id, events.MODEL_CALL, f"chat {model}".strip(), agent, parents, origins, {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model,
        }, {"input": _plain(items), "output": _plain(output_items)})
        with self._lock:
            self._last_response[(trace_key, agent)] = event_id
            if response_id:
                self._event_by_item[response_id] = event_id
            for item in output_items:
                if _field(item, "id"):
                    self._event_by_item[_field(item, "id")] = event_id
                if _field(item, "type") == "function_call":
                    key = (trace_key, _field(item, "name"), _field(item, "arguments"))
                    self._call[key] = (_field(item, "call_id"), event_id)
        self._remember_output(trace_id, event_id, _text(output_items))
        otel.end()

    def _function_ended(self, span, data) -> None:
        with self._lock:
            pending = self._pending.pop(span.span_id, None) or {}
        returned = links._returned(pending.get("link"))
        links._exit(pending.get("link"))
        event_id = pending.get("event_id") or str(uuid.uuid4())
        if returned:
            # What the tool got back came from these events, such as an A2A reply: they
            # feed the response that reads its output.
            with self._lock:
                self._returned_by_event[event_id] = returned
        agent = self._agent_for(span)
        otel = self._take(span)
        if otel is None:
            return
        trace_id = otel.get_span_context().trace_id
        parents: list[str] = []
        with self._lock:
            call = self._call.pop((span.trace_id, data.name, data.input), None)
            handoff = self._handed_off.pop((span.trace_id, agent), None)
        if handoff:
            parents.append(handoff)
        if call:
            call_id, requested_by = call
            parents.append(requested_by)
            with self._lock:
                if call_id:
                    self._event_by_call[call_id] = event_id
        elif isinstance(data.input, str) and data.input:
            # No exact match: the arguments themselves point at the response that chose them.
            parents += self._outputs_containing(trace_id, [data.input])
        mcp = data.mcp_data if isinstance(getattr(data, "mcp_data", None), dict) else None
        attributes = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": data.name}
        if mcp and mcp.get("server"):
            attributes["wake.mcp.server"] = str(mcp["server"])
        self._event(otel, span, event_id, events.TOOL_CALL, f"execute_tool {data.name}", agent, parents,
                    [f"tool:{data.name}"], attributes, {"input": data.input, "output": _plain(data.output)})
        self._remember_output(trace_id, event_id, _text(data.output) if not isinstance(data.output, str) else data.output)
        otel.end()

    def _handoff_ended(self, span, data) -> None:
        source, target = data.from_agent or "unknown", data.to_agent or "unknown"
        with self._lock:
            requested_by = self._last_response.get((span.trace_id, source))
        otel = self._take(span)
        if otel is None:
            return
        event_id = str(uuid.uuid4())
        self._event(otel, span, event_id, events.AGENT_MESSAGE, f"agent_message {source} -> {target}", target,
                    [requested_by] if requested_by else [], [f"agent:{source}"],
                    {"gen_ai.agent.name": target}, {"from": source, "to": target})
        with self._lock:
            self._handed_off[(span.trace_id, target)] = event_id
        otel.end()

    # Helpers.

    def _agent_for(self, span) -> str:
        with self._lock:
            sid = span.parent_id
            while sid is not None:
                if sid in self._agent_span:
                    return self._agent_span[sid]
                sid = self._parent.get(sid)
        return self._recorder.agent

    def _context(self, span):
        """Under the closest enclosing span Wake opened, else the workflow's root."""
        with self._lock:
            sid = span.parent_id
            while sid is not None:
                if sid in self._spans:
                    return trace.set_span_in_context(self._spans[sid])
                sid = self._parent.get(sid)
            root = self._roots.get(span.trace_id)
        return trace.set_span_in_context(root) if root is not None else None

    def _take(self, span):
        with self._lock:
            return self._spans.pop(span.span_id, None)

    def _event(self, otel, span, event_id, kind, name, agent, parents, origins, attributes, payload) -> None:
        parents = list(dict.fromkeys(parents))
        self._remember_lineage(event_id, parents, origins)
        otel.update_name(name)
        otel.set_attributes({
            events.EVENT_ID: event_id,
            events.EVENT_KIND: kind,
            events.AGENT_ID: agent,
            events.CAPTURE: "sdk",
            events.PARENTS: parents,
            events.ORIGINS: origins,
            **attributes,
        })
        if span.error:
            payload = {**payload, "error": _plain(span.error)}
            otel.set_status(Status(StatusCode.ERROR, str(_field(span.error, "message") or "error")))
        otel.set_attributes(events.payload_attributes(json.loads(json.dumps(payload, default=str))))
        ctx = otel.get_span_context()
        links._note(links.Link(event_id, ctx.trace_id, ctx.span_id))
