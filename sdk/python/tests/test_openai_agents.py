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
            # As the SDK's own model does, nothing of the exchange without sensitive data.
            if tracing.include_data():
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


def test_a_memory_write_in_a_tool_is_labeled_with_its_origins(tmp_path):
    from wake.memory import Memory

    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    processor = WakeTracingProcessor(recorder)
    memory = Memory(processor)

    @function_tool
    def read_email(subject: str) -> str:
        """Read the latest email with a subject."""
        memory.write_file(tmp_path / "notes.md", EMAIL)
        return EMAIL

    billing = Agent(name="billing", tools=[read_email], model=FakeModel([
        [call("read_email", json.dumps({"subject": "invoice"}), "call_read")],
        [say("Noted.")],
    ]))
    set_trace_processors([processor])
    try:
        asyncio.run(Runner.run(billing, "Is the Acme invoice legit?"))
    finally:
        set_trace_processors([])
    recorder.flush()
    events = wake_events(exporter.get_finished_spans())
    (tool_call,) = [e for e in events if attrs(e)["wake.event.kind"] == "tool_call"]
    (write,) = [e for e in events if attrs(e)["wake.event.kind"] == "memory_write"]
    assert attrs(write)["wake.parents"][0] == attrs(tool_call)["wake.event.id"]
    assert set(attrs(write)["wake.memory.origins"]) == {"tool:read_email", "user"}
    # In the name of the agent whose tool wrote it, not the recorder's.
    assert attrs(write)["wake.agent.id"] == "billing"
    assert json.loads((tmp_path / "notes.md.wake.json").read_text())["agent"] == "billing"


class FakeChatModel(FakeModel):
    """Answers from a script, and reports each turn as the SDK's Chat Completions models
    do, LiteLLM's included: a generation with chat messages in and out."""

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing,
                           previous_response_id=None, conversation_id=None, prompt=None, **kwargs):
        from agents.models.chatcmpl_converter import Converter
        from agents.tracing import generation_span

        output = next(self.turns)
        message = {"role": "assistant", "content": None}
        for item in output:
            if item.type == "function_call":
                message.setdefault("tool_calls", []).append(
                    {"id": item.call_id, "type": "function", "function": {"name": item.name, "arguments": item.arguments}})
            else:
                message["content"] = item.content[0].text
        with generation_span(model="fake-chat", disabled=tracing.is_disabled()) as span:
            if tracing.include_data():
                span.span_data.input = Converter.items_to_messages(input)
                span.span_data.output = [message]
        return ModelResponse(output=output, usage=Usage(), response_id=None)


def run(agent, **run_config):
    from agents import RunConfig

    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    set_trace_processors([WakeTracingProcessor(recorder)])
    try:
        asyncio.run(Runner.run(agent, "Is the Acme invoice legit?", run_config=RunConfig(**run_config)))
    finally:
        set_trace_processors([])
    recorder.flush()
    return wake_events(exporter.get_finished_spans())


def billing_with(model_class):
    return Agent(name="billing", tools=[read_email], model=model_class([
        [call("read_email", json.dumps({"subject": "invoice"}), "call_read")],
        [say("The email asks for a $10,000 wire to account 123.")],
    ]))


def test_chat_completions_messages_link_as_responses_do():
    first, read, last = run(billing_with(FakeChatModel))
    ids = lambda *es: {attrs(e)["wake.event.id"] for e in es}
    assert attrs(first)["wake.origins"] == ("user",)
    # The assistant message's tool call names the response that requested the tool...
    assert set(attrs(read)["wake.parents"]) == ids(first)
    # ...and the tool message in the next input names the tool call.
    assert ids(read) <= set(attrs(last)["wake.parents"])


def test_without_sensitive_data_the_loop_links_events():
    seen_links.clear()
    triage_call, handoff, first, read, last = run(agents_for_test(), trace_include_sensitive_data=False)
    ids = lambda *es: {attrs(e)["wake.event.id"] for e in es}
    # Nothing of the exchange is in the trace, but the run's shape is: the workflow's input
    # enters at the first response, a tool runs from its agent's latest response, and the
    # next response reads that response and the tool's result.
    assert attrs(triage_call)["wake.origins"] == ("user",)
    assert set(attrs(handoff)["wake.parents"]) == ids(triage_call)
    assert ids(handoff) <= set(attrs(first)["wake.parents"])
    assert set(attrs(read)["wake.parents"]) == ids(first)
    assert set(attrs(last)["wake.parents"]) == ids(first, read)
    assert "user" not in attrs(last)["wake.origins"]


def test_an_agent_run_inside_a_tool_descends_from_the_tool_call():
    summarizer = Agent(name="summarizer", model=FakeModel([[say("A request to wire $10,000.")]]))

    @function_tool
    async def summarize(subject: str) -> str:
        """Summarize the latest email with a subject."""
        result = await Runner.run(summarizer, f"Summarize this email: {EMAIL}")
        return result.final_output

    support = Agent(name="support", tools=[summarize], model=FakeModel([
        [call("summarize", json.dumps({"subject": "invoice"}), "call_summary")],
        [say("It asks for a wire.")],
    ]))
    first, tool, nested, last = run(support)
    assert attrs(tool)["wake.event.kind"] == "tool_call"
    assert (attrs(nested)["wake.agent.id"], attrs(nested)["wake.event.kind"]) == ("summarizer", "model_call")
    # The summarizer's input came from the tool call, not from the user.
    assert attrs(tool)["wake.event.id"] in attrs(nested)["wake.parents"]
    assert "user" not in attrs(nested)["wake.origins"]
    assert attrs(first)["wake.origins"] == ("user",)
    # It's an instance, started by support's tool call; support's own events aren't.
    assert attrs(nested)["wake.agent.instance"].startswith("summarizer#")
    assert attrs(nested)["wake.agent.lineage"] == ("support",)
    assert all("wake.agent.instance" not in attrs(e) for e in (first, tool, last))


class ResearcherModel(Model):
    """A researcher that looks its question up, then answers from what it found: by what's
    in its input, so copies running at once each follow their own run. It yields between
    steps, so copies interleave as they would on a real model."""

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing,
                           previous_response_id=None, conversation_id=None, prompt=None, **kwargs):
        await asyncio.sleep(0)
        found = [i for i in input if _type(i) == "function_call_output"] if isinstance(input, list) else []
        question = input if isinstance(input, str) else next(
            (i["content"] for i in input if isinstance(i, dict) and i.get("role") == "user"), "")
        output = [say(f"Answer: {found[-1]['output']}")] if found else [
            call("lookup", json.dumps({"q": question}), f"call_{uuid.uuid4().hex[:8]}")]
        with response_span(disabled=tracing.is_disabled()) as span:
            if tracing.include_data():
                span.span_data.response = Response.model_construct(id=f"resp_{uuid.uuid4().hex[:8]}", model="fake-model",
                                                                   output=output, object="response")
                span.span_data.input = input
        await asyncio.sleep(0)
        return ModelResponse(output=output, usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


def _type(item):
    return item.get("type") if isinstance(item, dict) else getattr(item, "type", None)


def two_researchers(memory=None):
    @function_tool
    async def lookup(q: str) -> str:
        """Look a question up."""
        await asyncio.sleep(0)
        if memory is not None:
            memory.write_file(memory_dir / f"{uuid.uuid4().hex}.md", f"Found: {q}")
        return f"Found: {q}"

    researcher = Agent(name="researcher", tools=[lookup], model=ResearcherModel())
    research = researcher.as_tool(tool_name="research", tool_description="Research one question.")
    # The same question twice: nothing in the arguments tells the copies apart.
    return Agent(name="triage", tools=[research], model=FakeModel([
        [call("research", json.dumps({"input": "refund window"}), "call_a"),
         call("research", json.dumps({"input": "refund window"}), "call_b")],
        [say("Both answered.")],
    ]))


memory_dir = None


@pytest.mark.parametrize("sensitive", [True, False])
def test_an_agent_called_twice_as_a_tool_is_two_instances_that_never_cross(sensitive, tmp_path):
    from agents import RunConfig
    from wake.memory import Memory

    global memory_dir
    memory_dir = tmp_path
    exporter = InMemorySpanExporter()
    processor = WakeTracingProcessor(Recorder(agent="support", exporter=exporter))
    set_trace_processors([processor])
    try:
        asyncio.run(Runner.run(two_researchers(Memory(processor)), "Check our refund terms.",
                               run_config=RunConfig(trace_include_sensitive_data=sensitive)))
    finally:
        set_trace_processors([])
    processor._recorder.flush()
    events_ = wake_events(exporter.get_finished_spans())

    by_instance: dict[str, list] = {}
    for e in events_:
        if attrs(e)["wake.agent.id"] == "researcher":
            assert attrs(e)["wake.agent.lineage"] == ("triage",), attrs(e)
            by_instance.setdefault(attrs(e)["wake.agent.instance"], []).append(e)
        else:
            assert "wake.agent.instance" not in attrs(e), attrs(e)
    assert len(by_instance) == 2, list(by_instance)
    triage_ids = {attrs(e)["wake.event.id"] for e in events_ if attrs(e)["wake.agent.id"] == "triage"}
    for instance, own in by_instance.items():
        kinds = sorted(attrs(e)["wake.event.kind"] for e in own)
        assert kinds == ["memory_write", "model_call", "model_call", "tool_call"], (instance, kinds)
        own_ids = {attrs(e)["wake.event.id"] for e in own}
        for e in own:
            # Each copy links only within itself, and to the triage call that started it.
            crossed = set(attrs(e)["wake.parents"]) - own_ids - triage_ids
            assert not crossed, f"{instance} {attrs(e)['wake.event.kind']} links to the other copy: {crossed}"
        first, last = sorted((e for e in own if attrs(e)["wake.event.kind"] == "model_call"), key=lambda e: e.start_time)
        (lookup,) = [e for e in own if attrs(e)["wake.event.kind"] == "tool_call"]
        # Its tool ran from its own first response, and its answer read that tool's result.
        assert set(attrs(lookup)["wake.parents"]) == {attrs(first)["wake.event.id"]}
        assert attrs(lookup)["wake.event.id"] in attrs(last)["wake.parents"]
