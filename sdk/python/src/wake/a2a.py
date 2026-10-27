"""Links A2A messages between agents, so the receiving agent's steps join the sender's
trace and descend from the step that sent the message. See wake.links and docs/proxy.md.

Sending, with the a2a-sdk client, from inside a recorded tool call:

    from wake import a2a as wake_a2a
    client = await create_client(url, ClientConfig(httpx_client=wake_a2a.http_client()))
    message = wake_a2a.link(new_text_message(question, role=Role.ROLE_USER))
    async for reply in client.send_message(SendMessageRequest(message=message)):
        ...

The headers reach the proxy, which records the message as a child of the tool call and
passes its own link on. The message metadata carries the same link end to end, for
agents that talk without the proxy between them.

Receiving, in an AgentExecutor:

    async def execute(self, context, event_queue):
        with wake_a2a.received(context):
            answer = await agent.run(context.get_user_input())

A run the agent starts inside the block descends from the message: from the proxy's
record of it when there is one, else from the sender's tool call.

Install with the `a2a` extra.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from wake import links

log = logging.getLogger("wake")


def link(message: Any) -> Any:
    """Adds the current event's link to an outgoing A2A message's metadata, and returns
    the message. Works on the a2a-sdk's protobuf messages and on plain dicts."""
    try:
        meta = links.meta()
        if not meta:
            return message
        metadata = message.get("metadata") if isinstance(message, dict) else getattr(message, "metadata", None)
        if metadata is None:
            if isinstance(message, dict):
                message["metadata"] = dict(meta)
            else:
                message.metadata = dict(meta)
        else:
            metadata.update(meta)
    except Exception:
        log.warning("wake: failed to link an A2A message", exc_info=True)
    return message


def http_client(**kwargs: Any):
    """An httpx.AsyncClient that adds the current event's link headers to each request.
    Takes httpx.AsyncClient's arguments."""
    import httpx

    client = httpx.AsyncClient(**kwargs)
    client.event_hooks.setdefault("request", []).append(_add_link)
    return client


async def _add_link(request) -> None:
    try:
        for name, value in links.headers().items():
            if name not in request.headers:
                request.headers[name] = value
    except Exception:
        log.warning("wake: failed to link an A2A request", exc_info=True)


@contextmanager
def received(context: Any) -> Iterator[links.Link | None]:
    """Makes the link an incoming A2A message carries current, for an a2a-sdk
    RequestContext. For other servers, use wake.links.received with the request's headers
    and the message's metadata."""
    headers: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    try:
        call_context = getattr(context, "call_context", None)
        state = getattr(call_context, "state", None) or {}
        headers = state.get("headers") or {}
        message = getattr(context, "message", None)
        metadata = _plain(getattr(message, "metadata", None)) or {}
    except Exception:
        log.warning("wake: failed to read an A2A message's link", exc_info=True)
    with links.received(headers=headers, metadata=metadata) as link:
        yield link


def _plain(metadata: Any) -> dict[str, Any] | None:
    """Protobuf Struct metadata as a dict."""
    if metadata is None or isinstance(metadata, dict):
        return metadata
    if hasattr(metadata, "DESCRIPTOR"):
        from google.protobuf import json_format

        return json_format.MessageToDict(metadata)
    return dict(metadata)
