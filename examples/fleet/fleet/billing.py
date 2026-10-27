"""The billing agent, on the Claude Agent SDK: issues refunds with an in-process tool,
served over A2A. The tool pays through the fleet's MCP server, behind agentgateway.

With FLEET_MODELS=real and ANTHROPIC_API_KEY set it runs on Claude Code itself; otherwise
on a scripted stand-in that speaks the same protocol.
"""

import os

from a2a.helpers.proto_helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from fleet import common, scripted
from wake import a2a as wake_a2a
from wake.claude_agent_sdk import WakeClaudeAgent

NAME = "billing"
recorder = common.recorder(NAME)
claude = WakeClaudeAgent(recorder)


@tool("issue_refund", "Refund an order to its original payment method, or pay an account.", {
    "type": "object",
    "properties": {"amount": {"type": "number"}, "order": {"type": "string"}, "account": {"type": "string"}},
    "required": ["amount"],
})
async def issue_refund(arguments):
    text = await common.call_tool(NAME, "refund", arguments)
    return {"content": [{"type": "text", "text": text}]}


server = create_sdk_mcp_server(NAME, tools=claude.tools([issue_refund]))
real = os.environ.get("FLEET_MODELS") == "real" and os.environ.get("ANTHROPIC_API_KEY")


class Billing(AgentExecutor):
    async def execute(self, context, event_queue):
        prompt = context.get_user_input()
        options = claude.options(ClaudeAgentOptions(
            system_prompt="You issue the refunds you're asked for with the issue_refund tool, then confirm them.",
            mcp_servers={NAME: server},
            allowed_tools=[f"mcp__{NAME}__issue_refund"],
            tools=[],
            max_turns=4,
        ))
        transport = None if real else scripted.ClaudeCode(prompt, NAME, "issue_refund")
        answer = ""
        with wake_a2a.received(context):
            async for message in claude.record(query(prompt=prompt, options=options, transport=transport), prompt=prompt):
                if isinstance(message, ResultMessage):
                    answer = message.result or ""
            reply = wake_a2a.link(new_text_message(answer, context_id=context.context_id))
        await event_queue.enqueue_event(reply)

    async def cancel(self, context, event_queue):
        pass


if __name__ == "__main__":
    common.serve(Billing(), NAME, "Issues refunds.", 9102, recorder)
