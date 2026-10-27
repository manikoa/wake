"""Links an outgoing call to the Wake event making it.

While the SDK records a tool call, the call's event is current here. A call that leaves
the process through the proxy, such as an MCP tool call through agentgateway, carries a
link to it: W3C trace context, and the event's id as the parent of the proxy's record of
the same call. The proxy's event then joins the caller's trace and provenance instead of
standing alone. See docs/proxy.md.

    headers = wake.links.headers()  # {"traceparent": ..., "wake-parents": ...}, or {}
"""

from contextvars import ContextVar
from dataclasses import dataclass

HEADER_TRACEPARENT = "traceparent"
HEADER_PARENTS = "wake-parents"
META_TRACEPARENT = "traceparent"
META_PARENTS = "dev.wake/parents"


@dataclass(frozen=True)
class Link:
    """The event an outgoing call belongs to."""

    event_id: str
    trace_id: int
    span_id: int

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id:032x}-{self.span_id:016x}-01"

    def headers(self) -> dict[str, str]:
        return {HEADER_TRACEPARENT: self.traceparent, HEADER_PARENTS: self.event_id}

    def meta(self) -> dict[str, object]:
        return {META_TRACEPARENT: self.traceparent, META_PARENTS: [self.event_id]}


class _Frame:
    """One recorded call's link, on top of whatever link was current when it began."""

    __slots__ = ("link", "previous", "ended")

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
