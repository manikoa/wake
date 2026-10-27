"""What every capture integration shares: bounded memory of recent events, the content
index that links events by what they carry when no ids do, and each event's parents and
origins, so a memory write can be labeled with the origins upstream of it."""

import threading
from collections import OrderedDict, deque

from opentelemetry import trace

from wake import links

# Content matching: an output is findable by its first characters, and only
# outputs long enough that a match is unlikely to be chance are indexed.
_SNIPPET = 160
_MIN_MATCH = 24
_OUTPUTS_PER_TRACE = 64
_KEPT_OUTPUT = 2000

class _Recent(OrderedDict):
    """A dict that forgets its oldest entries, so a long running agent's memory stays bounded."""

    def __init__(self, limit: int = 10_000) -> None:
        super().__init__()
        self._limit = limit

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        if len(self) > self._limit:
            self.popitem(last=False)



class EventGraph:
    """Bookkeeping shared by the capture integrations. Subclasses record events with the
    recorder, and call _remember_output and _remember_lineage as they do."""

    def __init__(self, recorder) -> None:
        self._recorder = recorder
        self._lock = threading.Lock()
        self._outputs = _Recent(limit=256)  # trace id -> recent (event id, output text)
        self._lineage = _Recent()  # event id -> (parents, origins), for memory labels

    def _remember_lineage(self, event_id: str, parents: list[str], origins: list[str]) -> None:
        with self._lock:
            self._lineage[event_id] = (tuple(parents), tuple(origins))

    def _origins_upstream(self, event_ids: list[str], limit: int = 2000) -> list[str]:
        """The origins of these events and everything upstream of them that this handler
        recorded. Coarse: an event's origins are the union over its ancestors."""
        seen: set[str] = set()
        out: list[str] = []
        queue = list(event_ids)
        with self._lock:
            while queue and len(seen) < limit:
                event_id = queue.pop()
                if event_id in seen:
                    continue
                seen.add(event_id)
                parents, origins = self._lineage.get(event_id, ((), ()))
                for o in origins:
                    if o not in out:
                        out.append(o)
                queue.extend(parents)
        return out

    def _event_context(self):
        """The trace context for an event recorded outside the framework's own hooks, such
        as a memory read or write: under the tool call making it."""
        link = links.current()
        if link is not None:
            return trace.set_span_in_context(
                trace.NonRecordingSpan(
                    trace.SpanContext(link.trace_id, link.span_id, is_remote=False, trace_flags=trace.TraceFlags(1))
                )
            )
        return None

    def _current_agent(self) -> str:
        return self._recorder.agent

    def _remember_output(self, trace_id: int, event_id: str, text: str) -> None:
        text = text.strip()
        if len(text) < _MIN_MATCH:
            return
        with self._lock:
            outputs = self._outputs.get(trace_id)
            if outputs is None:
                outputs = deque(maxlen=_OUTPUTS_PER_TRACE)
                self._outputs[trace_id] = outputs
            outputs.append((event_id, text[:_KEPT_OUTPUT]))

    def _matching_outputs(self, trace_id: int, text: str) -> list[str]:
        """Earlier events in this trace whose output appears in this text, or that contain all of it."""
        text = text.strip()
        whole = len(text) >= _MIN_MATCH
        with self._lock:
            outputs = list(self._outputs.get(trace_id, ()))
        return [
            event_id
            for event_id, output in outputs
            if output[:_SNIPPET] in text or (whole and text in output)
        ]

    def _outputs_containing(self, trace_id: int, arguments: list[str]) -> list[str]:
        """Earlier events in this trace whose output contains one of these arguments."""
        with self._lock:
            outputs = list(self._outputs.get(trace_id, ()))
        return [event_id for event_id, output in outputs if any(a in output for a in arguments)]

