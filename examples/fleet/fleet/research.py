"""The research agent, on the OpenAI Agents SDK: reads web pages to answer questions,
served over A2A. Its page reads go to the fleet's MCP server through agentgateway.

With FLEET_MODELS=real and OPENAI_API_KEY set it runs on the SDK's default OpenAI model;
otherwise on the scripted one.
"""

import os

from a2a.helpers.proto_helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor
from agents import Agent, Runner, function_tool

from fleet import common, scripted
from wake import a2a as wake_a2a
from wake.openai_agents import install

NAME = "research"
recorder = common.recorder(NAME)
install(recorder)


@function_tool
async def fetch_page(url: str) -> str:
    """Fetch a web page."""
    return await common.call_tool(NAME, "fetch_page", {"url": url})


real = os.environ.get("FLEET_MODELS") == "real" and os.environ.get("OPENAI_API_KEY")
agent = Agent(
    name=NAME,
    instructions="Answer questions about shop policy. Read the pages they name with fetch_page, and quote what they say.",
    tools=[fetch_page],
    **({} if real else {"model": scripted.ResearchModel()}),
)


class Research(AgentExecutor):
    async def execute(self, context, event_queue):
        with wake_a2a.received(context):
            result = await Runner.run(agent, context.get_user_input())
            reply = wake_a2a.link(new_text_message(str(result.final_output), context_id=context.context_id))
        await event_queue.enqueue_event(reply)

    async def cancel(self, context, event_queue):
        pass


if __name__ == "__main__":
    common.serve(Research(), NAME, "Answers questions about shop policy from the web.", 9101, recorder)
