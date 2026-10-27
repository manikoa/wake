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

The reply comes back the same way. The proxy records it and names its record in the
response's wake-parents header, which the client above reads, so the step that reads the
tool's result descends from the reply and from whatever the receiver did to produce it.
Without the proxy, call replied(reply) on what send_message yields.

Receiving, in an AgentExecutor:

    async def execute(self, context, event_queue):
        with wake_a2a.received(context):
            answer = await agent.run(context.get_user_input())
            reply = wake_a2a.link(new_text_message(answer, context_id=context.context_id))
        await event_queue.enqueue_event(reply)

A run the agent starts inside the block descends from the message: from the proxy's
record of it when there is one, else from the sender's tool call. Linked inside the
block, the reply names the run's last step.

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
    """An httpx.AsyncClient that adds the current event's link headers to each request,
    and notes the events a response names in its wake-parents header as where the call's
    result came from. Takes httpx.AsyncClient's arguments."""
    import httpx

    client = httpx.AsyncClient(**kwargs)
    client.event_hooks.setdefault("request", []).append(_add_link)
    client.event_hooks.setdefault("response", []).append(_note_reply)
    return client


async def _add_link(request) -> None:
    try:
        for name, value in links.headers().items():
            if name not in request.headers:
                request.headers[name] = value
    except Exception:
        log.warning("wake: failed to link an A2A request", exc_info=True)


async def _note_reply(response) -> None:
    try:
        link = links.from_headers(response.headers)
        if link is not None:
            links.returned(list(link.parents))
    except Exception:
        log.warning("wake: failed to read an A2A reply's link", exc_info=True)


def replied(reply: Any) -> None:
    """Notes the events an A2A reply names in its metadata as where the current call's
    result came from. Takes what the a2a-sdk client yields, a Message or Task, or a dict.
    Through the proxy, http_client() already did this from the response headers."""
    try:
        frame = links._top()
        if frame is None or frame.returned:
            return
        for message in _messages_in(reply):
            link = links.from_meta(_plain(_get(message, "metadata")))
            if link is not None:
                links.returned(list(link.parents))
    except Exception:
        log.warning("wake: failed to read an A2A reply's link", exc_info=True)


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    if hasattr(obj, "HasField"):
        try:
            if not obj.HasField(name):
                return None
        except ValueError:
            pass  # not a message field, such as a repeated one
    return getattr(obj, name, None)


def _messages_in(reply: Any) -> list:
    """The messages a reply carries: itself, or those in a task's or update's status."""
    out = []
    for payload in (reply, _get(reply, "message"), _get(reply, "task"), _get(reply, "status_update")):
        if payload is None:
            continue
        if _get(payload, "parts") is not None or _get(payload, "message_id") is not None:
            out.append(payload)
        status_message = _get(_get(payload, "status"), "message")
        if status_message is not None:
            out.append(status_message)
    return out


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
