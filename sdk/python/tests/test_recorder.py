import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_langgraph import ask, email_agent_graph

from wake import Recorder
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


def test_counts_events_pushed_out_of_a_full_queue():
    agent_thread = threading.get_ident()
    reported_on = []
    exporter = BlockedExporter()
    recorder = Recorder(
        agent="email-agent",
        exporter=exporter,
        max_queue_size=8,
        on_drop=lambda n, why: reported_on.append((threading.get_ident(), n, why)),
    )
    emit(recorder, 8)  # a full batch wakes the worker, which blocks exporting it
    time.sleep(0.1)
    # Eight more fill the queue and the next twelve push spans out.
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
