import asyncio
import hashlib

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from wake import Recorder
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
