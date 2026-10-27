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
        reply = {"text": answer, "metadata": wake.links.meta()}  # names the run's last step

Outside any recorded tool call, an outgoing call links to the latest event recorded under
the current link, so a reply names the step that produced it. And when what a call gets
back came from recorded events, such as an A2A reply the proxy recorded, returned() says
so, and the step that reads the result descends from them.
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
    """One recorded call's link, on top of whatever link was current when it began, with
    the latest event recorded while it was on top and the events its result came from."""

    __slots__ = ("ended", "latest", "link", "previous", "returned")

    def __init__(self, link: Link, previous: "_Frame | None") -> None:
        self.link = link
        self.previous = previous
        self.ended = False
        self.latest: Link | None = None
        self.returned: list[str] = []


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


def _outgoing() -> Link | None:
    """What a call made now comes from: the latest event recorded under the current link,
    such as a run's last step, else the link's own event, such as the tool call."""
    frame = _top()
    if frame is None:
        return None
    return frame.latest or frame.link


def headers() -> dict[str, str]:
    """HTTP headers linking an outgoing call to the current event, or {} if there's none."""
    link = _outgoing()
    return link.headers() if link else {}


def meta() -> dict[str, object]:
    """The same link as MCP params._meta fields, or {} if there's no current event."""
    link = _outgoing()
    return link.meta() if link else {}


def returned(parents: Any) -> None:
    """Record that what the current call got back came from these events, such as the
    proxy's record of an A2A reply. The step that reads the call's result descends from
    them. Ids that don't look like event ids are dropped."""
    frame = _top()
    if frame is None or not isinstance(parents, (list, tuple)):
        return
    for p in parents:
        p = p.strip() if isinstance(p, str) else ""
        if _EVENT_ID.match(p) and p not in frame.returned and len(frame.returned) < _MAX_PARENTS:
            frame.returned.append(p)


def _note(link: Link) -> None:
    """Called by the integrations for each event they record."""
    frame = _top()
    if frame is not None:
        frame.latest = link


def _returned(frame: _Frame | None) -> list[str]:
    return list(frame.returned) if frame is not None else []


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


# What a received link may carry (docs/linking.md). Headers and metadata come from another
# agent, so anything else is dropped rather than recorded. A traceparent is W3C's: version
# 00 has exactly four fields, a later version may add more, and ff is never valid.
_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}(-.*)?$")
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
    match = _TRACEPARENT.match(traceparent.strip()) if isinstance(traceparent, str) else None
    if match and match[1] != "ff" and not (match[1] == "00" and match[4]):
        trace_id, span_id = int(match[2], 16), int(match[3], 16)
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
