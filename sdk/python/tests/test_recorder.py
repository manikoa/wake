import datetime
import json
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from langchain_core.tools import tool
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
from test_langgraph import ask, email_agent_graph

from wake import Recorder
from wake.recorder import MAX_BATCH_BYTES
from wake.langgraph import WakeCallbackHandler

ANSWER = "The email asks for a $10,000 wire."


class FailingExporter(SpanExporter):
    def __init__(self):
        self.failing = True

    def export(self, spans):
        if self.failing:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


class BlockedExporter(SpanExporter):
    """Holds the first export until released, so the queue behind it fills."""

    def __init__(self):
        self.release = threading.Event()

    def export(self, spans):
        self.release.wait(5)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.release.set()


def emit(recorder, count):
    for i in range(count):
        recorder.tracer.start_span(f"event {i}").end()


def test_counts_sent_events():
    recorder = Recorder(agent="email-agent", exporter=InMemorySpanExporter())
    ask(email_agent_graph(), WakeCallbackHandler(recorder))
    recorder.flush()
    health = recorder.health()
    assert health.ok
    assert health.sent == 4  # the agent run plus three events
    assert health.dropped == 0


def test_counts_failed_exports_and_calls_on_drop():
    drops = []
    exporter = FailingExporter()
    recorder = Recorder(
        agent="email-agent", exporter=exporter, on_drop=lambda n, why: drops.append((n, why))
    )
    result = ask(email_agent_graph(), WakeCallbackHandler(recorder))
    recorder.flush()

    assert result["messages"][-1].content == ANSWER
    health = recorder.health()
    assert not health.ok
    assert health.dropped == 4
    assert sum(n for n, _ in drops) == 4

    # Once the collector is back, the recorder reports healthy again but keeps the count.
    exporter.failing = False
    emit(recorder, 1)
    recorder.flush()
    assert recorder.health().ok
    assert recorder.health().dropped == 4


def test_counts_events_a_full_queue_turns_away():
    agent_thread = threading.get_ident()
    reported_on = []
    exporter = BlockedExporter()
    recorder = Recorder(
        agent="email-agent",
        exporter=exporter,
        max_queue_size=8,
        on_drop=lambda n, why: reported_on.append((threading.get_ident(), n, why)),
    )
    emit(recorder, 8)  # a full batch wakes the export thread, which blocks exporting it
    time.sleep(0.1)
    # Eight more fill the queue and the next twelve are turned away.
    emit(recorder, 20)
    assert recorder.health().dropped == 12
    assert reported_on == []  # never reported from the agent's thread
    exporter.release.set()
    recorder.flush()
    assert recorder.health().sent == 16
    assert [(n, why) for _, n, why in reported_on] == [(12, "queue full")]
    assert all(thread != agent_thread for thread, _, _ in reported_on)


def test_on_drop_errors_never_reach_the_agent():
    def page(n, why):
        raise RuntimeError("pager is down")

    recorder = Recorder(agent="email-agent", exporter=FailingExporter(), on_drop=page)
    result = ask(email_agent_graph(), WakeCallbackHandler(recorder))
    recorder.flush()
    assert result["messages"][-1].content == ANSWER
    assert recorder.health().dropped == 4


class Collector:
    """A stand-in OTLP/HTTP collector that accepts every request."""

    def __init__(self):
        self.requests = 0

        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                collector.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def kill(self):
        self._server.shutdown()
        self._server.server_close()


def test_agent_keeps_working_when_the_collector_is_killed_mid_run():
    collector = Collector()
    recorder = Recorder(agent="email-agent", endpoint=collector.endpoint)
    handler = WakeCallbackHandler(recorder)

    ask(email_agent_graph(), handler)
    recorder.flush()
    assert collector.requests > 0
    assert recorder.health().sent == 4

    collector.kill()
    started = time.monotonic()
    result = ask(email_agent_graph(), handler)
    # The agent finishes without waiting on the dead collector.
    assert time.monotonic() - started < 0.5
    assert result["messages"][-1].content == ANSWER

    recorder.flush(timeout_millis=10_000)
    health = recorder.health()
    assert health.dropped == 4
    assert not health.ok


def spans_of(exporter):
    return [s for s in exporter.get_finished_spans() if "wake.event.kind" in s.attributes]


def test_records_under_the_apps_unsampled_span():
    # The app's own tracing left the current span unsampled (traceparent flags 00). Wake's
    # events don't join it, so they're recorded all the same, in a trace of their own.
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="email-agent", exporter=exporter)
    app_span = trace.NonRecordingSpan(trace.SpanContext(0xA0, 0xB0, is_remote=True, trace_flags=trace.TraceFlags(0)))
    token = otel_context.attach(trace.set_span_in_context(app_span))
    try:
        ask(email_agent_graph(), WakeCallbackHandler(recorder))
    finally:
        otel_context.detach(token)
    assert recorder.flush()
    assert recorder.health().sent == 4
    assert {s.context.trace_id for s in exporter.get_finished_spans()} != {0xA0}
    assert all(s.parent is None or s.parent.trace_id != 0xA0 for s in exporter.get_finished_spans())


def test_ignores_the_apps_otel_environment(monkeypatch):
    for name, value in {
        "OTEL_SDK_DISABLED": "true",
        "OTEL_TRACES_SAMPLER": "always_off",
        "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT": "16",
        "OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT": "2",
        "OTEL_BSP_MAX_QUEUE_SIZE": "1",
        "OTEL_BSP_SCHEDULE_DELAY": "nonsense",
        "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=app",
    }.items():
        monkeypatch.setenv(name, value)
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="email-agent", exporter=exporter)
    ask(email_agent_graph(), WakeCallbackHandler(recorder))
    assert recorder.flush()
    assert recorder.health().sent == 4
    events = spans_of(exporter)
    assert len(events) == 3
    for event in events:
        json.loads(event.attributes["wake.payload"])  # whole, not cut short
        assert event.attributes["wake.agent.id"] == "email-agent"
    assert "deployment.environment" not in events[0].resource.attributes


def test_counts_spans_that_arent_recorded_as_dropped():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="email-agent", exporter=exporter)
    recorder.tracer._tracer.sampler = ALWAYS_OFF  # however it happens, it can't go unnoticed
    ask(email_agent_graph(), WakeCallbackHandler(recorder))
    assert not recorder.flush()
    health = recorder.health()
    assert health.sent == 0
    assert health.dropped == 4
    assert not health.ok


class RecordingCollector(Collector):
    """Notes the headers and body of each request."""

    def __init__(self):
        self.seen = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                collector.seen.append((dict(self.headers), body))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()


def test_the_apps_otlp_settings_never_reach_the_collector(monkeypatch):
    collector = RecordingCollector()
    for name, value in {
        "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer app-secret",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "x-api-key=app-key",
        "OTEL_EXPORTER_OTLP_COMPRESSION": "gzip",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:9/v1/traces",
        "OTEL_EXPORTER_OTLP_TIMEOUT": "60",
    }.items():
        monkeypatch.setenv(name, value)
    recorder = Recorder(agent="email-agent", endpoint=collector.endpoint, headers={"x-wake": "ours"})
    emit(recorder, 1)
    assert recorder.flush()
    collector.kill()
    ((headers, body),) = collector.seen
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered["x-wake"] == "ours"
    assert "authorization" not in lowered and "x-api-key" not in lowered
    assert "content-encoding" not in lowered and not body.startswith(b"\x1f\x8b")


class HungCollector:
    """Accepts connections and never answers."""

    def __init__(self):
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(16)
        self.endpoint = f"http://127.0.0.1:{self._socket.getsockname()[1]}"

    def close(self):
        self._socket.close()


def test_flush_waits_no_longer_than_its_timeout():
    collector = HungCollector()
    recorder = Recorder(agent="email-agent", endpoint=collector.endpoint)
    emit(recorder, 600)  # more than one batch
    started = time.monotonic()
    assert recorder.flush(timeout_millis=300) is False
    assert time.monotonic() - started < 1
    collector.close()


def test_flush_says_whether_events_were_dropped():
    exporter = FailingExporter()
    recorder = Recorder(agent="email-agent", exporter=exporter)
    emit(recorder, 3)
    assert recorder.flush() is False
    exporter.failing = False
    emit(recorder, 3)
    assert recorder.flush() is True, "nothing dropped since the last flush"


def test_counts_events_it_failed_to_record(monkeypatch):
    drops = []
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="email-agent", exporter=exporter, on_drop=lambda n, why: drops.append((n, why)))
    handler = WakeCallbackHandler(recorder)

    def bug(*args, **kwargs):
        raise RuntimeError("recorder bug")

    monkeypatch.setattr(handler, "_start_event", bug)
    result = ask(email_agent_graph(), handler)
    assert result["messages"][-1].content == ANSWER
    assert recorder.flush() is False
    health = recorder.health()
    assert health.failed == 3  # two model calls and a tool call
    assert health.dropped == 0, "the collector got everything it was sent"
    assert sorted(why for _, why in drops) == ["failed to record on_chat_model_start", "failed to record on_tool_start"]
    assert sum(n for n, _ in drops) == 3


def test_a_payload_json_cant_hold_is_still_recorded():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="email-agent", exporter=exporter)
    handler = WakeCallbackHandler(recorder)
    key = uuid.uuid4()

    @tool
    def lookup(order: str) -> dict:
        """Look up an order."""
        return {key: datetime.datetime(2026, 10, 27), 1: "one", "two": 2}

    lookup.invoke({"order": "1234"}, config={"callbacks": [handler]})
    assert recorder.flush()
    (event,) = spans_of(exporter)
    assert event.attributes["wake.event.kind"] == "tool_call"
    payload = json.loads(event.attributes["wake.payload"])
    assert payload["output"] == {str(key): "2026-10-27 00:00:00", "1": "one", "two": 2}
    assert recorder.health().failed == 0


class SizedExporter(SpanExporter):
    """Notes how many spans, and how many payload bytes, each export carries."""

    def __init__(self):
        self.batches = []

    def export(self, spans):
        self.batches.append((len(spans), sum(len(s.attributes.get("wake.payload", "")) for s in spans)))
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


def emit_payloads(recorder, count, size):
    for i in range(count):
        span = recorder.tracer.start_span(f"event {i}")
        span.set_attribute("wake.payload", "x" * size)
        span.end()


def test_batches_stay_under_the_collectors_request_limit():
    exporter = SizedExporter()
    recorder = Recorder(agent="email-agent", exporter=exporter)
    # Five 3 MiB contexts: together past the collector's 20 MiB limit, so never one batch.
    emit_payloads(recorder, 5, 3 << 20)
    assert recorder.flush(timeout_millis=10_000)
    assert recorder.health().sent == 5
    assert sum(n for n, _ in exporter.batches) == 5
    assert all(size <= MAX_BATCH_BYTES for _, size in exporter.batches), exporter.batches


def test_the_queue_is_bounded_by_bytes_too():
    exporter = InMemorySpanExporter()
    recorder = Recorder(agent="email-agent", exporter=exporter, max_queue_bytes=1 << 20)
    emit_payloads(recorder, 5, 300_000)  # the export thread waits for more before sending
    assert recorder.health().dropped == 2
    assert not recorder.flush()
    assert recorder.health().sent == 3


def test_the_apps_proxy_settings_never_reach_the_collector(monkeypatch):
    # The app sends its own traffic through a proxy that isn't there; Wake's goes direct.
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    collector = Collector()
    recorder = Recorder(agent="email-agent", endpoint=collector.endpoint)
    emit(recorder, 3)
    assert recorder.flush()
    assert recorder.health().sent == 3 and collector.requests == 1
