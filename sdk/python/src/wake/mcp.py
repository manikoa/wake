"""Links MCP calls to the Wake events making them, so the proxy's record of each call
joins the caller's trace and provenance. See wake.links and docs/proxy.md.

With LangChain's MCP adapters, add the interceptor:

    from wake.mcp import link_interceptor
    client = MultiServerMCPClient(connections, tool_interceptors=[link_interceptor])

With the MCP client directly, use Wake's HTTP client and pass the link on each call:

    from wake import links
    from wake.mcp import http_client
    async with streamable_http_client(url, http_client=http_client()) as (read, write, _):
        ...
        await session.call_tool(name, arguments, meta=links.meta())

The MCP client sends requests from a background task, where the current event isn't
known, so its link travels in params._meta and Wake's HTTP client copies it into the
traceparent and wake-parents headers. agentgateway drops _meta before the proxy sees
the call, but forwards those headers.

Install with the `mcp` extra.
"""

import json
import logging
from typing import Any

from wake import links

log = logging.getLogger("wake")


async def link_interceptor(request: Any, handler: Any) -> Any:
    """A langchain-mcp-adapters tool interceptor that adds the link headers to each call.

    The adapters apply interceptor headers when they open a connection per call, which
    MultiServerMCPClient.get_tools() does. Tools loaded on a session you hold open don't
    get them.
    """
    try:
        extra = links.headers()
        if extra:
            request = request.override(headers={**(request.headers or {}), **extra})
    except Exception:
        log.warning("wake: failed to link an MCP call", exc_info=True)
    return await handler(request)


def http_client(**kwargs: Any):
    """The MCP client's default httpx.AsyncClient, plus a hook that copies each request's
    link from params._meta into headers. Takes create_mcp_http_client's arguments."""
    from mcp.shared._httpx_utils import create_mcp_http_client

    client = create_mcp_http_client(**kwargs)
    client.event_hooks.setdefault("request", []).append(_lift_link)
    return client


async def _lift_link(request) -> None:
    """Copies traceparent and dev.wake/parents from a JSON-RPC request's params._meta into
    headers. Never raises: a request it can't read goes out unchanged."""
    try:
        if request.method != "POST" or not request.content:
            return
        message = json.loads(request.content)
        meta = message.get("params", {}).get("_meta") if isinstance(message, dict) else None
        if not isinstance(meta, dict):
            return
        traceparent = meta.get(links.META_TRACEPARENT)
        if isinstance(traceparent, str) and links.HEADER_TRACEPARENT not in request.headers:
            request.headers[links.HEADER_TRACEPARENT] = traceparent
        parents = meta.get(links.META_PARENTS)
        if isinstance(parents, list) and links.HEADER_PARENTS not in request.headers:
            ids = [p for p in parents if isinstance(p, str) and p and "," not in p]
            if ids:
                request.headers[links.HEADER_PARENTS] = ",".join(ids)
    except Exception:
        log.warning("wake: failed to link an MCP request", exc_info=True)
