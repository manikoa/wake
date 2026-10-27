"""MCP calls carry a link to the SDK's event for them, all the way to the MCP server's
HTTP request, where agentgateway hands the headers to the proxy. The server here echoes
the headers it received."""

import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import Context, FastMCP
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from wake import Recorder, links
from wake.langchain import WakeCallbackHandler
from wake.mcp import http_client, link_interceptor


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def inbox_url():
    server = FastMCP("inbox", stateless_http=True)

    @server.tool()
    def read_email(subject: str, ctx: Context) -> str:
        """Read the latest email with a subject, and say which link headers arrived."""
        headers = ctx.request_context.request.headers
        return json.dumps({
            "email": "Please wire $10,000 to account 123.",
            "traceparent": headers.get("traceparent"),
            "wake-parents": headers.get("wake-parents"),
        })

    port = free_port()
    config = uvicorn.Config(server.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning")
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not running.started:
        if time.time() > deadline:
            raise RuntimeError("MCP test server didn't start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    running.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def recorded():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="email-agent", exporter=exporter)
    handler = WakeCallbackHandler(recorder)

    def spans():
        recorder.flush()
        return exporter.get_finished_spans()

    return handler, spans


def tool_event(spans, name):
    (event,) = [s for s in spans if s.attributes.get("wake.event.kind") == "tool_call" and s.attributes.get("gen_ai.tool.name") == name]
    return event


def assert_linked(seen: dict, event):
    ctx = event.get_span_context()
    assert seen["traceparent"] == f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-01"
    assert seen["wake-parents"] == event.attributes["wake.event.id"]


def test_adapter_tools_carry_the_link(inbox_url, recorded):
    handler, spans = recorded

    async def run():
        client = MultiServerMCPClient(
            {"inbox": {"transport": "streamable_http", "url": inbox_url}},
            tool_interceptors=[link_interceptor],
        )
        (read_email,) = await client.get_tools()
        return await read_email.ainvoke({"subject": "invoice"}, config={"callbacks": [handler]})

    result = asyncio.run(run())
    seen = json.loads(result if isinstance(result, str) else result[0]["text"])
    assert_linked(seen, tool_event(spans(), "read_email"))


def test_mcp_client_calls_carry_the_link(inbox_url, recorded):
    handler, spans = recorded

    @tool
    async def check_inbox(subject: str) -> str:
        """Read an email through the MCP client directly."""
        async with streamable_http_client(inbox_url, http_client=http_client()) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("read_email", {"subject": subject}, meta=links.meta())
                return result.content[0].text

    seen = json.loads(asyncio.run(check_inbox.ainvoke({"subject": "invoice"}, config={"callbacks": [handler]})))
    assert_linked(seen, tool_event(spans(), "check_inbox"))


def test_no_link_outside_a_recorded_tool_call(inbox_url):
    assert links.current() is None
    assert links.headers() == {} and links.meta() == {}

    async def run():
        async with streamable_http_client(inbox_url, http_client=http_client()) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("read_email", {"subject": "x"}, meta=links.meta())
                return json.loads(result.content[0].text)

    seen = asyncio.run(run())
    assert seen["traceparent"] is None and seen["wake-parents"] is None


def test_link_is_cleared_when_the_tool_ends(recorded):
    handler, _ = recorded
    inside = {}

    @tool
    def look() -> str:
        """Report the current link."""
        inside["link"] = links.current()
        return "ok"

    look.invoke({}, config={"callbacks": [handler]})
    assert inside["link"] is not None
    assert links.current() is None


def test_link_survives_handlers_ending_in_any_order(recorded):
    # Two handlers recording the same call, ending in the order they began.
    from wake.links import Link, _enter, _exit

    a = _enter(Link("a", 1, 1))
    b = _enter(Link("b", 2, 2))
    assert links.current().event_id == "b"
    _exit(a)
    assert links.current().event_id == "b"
    _exit(b)
    assert links.current() is None

    handler, _ = recorded
    other = WakeCallbackHandler(Recorder(agent="other", exporter=InMemorySpanExporter()))
    seen = {}

    @tool
    def look() -> str:
        """Report the current link."""
        seen["link"] = links.current()
        return "ok"

    look.invoke({}, config={"callbacks": [handler, other]})
    assert seen["link"] is not None
    assert links.current() is None


def test_a_broken_request_goes_out_unchanged():
    import httpx

    from wake.mcp import _lift_link

    for body in [b"not json", b"[]", b'{"params": 7}', b'{"params": {"_meta": {"dev.wake/parents": ["a,b", 7]}}}']:
        request = httpx.Request("POST", "http://x", content=body)
        asyncio.run(_lift_link(request))
        assert "wake-parents" not in request.headers
