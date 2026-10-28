"""The support agent, on LangGraph: handles one customer request. It looks the order up in
the CRM (MCP), asks research about the refund policy and billing to pay out (A2A), and
keeps a note of what it did in a memory file. Run it once per request:

    python -m fleet.support "Customer jane@example.com wants a refund on order 1234."
"""

import asyncio
import os
import sys
from pathlib import Path

from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from fleet import common, scripted
from wake.langchain import WakeCallbackHandler
from wake.mcp import link_interceptor
from wake.memory import Memory

NAME = "support"
NOTES = Path(os.environ.get("FLEET_NOTES", "/tmp/fleet/support-notes.md"))
REQUEST = "Customer jane@example.com wants a refund on order 1234: the shoes don't fit."

recorder = common.recorder(NAME)
handler = WakeCallbackHandler(recorder)
memory = Memory(handler)


@tool
async def ask_research(question: str) -> str:
    """Ask the research agent a question about shop policy."""
    return await common.ask(NAME, "research", question)


@tool
async def ask_billing(request: str) -> str:
    """Ask the billing agent to issue refunds."""
    return await common.ask(NAME, "billing", request)


@tool
def remember(note: str) -> str:
    """Keep a note for later conversations."""
    NOTES.parent.mkdir(parents=True, exist_ok=True)
    memory.write_file(NOTES, note)
    return "Noted."


async def crm_tools() -> list:
    client = MultiServerMCPClient(
        {"fleet": {"url": f"{common.GATEWAY}/mcp", "transport": "streamable_http",
                   "headers": await common.tool_headers(NAME)}},
        tool_interceptors=[link_interceptor],
    )
    return [t for t in await client.get_tools() if t.name.endswith("lookup_order")]


async def handle(request: str) -> str:
    crm = await crm_tools()
    model = scripted.SupportModel(lookup_tool=crm[0].name)
    tools = [*crm, ask_research, ask_billing, remember]
    builder = StateGraph(MessagesState)
    builder.add_node("model", lambda state: {"messages": [model.invoke(state["messages"])]})
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", tools_condition)
    builder.add_edge("tools", "model")
    graph = builder.compile()
    result = await graph.ainvoke({"messages": [("user", request)]}, config={"callbacks": [handler]})
    return result["messages"][-1].content


if __name__ == "__main__":
    try:
        print(asyncio.run(handle(sys.argv[1] if len(sys.argv) > 1 else REQUEST)))
    finally:
        recorder.shutdown()
        print(recorder.health(), file=sys.stderr)
