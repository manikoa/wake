"""The recorder: sends events to the Wake collector without ever blocking the agent."""

import logging
import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Tracer

from wake import events

DEFAULT_ENDPOINT = "http://localhost:4318"

log = logging.getLogger("wake")


@dataclass(frozen=True)
class Health:
    """What the recorder has sent and dropped since it started."""

    sent: int
    dropped: int
    last_error: str | None

    @property
    def ok(self) -> bool:
        return self.last_error is None


class _Counts:
    """Counts sent and dropped spans.

    Drops can be counted from any thread, including the agent's, so counting
    only takes a lock. Reporting them, the warning and the on_drop call,
    happens when spans are exported: on the export thread, or in an explicit
    flush(). A slow on_drop can never slow the agent's own calls.
    """

    def __init__(self, on_drop: Callable[[int, str], None] | None) -> None:
        self._lock = threading.Lock()
        self._on_drop = on_drop
        self._unreported: dict[str, int] = {}  # reason -> drops not yet reported
        self._warned = False
        self.sent = 0
        self.dropped = 0
        self.last_error: str | None = None

    def snapshot(self) -> Health:
        with self._lock:
            return Health(self.sent, self.dropped, self.last_error)

    def record_sent(self, count: int) -> None:
        with self._lock:
            self.sent += count
            self.last_error = None

    def record_dropped(self, count: int, reason: str) -> None:
        with self._lock:
            self.dropped += count
            self.last_error = reason
            self._unreported[reason] = self._unreported.get(reason, 0) + count

    def report(self) -> None:
        """Warn and call on_drop for drops since the last report. Runs on the export thread."""
        with self._lock:
            pending, self._unreported = self._unreported, {}
            recovered = not pending and self._warned and self.last_error is None
            started = bool(pending) and not self._warned
            if pending:
                self._warned = True
            elif recovered:
                self._warned = False
        if started:
            log.warning("wake: dropping events (%s); the agent keeps working", ", ".join(pending))
        if recovered:
            log.warning("wake: collector reachable again, recording resumed")
        if self._on_drop is None:
            return
        for reason, count in pending.items():
            try:
                self._on_drop(count, reason)
            except Exception:
                log.warning("wake: on_drop callback failed", exc_info=True)


class _CountingExporter(SpanExporter):
    """Counts every exported span as sent or dropped, then reports drops. Never raises."""

    def __init__(self, exporter: SpanExporter, counts: _Counts) -> None:
        self._exporter = exporter
        self._counts = counts

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._exporter.export(spans)
        except Exception as error:
            self._counts.record_dropped(len(spans), f"export failed: {error!r}")
            result = SpanExportResult.FAILURE
        else:
            if result == SpanExportResult.SUCCESS:
                self._counts.record_sent(len(spans))
            else:
                self._counts.record_dropped(len(spans), "export failed")
        self._counts.report()
        return result

    def shutdown(self) -> None:
        self._counts.report()
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)


class _CountingBatchProcessor(BatchSpanProcessor):
    """Counts the spans the batch processor drops when its queue is full.

    The queue is a bounded deque that silently pushes out its oldest span, so
    a span arriving at a full queue means one is dropped. The queue is an
    internal of the OpenTelemetry SDK; a test covers it so an upgrade that
    changes it fails loudly.
    """

    def __init__(self, exporter: SpanExporter, counts: _Counts, max_queue_size: int) -> None:
        super().__init__(
            exporter,
            max_queue_size=max_queue_size,
            max_export_batch_size=min(512, max_queue_size),
        )
        self._counts = counts
        self._max_queue_size = max_queue_size

    def on_end(self, span: ReadableSpan) -> None:
        queue = getattr(getattr(self, "_batch_processor", None), "_queue", None)
        if queue is not None and len(queue) >= self._max_queue_size:
            self._counts.record_dropped(1, "queue full")
        super().on_end(span)


class Recorder:
    """Owns a private OpenTelemetry pipeline, so it never touches the app's own tracing setup.

    Spans are exported from a bounded queue on a background thread. If the
    collector is slow or down, events are dropped and counted, and the agent
    keeps working. Check health(), or pass on_drop to page someone. The
    recorder holds no signing keys: the collector signs events on arrival
    (see docs/signing.md).
    """

    def __init__(
        self,
        agent: str,
        endpoint: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        exporter: SpanExporter | None = None,
        max_queue_size: int = 2048,
        on_drop: Callable[[int, str], None] | None = None,
    ) -> None:
        self.agent = agent
        endpoint = endpoint or os.environ.get("WAKE_ENDPOINT", DEFAULT_ENDPOINT)
        if exporter is None:
            exporter = OTLPSpanExporter(
                endpoint=f"{endpoint.rstrip('/')}/v1/traces", headers=headers, timeout=5
            )
        self._counts = _Counts(on_drop)
        resource = Resource.create({"service.name": agent, events.AGENT_ID: agent})
        self._provider = TracerProvider(resource=resource)
        self._provider.add_span_processor(
            _CountingBatchProcessor(
                _CountingExporter(exporter, self._counts), self._counts, max_queue_size
            )
        )
        self.tracer: Tracer = self._provider.get_tracer("wake")

    def health(self) -> Health:
        return self._counts.snapshot()

    def flush(self, timeout_millis: int = 5000) -> bool:
        return self._provider.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._provider.shutdown()
