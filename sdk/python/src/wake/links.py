"""Links an outgoing call to the Wake event making it.

While the SDK records a tool call, the call's event is current here. A call that leaves
the process through the proxy, such as an MCP tool call through agentgateway, carries a
link to it: W3C trace context, and the event's id as the parent of the proxy's record of
the same call. The proxy's event then joins the caller's trace and provenance instead of
standing alone. See docs/proxy.md.

    headers = wake.links.headers()  # {"traceparent": ..., "wake-parents": ...}, or {}

The receiving end does the reverse. While an agent handles a message from another agent,
the link the message carried is current, and a run the agent starts descends from it:

    with wake.links.received(headers=request.headers, metadata=message.metadata):
        answer = await agent.run(...)
"""

import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("wake")

HEADER_TRACEPARENT = "traceparent"
HEADER_PARENTS = "wake-parents"
META_TRACEPARENT = "traceparent"
META_PARENTS = "dev.wake/parents"


@dataclass(frozen=True)
class Link:
    """The event an outgoing call belongs to. A received message can name more than one
    event it came from, and may carry no trace (trace_id 0)."""

    event_id: str
    trace_id: int
    span_id: int
    other_parents: tuple[str, ...] = ()

    @property
    def parents(self) -> tuple[str, ...]:
        return (self.event_id, *self.other_parents)

    @property
    def traceparent(self) -> str | None:
        if not self.trace_id or not self.span_id:
            return None
        return f"00-{self.trace_id:032x}-{self.span_id:016x}-01"

    def headers(self) -> dict[str, str]:
        headers = {HEADER_PARENTS: ",".join(self.parents)}
        if self.traceparent:
            headers[HEADER_TRACEPARENT] = self.traceparent
        return headers

    def meta(self) -> dict[str, object]:
        meta: dict[str, object] = {META_PARENTS: list(self.parents)}
        if self.traceparent:
            meta[META_TRACEPARENT] = self.traceparent
        return meta


class _Frame:
    """One recorded call's link, on top of whatever link was current when it began."""

    __slots__ = ("ended", "link", "previous")

    def __init__(self, link: Link, previous: "_Frame | None") -> None:
        self.link = link
        self.previous = previous
        self.ended = False


_current: ContextVar[_Frame | None] = ContextVar("wake_link", default=None)


def _top() -> _Frame | None:
    # Frames can end in any order, such as when two handlers record the same call, so
    # skip any that have ended.
    frame = _current.get()
    while frame is not None and frame.ended:
        frame = frame.previous
    return frame


def current() -> Link | None:
    """The event the code running now belongs to, if the SDK is recording one."""
    frame = _top()
    return frame.link if frame else None


def headers() -> dict[str, str]:
    """HTTP headers linking an outgoing call to the current event, or {} if there's none."""
    link = current()
    return link.headers() if link else {}


def meta() -> dict[str, object]:
    """The same link as MCP params._meta fields, or {} if there's no current event."""
    link = current()
    return link.meta() if link else {}


def _enter(link: Link) -> _Frame:
    frame = _Frame(link, _current.get())
    _current.set(frame)
    return frame


def _exit(frame: _Frame | None) -> None:
    if frame is None:
        return
    frame.ended = True
    # Drop ended frames from the top, in this context. A context that never saw the frame
    # isn't affected.
    _current.set(_top())


# What a received link may carry. Headers and metadata come from another agent, so
# anything else is dropped rather than recorded.
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAX_PARENTS = 64


def _parse(traceparent: Any, parents: Any) -> Link | None:
    if isinstance(parents, str):
        parents = parents.split(",")
    if not isinstance(parents, (list, tuple)):
        return None
    ids = [p.strip() for p in parents if isinstance(p, str)]
    ids = list(dict.fromkeys(p for p in ids if _EVENT_ID.match(p)))[:_MAX_PARENTS]
    if not ids:
        return None
    trace_id = span_id = 0
    match = _TRACEPARENT.match(traceparent.strip().lower()) if isinstance(traceparent, str) else None
    if match:
        trace_id, span_id = int(match[1], 16), int(match[2], 16)
        if not trace_id or not span_id:
            trace_id = span_id = 0
    return Link(ids[0], trace_id, span_id, tuple(ids[1:]))


def from_headers(headers: Mapping[str, Any] | None) -> Link | None:
    """The link in a request's traceparent and wake-parents headers, if it has one."""
    if not headers:
        return None
    lowered = {str(k).lower(): v for k, v in headers.items()}
    return _parse(lowered.get(HEADER_TRACEPARENT), lowered.get(HEADER_PARENTS))


def from_meta(meta: Mapping[str, Any] | None) -> Link | None:
    """The link in message metadata or MCP _meta, if it has one."""
    if not meta:
        return None
    return _parse(meta.get(META_TRACEPARENT), meta.get(META_PARENTS))


@contextmanager
def received(headers: Mapping[str, Any] | None = None, metadata: Mapping[str, Any] | None = None) -> Iterator[Link | None]:
    """Make the link an incoming message carries current while the agent handles it.

    The headers win when both carry one: through the proxy, they name the proxy's record
    of the message, which is itself a child of the sender's event named in the metadata.
    """
    link = None
    try:
        link = from_headers(headers) or from_meta(metadata)
    except Exception:
        log.warning("wake: failed to read a received link", exc_info=True)
    frame = _enter(link) if link is not None else None
    try:
        yield link
    finally:
        _exit(frame)
