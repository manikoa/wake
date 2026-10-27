"""The billing agent, on the Claude Agent SDK: issues refunds with an in-process tool,
served over A2A. The tool pays through the fleet's MCP server, behind agentgateway, and
notes each refund in billing's ledger, a memory file.

Billing runs for as long as the fleet does, so it's the agent whose memory adapter enforces
the brake's quarantines: it fetches its rules from wake-proxy every couple of seconds, which
is how the brake confirms the adapter has a halt's memory lock. It also keeps a sandbox at
E2B (the fleet's stand-in), found by its metadata, which a halt freezes.

With FLEET_MODELS=real and ANTHROPIC_API_KEY set it runs on Claude Code itself; otherwise
on a scripted stand-in that speaks the same protocol.
"""

import os
from pathlib import Path

import httpx
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
from wake.memory import Memory
from wake.quarantine import MemoryLocked, Quarantine

NAME = "billing"
LEDGER = Path(os.environ.get("FLEET_LEDGER", "/tmp/fleet/billing-ledger.md"))
E2B = os.environ.get("FLEET_E2B", "http://localhost:8081").rstrip("/")
recorder = common.recorder(NAME)
claude = WakeClaudeAgent(recorder)
memory = Memory(claude, quarantine=Quarantine(os.environ.get("WAKE_MEMORY_RULES", "http://localhost:9003"), agent=NAME))


@tool("issue_refund", "Refund an order to its original payment method, or pay an account.", {
    "type": "object",
    "properties": {"amount": {"type": "number"}, "order": {"type": "string"}, "account": {"type": "string"}},
    "required": ["amount"],
})
async def issue_refund(arguments):
    text = await common.call_tool(NAME, "refund", arguments)
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        memory.write_file(LEDGER, text)
    except MemoryLocked as e:
        text += f" It isn't in the ledger: {e}."
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


def keep_sandbox() -> None:
    """Finds billing's running sandbox by its metadata, or starts one. It auto-resumes on
    traffic, as a sandbox an agent works in often would, so the brake has to freeze it with
    its filesystem only. Billing serves refunds without it if E2B can't be reached."""
    try:
        key = (common.SECRETS / "e2b-api-key").read_text().strip()
        with httpx.Client(base_url=E2B, headers={"X-API-Key": key}, timeout=10) as http:
            running = http.get("/v2/sandboxes", params={"metadata": f"agent={NAME}", "state": "running"})
            running.raise_for_status()
            if running.json():
                return
            created = http.post("/sandboxes", json={"templateID": "base", "metadata": {"agent": NAME},
                                                    "lifecycle": {"autoResume": True}})
            created.raise_for_status()
    except (OSError, httpx.HTTPError, ValueError) as e:
        common.log.warning("billing has no sandbox: %s", e)


if __name__ == "__main__":
    keep_sandbox()
    common.serve(Billing(), NAME, "Issues refunds.", 9102, recorder)
