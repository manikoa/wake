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


def guarded_run(agent, quarantine):
    exporter = InMemorySpanExporter()
    processor = WakeTracingProcessor(Recorder(agent="support", exporter=exporter), quarantine=quarantine)
    set_trace_processors([processor])
    try:
        result = asyncio.run(Runner.run(processor.guard(agent), "Check our refund terms."))
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


def test_guard_raises_rather_than_leave_a_tool_it_cant_refuse_and_leaves_hosted_tools_be():
    from agents import WebSearchTool

    class Gadget:
        name = "gadget"

    triage = Agent(name="triage", tools=[WebSearchTool()])
    processor = WakeTracingProcessor(Recorder(agent="support", exporter=InMemorySpanExporter()), quarantine=Halts())
    processor.guard(triage)
    assert isinstance(triage.tools[0], WebSearchTool), "a hosted tool runs at OpenAI: nothing here to refuse"
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
