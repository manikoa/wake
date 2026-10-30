"""OpenAI Agents SDK runs: a triage agent hands off to a billing agent, which reads an email
with a tool and answers. A fake model stands in for OpenAI and reports its responses to
tracing the way the SDK's own models do."""

import asyncio
import json
import uuid
from pathlib import Path

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


def researcher_agent(memory=None):
    @function_tool
    async def lookup(q: str) -> str:
        """Look a question up."""
        await asyncio.sleep(0)
        if memory is not None:
            memory.write_file(memory_dir / f"{uuid.uuid4().hex}.md", f"Found: {q}")
        return f"Found: {q}"

    return Agent(name="researcher", tools=[lookup], model=ResearcherModel())


def two_researchers(memory=None, question="refund window"):
    research = researcher_agent(memory).as_tool(tool_name="research", tool_description="Research one question.")
    # The same question twice: nothing in the arguments tells the copies apart.
    return Agent(name="triage", tools=[research], model=FakeModel([
        [call("research", json.dumps({"input": question}), "call_a"),
         call("research", json.dumps({"input": question}), "call_b")],
        [say("Both answered.")],
    ]))


memory_dir = None
# Long enough that content matching would link two copies' steps, as their answers match.
LONG_QUESTION = "What is the refund window for damaged items?"


@pytest.mark.parametrize("question", ["refund window", LONG_QUESTION], ids=["short", "long"])
@pytest.mark.parametrize("sensitive", [True, False])
def test_an_agent_called_twice_as_a_tool_is_two_instances_that_never_cross(sensitive, question, tmp_path):
    from agents import RunConfig
    from wake.memory import Memory

    global memory_dir
    memory_dir = tmp_path
    exporter = InMemorySpanExporter()
    processor = WakeTracingProcessor(Recorder(agent="support", exporter=exporter))
    set_trace_processors([processor])
    try:
        asyncio.run(Runner.run(two_researchers(Memory(processor), question), "Check our refund terms.",
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


# Cooperative stops.


class Halts:
    """Stands in for the brake's rules (wake.quarantine.Quarantine): the halts in force."""

    def __init__(self, *halts) -> None:
        self.halts = list(halts)

    def halted(self, names):
        for h in self.halts:
            if h.get("fleet") or set(names) & set(h.get("agents", [])):
                return h["id"]
        return None


class HaltOneResearcher:
    """Halts the first copy of the researcher it's asked about."""

    def __init__(self) -> None:
        self.instance = None

    def halted(self, names):
        for name in sorted(names):
            if name.startswith("researcher#"):
                self.instance = self.instance or name
                if name == self.instance:
                    return "halt-researcher"
        return None


class BrokenHalts:
    """Rules whose check fails."""

    def halted(self, names):
        raise RuntimeError("the rules can't be read")


def guarded_run(agent, quarantine, **run_config):
    from agents import RunConfig

    exporter = InMemorySpanExporter()
    processor = WakeTracingProcessor(Recorder(agent="support", exporter=exporter), quarantine=quarantine)
    set_trace_processors([processor])
    try:
        result = asyncio.run(Runner.run(processor.guard(agent), "Check our refund terms.",
                                        run_config=RunConfig(**run_config)))
    finally:
        set_trace_processors([])
    processor._recorder.flush()
    return result, wake_events(exporter.get_finished_spans()), processor


def kinds_of(events_, *, tool=None):
    return [attrs(e)["wake.event.kind"] for e in events_ if tool is None or attrs(e).get("gen_ai.tool.name") == tool]


def test_a_halted_instance_is_stopped_in_process_and_its_sibling_goes_on():
    halts = HaltOneResearcher()
    _, events_, _ = guarded_run(two_researchers(), halts)
    lookups = [e for e in events_ if attrs(e).get("gen_ai.tool.name") == "lookup"]
    assert sorted(kinds_of(lookups)) == ["denied", "tool_call"], "only the halted copy's call was refused"
    (denied,) = [e for e in lookups if attrs(e)["wake.event.kind"] == "denied"]
    assert attrs(denied)["wake.brake.halt_id"] == "halt-researcher"
    assert attrs(denied)["wake.agent.instance"] == halts.instance, "recorded as the halted copy's attempt"
    assert attrs(denied)["wake.agent.lineage"] == ("triage",)
    assert attrs(denied)["wake.origins"] == (), "the tool never ran: nothing of it entered"
    assert json.loads(attrs(denied)["wake.payload"])["output"].startswith(f"Wake halted {halts.instance} (halt-researcher)")
    (ran,) = [e for e in lookups if attrs(e)["wake.event.kind"] == "tool_call"]
    assert attrs(ran)["wake.agent.instance"] != halts.instance
    # The refused call has the response that asked for it as its parent, like any call.
    responses = {attrs(e)["wake.event.id"] for e in events_ if attrs(e)["wake.event.kind"] == "model_call"
                 and attrs(e).get("wake.agent.instance") == halts.instance}
    assert set(attrs(denied)["wake.parents"]) <= responses and attrs(denied)["wake.parents"]


def test_a_halted_agents_spawn_starts_nothing():
    halts = Halts({"id": "halt-triage", "agents": ["triage"]})
    result, events_, _ = guarded_run(two_researchers(), halts)
    assert [(attrs(e)["wake.agent.id"], attrs(e)["wake.event.kind"], attrs(e).get("gen_ai.tool.name")) for e in events_] == [
        ("triage", "model_call", None),
        ("triage", "denied", "research"),
        ("triage", "denied", "research"),
        ("triage", "model_call", None),
    ], "no copy of the researcher started"
    assert {attrs(e)["wake.brake.halt_id"] for e in events_ if attrs(e)["wake.event.kind"] == "denied"} == {"halt-triage"}
    assert result.final_output == "Both answered."


def test_stale_rules_refuse_every_call():
    from wake.quarantine import Quarantine

    never_fetched = Quarantine("http://127.0.0.1:9", agent="support", start=False)
    _, events_, _ = guarded_run(two_researchers(), never_fetched)
    assert kinds_of(events_) == ["model_call", "denied", "denied", "model_call"]
    assert {attrs(e)["wake.brake.halt_id"] for e in events_ if attrs(e)["wake.event.kind"] == "denied"} == {"stale-rules"}


def test_a_halt_check_that_fails_refuses_the_call():
    _, events_, _ = guarded_run(two_researchers(), BrokenHalts())
    assert kinds_of(events_) == ["model_call", "denied", "denied", "model_call"]
    (refused, _) = [e for e in events_ if attrs(e)["wake.event.kind"] == "denied"]
    assert attrs(refused)["wake.brake.halt_id"] == "halt-check-failed"
    assert json.loads(attrs(refused)["wake.payload"])["output"] == "Wake couldn't check its halts, so this tool call wasn't run."


def test_while_wake_cant_place_a_run_a_halt_on_any_copy_of_its_agent_stops_it():
    halts = HaltOneResearcher()
    _, _, processor = guarded_run(two_researchers(), halts)
    # Outside any run: Wake can't tell which copy of the researcher this would be.
    refused = processor._refusal("researcher")
    assert refused is not None and refused.halt == "halt-researcher"
    assert f"can't yet tell which copy of researcher is calling" in refused.reason
    assert processor._refusal("triage") is None


class OfferedModel(FakeModel):
    """Answers at once, noting the handoffs it was offered."""

    def __init__(self) -> None:
        super().__init__([[say("Done.")]])
        self.offered = None

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing,
                           previous_response_id=None, conversation_id=None, prompt=None, **kwargs):
        self.offered = [h.tool_name for h in handoffs]
        return await super().get_response(system_instructions, input, model_settings, tools, output_schema, handoffs,
                                          tracing, previous_response_id, conversation_id, prompt, **kwargs)


@pytest.mark.parametrize("halted", [False, True], ids=["running", "halted"])
def test_a_halted_agent_is_offered_no_handoff(halted):
    model = OfferedModel()
    billing = Agent(name="billing", model=FakeModel([[say("Paid.")]]))
    triage = Agent(name="triage", handoffs=[billing], model=model)
    guarded_run(triage, Halts(*[{"id": "halt-triage", "agents": ["triage"]}] * halted))
    assert model.offered == ([] if halted else ["transfer_to_billing"])


def test_guard_reaches_agents_used_as_tools_and_handed_off_to_once():
    @function_tool
    def look(q: str) -> str:
        """Look a question up."""
        return q

    @function_tool
    def pay(order: str) -> str:
        """Pay an order."""
        return order

    processor = WakeTracingProcessor(Recorder(agent="support", exporter=InMemorySpanExporter()), quarantine=Halts())
    researcher = Agent(name="researcher", tools=[look])
    billing = Agent(name="billing", tools=[pay])
    triage = Agent(name="triage", tools=[researcher.as_tool(tool_name="research", tool_description="Research.")],
                   handoffs=[billing])
    processor.guard(triage)
    processor.guard(triage)
    guarded = [processor._guardrail]
    assert look.tool_input_guardrails == guarded, "the agent used as a tool, once however often it's guarded"
    assert pay.tool_input_guardrails == guarded, "the agent handed off to"
    assert triage.tools[0].tool_input_guardrails == guarded, "the agent tool itself: a spawn"
    with pytest.raises(ValueError):
        WakeTracingProcessor(Recorder(agent="support", exporter=InMemorySpanExporter())).guard(triage)


# Tools the agent runs in its own process.


class Editor:
    """An apply_patch editor that notes each change it makes."""

    def __init__(self, ran) -> None:
        self.ran = ran

    def create_file(self, operation):
        self.ran.append(operation.path)
        return "created"

    def update_file(self, operation):
        self.ran.append(operation.path)
        return "updated"

    def delete_file(self, operation):
        self.ran.append(operation.path)
        return "deleted"


def local_tool(kind, ran):
    """A tool the agent runs itself, and the model's call to it."""
    from agents import ApplyPatchTool, CustomTool, LocalShellTool, ShellTool
    from openai.types.responses import ResponseApplyPatchToolCall, ResponseCustomToolCall, ResponseFunctionShellToolCall
    from openai.types.responses.response_output_item import LocalShellCall, LocalShellCallAction

    if kind == "local_shell":
        return LocalShellTool(executor=lambda request: ran.append(request.data.action.command) or "ok"), LocalShellCall(
            type="local_shell_call", id="lsh_1", call_id="call_x", status="completed",
            action=LocalShellCallAction(type="exec", command=["rm", "-rf", "/srv/orders"], env={}))
    if kind == "shell":
        return ShellTool(executor=lambda request: ran.append(request.data.action.commands) or "ok"), ResponseFunctionShellToolCall(
            type="shell_call", id="sh_1", call_id="call_x", status="completed", action={"commands": ["rm -rf /srv/orders"]})
    if kind == "run_sql":
        return CustomTool(name="run_sql", description="Run SQL.", on_invoke_tool=lambda ctx, text: ran.append(text) or "ok"), \
            ResponseCustomToolCall(type="custom_tool_call", id="ctc_1", call_id="call_x", name="run_sql", input="DELETE FROM orders")
    return ApplyPatchTool(editor=Editor(ran)), ResponseApplyPatchToolCall(
        type="apply_patch_call", id="ap_1", call_id="call_x", status="completed",
        operation={"type": "delete_file", "path": "orders.db"})


@pytest.mark.parametrize("kind", ["local_shell", "shell", "run_sql", "apply_patch"])
def test_a_halted_agents_own_tools_are_refused_as_they_start(kind):
    ran = []
    tool, requested = local_tool(kind, ran)
    triage = Agent(name="triage", tools=[tool], model=FakeModel([[requested], [say("Done.")]]))
    result, events_, _ = guarded_run(triage, Halts({"id": "halt-triage", "agents": ["triage"]}))
    assert ran == [], "the tool never ran"
    assert result.final_output == "Done."
    first, denied, second = events_
    assert kinds_of(events_) == ["model_call", "denied", "model_call"]
    assert attrs(denied)["wake.brake.halt_id"] == "halt-triage"
    assert attrs(denied)["gen_ai.tool.name"] == kind
    assert attrs(denied)["wake.agent.id"] == "triage"
    assert attrs(denied)["wake.origins"] == (), "the tool never ran: nothing of it entered"
    assert attrs(denied)["wake.parents"] == (attrs(first)["wake.event.id"],), "the response that asked for it"
    assert attrs(denied)["wake.event.id"] in attrs(second)["wake.parents"], "the model read the refusal"
    reason = "Wake halted triage (halt-triage), so this tool call wasn't run."
    assert reason in attrs(second)["wake.payload"], "the refusal is the tool's output"
    # Without a halt the tool runs.
    ran.clear()
    tool, requested = local_tool(kind, ran)
    triage = Agent(name="triage", tools=[tool], model=FakeModel([[requested], [say("Done.")]]))
    _, events_, _ = guarded_run(triage, Halts())
    assert len(ran) == 1 and "denied" not in kinds_of(events_)


class Screen:
    """A computer that notes each action it carries out."""

    def __init__(self) -> None:
        self.did = []

    def screenshot(self) -> str:
        self.did.append("screenshot")
        return "iVBORw0KGgo="

    def click(self, x, y, button) -> None:
        self.did.append(("click", x, y))

    def double_click(self, x, y) -> None:
        self.did.append(("double_click", x, y))

    def scroll(self, x, y, scroll_x, scroll_y) -> None:
        self.did.append("scroll")

    def type(self, text) -> None:
        self.did.append(("type", text))

    def wait(self) -> None:
        self.did.append("wait")

    def move(self, x, y) -> None:
        self.did.append("move")

    def keypress(self, keys) -> None:
        self.did.append(("keypress", keys))

    def drag(self, path) -> None:
        self.did.append("drag")


@pytest.mark.parametrize("made", [False, True], ids=["computer", "made_per_run"])
def test_a_halted_agents_computer_does_nothing_and_its_run_ends(made):
    from agents import Computer, ComputerTool
    from openai.types.responses import ResponseComputerToolCall

    from wake.stop import AgentHalted

    screen = type("RealScreen", (Screen, Computer), {})()
    tool = ComputerTool(computer=(lambda **_: screen) if made else screen)
    clicked = ResponseComputerToolCall(type="computer_call", id="cu_1", call_id="call_x", status="completed",
                                       pending_safety_checks=[], action={"type": "click", "x": 1, "y": 2, "button": "left"})
    triage = Agent(name="triage", tools=[tool], model=FakeModel([[clicked], [say("Done.")]]))
    exporter = InMemorySpanExporter()
    processor = WakeTracingProcessor(Recorder(agent="support", exporter=exporter),
                                     quarantine=Halts({"id": "halt-triage", "agents": ["triage"]}))
    set_trace_processors([processor])
    try:
        with pytest.raises(AgentHalted) as refused:
            asyncio.run(Runner.run(processor.guard(triage), "Click it."))
    finally:
        set_trace_processors([])
    processor._recorder.flush()
    assert screen.did == [], "the computer did nothing"
    assert refused.value.halt == "halt-triage"
    events_ = wake_events(exporter.get_finished_spans())
    assert kinds_of(events_) == ["model_call", "denied"]
    assert attrs(events_[1])["wake.brake.halt_id"] == "halt-triage"
    assert attrs(events_[1])["wake.origins"] == ()


def test_guard_raises_rather_than_leave_a_tool_it_cant_refuse_and_checks_hosted_tools_around_the_model_call():
    from agents import WebSearchTool

    class Gadget:
        name = "gadget"

    triage = Agent(name="triage", tools=[WebSearchTool()])
    processor = WakeTracingProcessor(Recorder(agent="support", exporter=InMemorySpanExporter()), quarantine=Halts())
    processor.guard(triage)
    assert isinstance(triage.tools[0], WebSearchTool), "a hosted tool runs at OpenAI: it's checked before each model call"
    assert getattr(triage.hooks.on_llm_start, "_wake", False)
    ops = Agent(name="ops")
    ops.tools.append(Gadget())  # a kind of tool this version of Wake doesn't know
    with pytest.raises(TypeError, match="Gadget"):
        processor.guard(ops)


# Handoffs.


@pytest.mark.parametrize("halted", ["triage", "billing"])
def test_a_handoff_to_or_from_a_halted_agent_isnt_offered(halted):
    model = OfferedModel()
    billing = Agent(name="billing", model=FakeModel([[say("Paid.")]]))
    triage = Agent(name="triage", handoffs=[billing], model=model)
    guarded_run(triage, Halts({"id": f"halt-{halted}", "agents": [halted]}))
    assert model.offered == []


class HaltDuringTurn(FakeModel):
    """The halt lands while the model is answering the turn that hands off."""

    def __init__(self, turns, halts, halt):
        super().__init__(turns)
        self.halts, self.halt = halts, halt

    async def get_response(self, *args, **kwargs):
        self.halts.halts.append(self.halt)
        return await super().get_response(*args, **kwargs)


@pytest.mark.parametrize("halted", ["triage", "billing"])
def test_a_handoff_picked_before_a_halt_landed_is_refused_as_it_starts(halted):
    from wake.stop import AgentHalted

    halts, paid = Halts(), []

    @function_tool
    def pay(order: str) -> str:
        """Pay an order."""
        paid.append(order)
        return "Paid."

    billing = Agent(name="billing", tools=[pay], model=FakeModel([
        [call("pay", json.dumps({"order": "1234"}), "call_pay")], [say("Paid.")]]))
    triage = Agent(name="triage", handoffs=[billing], model=HaltDuringTurn(
        [[call("transfer_to_billing", "{}", "call_h")]], halts, {"id": f"halt-{halted}", "agents": [halted]}))
    exporter = InMemorySpanExporter()
    processor = WakeTracingProcessor(Recorder(agent="support", exporter=exporter), quarantine=halts)
    set_trace_processors([processor])
    try:
        with pytest.raises(AgentHalted) as refused:
            asyncio.run(Runner.run(processor.guard(triage), "Refund 1234."))
    finally:
        set_trace_processors([])
    processor._recorder.flush()
    assert paid == [], "billing never ran"
    assert refused.value.halt == f"halt-{halted}"
    assert str(refused.value).endswith("so the handoff to billing wasn't made.")
    response, denied = wake_events(exporter.get_finished_spans())
    assert kinds_of([response, denied]) == ["model_call", "denied"]
    assert denied.name == "agent_message triage -> billing"
    assert attrs(denied)["wake.agent.id"] == "triage", "recorded as what tried to hand off"
    assert attrs(denied)["wake.brake.receiver"] == "billing"
    assert attrs(denied)["wake.brake.halt_id"] == f"halt-{halted}"
    assert attrs(denied)["wake.parents"] == (attrs(response)["wake.event.id"],)
    assert attrs(denied)["wake.origins"] == (), "nothing reached billing"


# Runs of one agent at once.


def researching_twice_in_one_tool():
    researcher = researcher_agent()

    @function_tool
    async def research_twice(question: str) -> str:
        """Research a question twice over."""
        answers = await asyncio.gather(*(Runner.run(researcher, question) for _ in range(2)))
        return "\n".join(a.final_output for a in answers)

    return Agent(name="triage", tools=[research_twice], model=FakeModel([
        [call("research_twice", json.dumps({"question": LONG_QUESTION}), "call_twice")],
        [say("Both answered.")],
    ]))


def test_two_runs_of_one_agent_inside_one_tool_call_are_two_instances_that_never_cross():
    events_ = run(researching_twice_in_one_tool())
    by_instance: dict[str, list] = {}
    for e in events_:
        if attrs(e)["wake.agent.id"] == "researcher":
            assert attrs(e)["wake.agent.lineage"] == ("triage",)
            by_instance.setdefault(attrs(e)["wake.agent.instance"], []).append(e)
    assert len(by_instance) == 2, list(by_instance)
    tool = next(e for e in events_ if attrs(e).get("gen_ai.tool.name") == "research_twice")
    triage_ids = {attrs(e)["wake.event.id"] for e in events_ if attrs(e)["wake.agent.id"] == "triage"}
    last_answers = set()
    for instance, own in by_instance.items():
        assert sorted(kinds_of(own)) == ["model_call", "model_call", "tool_call"], instance
        own_ids = {attrs(e)["wake.event.id"] for e in own}
        for e in own:
            crossed = set(attrs(e)["wake.parents"]) - own_ids - triage_ids
            assert not crossed, f"{instance} {attrs(e)['wake.event.kind']} links to the other copy: {crossed}"
        first, last = sorted((e for e in own if attrs(e)["wake.event.kind"] == "model_call"), key=lambda e: e.start_time)
        assert attrs(tool)["wake.event.id"] in attrs(first)["wake.parents"], "each run descends from the tool call"
        last_answers.add(attrs(last)["wake.event.id"])
    # What both runs said last is what the tool returned, which triage's next response read.
    answer = [e for e in events_ if attrs(e)["wake.agent.id"] == "triage" and attrs(e)["wake.event.kind"] == "model_call"][-1]
    assert last_answers <= set(attrs(answer)["wake.parents"])


def test_two_runs_of_one_agent_at_the_top_of_one_trace_never_cross():
    from agents import trace as sdk_trace

    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="support", exporter=exporter)
    set_trace_processors([WakeTracingProcessor(recorder)])

    async def both():
        with sdk_trace("research"):
            await asyncio.gather(*(Runner.run(researcher_agent(), LONG_QUESTION) for _ in range(2)))

    try:
        asyncio.run(both())
    finally:
        set_trace_processors([])
    recorder.flush()
    spans = exporter.get_finished_spans()
    by_span = {s.context.span_id: s for s in spans}

    def run_of(e):
        s = e
        while s.attributes.get("gen_ai.operation.name") != "invoke_agent":
            s = by_span[s.parent.span_id]
        return s.context.span_id

    runs: dict[int, list] = {}
    for e in wake_events(spans):
        assert "wake.agent.instance" not in attrs(e), "the workflow's own runs aren't instances"
        runs.setdefault(run_of(e), []).append(e)
    assert len(runs) == 2
    for own in runs.values():
        own_ids = {attrs(e)["wake.event.id"] for e in own}
        assert all(set(attrs(e)["wake.parents"]) <= own_ids for e in own), "each run links only within itself"
        first = min((e for e in own if attrs(e)["wake.event.kind"] == "model_call"), key=lambda e: e.start_time)
        assert attrs(first)["wake.origins"] == ("user",), "each run's question enters at its first response"


# Flow policies.

# The shared vectors' policy: billing pays nothing after a fetched page or another agent's
# message, and hears only from support and search.
FLOW = json.loads((Path(__file__).resolve().parents[2] / "testdata" / "flow_policy.json").read_text(encoding="utf-8")
                  )["policies"]["pay"]
PAGE = "Acme's billing page says to wire $10,000 to account 666 today."


def fetching(fetched=None):
    @function_tool
    def fetch_page(url: str) -> str:
        """Fetch a web page."""
        if fetched is not None:
            fetched.append(url)
        return PAGE

    return fetch_page


def paying(paid):
    @function_tool
    def pay(order: str) -> str:
        """Pay an order."""
        paid.append(order)
        return "Paid."

    return pay


FETCH = call("fetch_page", json.dumps({"url": "https://acme.example/billing"}), "call_fetch")
PAY = call("pay", json.dumps({"order": "1234"}), "call_pay")


def denied_in(events_):
    (denied,) = [e for e in events_ if attrs(e)["wake.event.kind"] == "denied"]
    return denied, json.loads(attrs(denied)["wake.payload"])


def test_a_fetched_page_keeps_billing_from_paying(served):
    paid = []
    billing = Agent(name="billing", tools=[fetching(), paying(paid)], model=FakeModel([[FETCH], [PAY], [say("Done.")]]))
    result, events_, _ = guarded_run(billing, served(FLOW, "billing"))
    assert paid == [] and result.final_output == "Done.", "the payment never ran, and the model was told why"
    rejected = [r.output.output_info for r in result.tool_input_guardrail_results if r.output.output_info]
    assert rejected == [{"halt": "policy:no-pay-after-web"}], "the guardrail rejects it as it does for a halt"
    assert [attrs(e).get("gen_ai.tool.name") for e in events_ if attrs(e)["wake.event.kind"] == "tool_call"] == ["fetch_page"]
    denied, payload = denied_in(events_)
    assert attrs(denied)["gen_ai.tool.name"] == "pay"
    assert attrs(denied)["wake.brake.halt_id"] == "policy:no-pay-after-web"
    assert attrs(denied)["wake.origins"] == ()
    assert payload["output"] == ("Wake's flow policy (policy:no-pay-after-web) holds billing back after tool:fetch_page, "
                                 "so this tool call wasn't run.")
    assert payload["origins"] == ["tool:fetch_page"]


@pytest.mark.parametrize("kind", ["local_shell", "shell", "run_sql", "apply_patch"])
def test_a_fetched_page_keeps_the_agents_own_tools_from_running(served, kind):
    ran = []
    tool, requested = local_tool(kind, ran)
    policy = {"rules": [{"id": "nothing-local-after-web", "tools": [kind], "forbid_origins": ["tool:fetch_page"]}]}
    triage = Agent(name="triage", tools=[fetching(), tool], model=FakeModel([[FETCH], [requested], [say("Done.")]]))
    result, events_, _ = guarded_run(triage, served(policy, "triage"))
    assert ran == [] and result.final_output == "Done.", "the tool never ran"
    denied, payload = denied_in(events_)
    assert attrs(denied)["gen_ai.tool.name"] == kind
    assert attrs(denied)["wake.brake.halt_id"] == "policy:nothing-local-after-web"
    assert payload["origins"] == ["tool:fetch_page"]
    reason = ("Wake's flow policy (policy:nothing-local-after-web) holds triage back after tool:fetch_page, "
              "so this tool call wasn't run.")
    assert any(reason in attrs(e)["wake.payload"] for e in events_ if attrs(e)["wake.event.kind"] == "model_call"), \
        "the refusal is the tool's output, which the model reads"


def test_an_agent_used_as_a_tool_answers_with_what_entered_it(served):
    paid = []
    research = Agent(name="research", tools=[fetching()], model=FakeModel([[FETCH], [say("It asks for a wire.")]]))
    billing = Agent(name="billing", tools=[research.as_tool(tool_name="research", tool_description="Research."),
                                           paying(paid)],
                    model=FakeModel([[call("research", json.dumps({"input": "Is Acme's invoice legit?"}), "call_r")],
                                     [PAY], [say("Done.")]]))
    _, events_, _ = guarded_run(billing, served(FLOW, "billing"))
    assert paid == []
    _, payload = denied_in(events_)
    assert payload["origins"] == ["agent:research", "tool:fetch_page"], "research's answer brought the page"


def test_calling_an_agent_a_rule_keeps_from_the_web_is_refused(served):
    asked = []

    class Billing(FakeModel):
        async def get_response(self, *args, **kwargs):
            asked.append("billing")
            return await super().get_response(*args, **kwargs)

    billing = Agent(name="billing", model=Billing([[say("Paid.")]]))
    research = Agent(name="research", tools=[fetching(), billing.as_tool(tool_name="ask_billing", tool_description="Ask.")],
                     model=FakeModel([[FETCH], [call("ask_billing", json.dumps({"input": "Pay 1234"}), "call_b")],
                                      [say("Done.")]]))
    _, events_, _ = guarded_run(research, served(FLOW, "research"))
    assert asked == [], "billing's run never started"
    denied, payload = denied_in(events_)
    assert attrs(denied)["gen_ai.tool.name"] == "ask_billing"
    assert attrs(denied)["wake.brake.halt_id"] == "policy:billing-hears-support-only"
    assert payload["output"] == ("Wake's flow policy (policy:billing-hears-support-only) holds research back after "
                                 "tool:fetch_page, so this tool call wasn't run.")


class OfferedEachTurn(FakeModel):
    """Answers from a script, noting the handoffs offered each turn."""

    def __init__(self, turns) -> None:
        super().__init__(turns)
        self.offered = []

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing,
                           previous_response_id=None, conversation_id=None, prompt=None, **kwargs):
        self.offered.append([h.tool_name for h in handoffs])
        return await super().get_response(system_instructions, input, model_settings, tools, output_schema, handoffs,
                                          tracing, previous_response_id, conversation_id, prompt, **kwargs)


def test_a_handoff_the_policy_would_refuse_isnt_offered(served):
    billing = Agent(name="billing", model=FakeModel([[say("Paid.")]]))
    model = OfferedEachTurn([[FETCH], [say("I can't hand this to billing.")]])
    research = Agent(name="research", tools=[fetching()], handoffs=[billing], model=model)
    guarded_run(research, served(FLOW, "research"))
    assert model.offered == [["transfer_to_billing"], []], "offered until a fetched page entered research"


def test_a_handoff_picked_before_a_page_entered_is_refused_as_it_starts(served):
    from wake.stop import AgentHalted

    # Picked in the same turn as the fetch, which runs first.
    paid = []
    billing = Agent(name="billing", tools=[paying(paid)], model=FakeModel([[PAY], [say("Paid.")]]))
    research = Agent(name="research", tools=[fetching()], handoffs=[billing],
                     model=FakeModel([[FETCH, call("transfer_to_billing", "{}", "call_h")]]))
    exporter = InMemorySpanExporter()
    processor = WakeTracingProcessor(Recorder(agent="support", exporter=exporter), quarantine=served(FLOW, "research"))
    set_trace_processors([processor])
    try:
        with pytest.raises(AgentHalted) as refused:
            asyncio.run(Runner.run(processor.guard(research), "Check our refund terms."))
    finally:
        set_trace_processors([])
    processor._recorder.flush()
    assert paid == [], "billing never ran"
    assert refused.value.halt == "policy:billing-hears-support-only"
    assert str(refused.value) == ("Wake's flow policy (policy:billing-hears-support-only) holds research back after "
                                  "tool:fetch_page, so the handoff to billing wasn't made.")
    denied, payload = denied_in(wake_events(exporter.get_finished_spans()))
    assert denied.name == "agent_message research -> billing"
    assert attrs(denied)["wake.brake.receiver"] == "billing"
    assert payload["origins"] == ["tool:fetch_page"]


def test_a_handoff_brings_what_entered_the_agent_handing_off(served):
    paid = []
    only_pay = {"window_seconds": 600, "rules": [FLOW["rules"][0]]}
    billing = Agent(name="billing", tools=[paying(paid)], model=FakeModel([[PAY], [say("Couldn't pay.")]]))
    research = Agent(name="research", tools=[fetching()], handoffs=[billing],
                     model=FakeModel([[FETCH], [call("transfer_to_billing", "{}", "call_h")]]))
    _, events_, _ = guarded_run(research, served(only_pay, "research"))
    assert paid == []
    denied, payload = denied_in(events_)
    assert attrs(denied)["wake.agent.id"] == "billing"
    assert payload["origins"] == ["agent:research", "tool:fetch_page"]


def test_a_policy_wake_proxy_cant_use_refuses_every_call_and_offers_no_handoff(served):
    fetched = []
    model = OfferedEachTurn([[FETCH], [say("Done.")]])
    billing = Agent(name="billing", model=FakeModel([[say("Paid.")]]))
    research = Agent(name="research", tools=[fetching(fetched)], handoffs=[billing], model=model)
    _, events_, _ = guarded_run(research, served({"unreadable": True}, "research"))
    assert fetched == [] and model.offered == [[], []]
    denied, payload = denied_in(events_)
    assert attrs(denied)["wake.brake.halt_id"] == "policy:unreadable-policy"
    assert payload["output"] == "Wake's flow policy can't be read, so this tool call wasn't run."


def test_without_a_policy_nothing_is_refused(served):
    paid = []
    billing = Agent(name="billing", tools=[fetching(), paying(paid)], model=FakeModel([[FETCH], [PAY], [say("Done.")]]))
    _, events_, _ = guarded_run(billing, served(None, "billing"))
    assert paid == ["1234"] and "denied" not in kinds_of(events_)


# Hosted tools: they run at OpenAI, inside the model call.


def searched(query="Acme invoice"):
    from openai.types.responses.response_function_web_search import ActionSearch, ResponseFunctionWebSearch

    return ResponseFunctionWebSearch(type="web_search_call", id=f"ws_{uuid.uuid4().hex[:8]}", status="completed",
                                     action=ActionSearch(type="search", query=query))


def asking(name="lookup_order", arguments='{"order": "1234"}'):
    """A hosted MCP tool asking to call the MCP server's tool name."""
    from openai.types.responses.response_output_item import McpApprovalRequest

    return McpApprovalRequest(type="mcp_approval_request", id=f"mcpr_{uuid.uuid4().hex[:8]}", arguments=arguments,
                              name=name, server_label="crm")


def mcp_called(name="lookup_order", output="Order 1234: paid in full."):
    from openai.types.responses.response_output_item import McpCall

    return McpCall(type="mcp_call", id=f"mcp_{uuid.uuid4().hex[:8]}", arguments='{"order": "1234"}', name=name,
                   server_label="crm", output=output, status="completed")


def crm(require_approval=None, on_approval_request=None):
    """A hosted MCP server's tools."""
    from agents import HostedMCPTool

    config = {"type": "mcp", "server_label": "crm", "server_url": "https://crm.example/mcp"}
    if require_approval is not None:
        config["require_approval"] = require_approval
    return HostedMCPTool(tool_config=config, on_approval_request=on_approval_request)


def interpreter():
    from agents import CodeInterpreterTool

    return CodeInterpreterTool(tool_config={"type": "code_interpreter", "container": {"type": "auto"}})


class Asked(FakeModel):
    """Answers from a script, keeping the input of each model call it was asked."""

    def __init__(self, turns) -> None:
        super().__init__(turns)
        self.inputs = []

    async def get_response(self, system_instructions, input, *args, **kwargs):
        self.inputs.append(input)
        return await super().get_response(system_instructions, input, *args, **kwargs)


def approvals(model: Asked) -> list[dict]:
    """The answers to hosted MCP tools' requests, each once, in the inputs of the model calls."""
    answers = {item["approval_request_id"]: {k: item[k] for k in ("approve", "reason") if k in item}
               for turn in model.inputs if isinstance(turn, list)
               for item in turn if isinstance(item, dict) and item.get("type") == "mcp_approval_response"}
    return list(answers.values())


def halted_run(agent, quarantine, **run_config):
    """Runs the guarded agent until it raises AgentHalted: the refusal, the events and the processor."""
    from agents import RunConfig

    from wake.stop import AgentHalted

    exporter = InMemorySpanExporter()
    processor = WakeTracingProcessor(Recorder(agent="support", exporter=exporter), quarantine=quarantine)
    set_trace_processors([processor])
    try:
        with pytest.raises(AgentHalted) as refused:
            asyncio.run(Runner.run(processor.guard(agent), "Check our refund terms.", run_config=RunConfig(**run_config)))
    finally:
        set_trace_processors([])
    processor._recorder.flush()
    return refused.value, wake_events(exporter.get_finished_spans()), processor


NO_PAY_AFTER_SEARCH = {"rules": [{"id": "no-pay-after-search", "callers": ["billing"], "tools": ["pay"],
                                  "forbid_origins": ["tool:web_search", "tool:lookup_order"]}]}


@pytest.mark.parametrize("sensitive", [True, False])
def test_a_hosted_search_enters_before_a_tool_from_the_same_response_is_checked(served, sensitive):
    from agents import WebSearchTool

    paid = []
    billing = Agent(name="billing", tools=[WebSearchTool(), paying(paid)], model=FakeModel([[searched(), PAY],
                                                                                            [say("Done.")]]))
    result, events_, _ = guarded_run(billing, served(NO_PAY_AFTER_SEARCH, "billing"),
                                     trace_include_sensitive_data=sensitive)
    assert paid == [] and result.final_output == "Done.", "the payment from the response that searched never ran"
    denied, payload = denied_in(events_)
    assert attrs(denied)["gen_ai.tool.name"] == "pay"
    assert attrs(denied)["wake.brake.halt_id"] == "policy:no-pay-after-search"
    assert payload["origins"] == ["tool:web_search"]


@pytest.mark.parametrize("sensitive", [True, False])
def test_without_the_response_in_the_trace_every_hosted_tool_offered_enters(served, sensitive):
    from agents import WebSearchTool

    from wake import flow

    # The response searched nothing. Only with the response in the trace can Wake tell.
    paid = []
    billing = Agent(name="billing", tools=[WebSearchTool(), interpreter(), crm("never"), paying(paid)],
                    model=FakeModel([[PAY], [say("Done.")]]))
    _, events_, _ = guarded_run(billing, served(NO_PAY_AFTER_SEARCH, "billing"), trace_include_sensitive_data=sensitive)
    entered = flow.entered().seen("billing").origins
    if sensitive:
        assert paid == ["1234"] and entered == ("tool:pay",)
        return
    assert paid == [] and set(entered) == {"tool:code_interpreter", "tool:web_search"}, "but no hosted MCP tool"
    _, payload = denied_in(events_)
    assert payload["origins"] == ["tool:web_search"]


def shell_called(call_id="call_sh"):
    from openai.types.responses import ResponseFunctionShellToolCall

    return ResponseFunctionShellToolCall(type="shell_call", id=f"sh_{uuid.uuid4().hex[:8]}", call_id=call_id,
                                         status="completed", action={"commands": ["cat /srv/orders.csv"]})


def test_a_shell_call_enters_with_its_response_only_when_the_shell_runs_at_openai(served):
    from agents import ShellTool

    # Hosted: what it read enters as the response comes in, before the payment it asked for.
    paid = []
    hosted = ShellTool(environment={"type": "container_auto"})
    billing = Agent(name="billing", tools=[hosted, paying(paid)], model=FakeModel([[shell_called(), PAY], [say("Done.")]]))
    no_pay = {"rules": [{"id": "no-pay-after-shell", "tools": ["pay"], "forbid_origins": ["tool:shell"]}]}
    _, events_, _ = guarded_run(billing, served(no_pay, "billing"))
    assert paid == [] and denied_in(events_)[1]["origins"] == ["tool:shell"]
    # Local: it enters once it has run, so it's checked before anything of it entered.
    ran = []
    local = ShellTool(executor=lambda request: ran.append(request.data.action.commands) or "ok")
    ops = Agent(name="ops", tools=[local], model=FakeModel([[shell_called()], [say("Done.")]]))
    one_shell = {"rules": [{"id": "one-shell", "tools": ["shell"], "forbid_origins": ["tool:shell"]}]}
    _, events_, _ = guarded_run(ops, served(one_shell, "ops"))
    assert ran == [["cat /srv/orders.csv"]] and "denied" not in kinds_of(events_)


def test_hosted_calls_enter_by_the_name_the_agent_offers_them_by(served):
    from agents import FileSearchTool, ImageGenerationTool, WebSearchTool

    from wake import flow

    billing = Agent(name="billing", tools=[FileSearchTool(vector_store_ids=["vs_1"]), crm("never"), WebSearchTool(),
                                           ImageGenerationTool(tool_config={"type": "image_generation"})],
                    model=FakeModel([[mcp_called("refund_order"), searched(), say("Done.")]]))
    guarded_run(billing, served({"rules": []}, "billing"))
    # Only what the response ran, a hosted MCP call by its MCP tool's own name.
    assert set(flow.entered().seen("billing").origins) == {"tool:refund_order", "tool:web_search"}


NO_SEARCH_AFTER_WEB = {"rules": [{"id": "no-hosted-after-web", "callers": ["billing"],
                                  "tools": ["web_search", "code_interpreter"], "forbid_origins": ["tool:fetch_page"]}]}


def test_a_model_call_that_could_run_a_hosted_tool_held_back_isnt_made_and_the_run_ends(served):
    from agents import FileSearchTool, WebSearchTool

    model = Asked([[FETCH], [say("Searched.")]])
    billing = Agent(name="billing", model=model, tools=[fetching(), WebSearchTool(), interpreter(),
                                                        FileSearchTool(vector_store_ids=["vs_1"])])
    refused, events_, _ = halted_run(billing, served(NO_SEARCH_AFTER_WEB, "billing"))
    assert len(model.inputs) == 1, "the model call after the fetch wasn't made"
    assert refused.halt == "policy:no-hosted-after-web"
    assert str(refused) == ("Wake's flow policy (policy:no-hosted-after-web) holds billing back after tool:fetch_page, "
                            "so its model call, which could run web_search at OpenAI, wasn't made.")
    first, fetch, *denied = events_
    assert kinds_of([first, fetch]) == ["model_call", "tool_call"]
    assert [(attrs(e)["wake.event.kind"], attrs(e)["gen_ai.tool.name"], attrs(e)["wake.brake.halt_id"]) for e in denied] == [
        ("denied", "web_search", "policy:no-hosted-after-web"), ("denied", "code_interpreter", "policy:no-hosted-after-web"),
    ], "each hosted tool held back, and only those"
    for event in denied:
        name, payload = attrs(event)["gen_ai.tool.name"], json.loads(attrs(event)["wake.payload"])
        assert payload["origins"] == ["tool:fetch_page"] and "every_origin" not in payload
        assert payload["output"].endswith(f"so its model call, which could run {name} at OpenAI, wasn't made.")
        assert attrs(event)["wake.agent.id"] == "billing" and attrs(event)["wake.origins"] == ()
        assert attrs(event)["wake.parents"] == (attrs(first)["wake.event.id"],), "the agent's last response"


def test_every_origin_holds_back_a_model_call_and_says_why(served):
    from agents import WebSearchTool

    from wake import flow

    for i in range(flow.MAX_ORIGINS + 1):
        flow.note("billing", f"tool:t{i}")
    billing = Agent(name="billing", tools=[WebSearchTool()], model=FakeModel([[say("Done.")]]))
    refused, events_, _ = halted_run(billing, served(NO_SEARCH_AFTER_WEB, "billing"))
    assert str(refused) == ("Wake's flow policy (policy:no-hosted-after-web) holds billing back, because more origins "
                            "entered it than Wake keeps, so its model call, which could run web_search at OpenAI, "
                            "wasn't made.")
    _, payload = denied_in(events_)
    assert payload["origins"] == ["*"] and payload["every_origin"] == flow.SATURATED


def test_a_halt_stops_the_model_calls_of_an_agent_that_offers_hosted_tools():
    from agents import WebSearchTool

    model = Asked([[say("Searched.")]])
    billing = Agent(name="billing", tools=[WebSearchTool(), interpreter()], model=model)
    refused, events_, _ = halted_run(billing, Halts({"id": "halt-billing", "agents": ["billing"]}))
    assert model.inputs == [], "no model call was made"
    assert str(refused) == ("Wake halted billing (halt-billing), so its model call, which could run web_search at "
                            "OpenAI, wasn't made.")
    assert [(attrs(e)["gen_ai.tool.name"], attrs(e)["wake.brake.halt_id"]) for e in events_] == [
        ("web_search", "halt-billing"), ("code_interpreter", "halt-billing")]
    # Stale rules, and a check that fails, refuse it too.
    from wake.quarantine import Quarantine

    for rules, halt in ((Quarantine("http://127.0.0.1:9", agent="support", start=False), "stale-rules"),
                        (BrokenHalts(), "halt-check-failed")):
        billing = Agent(name="billing", tools=[WebSearchTool()], model=FakeModel([[say("Searched.")]]))
        assert halted_run(billing, rules)[0].halt == halt


class Hooks:
    """An app's own hooks for its agent, noting what they're called for."""

    def __init__(self) -> None:
        from agents import AgentHooks

        noted = self.noted = []

        class Noting(AgentHooks):
            async def on_start(self, context, agent):
                noted.append("start")

            async def on_llm_start(self, context, agent, system_prompt, input_items):
                noted.append("llm_start")

            async def on_llm_end(self, context, agent, response):
                noted.append("llm_end")

        self.hooks = Noting()


def test_the_agents_own_hooks_are_kept_and_called_and_an_agent_without_hosted_tools_keeps_its_own():
    from agents import WebSearchTool

    own = Hooks()
    billing = Agent(name="billing", tools=[WebSearchTool()], hooks=own.hooks, model=FakeModel([[say("Done.")]]))
    plain = Agent(name="plain", model=FakeModel([[say("Done.")]]))
    result, _, processor = guarded_run(billing, Halts())
    processor.guard(plain)
    assert result.final_output == "Done."
    assert billing.hooks is own.hooks and own.noted == ["start", "llm_start", "llm_end"]
    assert plain.hooks is None, "no hosted tools: nothing to check before its model calls"
    # Once held back, the agent's own hook isn't told a model call is starting.
    own = Hooks()
    billing = Agent(name="billing", tools=[WebSearchTool()], hooks=own.hooks, model=FakeModel([[say("Done.")]]))
    halted_run(billing, Halts({"id": "halt-billing", "agents": ["billing"]}))
    assert own.noted == ["start"]


NO_CRM_AFTER_WEB = {"rules": [{"id": "no-crm-after-web", "callers": ["billing"], "tools": ["lookup_order"],
                               "forbid_origins": ["tool:fetch_page"]}]}


def test_a_hosted_mcp_call_held_back_is_rejected_and_recorded(served):
    from wake import flow

    model = Asked([[FETCH], [asking()], [say("Couldn't look it up.")]])
    tool = crm("never")
    billing = Agent(name="billing", tools=[fetching(), tool], model=model)
    result, events_, _ = guarded_run(billing, served(NO_CRM_AFTER_WEB, "billing"))
    assert result.final_output == "Couldn't look it up."
    reason = ("Wake's flow policy (policy:no-crm-after-web) holds billing back after tool:fetch_page, so this tool call "
              "wasn't run.")
    assert approvals(model) == [{"approve": False, "reason": reason}], "rejected, with why"
    assert tool.tool_config["require_approval"] == "always", "every call asks first"
    denied, payload = denied_in(events_)
    assert attrs(denied)["gen_ai.tool.name"] == "lookup_order"
    assert attrs(denied)["wake.brake.halt_id"] == "policy:no-crm-after-web"
    assert payload["origins"] == ["tool:fetch_page"] and payload["input"] == '{"order": "1234"}'
    assert payload["output"] == reason
    asked = [e for e in events_ if attrs(e)["wake.event.kind"] == "model_call"][1]
    assert attrs(denied)["wake.parents"] == (attrs(asked)["wake.event.id"],), "the response that asked"
    assert "tool:lookup_order" not in flow.entered().seen("billing").origins, "nothing came back from it"


def test_a_hosted_mcp_call_allowed_is_approved_and_its_result_enters_then(served):
    from wake import flow

    entered = []

    class Answering(Asked):
        async def get_response(self, *args, **kwargs):
            entered.append(flow.entered().seen("billing").origins)
            return await super().get_response(*args, **kwargs)

    paid = []
    model = Answering([[asking()], [mcp_called(), PAY], [say("Done.")]])
    billing = Agent(name="billing", tools=[crm("never"), paying(paid)], model=model)
    policy = {"rules": [*NO_CRM_AFTER_WEB["rules"], *NO_PAY_AFTER_SEARCH["rules"]]}
    _, events_, _ = guarded_run(billing, served(policy, "billing"), trace_include_sensitive_data=False)
    assert approvals(model) == [{"approve": True}]
    assert entered[1] == ("tool:lookup_order",), "noted as approved, before the call that runs it"
    assert paid == [], "what it brought back keeps billing from paying, the trace or not"
    assert denied_in(events_)[1]["origins"] == ["tool:lookup_order"]


def test_a_hosted_mcp_call_allowed_gets_the_tools_own_answer_when_its_setting_would_have_asked(served):
    from wake import flow

    asked = []

    def own(request):
        asked.append(request.data.name)
        return {"approve": request.data.name != "refund_order", "reason": "Refunds need a person."}

    setting = {"never": {"tool_names": ["lookup_order"]}}
    model = Asked([[asking("lookup_order"), asking("refund_order")], [say("Done.")]])
    tool = crm(setting, own)
    billing = Agent(name="billing", tools=[tool], model=model)
    guarded_run(billing, served({"rules": []}, "billing"))
    assert asked == ["refund_order"], "only what its own setting asks about"
    assert approvals(model) == [{"approve": True}, {"approve": False, "reason": "Refunds need a person."}]
    assert flow.entered().seen("billing").origins == ("tool:lookup_order",), "a rejected call brings nothing"
    assert tool.tool_config["require_approval"] == "always"


@pytest.mark.parametrize("setting, name, asks", [
    ("never", "lookup_order", False),
    ("always", "lookup_order", True),
    (None, "lookup_order", True),  # OpenAI asks by default
    ({"never": {"tool_names": ["lookup_order"]}}, "lookup_order", False),
    ({"never": {"tool_names": ["lookup_order"]}}, "refund_order", True),
    ({"never": {"tool_names": ["lookup_order"], "read_only": True}}, "lookup_order", True),  # a hint Wake can't see
    ({"never": {"read_only": True}}, "lookup_order", True),
    ({"always": {"tool_names": ["refund_order"]}}, "lookup_order", True),
    ({"always": {"tool_names": ["lookup_order"]}, "never": {"tool_names": ["lookup_order"]}}, "lookup_order", True),
])
def test_whether_a_hosted_mcp_tools_own_setting_would_have_asked(setting, name, asks):
    from wake.openai_agents import _asks

    assert _asks(setting, name) is asks


def test_a_hosted_mcp_tool_the_app_approves_through_interruptions_is_left_as_it_is():
    for setting in ("always", None, {"never": {"tool_names": ["lookup_order"]}}):
        tool = crm(setting)
        config = dict(tool.tool_config)
        processor = WakeTracingProcessor(Recorder(agent="support", exporter=InMemorySpanExporter()), quarantine=Halts())
        processor.guard(Agent(name="billing", tools=[tool]))
        assert tool.tool_config == config and tool.on_approval_request is None, setting


def test_a_halt_rejects_every_hosted_mcp_call():
    model = Asked([[asking()], [say("Done.")]])
    billing = Agent(name="billing", tools=[crm(on_approval_request=lambda request: {"approve": True})], model=model)
    _, events_, _ = guarded_run(billing, Halts({"id": "halt-billing", "agents": ["billing"]}))
    assert approvals(model) == [{"approve": False,
                                 "reason": "Wake halted billing (halt-billing), so this tool call wasn't run."}]
    denied, _ = denied_in(events_)
    assert (attrs(denied)["gen_ai.tool.name"], attrs(denied)["wake.brake.halt_id"]) == ("lookup_order", "halt-billing")
