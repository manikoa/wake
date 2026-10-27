//! Sends signed events to the collector over OTLP/HTTP, in batches, from a bounded queue.
//! When the collector is down, events are retried a few times and then dropped and
//! counted; the processor and agentgateway never wait on it.

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use opentelemetry_proto::tonic::collector::trace::v1::ExportTraceServiceRequest;
use opentelemetry_proto::tonic::common::v1::{AnyValue, ArrayValue, InstrumentationScope, KeyValue, any_value};
use opentelemetry_proto::tonic::resource::v1::Resource;
use opentelemetry_proto::tonic::trace::v1::{ResourceSpans, ScopeSpans, Span, span::SpanKind};
use prost::Message;
use tokio::sync::mpsc;

use crate::canonical::{Event, Value};

/// What the processor has done since it started. Logged, and checked by tests.
#[derive(Default)]
pub struct Counters {
    /// Hook calls dropped because the capture queue was full.
    pub hooks_dropped: AtomicU64,
    /// Events sent to the collector.
    pub sent: AtomicU64,
    /// Events dropped because the collector couldn't take them.
    pub export_dropped: AtomicU64,
    /// Responses paired with their request by the event id the gateway passed back.
    pub paired_by_id: AtomicU64,
    /// Responses paired by order within a session, because the id didn't come back.
    pub paired_in_order: AtomicU64,
}

impl Counters {
    pub fn get(c: &AtomicU64) -> u64 {
        c.load(Ordering::Relaxed)
    }
}

pub struct Exporter {
    url: String,
    client: reqwest::Client,
    counters: Arc<Counters>,
    batch: usize,
    linger: Duration,
    attempts: u32,
}

impl Exporter {
    /// endpoint is the collector's OTLP/HTTP base URL, such as http://localhost:4318.
    /// ca, if given, is a PEM CA certificate to trust for the collector's TLS.
    pub fn new(endpoint: &str, ca: Option<&[u8]>, counters: Arc<Counters>) -> Self {
        let mut builder = reqwest::Client::builder().timeout(Duration::from_secs(5));
        if let Some(pem) = ca {
            match reqwest::Certificate::from_pem(pem) {
                Ok(cert) => builder = builder.add_root_certificate(cert),
                Err(e) => {
                    tracing::error!(error = %e, "collector CA isn't a PEM certificate; trusting only the system's")
                }
            }
        }
        Exporter {
            url: format!("{}/v1/traces", endpoint.trim_end_matches('/')),
            client: builder.build().expect("http client"),
            counters,
            batch: 256,
            linger: Duration::from_millis(200),
            attempts: 3,
        }
    }

    /// Sends events from rx until it closes, then sends what's left.
    pub async fn run(self, mut rx: mpsc::Receiver<Event>) {
        let mut batch = Vec::with_capacity(self.batch);
        loop {
            let first = rx.recv().await;
            let Some(first) = first else { break };
            batch.push(first);
            let deadline = tokio::time::sleep(self.linger);
            tokio::pin!(deadline);
            while batch.len() < self.batch {
                tokio::select! {
                    next = rx.recv() => match next {
                        Some(e) => batch.push(e),
                        None => break,
                    },
                    _ = &mut deadline => break,
                }
            }
            self.send(std::mem::take(&mut batch)).await;
        }
    }

    async fn send(&self, events: Vec<Event>) {
        let n = events.len() as u64;
        let body = request(&events).encode_to_vec();
        let mut delay = Duration::from_millis(200);
        for attempt in 1..=self.attempts {
            let result = self
                .client
                .post(&self.url)
                .header("content-type", "application/x-protobuf")
                .body(body.clone())
                .send()
                .await;
            match result {
                Ok(r) if r.status().is_success() => {
                    self.counters.sent.fetch_add(n, Ordering::Relaxed);
                    return;
                }
                Ok(r) => tracing::warn!(status = %r.status(), attempt, "collector refused events"),
                Err(e) => tracing::warn!(error = %e, attempt, "collector unreachable"),
            }
            if attempt < self.attempts {
                tokio::time::sleep(delay).await;
                delay *= 2;
            }
        }
        self.counters.export_dropped.fetch_add(n, Ordering::Relaxed);
        tracing::error!(events = n, "dropped events the collector couldn't take");
    }
}

/// The OTLP request for a batch of events.
pub fn request(events: &[Event]) -> ExportTraceServiceRequest {
    let spans = events.iter().map(span).collect();
    ExportTraceServiceRequest {
        resource_spans: vec![ResourceSpans {
            resource: Some(Resource {
                attributes: vec![kv("service.name", &Value::Str("wake-proxy".into()))],
                ..Default::default()
            }),
            scope_spans: vec![ScopeSpans {
                scope: Some(InstrumentationScope {
                    name: "wake-proxy".into(),
                    version: env!("CARGO_PKG_VERSION").into(),
                    ..Default::default()
                }),
                spans,
                ..Default::default()
            }],
            ..Default::default()
        }],
    }
}

fn span(e: &Event) -> Span {
    Span {
        trace_id: e.trace_id.to_vec(),
        span_id: e.span_id.to_vec(),
        parent_span_id: e.parent_span_id.map(|p| p.to_vec()).unwrap_or_default(),
        name: e.name.clone(),
        kind: SpanKind::Client as i32,
        start_time_unix_nano: e.start_time_unix_nano,
        end_time_unix_nano: e.end_time_unix_nano,
        attributes: e.attributes.iter().map(|(k, v)| kv(k, v)).collect(),
        ..Default::default()
    }
}

fn kv(key: &str, value: &Value) -> KeyValue {
    let string = |s: &str| AnyValue {
        value: Some(any_value::Value::StringValue(s.to_string())),
    };
    let value = match value {
        Value::Str(s) => string(s),
        Value::Int(n) => AnyValue {
            value: Some(any_value::Value::IntValue(*n)),
        },
        Value::Bool(b) => AnyValue {
            value: Some(any_value::Value::BoolValue(*b)),
        },
        Value::Strs(list) => AnyValue {
            value: Some(any_value::Value::ArrayValue(ArrayValue {
                values: list.iter().map(|s| string(s)).collect(),
            })),
        },
    };
    KeyValue {
        key: key.to_string(),
        value: Some(value),
        ..Default::default()
    }
}
