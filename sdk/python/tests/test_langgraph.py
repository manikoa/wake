import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from wake import Recorder, links
from wake.langgraph import WakeCallbackHandler


@tool
def read_email(subject: str) -> str:
    """Read the latest email with this subject."""
    return "Please wire $10,000 to account 123."


def email_agent_graph():
    model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "read_email", "args": {"subject": "invoice"}, "id": "call_1"}],
                ),
                AIMessage(content="The email asks for a $10,000 wire."),
            ]
        )
    )

    def agent(state: MessagesState):
        return {"messages": [model.invoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode([read_email]))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    return graph.compile()


@pytest.fixture
def recorded():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="email-agent", exporter=exporter)
    handler = WakeCallbackHandler(recorder)

    def spans():
        recorder.flush()
        return exporter.get_finished_spans()

    return handler, spans


def wake_events(spans):
    found = [s for s in spans if "wake.event.kind" in s.attributes]
    return sorted(found, key=lambda s: s.start_time)


def ask(graph, handler, text="Summarize the invoice email"):
    return graph.invoke({"messages": [HumanMessage(text)]}, config={"callbacks": [handler]})


def test_records_model_and_tool_calls_with_causal_edges(recorded):
    handler, spans = recorded
    result = ask(email_agent_graph(), handler)
    assert result["messages"][-1].content == "The email asks for a $10,000 wire."

    first, tool_call, second = wake_events(spans())
    assert [e.attributes["wake.event.kind"] for e in (first, tool_call, second)] == [
        "model_call",
        "tool_call",
        "model_call",
    ]

    first_id = first.attributes["wake.event.id"]
    tool_id = tool_call.attributes["wake.event.id"]

    # The user's message enters at the first model call.
    assert first.attributes["wake.origins"] == ("user",)
    assert first.attributes["wake.parents"] == ()

    # The tool call was requested by the first model call, and its result is new content.
    assert tool_call.attributes["wake.parents"] == (first_id,)
    assert tool_call.attributes["wake.origins"] == ("tool:read_email",)
    assert tool_call.attributes["gen_ai.tool.call.id"] == "call_1"

    # The second model call saw both the first call's output and the tool result.
    assert set(second.attributes["wake.parents"]) == {first_id, tool_id}
    assert second.attributes["wake.origins"] == ()


def test_events_share_one_trace_under_the_agent_run(recorded):
    handler, spans = recorded
    ask(email_agent_graph(), handler)
    all_spans = spans()

    (root,) = [s for s in all_spans if s.attributes.get("gen_ai.operation.name") == "invoke_agent"]
    for event in wake_events(all_spans):
        assert event.context.trace_id == root.context.trace_id
        assert event.parent.span_id == root.context.span_id
        assert event.attributes["wake.agent.id"] == "email-agent"
        assert event.attributes["wake.capture"] == "sdk"


def test_payload_digest_matches_and_sdk_never_signs(recorded):
    handler, spans = recorded
    ask(email_agent_graph(), handler)
    for event in wake_events(spans()):
        payload = event.attributes["wake.payload"]
        assert event.attributes["wake.payload.sha256"] == hashlib.sha256(payload.encode()).hexdigest()
        assert "wake.signature" not in event.attributes
    tool_payload = wake_events(spans())[1].attributes["wake.payload"]
    assert "Please wire $10,000" in tool_payload


def test_edges_cross_agents_sharing_a_graph(recorded):
    handler, spans = recorded
    researcher = GenericFakeChatModel(messages=iter([AIMessage(content="Vendor is unverified.")]))
    writer = GenericFakeChatModel(messages=iter([AIMessage(content="Do not pay yet.")]))

    graph = StateGraph(MessagesState)
    graph.add_node("researcher", lambda s: {"messages": [researcher.invoke(s["messages"])]})
    graph.add_node("writer", lambda s: {"messages": [writer.invoke(s["messages"])]})
    graph.add_edge(START, "researcher")
    graph.add_edge("researcher", "writer")
    ask(graph.compile(), handler, "Should we pay this vendor?")

    research_call, write_call = wake_events(spans())
    assert research_call.attributes["wake.langgraph.node"] == "researcher"
    assert write_call.attributes["wake.langgraph.node"] == "writer"
    assert write_call.attributes["wake.parents"] == (research_call.attributes["wake.event.id"],)
    # The user's message was already seen by the researcher, so it doesn't enter again.
    assert write_call.attributes["wake.origins"] == ()


def test_async_runs_record_the_same_edges(recorded):
    handler, spans = recorded
    graph = email_agent_graph()
    asyncio.run(graph.ainvoke({"messages": [HumanMessage("Summarize")]}, config={"callbacks": [handler]}))

    first, tool_call, second = wake_events(spans())
    assert tool_call.attributes["wake.parents"] == (first.attributes["wake.event.id"],)
    assert set(second.attributes["wake.parents"]) == {
        first.attributes["wake.event.id"],
        tool_call.attributes["wake.event.id"],
    }


class BrokenExporter(SpanExporter):
    def export(self, spans):
        raise ConnectionError("collector is down")

    def shutdown(self):
        pass


def test_agent_keeps_working_when_the_collector_is_down():
    recorder = Recorder(agent="email-agent", exporter=BrokenExporter())
    result = ask(email_agent_graph(), WakeCallbackHandler(recorder))
    recorder.flush()
    assert result["messages"][-1].content == "The email asks for a $10,000 wire."


def test_agent_keeps_working_when_recording_fails(recorded, monkeypatch):
    handler, _ = recorded

    def fail(*args, **kwargs):
        raise RuntimeError("recorder bug")

    monkeypatch.setattr(handler._recorder.tracer, "start_span", fail)
    result = ask(email_agent_graph(), handler)
    assert result["messages"][-1].content == "The email asks for a $10,000 wire."


def test_a_graph_run_inside_a_tool_descends_from_the_tool_call(recorded):
    handler, spans = recorded
    summarizer = email_agent_graph()

    @tool
    def summarize(subject: str) -> str:
        """Summarize the latest email with this subject."""
        # The tool's own prompt: LangChain passes its callbacks on to the nested run.
        result = summarizer.invoke({"messages": [HumanMessage(f"Summarize the {subject} email")]})
        return result["messages"][-1].content

    model = GenericFakeChatModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "summarize", "args": {"subject": "invoice"}, "id": "call_s"}]),
        AIMessage(content="It asks for a $10,000 wire."),
    ]))

    def agent(state: MessagesState):
        return {"messages": [model.invoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode([summarize]))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    ask(graph.compile(), handler)

    events = wake_events(spans())
    kinds = [e.attributes["wake.event.kind"] for e in events]
    assert kinds == ["model_call", "tool_call", "model_call", "tool_call", "model_call", "model_call"]
    outer, summarize_call, nested_first = events[:3]
    assert summarize_call.attributes["gen_ai.tool.name"] == "summarize"
    # The nested run's first step descends from the tool call that ran it, and its input
    # is the tool's prompt, not the user's.
    assert summarize_call.attributes["wake.event.id"] in nested_first.attributes["wake.parents"]
    assert "user" not in nested_first.attributes["wake.origins"]
    assert outer.attributes["wake.origins"] == ("user",)
    assert len({e.context.trace_id for e in events}) == 1


def test_a_user_message_with_no_id_is_new_only_once_in_a_trace(recorded):
    from langchain_core.runnables import RunnableLambda

    handler, spans = recorded
    model = GenericFakeChatModel(messages=iter([AIMessage(content=f"Draft {n}.") for n in ("one", "two", "three")]))
    question = HumanMessage("Write a reply to the invoice email")  # no id, as plain LangChain apps send

    def twice(_):
        first = model.invoke([question])
        return model.invoke([question, first])

    RunnableLambda(twice).invoke({}, config={"callbacks": [handler]})
    first, second = wake_events(spans())
    assert first.attributes["wake.origins"] == ("user",)
    # Asked again in the same run, it isn't new: the second call descends from the first.
    assert second.attributes["wake.origins"] == ()
    assert first.attributes["wake.event.id"] in second.attributes["wake.parents"]

    # A run of its own sees it for the first time.
    RunnableLambda(lambda _: model.invoke([question])).invoke({}, config={"callbacks": [handler]})
    third = wake_events(spans())[-1]
    assert third.attributes["wake.origins"] == ("user",)


def test_a_cancelled_tool_call_is_still_recorded_and_its_link_ends(recorded):
    handler, spans = recorded

    @tool
    async def wait_for_reply(subject: str) -> str:
        """Wait for a reply to an email."""
        await asyncio.sleep(60)
        return "never"

    async def cancel_it():
        task = asyncio.create_task(wait_for_reply.ainvoke({"subject": "invoice"}, config={"callbacks": [handler]}))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        # Whatever ran in the cancelled task, nothing outside it is under the tool's link.
        return links.current()

    assert asyncio.run(cancel_it()) is None
    (call,) = wake_events(spans())
    assert call.attributes["wake.event.kind"] == "tool_call"
    assert call.status.status_code == StatusCode.ERROR
    assert "CancelledError" in json.loads(call.attributes["wake.payload"])["error"]
    assert handler._pending == {}, "nothing is left open"


def test_a_tool_call_that_ends_isnt_ended_again(recorded):
    handler, spans = recorded

    @tool
    async def look_up(order: str) -> str:
        """Look up an order."""
        return "shipped"

    async def run_and_finish():
        await look_up.ainvoke({"order": "1234"}, config={"callbacks": [handler]})
        await asyncio.sleep(0)

    asyncio.run(run_and_finish())
    (call,) = wake_events(spans())
    assert call.status.status_code != StatusCode.ERROR
    assert json.loads(call.attributes["wake.payload"])["output"] == "shipped"


# Cooperative stops.

wired = []


@tool
def wire(account: str) -> str:
    """Wire a refund to an account."""
    wired.append(account)
    return "Wired."


def wiring_graph(handler):
    model = GenericFakeChatModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "wire", "args": {"account": "123"}, "id": "call_wire"}]),
        AIMessage(content="Done."),
    ]))

    def agent(state: MessagesState):
        return {"messages": [model.invoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode([wire], wrap_tool_call=handler.wrap_tool_call, awrap_tool_call=handler.awrap_tool_call))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    return graph.compile()


class Halts:
    """Stands in for the brake's rules (wake.quarantine.Quarantine): the halts in force."""

    def __init__(self, *halts) -> None:
        self.halts = list(halts)

    def halted(self, names):
        for h in self.halts:
            if h.get("fleet") or set(names) & set(h.get("agents", [])):
                return h["id"]
        return None


class HaltOneEmailRun:
    """Halts the first run of the email agent it's asked about."""

    def __init__(self) -> None:
        self.instance = None

    def halted(self, names):
        for name in sorted(names):
            if name.startswith("email#"):
                self.instance = self.instance or name
                if name == self.instance:
                    return "halt-email-run"
        return None


class BrokenHalts:
    """Rules whose check fails."""

    def halted(self, names):
        raise RuntimeError("the rules can't be read")


def guarded(quarantine):
    from wake.langchain import WakeCallbackHandler as Handler

    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="clavr", exporter=exporter)
    handler = Handler(recorder, quarantine=quarantine)

    def events_():
        recorder.flush()
        return wake_events(exporter.get_finished_spans())

    return handler, events_


def wire_it(handler, *, asynchronous=False):
    graph, inputs, config = wiring_graph(handler), {"messages": [HumanMessage("Wire the refund")]}, {"callbacks": [handler]}
    return asyncio.run(graph.ainvoke(inputs, config=config)) if asynchronous else graph.invoke(inputs, config=config)


def kinds(found):
    return [e.attributes["wake.event.kind"] for e in found]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_a_halted_agents_tool_call_is_refused_before_it_runs(asynchronous):
    wired.clear()
    handler, events_ = guarded(Halts({"id": "halt-clavr", "agents": ["clavr"]}))
    result = wire_it(handler, asynchronous=asynchronous)
    assert wired == [], "the tool never ran"
    refusal = result["messages"][-2]
    assert refusal.content == "Wake halted clavr (halt-clavr), so this tool call wasn't run." and refusal.status == "error"
    first, denied, second = events_()
    assert kinds((first, denied, second)) == ["model_call", "denied", "model_call"]
    assert denied.attributes["wake.brake.halt_id"] == "halt-clavr"
    assert denied.attributes["gen_ai.tool.name"] == "wire"
    assert denied.attributes["wake.agent.id"] == "clavr"
    assert denied.attributes["wake.origins"] == (), "the tool never ran: nothing of it entered"
    assert json.loads(denied.attributes["wake.payload"])["input"] == {"account": "123"}
    assert tuple(denied.attributes["wake.parents"]) == (first.attributes["wake.event.id"],), "the call that asked for it"
    assert denied.attributes["wake.event.id"] in second.attributes["wake.parents"], "the model read the refusal"
    assert denied.context.trace_id == first.context.trace_id
    # Without a halt the same graph runs its tool.
    handler, events_ = guarded(Halts())
    wire_it(handler, asynchronous=asynchronous)
    assert wired == ["123"] and kinds(events_()) == ["model_call", "tool_call", "model_call"]


def test_a_halted_run_is_stopped_in_process_and_its_sibling_goes_on():
    wired.clear()
    halts = HaltOneEmailRun()
    handler, events_ = guarded(halts)
    with handler.agent("supervisor", input="Refund both orders", origin="user"):
        for order in ("1234", "5678"):
            with handler.agent("email", input=f"Refund order {order}"):
                wire_it(handler)
    assert len(wired) == 1, "only the sibling's tool ran"
    (denied,) = [e for e in events_() if e.attributes["wake.event.kind"] == "denied"]
    assert denied.attributes["wake.brake.halt_id"] == "halt-email-run"
    assert denied.attributes["wake.agent.instance"] == halts.instance, "recorded as the halted run's attempt"
    assert denied.attributes["wake.agent.lineage"] == ("supervisor",)


def test_a_run_a_halt_covers_is_never_started():
    from wake.langchain import AgentHalted

    halts = Halts({"id": "halt-email", "agents": ["email"]})
    handler, events_ = guarded(halts)
    started = []
    with handler.agent("supervisor", input="Refund order 1234", origin="user"):
        with pytest.raises(AgentHalted) as refused:
            with handler.agent("email", input="Refund order 1234 to account 123, please"):
                started.append("email")
        # A halt on what starts a run stops the runs it starts too.
        halts.halts = [{"id": "halt-supervisor", "agents": ["supervisor"]}]
        with pytest.raises(AgentHalted):
            with handler.agent("billing", input="Bill it"):
                started.append("billing")
    assert started == [], "nothing started"
    assert refused.value.halt == "halt-email"
    assert str(refused.value) == "Wake halted email (halt-email), so this run of email wasn't started."
    first, second = events_()
    assert kinds((first, second)) == ["denied", "denied"]
    assert first.name == "agent_message supervisor -> email"
    assert first.attributes["wake.agent.id"] == "supervisor", "recorded as what tried to start it"
    assert first.attributes["wake.brake.receiver"] == "email"
    assert first.attributes["wake.origins"] == (), "nothing entered the run"
    assert json.loads(first.attributes["wake.payload"])["content"] == "Refund order 1234 to account 123, please"
    assert second.attributes["wake.brake.halt_id"] == "halt-supervisor"


def test_stale_rules_refuse_every_run_and_tool_call():
    from wake.langchain import AgentHalted
    from wake.quarantine import Quarantine

    wired.clear()
    handler, events_ = guarded(Quarantine("http://127.0.0.1:9", agent="clavr", start=False))
    with pytest.raises(AgentHalted) as refused:
        with handler.agent("supervisor"):
            pass
    assert refused.value.halt == "stale-rules"
    wire_it(handler)
    assert wired == []
    assert {e.attributes["wake.brake.halt_id"] for e in events_() if e.attributes["wake.event.kind"] == "denied"} == {"stale-rules"}


def test_a_halt_check_that_fails_refuses_the_call():
    wired.clear()
    handler, events_ = guarded(BrokenHalts())
    result = wire_it(handler)
    assert wired == []
    assert result["messages"][-2].content == "Wake couldn't check its halts, so this tool call wasn't run."
    (denied,) = [e for e in events_() if e.attributes["wake.event.kind"] == "denied"]
    assert denied.attributes["wake.brake.halt_id"] == "halt-check-failed"


class ToolCallingModel(GenericFakeChatModel):
    """A scripted model an agent can bind tools to."""

    def bind_tools(self, tools, **kwargs):
        return self


def wiring_model():
    return ToolCallingModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "wire", "args": {"account": "123"}, "id": "call_wire"}]),
        AIMessage(content="Done."),
    ]))


def refused_once(events_, halt="halt-clavr"):
    """The one denied event a refused call is recorded as, checked."""
    found = events_()
    (denied,) = [e for e in found if e.attributes["wake.event.kind"] == "denied"]
    assert not [e for e in found if e.attributes["wake.event.kind"] == "tool_call"], "the tool never started"
    assert denied.attributes["wake.brake.halt_id"] == halt
    assert denied.attributes["gen_ai.tool.name"] == "wire"
    assert denied.attributes["wake.origins"] == ()
    assert json.loads(denied.attributes["wake.payload"])["input"] == {"account": "123"}
    return found, denied


@pytest.mark.filterwarnings("ignore:create_react_agent has been moved")
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_a_tool_call_outside_a_wrapped_tool_node_is_refused_as_it_starts(asynchronous):
    # create_react_agent builds its own ToolNode, without the wrappers: the handler refuses the
    # call as the tool starts, and the ToolNode passes the refusal on, ending the run.
    from langgraph.prebuilt import create_react_agent

    from wake.langchain import AgentHalted

    wired.clear()
    handler, events_ = guarded(Halts({"id": "halt-clavr", "agents": ["clavr"]}))
    graph = create_react_agent(wiring_model(), [wire])
    inputs, config = {"messages": [HumanMessage("Wire the refund")]}, {"callbacks": [handler]}
    with pytest.raises(AgentHalted) as refused:
        asyncio.run(graph.ainvoke(inputs, config=config)) if asynchronous else graph.invoke(inputs, config=config)
    assert wired == [], "the tool never ran"
    assert str(refused.value) == "Wake halted clavr (halt-clavr), so this tool call wasn't run."
    found, denied = refused_once(events_)
    (first,) = [e for e in found if e.attributes["wake.event.kind"] == "model_call"]
    assert tuple(denied.attributes["wake.parents"]) == (first.attributes["wake.event.id"],), "the call that asked for it"


def test_a_tool_node_that_catches_errors_answers_the_model_with_the_refusal():
    wired.clear()
    handler, events_ = guarded(Halts({"id": "halt-clavr", "agents": ["clavr"]}))
    model = wiring_model()

    def agent(state: MessagesState):
        return {"messages": [model.invoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode([wire], handle_tool_errors=True))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    result = builder.compile().invoke({"messages": [HumanMessage("Wire the refund")]}, config={"callbacks": [handler]})
    assert wired == []
    refusal = result["messages"][-2]
    assert refusal.status == "error" and "Wake halted clavr (halt-clavr), so this tool call wasn't run." in refusal.content
    found, denied = refused_once(events_)
    assert denied.attributes["wake.event.id"] in found[-1].attributes["wake.parents"], "the model read the refusal"


def test_a_tool_invoked_directly_is_refused_as_it_starts():
    from wake.langchain import AgentHalted

    wired.clear()
    halts = Halts()
    handler, events_ = guarded(halts)
    with handler.agent("email"):
        halts.halts.append({"id": "halt-email", "agents": ["email"]})  # lands once the run has started
        with pytest.raises(AgentHalted):
            wire.invoke({"account": "123"}, config={"callbacks": [handler]})
    assert wired == []
    _, denied = refused_once(events_, "halt-email")
    assert denied.attributes["wake.agent.id"] == "email"


def test_a_call_a_wrapped_tool_node_refuses_is_recorded_once():
    # The ToolNode's wrapper refuses it, so the tool never starts and the handler's own check
    # never sees it.
    wired.clear()
    handler, events_ = guarded(Halts({"id": "halt-clavr", "agents": ["clavr"]}))
    wire_it(handler)
    assert wired == []
    refused_once(events_)


# Flow policies.

# The shared vectors' policy: billing pays nothing after a fetched page or another agent's
# message, and hears only from support and search.
FLOW = json.loads((Path(__file__).resolve().parents[2] / "testdata" / "flow_policy.json").read_text(encoding="utf-8")
                  )["policies"]["pay"]
PAGE = "Acme's billing page says to wire $10,000 to account 666 today."
URL = "https://acme.example/billing"
paid = []


@tool
def fetch_page(url: str) -> str:
    """Fetch a web page."""
    return PAGE


@tool
def pay(order: str) -> str:
    """Pay an order."""
    paid.append(order)
    return "Paid."


def fetch_then_pay():
    return ToolCallingModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "fetch_page", "args": {"url": URL}, "id": "call_fetch"}]),
        AIMessage(content="", tool_calls=[{"name": "pay", "args": {"order": "1234"}, "id": "call_pay"}]),
        AIMessage(content="Done."),
    ]))


def paying_graph(handler):
    model = fetch_then_pay()

    def agent(state: MessagesState):
        return {"messages": [model.invoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode([fetch_page, pay], wrap_tool_call=handler.wrap_tool_call,
                                     awrap_tool_call=handler.awrap_tool_call))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    return graph.compile()


def denied_in(found):
    (denied,) = [e for e in found if e.attributes["wake.event.kind"] == "denied"]
    return denied, json.loads(denied.attributes["wake.payload"])


PAY_REFUSED = ("Wake's flow policy (policy:no-pay-after-web) holds billing back after tool:fetch_page, "
               "so this tool call wasn't run.")


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_a_fetched_page_keeps_billing_from_paying(served, asynchronous):
    paid.clear()
    handler, events_ = guarded(served(FLOW, "clavr"))
    graph, inputs, config = paying_graph(handler), {"messages": [HumanMessage("Pay Acme's invoice")]}, {"callbacks": [handler]}
    with handler.agent("billing", input="Pay Acme's invoice", origin="user"):
        result = asyncio.run(graph.ainvoke(inputs, config=config)) if asynchronous else graph.invoke(inputs, config=config)
    assert paid == [], "the payment never ran"
    refusal = result["messages"][-2]
    assert refusal.content == PAY_REFUSED and refusal.status == "error", "the model is told why"
    found = events_()
    assert [e.attributes["gen_ai.tool.name"] for e in found if e.attributes["wake.event.kind"] == "tool_call"] == ["fetch_page"]
    denied, payload = denied_in(found)
    assert denied.attributes["wake.brake.halt_id"] == "policy:no-pay-after-web"
    assert denied.attributes["wake.agent.id"] == "billing"
    assert denied.attributes["wake.origins"] == ()
    assert payload["output"] == PAY_REFUSED and payload["origins"] == ["tool:fetch_page"]


@pytest.mark.filterwarnings("ignore:create_react_agent has been moved")
def test_a_tool_call_outside_a_wrapped_tool_node_is_held_back_as_it_starts(served):
    from langgraph.prebuilt import create_react_agent

    from wake.langchain import AgentHalted

    paid.clear()
    handler, events_ = guarded(served(FLOW, "clavr"))
    graph = create_react_agent(fetch_then_pay(), [fetch_page, pay])
    with handler.agent("billing", input="Pay Acme's invoice", origin="user"):
        with pytest.raises(AgentHalted) as refused:
            graph.invoke({"messages": [HumanMessage("Pay Acme's invoice")]}, config={"callbacks": [handler]})
    assert paid == []
    assert refused.value.halt == "policy:no-pay-after-web" and str(refused.value) == PAY_REFUSED
    _, payload = denied_in(events_())
    assert payload["origins"] == ["tool:fetch_page"]


def test_a_run_the_policy_keeps_from_the_web_isnt_started(served):
    from wake.langchain import AgentHalted

    handler, events_ = guarded(served(FLOW, "clavr"))
    started = []
    config = {"callbacks": [handler]}
    with handler.agent("research", input="Check Acme's billing page", origin="user"):
        with handler.agent("support", input="Look it up"):
            pass  # a run the policy doesn't name starts
        fetch_page.invoke({"url": URL}, config=config)
        with pytest.raises(AgentHalted) as refused:
            with handler.agent("billing", input="Pay Acme's invoice"):
                started.append("billing")
    assert started == [], "billing never started"
    assert refused.value.halt == "policy:billing-hears-support-only"
    assert str(refused.value) == ("Wake's flow policy (policy:billing-hears-support-only) holds research back after "
                                  "tool:fetch_page, so this run of billing wasn't started.")
    denied, payload = denied_in(events_())
    assert denied.name == "agent_message research -> billing"
    assert denied.attributes["wake.agent.id"] == "research", "recorded as what tried to start it"
    assert denied.attributes["wake.brake.receiver"] == "billing"
    assert payload["content"] == "Pay Acme's invoice" and payload["origins"] == ["tool:fetch_page"]


def test_a_runs_answer_brings_what_entered_it(served):
    from wake.langchain import AgentHalted

    paid.clear()
    handler, _ = guarded(served(FLOW, "clavr"))
    config = {"callbacks": [handler]}
    with handler.agent("billing", input="Pay Acme's invoice", origin="user"):
        with handler.agent("research", input="Is Acme's invoice legit?") as run:
            fetch_page.invoke({"url": URL}, config=config)
            run.reply("It asks for a wire to account 666.")
        with pytest.raises(AgentHalted) as refused:
            pay.invoke({"order": "1234"}, config=config)
    assert paid == []
    assert str(refused.value) == ("Wake's flow policy (policy:no-pay-after-web) holds billing back after "
                                  "agent:research, tool:fetch_page, so this tool call wasn't run.")


def test_a_policy_wake_proxy_cant_use_refuses_every_run_and_tool_call(served):
    from wake.langchain import AgentHalted

    paid.clear()
    handler, events_ = guarded(served({"unreadable": True}, "clavr"))
    with pytest.raises(AgentHalted) as refused:
        with handler.agent("billing"):
            pass
    assert refused.value.halt == "policy:unreadable-policy"
    assert str(refused.value) == "Wake's flow policy can't be read, so this run of billing wasn't started."
    result = paying_graph(handler).invoke({"messages": [HumanMessage("Pay")]}, config={"callbacks": [handler]})
    assert paid == [] and result["messages"][2].content == "Wake's flow policy can't be read, so this tool call wasn't run."
    assert {e.attributes["wake.brake.halt_id"] for e in events_() if e.attributes["wake.event.kind"] == "denied"} == {
        "policy:unreadable-policy"}


def test_without_a_policy_nothing_is_refused(served):
    paid.clear()
    handler, events_ = guarded(served(None, "clavr"))
    with handler.agent("research", input="Check Acme's billing page", origin="user"):
        fetch_page.invoke({"url": URL}, config={"callbacks": [handler]})
        with handler.agent("billing", input="Pay Acme's invoice"):
            paying_graph(handler).invoke({"messages": [HumanMessage("Pay")]}, config={"callbacks": [handler]})
    assert paid == ["1234"]
    assert "denied" not in kinds(events_())
