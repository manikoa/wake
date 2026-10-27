"""Messages between agents: a run an agent starts for a message descends from what sent
it, over the a2a-sdk's own client and server, through the proxy or without it."""

import asyncio
import json

import httpx
from a2a.client import ClientConfig, create_client, minimal_agent_card
from a2a.helpers.proto_helpers import get_message_text, new_text_message
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import Role, SendMessageRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.applications import Starlette

from wake import Recorder, links
from wake import a2a as wake_a2a
from wake.langchain import WakeCallbackHandler

TRACE = 0x4BF92F3577B34DA6A3CE929D0E0E4736
SENDER = links.Link("sender-tool-call", TRACE, 0x00F067AA0BA902B7)
PROXY_HEADERS = {"traceparent": f"00-{TRACE:032x}-{0x1111111111111111:016x}-01", "wake-parents": "proxy-message"}


def test_received_prefers_the_proxys_headers():
    metadata = SENDER.meta()
    with links.received(headers={"Traceparent": PROXY_HEADERS["traceparent"], "Wake-Parents": "proxy-message"},
                        metadata=metadata) as link:
        assert links.current() == link
        assert link.parents == ("proxy-message",)
        assert (link.trace_id, link.span_id) == (TRACE, 0x1111111111111111)
    assert links.current() is None
    with links.received(metadata=metadata) as link:
        assert link == SENDER


def test_received_keeps_only_what_looks_like_a_link():
    with links.received(headers={"wake-parents": "a, b,a,  ,bad id!,c"}) as link:
        assert link.parents == ("a", "b", "c")
        assert link.trace_id == 0, "no traceparent, no trace"
        assert link.headers() == {"wake-parents": "a,b,c"}
    with links.received(headers={"traceparent": "garbage", "wake-parents": "x"}) as link:
        assert link.trace_id == 0
    with links.received(headers={"traceparent": PROXY_HEADERS["traceparent"]}) as link:
        assert link is None, "a trace alone names no event"
    with links.received(headers={"wake-parents": ",".join(f"e{i}" for i in range(500))}) as link:
        assert len(link.parents) == 64
    with links.received(metadata={"dev.wake/parents": "not-a-list-but-a-string"}) as link:
        assert link.parents == ("not-a-list-but-a-string",)
    with links.received(headers=None, metadata={"dev.wake/parents": [1, None]}) as link:
        assert link is None


class Researcher(AgentExecutor):
    """Answers with a LangChain model call, recorded by its own Wake handler."""

    def __init__(self, handler: WakeCallbackHandler) -> None:
        self.handler = handler
        self.seen = []

    async def execute(self, context, event_queue):
        with wake_a2a.received(context) as link:
            self.seen.append(link)
            model = GenericFakeChatModel(messages=iter([AIMessage(content="Refunds within 30 days.")]))
            answer = await model.ainvoke(context.get_user_input(), config={"callbacks": [self.handler]})
            # Linked here, the reply names the step that produced it.
            reply = wake_a2a.link(new_text_message(answer.content, context_id=context.context_id))
        await event_queue.enqueue_event(reply)

    async def cancel(self, context, event_queue):
        pass


def exchange(extra_headers=None):
    """The support agent asks the researcher, from inside a recorded tool call."""
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="researcher", exporter=exporter)
    researcher = Researcher(WakeCallbackHandler(recorder))
    card = minimal_agent_card("http://researcher/a2a", ["JSONRPC"])
    handler = DefaultRequestHandler(agent_executor=researcher, task_store=InMemoryTaskStore(), agent_card=card)
    app = Starlette(routes=create_jsonrpc_routes(handler, "/a2a"))
    sent = []

    async def capture(request):
        sent.append(request)

    async def ask():
        http = wake_a2a.http_client(transport=httpx.ASGITransport(app=app), headers=extra_headers or {})
        http.event_hooks["request"].append(capture)
        client = await create_client(card, ClientConfig(httpx_client=http, streaming=False))
        frame = links._enter(SENDER)
        try:
            message = wake_a2a.link(new_text_message("What is our refund window?", role=Role.ROLE_USER))
            return [r async for r in client.send_message(SendMessageRequest(message=message))]
        finally:
            links._exit(frame)
            await http.aclose()

    replies = asyncio.run(ask())
    recorder.flush()
    return replies, researcher.seen, sent, exporter.get_finished_spans()


def test_a_message_without_the_proxy_links_to_the_sender():
    replies, seen, sent, spans = exchange()
    assert get_message_text(replies[0].message) == "Refunds within 30 days."
    (request,) = sent
    assert request.headers["wake-parents"] == "sender-tool-call"
    assert request.headers["traceparent"] == SENDER.traceparent
    body = json.loads(request.content)
    assert body["method"] == "SendMessage"
    assert body["params"]["message"]["metadata"] == SENDER.meta()
    assert seen == [SENDER]
    (call,) = [s for s in spans if s.attributes.get("wake.event.kind") == "model_call"]
    assert call.attributes["wake.parents"] == ("sender-tool-call",)
    assert call.attributes["wake.origins"] == (), "the content came from the sender, not a user"
    assert call.context.trace_id == TRACE


def test_a_message_through_the_proxy_links_to_its_record():
    # The proxy replaces the link headers with its own record of the message.
    _, seen, _, spans = exchange(extra_headers=PROXY_HEADERS)
    (link,) = seen
    assert link.parents == ("proxy-message",)
    (call,) = [s for s in spans if s.attributes.get("wake.event.kind") == "model_call"]
    assert call.attributes["wake.parents"] == ("proxy-message",)
    assert call.parent.span_id == 0x1111111111111111


def test_link_leaves_a_message_alone_without_a_current_event():
    message = wake_a2a.link(new_text_message("hi", role=Role.ROLE_USER))
    assert not message.HasField("metadata")
    assert wake_a2a.link({"parts": []}) == {"parts": []}
    with links.received(metadata=SENDER.meta()):
        assert wake_a2a.link({"parts": []})["metadata"] == SENDER.meta()


def test_a_chain_under_a_link_descends_from_it_once():
    from langchain_core.runnables import RunnableLambda

    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="researcher", exporter=exporter)
    handler = WakeCallbackHandler(recorder)
    model = GenericFakeChatModel(messages=iter([AIMessage(content="Draft answer about refunds."),
                                                AIMessage(content="Final answer about refunds.")]))
    chain = model | RunnableLambda(lambda m: f"Improve this: {m.content}") | model
    with links.received(metadata=SENDER.meta()):
        chain.invoke("What is our refund window?", config={"callbacks": [handler]})
    recorder.flush()
    spans = exporter.get_finished_spans()
    first, second = sorted((s for s in spans if s.attributes.get("wake.event.kind") == "model_call"),
                           key=lambda s: s.start_time)
    assert first.attributes["wake.parents"] == ("sender-tool-call",)
    assert "sender-tool-call" not in second.attributes["wake.parents"]
    assert {s.context.trace_id for s in spans} == {TRACE}


def test_an_openai_agents_workflow_under_a_link_descends_from_it():
    from agents import Agent, Runner
    from agents.tracing import set_trace_processors
    from test_openai_agents import FakeModel, say

    from wake.openai_agents import WakeTracingProcessor

    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="researcher", exporter=exporter)
    set_trace_processors([WakeTracingProcessor(recorder)])
    try:
        agent = Agent(name="researcher", model=FakeModel([[say("Refunds within 30 days.")]]))

        async def run():
            with links.received(metadata=SENDER.meta()):
                return await Runner.run(agent, "What is our refund window?")

        asyncio.run(run())
    finally:
        set_trace_processors([])
    recorder.flush()
    spans = exporter.get_finished_spans()
    (call,) = [s for s in spans if s.attributes.get("wake.event.kind") == "model_call"]
    assert call.attributes["wake.parents"] == ("sender-tool-call",)
    assert call.attributes["wake.origins"] == ()
    assert {s.context.trace_id for s in spans} == {TRACE}


class ProxyReplyHeaders:
    """Adds the headers the proxy puts on a reply going back to the sender."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        async def send_with_link(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"wake-parents", b"proxy-reply")]
            await send(message)

        await self.app(scope, receive, send_with_link)


def ask_from_a_graph(through_proxy: bool):
    """A LangGraph support agent asks the researcher from a tool, then answers."""
    from langchain_core.tools import tool
    from langgraph.graph import START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode, tools_condition

    exporter = InMemorySpanExporter()
    research_recorder = Recorder(agent="researcher", exporter=exporter)
    support_recorder = Recorder(agent="support", exporter=exporter)
    card = minimal_agent_card("http://researcher/a2a", ["JSONRPC"])
    handler = DefaultRequestHandler(agent_executor=Researcher(WakeCallbackHandler(research_recorder)),
                                    task_store=InMemoryTaskStore(), agent_card=card)
    app = Starlette(routes=create_jsonrpc_routes(handler, "/a2a"))
    if through_proxy:
        app = ProxyReplyHeaders(app)

    @tool
    async def ask_research(question: str) -> str:
        """Ask the research agent."""
        async with wake_a2a.http_client(transport=httpx.ASGITransport(app=app)) as http:
            client = await create_client(card, ClientConfig(httpx_client=http, streaming=False))
            message = wake_a2a.link(new_text_message(question, role=Role.ROLE_USER))
            texts = []
            async for reply in client.send_message(SendMessageRequest(message=message)):
                wake_a2a.replied(reply)
                texts.append(get_message_text(reply.message))
            return "\n".join(texts)

    model = GenericFakeChatModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "ask_research", "args": {"question": "Refund window?"}, "id": "c1"}]),
        AIMessage(content="We can refund within 30 days."),
    ]))
    builder = StateGraph(MessagesState)
    builder.add_node("model", lambda state: {"messages": [model.invoke(state["messages"])]})
    builder.add_node("tools", ToolNode([ask_research]))
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", tools_condition)
    builder.add_edge("tools", "model")
    graph = builder.compile()
    support = WakeCallbackHandler(support_recorder)
    asyncio.run(graph.ainvoke({"messages": [("user", "Can we refund order 1234?")]}, config={"callbacks": [support]}))
    research_recorder.flush()
    support_recorder.flush()
    return exporter.get_finished_spans()


def events_of(spans, agent, kind):
    return sorted((s for s in spans if s.attributes.get("wake.agent.id") == agent
                   and s.attributes.get("wake.event.kind") == kind), key=lambda s: s.start_time)


def test_the_reply_through_the_proxy_feeds_the_senders_next_step():
    spans = ask_from_a_graph(through_proxy=True)
    (ask,) = events_of(spans, "support", "tool_call")
    _, second = events_of(spans, "support", "model_call")
    parents = second.attributes["wake.parents"]
    assert attr_id(ask) in parents and "proxy-reply" in parents
    assert "proxy-reply" not in ask.attributes["wake.parents"], "the tool call can't descend from its own reply"


def test_the_reply_without_the_proxy_names_the_receivers_last_step():
    spans = ask_from_a_graph(through_proxy=False)
    (answer,) = events_of(spans, "researcher", "model_call")
    (ask,) = events_of(spans, "support", "tool_call")
    assert answer.attributes["wake.parents"] == (attr_id(ask),), "the receiver descends from the tool call"
    _, second = events_of(spans, "support", "model_call")
    assert second.attributes["wake.parents"][-2:] == (attr_id(ask), attr_id(answer))


def attr_id(span):
    return span.attributes["wake.event.id"]
