"""The recorder: sends events to the Wake collector without ever blocking the agent."""

import os

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Tracer

from wake import events

DEFAULT_ENDPOINT = "http://localhost:4318"


class Recorder:
    """Owns a private OpenTelemetry pipeline, so it never touches the app's own tracing setup.

    Spans are exported from a bounded queue on a background thread. If the
    collector is slow or down, events are dropped and the agent keeps working.
    The recorder holds no signing keys: the collector signs events on arrival
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
    ) -> None:
        self.agent = agent
        endpoint = endpoint or os.environ.get("WAKE_ENDPOINT", DEFAULT_ENDPOINT)
        if exporter is None:
            exporter = OTLPSpanExporter(
                endpoint=f"{endpoint.rstrip('/')}/v1/traces", headers=headers, timeout=5
            )
        resource = Resource.create({"service.name": agent, events.AGENT_ID: agent})
        self._provider = TracerProvider(resource=resource)
        self._provider.add_span_processor(
            BatchSpanProcessor(exporter, max_queue_size=max_queue_size)
        )
        self.tracer: Tracer = self._provider.get_tracer("wake")

    def flush(self, timeout_millis: int = 5000) -> bool:
        return self._provider.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._provider.shutdown()
