"""The recorder: sends events to the Wake collector without ever blocking the agent."""

import logging
import os
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

import requests
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanLimits, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Span, Tracer

from wake import events

DEFAULT_ENDPOINT = "http://localhost:4318"
# How long one export may take: short, so a hung collector costs a flush little, plus a
# second for each MiB the batch holds. A batch that times out is dropped and counted.
EXPORT_TIMEOUT = 3.0
# What may wait to be sent. A span that would take the queue past either bound is dropped
# and counted, so a flood of large payloads can't grow the agent's memory.
MAX_QUEUE_BYTES = 64 << 20
# What one export sends: well under the collector's 20 MiB request limit, and a payload is
# never over events.MAX_PAYLOAD_BYTES, so every event fits a batch on its own.
MAX_BATCH_SIZE = 512
MAX_BATCH_BYTES = 8 << 20
# How long a span waits for others to send with, unless a batch fills first.
EXPORT_DELAY = 5.0

log = logging.getLogger("wake")


@dataclass(frozen=True)
class Health:
    """What the recorder has sent and dropped since it started. failed counts the events
    it couldn't record at all, such as one whose hook raised."""

    sent: int
    dropped: int
    last_error: str | None
    failed: int = 0

    @property
    def ok(self) -> bool:
        return self.last_error is None


class _Counts:
    """Counts sent, dropped and failed events.

    Drops and failures can be counted from any thread, including the agent's, so counting
    only takes a lock. Reporting them, the warning and the on_drop call, happens when
    spans are exported: on the export thread, or in an explicit flush(). A slow on_drop
    can never slow the agent's own calls.
    """

    def __init__(self, on_drop: Callable[[int, str], None] | None) -> None:
        self._lock = threading.Lock()
        self._on_drop = on_drop
        self._unreported: dict[str, int] = {}  # reason -> drops not yet reported
        self._warned = False
        self.sent = 0
        self.dropped = 0
        self.failed = 0
        self.last_error: str | None = None

    def snapshot(self) -> Health:
        with self._lock:
            return Health(self.sent, self.dropped, self.last_error, self.failed)

    def lost(self) -> int:
        with self._lock:
            return self.dropped + self.failed

    def record_sent(self, count: int) -> None:
        with self._lock:
            self.sent += count
            self.last_error = None

    def record_dropped(self, count: int, reason: str) -> None:
        with self._lock:
            self.dropped += count
            self._lose(count, reason)

    def record_failed(self, reason: str) -> None:
        with self._lock:
            self.failed += 1
            self._lose(1, reason)

    def _lose(self, count: int, reason: str) -> None:
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


class _OTLPExporter(SpanExporter):
    """Sends spans to the collector as OTLP/HTTP protobuf, configured only by its arguments.

    The app's OTEL_EXPORTER_OTLP_* settings are for its own telemetry, and its headers can
    hold its secrets, so none of them apply here. Nor do its HTTPS_PROXY, REQUESTS_CA_BUNDLE
    or .netrc: events go straight to the collector. Each batch is tried once, with a short
    timeout: a batch that fails is dropped and counted, and the next one goes on.
    """

    def __init__(self, endpoint: str, headers: Mapping[str, str] | None, ca_file: str | None, timeout: float) -> None:
        self._url = f"{endpoint.rstrip('/')}/v1/traces"
        self._headers = {**(headers or {}), "Content-Type": "application/x-protobuf"}
        self._verify: str | bool = ca_file or True
        self._timeout = timeout
        self._session = requests.Session()
        self._session.trust_env = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        body = encode_spans(spans).SerializeToString()
        timeout = self._timeout + len(body) / (1 << 20)
        with self._session.post(
            self._url, data=body, headers=self._headers, verify=self._verify, timeout=timeout
        ) as response:
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f"the collector answered HTTP {response.status_code}")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._session.close()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


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


def _size(span: ReadableSpan) -> int:
    """About how many bytes a span takes on the wire: its strings, wake.payload above all,
    which is ASCII, so its length is its size, and a little for everything else."""
    size = 256
    for key, value in (span.attributes or {}).items():
        size += len(key) + 16
        if isinstance(value, str):
            size += len(value)
        elif isinstance(value, (list, tuple)):
            size += sum(len(v) + 8 if isinstance(v, str) else 16 for v in value)
    return size


class _BatchProcessor(SpanProcessor):
    """Queues ended spans and exports them in batches on a thread of its own, so the agent
    never waits on the collector.

    The queue and each batch are bounded by count and by bytes. A span that doesn't fit the
    queue is dropped and counted; a batch stays under the collector's request limit, however
    large the payloads. Its settings come from its arguments, never the app's OTEL_BSP_*
    environment. A forked process starts with an empty queue and an export thread of its own.
    """

    def __init__(self, exporter: SpanExporter, counts: _Counts, max_queue_size: int, max_queue_bytes: int) -> None:
        self._exporter = exporter
        self._counts = counts
        self._max_queue_size = max_queue_size
        self._max_queue_bytes = max_queue_bytes
        self._max_batch_size = min(MAX_BATCH_SIZE, max_queue_size)
        self._closed = False
        self._start()
        if hasattr(os, "register_at_fork"):
            ref = weakref.ref(self)

            def after_in_child() -> None:
                processor = ref()
                if processor is not None and not processor._closed:
                    processor._start()

            os.register_at_fork(after_in_child=after_in_child)

    def _start(self) -> None:
        self._ready = threading.Condition()
        self._queue: deque[tuple[ReadableSpan, int]] = deque()
        self._queued_bytes = 0
        self._exporting = 0  # spans taken from the queue and not yet exported
        self._flushing = 0  # flushes waiting for the queue to empty
        self._worker = threading.Thread(target=self._run, name="wake-export", daemon=True)
        self._worker.start()

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        if not span.context.trace_flags.sampled:
            return
        size = _size(span)
        with self._ready:
            if self._closed:
                return
            full = len(self._queue) >= self._max_queue_size or self._queued_bytes + size > self._max_queue_bytes
            if not full:
                self._queue.append((span, size))
                self._queued_bytes += size
                if len(self._queue) == 1 or self._batch_ready():
                    self._ready.notify_all()
        if full:
            self._counts.record_dropped(1, "queue full")

    def _batch_ready(self) -> bool:
        return len(self._queue) >= self._max_batch_size or self._queued_bytes >= MAX_BATCH_BYTES

    def _run(self) -> None:
        while True:
            with self._ready:
                while not self._queue and not self._closed:
                    self._ready.wait()
                if not self._queue:
                    return
                # Wait for a full batch, a flush or a close, but no longer than the delay.
                deadline = time.monotonic() + EXPORT_DELAY
                while not (self._closed or self._flushing or self._batch_ready()):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    self._ready.wait(left)
                batch = self._take()
                self._exporting = len(batch)
            self._exporter.export(batch)  # counts and never raises
            with self._ready:
                self._exporting = 0
                self._ready.notify_all()

    def _take(self) -> list[ReadableSpan]:
        batch: list[ReadableSpan] = []
        size = 0
        while self._queue and len(batch) < self._max_batch_size:
            span, n = self._queue[0]
            if batch and size + n > MAX_BATCH_BYTES:
                break
            self._queue.popleft()
            self._queued_bytes -= n
            batch.append(span)
            size += n
        return batch

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Sends what's queued now, waiting at most timeout_millis. Returns whether it all went."""
        deadline = time.monotonic() + timeout_millis / 1000
        with self._ready:
            self._flushing += 1
            self._ready.notify_all()
            try:
                while self._queue or self._exporting:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return False
                    self._ready.wait(left)
                return True
            finally:
                self._flushing -= 1

    def shutdown(self) -> None:
        """Sends what's queued, waiting at most 30 seconds, then shuts the exporter down."""
        with self._ready:
            self._closed = True
            self._ready.notify_all()
        self._worker.join(30)
        self._exporter.shutdown()


class _Tracer(Tracer):
    """Wake's tracer. A span with no Wake parent starts a trace of its own, instead of
    joining whatever span the app has current, which the app's sampler may have left
    unsampled. A span that isn't recorded anyway is counted as dropped, so health() can't
    stay ok while nothing is recorded."""

    def __init__(self, tracer: Tracer, counts: _Counts) -> None:
        self._tracer = tracer
        self._counts = counts

    def start_span(self, name: str, context: Context | None = None, *args, **kwargs) -> Span:
        span = self._tracer.start_span(name, Context() if context is None else context, *args, **kwargs)
        if not span.is_recording():
            self._counts.record_dropped(1, "span not recorded")
        return span

    @contextmanager
    def start_as_current_span(self, name: str, context: Context | None = None, *args, end_on_exit: bool = True,
                              **kwargs) -> Iterator[Span]:
        span = self.start_span(name, context, *args, **kwargs)
        with trace.use_span(span, end_on_exit=end_on_exit) as current:
            yield current


class Recorder:
    """Owns a private OpenTelemetry pipeline, so it never touches the app's own tracing setup.

    Spans are exported from a queue on a background thread, bounded by max_queue_size
    events and max_queue_bytes. If the collector is slow or down, events are dropped and
    counted, and the agent keeps working. Check health(), or pass on_drop to page someone.
    The recorder holds no signing keys: the collector signs events on arrival (see
    docs/signing.md).

    Its settings come from its arguments and WAKE_* variables only: WAKE_ENDPOINT, and
    WAKE_CA_FILE for the CA that signed the collector's TLS certificate. The app's OTEL_*
    variables configure the app's telemetry, not Wake's, and its proxy settings its own
    traffic.
    """

    def __init__(
        self,
        agent: str,
        endpoint: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        ca_file: str | None = None,
        exporter: SpanExporter | None = None,
        max_queue_size: int = 2048,
        max_queue_bytes: int = MAX_QUEUE_BYTES,
        on_drop: Callable[[int, str], None] | None = None,
    ) -> None:
        self.agent = agent
        if exporter is None:
            exporter = _OTLPExporter(
                endpoint or os.environ.get("WAKE_ENDPOINT") or DEFAULT_ENDPOINT,
                headers,
                ca_file or os.environ.get("WAKE_CA_FILE") or None,
                EXPORT_TIMEOUT,
            )
        self._counts = _Counts(on_drop)
        self._flushed_lost = 0
        self._flush_lock = threading.Lock()
        self._flushing: threading.Thread | None = None
        self._flushed_all = False  # whether the last flush sent everything queued before its time ran out
        # Every event is recorded: no sampler, span limits or resource from the environment,
        # and no OTEL_SDK_DISABLED. Attribute values aren't cut short, since wake.payload
        # must stay valid JSON.
        self._provider = TracerProvider(
            sampler=ALWAYS_ON,
            resource=Resource({"service.name": agent, events.AGENT_ID: agent}),
            span_limits=SpanLimits(
                max_attributes=128,
                max_events=128,
                max_links=128,
                max_span_attributes=128,
                max_event_attributes=128,
                max_link_attributes=128,
                max_attribute_length=SpanLimits.UNSET,
                max_span_attribute_length=SpanLimits.UNSET,
            ),
            meter_provider=NoOpMeterProvider(),
        )
        self._provider._disabled = False  # set from OTEL_SDK_DISABLED; a test covers it
        self._provider.add_span_processor(
            _BatchProcessor(_CountingExporter(exporter, self._counts), self._counts, max_queue_size, max_queue_bytes)
        )
        self.tracer: Tracer = _Tracer(self._provider.get_tracer("wake"), self._counts)

    def health(self) -> Health:
        return self._counts.snapshot()

    def flush(self, timeout_millis: int = 5000) -> bool:
        """Sends what's queued, waiting at most timeout_millis. Returns False if that ran
        out, or if any event was dropped or failed since the last flush."""
        with self._flush_lock:
            if self._flushing is None or not self._flushing.is_alive():
                # On its own thread, so a hung collector can't hold the caller past the timeout.
                self._flushing = threading.Thread(target=self._flush, args=(timeout_millis,),
                                                  name="wake-flush", daemon=True)
                self._flushing.start()
            flushing = self._flushing
        flushing.join(timeout_millis / 1000)
        with self._flush_lock:
            lost = self._counts.lost()
            clean = lost == self._flushed_lost
            self._flushed_lost = lost
            return clean and not flushing.is_alive() and self._flushed_all

    def _flush(self, timeout_millis: int) -> None:
        sent_all = False
        try:
            sent_all = self._provider.force_flush(timeout_millis)
        except Exception:
            log.warning("wake: flush failed", exc_info=True)
        with self._flush_lock:
            self._flushed_all = sent_all
        self._counts.report()

    def shutdown(self) -> None:
        self._provider.shutdown()

    def _failed(self, what: str) -> None:
        """Counts an event an integration couldn't record."""
        self._counts.record_failed(f"failed to record {what}")
