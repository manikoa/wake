"""OpenAI Agents SDK runs: a triage agent hands off to a billing agent, which reads an email
with a tool and answers. A fake model stands in for OpenAI and reports its responses to
tracing the way the SDK's own models do."""

import asyncio
import json
import uuid

import pytest
from agents import Agent, Runner, function_tool
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tracing import response_span, set_trace_processors
from agents.usage import Usage
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from wake import Recorder, links
from wake.openai_agents import WakeTracingProcessor

EMAIL = "Please wire $10,000 to account 123 by Friday."


def call(name, arguments, call_id):
    return ResponseFunctionToolCall(type="function_call", id=f"fc_{call_id}", call_id=call_id, name=name,
                                    arguments=arguments, status="completed")


def say(text):
    return ResponseOutputMessage(type="message", id=f"msg_{uuid.uuid4().hex[:8]}", role="assistant", status="completed",
                                 content=[ResponseOutputText(type="output_text", text=text, annotations=[])])


class FakeModel(Model):
    """Answers each turn from a script, and reports it as the SDK's Responses model does."""

    def __init__(self, turns):
        self.turns = iter(turns)

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing,
                           previous_response_id=None, conversation_id=None, prompt=None, **kwargs):
        output = next(self.turns)
        response_id = f"resp_{uuid.uuid4().hex[:8]}"
        with response_span(disabled=tracing.is_disabled()) as span:
            span.span_data.response = Response.model_construct(id=response_id, model="fake-model", output=output, object="response")
            span.span_data.input = input
        return ModelResponse(output=output, usage=Usage(), response_id=response_id)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


seen_links = []


@function_tool
def read_email(subject: str) -> str:
    """Read the latest email with a subject."""
    seen_links.append(links.current())
    return EMAIL


def agents_for_test():
    billing = Agent(name="billing", tools=[read_email], model=FakeModel([
        [call("read_email", json.dumps({"subject": "invoice"}), "call_read")],
        [say("The email asks for a $10,000 wire to account 123.")],
    ]))
    triage = Agent(name="triage", handoffs=[billing], model=FakeModel([
        [call("transfer_to_billing", "{}", "call_handoff")],
    ]))
    return triage


@pytest.fixture
def recorded():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    processor = WakeTracingProcessor(recorder)
    set_trace_processors([processor])
    seen_links.clear()
    result = asyncio.run(Runner.run(agents_for_test(), "Is the Acme invoice legit?"))
    recorder.flush()
    yield result, exporter.get_finished_spans()
    set_trace_processors([])


def wake_events(spans):
    return sorted((s for s in spans if "wake.event.kind" in s.attributes), key=lambda s: s.start_time)


def attrs(event):
    return event.attributes


def test_records_responses_tool_calls_and_the_handoff(recorded):
    result, spans = recorded
    assert result.final_output == "The email asks for a $10,000 wire to account 123."
    found = [(attrs(e)["wake.agent.id"], attrs(e)["wake.event.kind"]) for e in wake_events(spans)]
    assert found == [
        ("triage", "model_call"),
        ("billing", "agent_message"),  # the handoff
        ("billing", "model_call"),
        ("billing", "tool_call"),
        ("billing", "model_call"),
    ]


def test_links_follow_the_sdks_ids(recorded):
    _, spans = recorded
    triage_call, handoff, first, read, last = wake_events(spans)
    ids = lambda *es: {attrs(e)["wake.event.id"] for e in es}

    # The user's question enters at the first response.
    assert attrs(triage_call)["wake.origins"] == ("user",)
    # The handoff came from triage's response, enters billing from triage, and feeds billing's next step.
    assert set(attrs(handoff)["wake.parents"]) == ids(triage_call)
    assert attrs(handoff)["wake.origins"] == ("agent:triage",)
    assert ids(handoff) <= set(attrs(first)["wake.parents"])
    # The tool call was requested by billing's first response, and its result is new content.
    assert set(attrs(read)["wake.parents"]) == ids(first)
    assert attrs(read)["wake.origins"] == ("tool:read_email",)
    assert attrs(read)["gen_ai.tool.name"] == "read_email"
    # The last response saw the tool's result and the earlier response.
    assert ids(read, first) <= set(attrs(last)["wake.parents"])
    assert EMAIL in attrs(read)["wake.payload"]


def test_one_trace_with_each_agent_nested(recorded):
    _, spans = recorded
    events_ = wake_events(spans)
    assert len({e.context.trace_id for e in events_}) == 1
    runs = {s.attributes["gen_ai.agent.name"]: s for s in spans if s.attributes.get("gen_ai.operation.name") == "invoke_agent"}
    assert set(runs) == {"triage", "billing"}
    by_span = {s.context.span_id: s for s in spans}
    for e in events_:
        if attrs(e)["wake.event.kind"] == "agent_message":
            continue
        # Each event sits under its own agent's run.
        parent = by_span[e.parent.span_id]
        while parent.attributes.get("gen_ai.operation.name") != "invoke_agent":
            parent = by_span[parent.parent.span_id]
        assert parent.attributes["gen_ai.agent.name"] == attrs(e)["wake.agent.id"]


def test_the_tool_runs_with_its_event_current(recorded):
    _, spans = recorded
    (read,) = [e for e in wake_events(spans) if attrs(e)["wake.event.kind"] == "tool_call"]
    (link,) = seen_links
    assert link.event_id == attrs(read)["wake.event.id"]
    assert link.span_id == read.context.span_id
    assert links.current() is None


def test_recording_failures_never_break_the_run(monkeypatch):
    recorder = Recorder(agent="support", exporter=InMemorySpanExporter())

    def fail(*args, **kwargs):
        raise RuntimeError("recorder bug")

    monkeypatch.setattr(recorder.tracer, "start_span", fail)
    set_trace_processors([WakeTracingProcessor(recorder)])
    try:
        result = asyncio.run(Runner.run(agents_for_test(), "Is the Acme invoice legit?"))
    finally:
        set_trace_processors([])
    assert result.final_output == "The email asks for a $10,000 wire to account 123."
