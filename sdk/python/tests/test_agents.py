"""Agents written as plain Python around LangChain models and tools, the way Clavr is built:
prompts are built from strings, tools are called directly, and a supervisor hands work to
domain agents in the same process. No callbacks are passed at any call site."""

import asyncio
import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from wake import Recorder
from wake.langchain import WakeCallbackHandler

STEP = "Find the latest invoice email from Acme"
SEARCH = "subject:invoice from:acme-billing"
EMAIL = "Please wire $10,000 to account 123 by Friday."
SUMMARY = "Acme's email asks for a $10,000 wire by Friday."


@tool
def search_email(query: str) -> str:
    """Search the user's inbox."""
    return EMAIL


def model(*replies):
    return GenericFakeChatModel(messages=iter([AIMessage(content=r) for r in replies]))


class EmailAgent:
    def __init__(self):
        self.llm = model(json.dumps({"query": SEARCH}), SUMMARY)

    async def run(self, query):
        params = json.loads((await asyncio.to_thread(self.llm.invoke, [HumanMessage(f"Extract search params: {query}")])).content)
        found = await search_email.arun(params)
        return (await self.llm.ainvoke([HumanMessage(f"Summarize this email for the user:\n{found}")])).content


class Supervisor:
    def __init__(self, handler):
        self.handler = handler
        self.llm = model(json.dumps({"steps": [{"domain": "email", "query": STEP}]}), "Hold the payment.")
        self.agents = {"email": EmailAgent()}

    async def answer(self, query):
        with self.handler.agent("supervisor", input=query, origin="user"):
            plan = json.loads(self.llm.invoke([HumanMessage(f"Plan steps for: {query}")]).content)
            results = []
            for step in plan["steps"]:
                with self.handler.agent(step["domain"], input=step["query"]) as run:
                    result = await self.agents[step["domain"]].run(step["query"])
                    run.reply(result)
                results.append(result)
            return self.llm.invoke([HumanMessage(f"Answer the user using: {results}")]).content


@pytest.fixture(scope="module")
def recorded():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="clavr", exporter=exporter)
    handler = WakeCallbackHandler(recorder).install()
    answer = asyncio.run(Supervisor(handler).answer("Should I pay Acme's invoice?"))
    recorder.flush()
    yield answer, exporter.get_finished_spans()
    handler.uninstall()


def wake_events(spans):
    found = [s for s in spans if "wake.event.kind" in s.attributes]
    return sorted(found, key=lambda s: s.start_time)


def attrs(event):
    return event.attributes


def ids(*events):
    return {e.attributes["wake.event.id"] for e in events}


def test_records_every_call_without_callbacks_at_call_sites(recorded):
    answer, spans = recorded
    assert answer == "Hold the payment."
    found = [(e.attributes["wake.agent.id"], e.attributes["wake.event.kind"]) for e in wake_events(spans)]
    assert found == [
        ("supervisor", "model_call"),  # plan
        ("email", "agent_message"),  # the step handed to the email agent
        ("email", "model_call"),  # extract search params
        ("email", "tool_call"),  # search the inbox
        ("email", "model_call"),  # summarize
        ("supervisor", "agent_message"),  # the summary handed back
        ("supervisor", "model_call"),  # answer
    ]


def test_edges_follow_content_across_the_handoff(recorded):
    _, spans = recorded
    plan, handoff, extract, search, summarize, reply, final = wake_events(spans)

    # The user's question enters at the supervisor's first call, and only there.
    assert attrs(plan)["wake.origins"] == ("user",)
    assert attrs(final)["wake.origins"] == ()

    # The handoff carries the step the planner wrote, and enters the email agent from the supervisor.
    assert set(attrs(handoff)["wake.parents"]) == ids(plan)
    assert attrs(handoff)["wake.origins"] == ("agent:supervisor",)
    assert json.loads(attrs(handoff)["wake.payload"]) == {"from": "supervisor", "to": "email", "content": STEP}

    # The email agent's next step is fed by the handoff.
    assert set(attrs(extract)["wake.parents"]) == ids(handoff)

    # The search was chosen by the call that wrote its arguments, and its result is new content.
    assert set(attrs(search)["wake.parents"]) == ids(extract)
    assert attrs(search)["wake.origins"] == ("tool:search_email",)

    # The summary saw the email, and the reply carries the summary back.
    assert set(attrs(summarize)["wake.parents"]) == ids(search)
    assert set(attrs(reply)["wake.parents"]) == ids(summarize)
    assert attrs(reply)["wake.origins"] == ("agent:email",)

    # The final answer saw the reply. Following parents from it reaches the email.
    assert ids(reply) <= set(attrs(final)["wake.parents"])


def test_one_trace_with_each_agent_run_nested(recorded):
    _, spans = recorded
    runs = {s.attributes["gen_ai.agent.name"]: s for s in spans if s.attributes.get("gen_ai.operation.name") == "invoke_agent"}
    assert set(runs) == {"supervisor", "email"}
    assert runs["email"].parent.span_id == runs["supervisor"].context.span_id
    for event in wake_events(spans):
        assert event.context.trace_id == runs["supervisor"].context.trace_id
        assert event.parent.span_id == runs[event.attributes["wake.agent.id"]].context.span_id


def test_agent_runs_never_break_the_agent(monkeypatch):
    recorder = Recorder(agent="clavr", exporter=InMemorySpanExporter())
    handler = WakeCallbackHandler(recorder)

    def fail(*args, **kwargs):
        raise RuntimeError("recorder bug")

    monkeypatch.setattr(recorder.tracer, "start_span", fail)
    with handler.agent("supervisor", input="hi", origin="user"):
        with handler.agent("email", input=STEP) as run:
            run.reply(SUMMARY)


def test_uninstall_stops_recording_other_runs():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="clavr", exporter=exporter)
    handler = WakeCallbackHandler(recorder).install()
    model("first").invoke("hi")
    handler.uninstall()
    model("second").invoke("hi")
    recorder.flush()
    calls = [s for s in exporter.get_finished_spans() if s.attributes.get("wake.event.kind") == "model_call"]
    assert len(calls) == 1


def test_runs_of_one_agent_at_once_are_instances_of_their_own():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="clavr", exporter=exporter)
    handler = WakeCallbackHandler(recorder).install()

    async def step(query):
        with handler.agent("email", input=query) as run:
            found = await search_email.arun({"query": query})
            run.reply(found)

    async def supervise():
        with handler.agent("supervisor", input="Check both senders", origin="user"):
            await asyncio.gather(step("from:acme-billing"), step("from:globex-billing"))

    try:
        asyncio.run(supervise())
    finally:
        handler.uninstall()
    recorder.flush()
    spans = exporter.get_finished_spans()
    kinds: dict[str, list[str]] = {}
    for e in wake_events(spans):
        if attrs(e)["wake.agent.id"] == "email":
            assert attrs(e)["wake.agent.lineage"] == ("supervisor",)
            kinds.setdefault(attrs(e)["wake.agent.instance"], []).append(attrs(e)["wake.event.kind"])
        else:
            assert "wake.agent.instance" not in attrs(e), "the supervisor and the replies it reads are its own"
    assert len(kinds) == 2 and all(sorted(k) == ["agent_message", "tool_call"] for k in kinds.values()), kinds
    runs = [s for s in spans if s.attributes.get("gen_ai.operation.name") == "invoke_agent"
            and s.attributes["gen_ai.agent.name"] == "email"]
    assert {r.attributes["wake.agent.instance"] for r in runs} == set(kinds)
    for r in runs:
        assert r.attributes["wake.agent.instance"] == f"email#{r.context.span_id:016x}", "named for its run's span"


def test_errors_in_the_agent_still_reach_the_caller():
    handler = WakeCallbackHandler(Recorder(agent="clavr", exporter=InMemorySpanExporter()))
    with pytest.raises(ValueError):
        with handler.agent("supervisor"):
            raise ValueError("agent bug")
