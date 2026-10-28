"""What the fleet's agents share: a recorder each, and calls to each other and to the tools
through agentgateway, linked by Wake."""

import logging
import os
import signal
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import get_message_text, new_text_message
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Role,
    SendMessageRequest,
)
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from wake import Recorder, links
from wake import a2a as wake_a2a
from wake import mcp as wake_mcp

GATEWAY = os.environ.get("FLEET_GATEWAY", "http://localhost:3000").rstrip("/")
# Who each agent says it is to the gateway. A stand-in for the SPIFFE ID or JWT the gateway
# reads in production; see agentgateway.yaml.
AGENT_HEADER = "x-fleet-agent"
# The stand-in identity provider (fleet/standins/oauth.py), and where this agent's credentials
# are: its own volume, which no other agent and not the brake reads.
IDP = os.environ.get("FLEET_IDP", "http://localhost:8080/oauth2/v1").rstrip("/")
SECRETS = Path(os.environ.get("FLEET_SECRETS", "/etc/fleet/secrets"))

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
log = logging.getLogger("fleet")


def recorder(agent: str) -> Recorder:
    """A recorder sending to WAKE_ENDPOINT. Dropped events are logged here; in production
    on_drop pages someone."""
    return Recorder(agent=agent, on_drop=lambda count, reason: log.error("wake dropped %d events: %s", count, reason))


def card(name: str, description: str, url: str) -> AgentCard:
    return AgentCard(
        name=name,
        description=description,
        version="0.1.0",
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url, protocol_version="1.0")],
        capabilities=AgentCapabilities(),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[AgentSkill(id=name, name=name, description=description, tags=["fleet"])],
    )


def serve(executor, name: str, description: str, port: int, recorder: Recorder) -> None:
    """Serve an agent over A2A (JSON-RPC, protocol v1.0) on this port, and send the events
    still queued before exiting on SIGTERM. uvicorn re-raises the signal once it has shut
    down, which would otherwise end the process before they're sent."""

    def stop(signum, frame):
        recorder.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card(name, description, f"{GATEWAY}/agents/{name}"),
    )
    app = Starlette(routes=create_jsonrpc_routes(handler, "/"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


async def ask(sender: str, receiver: str, text: str) -> str:
    """Send another agent a message through the gateway and return its reply. Called from
    inside a recorded tool call, so the message links to it and the reply links back."""
    async with wake_a2a.http_client(timeout=120, headers={AGENT_HEADER: sender}) as http:
        url = f"{GATEWAY}/agents/{receiver}"
        client = await create_client(card(receiver, receiver, url), ClientConfig(httpx_client=http, streaming=False))
        message = wake_a2a.link(new_text_message(text, role=Role.ROLE_USER))
        texts = []
        async for reply in client.send_message(SendMessageRequest(message=message)):
            wake_a2a.replied(reply)
            if reply.HasField("message"):
                texts.append(get_message_text(reply.message))
        return "\n".join(texts)


_access: dict[str, tuple[str, float]] = {}


async def access_token(agent: str) -> str:
    """The agent's access token for the tools, swapped for the refresh token the identity
    provider issued it, and swapped again a minute before it expires. If the swap fails, as
    it does once the brake has revoked the refresh token, the last one is sent anyway: the
    tools server decides whether it's still good."""
    token, expires = _access.get(agent, ("", 0.0))
    if time.time() < expires - 60:
        return token
    try:
        refresh = (SECRETS / "refresh-token").read_text().strip()
        async with httpx.AsyncClient(timeout=10) as http:
            response = await http.post(f"{IDP}/token", data={
                "grant_type": "refresh_token", "refresh_token": refresh, "client_id": agent})
        response.raise_for_status()
        body = response.json()
        token = body["access_token"]
        _access[agent] = (token, time.time() + int(body.get("expires_in", 0)))
    except (OSError, httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("%s couldn't get an access token: %s", agent, e)
    return token


async def tool_headers(agent: str) -> dict[str, str]:
    """What an agent sends with its MCP calls: who it is, for the gateway, and its access
    token, for the tools server, which agentgateway passes it to."""
    headers = {AGENT_HEADER: agent}
    if token := await access_token(agent):
        headers["authorization"] = f"Bearer {token}"
    return headers


async def call_tool(caller: str, name: str, arguments: dict) -> str:
    """Call one of the fleet's MCP tools through the gateway, linked to the tool call making it."""
    http = wake_mcp.http_client(headers=await tool_headers(caller))
    async with (
        streamable_http_client(f"{GATEWAY}/mcp", http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(name, arguments, meta=links.meta())
    return "\n".join(c.text for c in result.content if getattr(c, "text", None))
