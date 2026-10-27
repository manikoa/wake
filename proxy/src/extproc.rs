//! The Envoy ext_proc gRPC service agentgateway calls on A2A routes. One gRPC stream is one
//! HTTP exchange between two agents: request headers and body, then response headers and
//! body.
//!
//! Each message is answered at once with "continue", and the processor never changes a
//! body. Bodies are buffered by agentgateway (bufferedPartial), except a streamed
//! (text/event-stream) response: there the processor asks to receive it in full duplex
//! mode and passes every chunk straight back.
//!
//! A message request (message/send or SendMessage, and their streaming forms) becomes an
//! agent_message event in the receiver's name, with the sender as its origin. The reply
//! becomes another, in the sender's name, with the receiver as its origin. The request
//! headers forwarded to the receiver gain traceparent and wake-parents, so a receiver with a
//! Wake SDK links its next step to the message, the same way the proxy reads a sender's
//! links (see docs/proxy.md). A reply to tasks/get or GetTask links to the message that
//! started the task, which an earlier exchange recorded.

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::{SystemTime, UNIX_EPOCH};

use prost_types::value::Kind;
use tokio::sync::mpsc;
use tokio_stream::StreamExt;
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Request, Response, Status, Streaming};

use crate::a2a;
use crate::canonical::{Event, Value};
use crate::export::Counters;
use crate::mcp::{self, TraceParent};

// Generated from Envoy's protos, doc comments and all.
#[allow(clippy::all)]
pub mod pb {
    pub mod envoy {
        pub mod service {
            pub mod common {
                pub mod v3 {
                    tonic::include_proto!("envoy.service.common.v3");
                }
            }
            pub mod ext_proc {
                pub mod v3 {
                    tonic::include_proto!("envoy.service.ext_proc.v3");
                }
            }
        }
    }
    pub use envoy::service::common::v3 as common;
    pub use envoy::service::ext_proc::v3 as ext_proc;
}

use pb::common::{HeaderValue, HeaderValueOption, header_value_option::HeaderAppendAction};
use pb::ext_proc::external_processor_server::ExternalProcessor;
use pb::ext_proc::processing_mode::BodySendMode;
use pb::ext_proc::processing_request::Request as Phase;
use pb::ext_proc::processing_response::Response as Answer;
use pb::ext_proc::{
    BodyMutation, BodyResponse, CommonResponse, HeaderMutation, HeadersResponse, HttpHeaders, ProcessingMode,
    ProcessingRequest, ProcessingResponse, StreamedBodyResponse, TrailersResponse, body_mutation,
};

/// The namespace agentgateway puts the route's requestAttributes under.
pub const ATTRIBUTES: &str = "envoy.filters.http.ext_proc";
/// requestAttributes keys the gateway config fills from CEL.
pub const ATTR_SENDER: &str = "sender";
pub const ATTR_RECEIVER: &str = "receiver";

/// How much of each body is kept for the event's payload.
pub const MAX_CAPTURE: usize = 1 << 20;

/// One HTTP exchange between two agents.
#[derive(Default)]
pub struct Exchange {
    started: bool,
    request_mode: i32,
    response_mode: i32,
    recorded: bool, // a POST: request and reply are recorded
    sender: String,
    receiver: String,
    header_trace: Option<TraceParent>,
    header_parents: Vec<String>,
    trace_id: [u8; 16],
    parent_span: Option<[u8; 8]>,
    message_id: String,
    message_span: [u8; 8],
    request_body: Vec<u8>,
    request_truncated: bool,
    request: Option<a2a::Request>,
    message_emitted: bool,
    started_at: u64,
    response_seen: bool,
    response_streamed: bool,
    response_body: Vec<u8>,
    response_truncated: bool,
    reply_emitted: bool,
    tasks: Option<Arc<a2a::Tasks>>,
}

impl Exchange {
    /// An exchange that shares, with every other, which message started each task.
    pub fn with_tasks(tasks: Arc<a2a::Tasks>) -> Self {
        Exchange {
            tasks: Some(tasks),
            ..Default::default()
        }
    }

    /// Answers one message from the gateway and returns any events it completes. The
    /// answer is always to continue, unchanged.
    pub fn handle(&mut self, request: ProcessingRequest, now: SystemTime) -> (ProcessingResponse, Vec<Event>) {
        if !self.started {
            self.started = true;
            if let Some(c) = &request.protocol_config {
                self.request_mode = c.request_body_mode;
                self.response_mode = c.response_body_mode;
            }
            let attrs = attributes(&request);
            self.sender = attrs
                .get(ATTR_SENDER)
                .filter(|s| !s.is_empty())
                .cloned()
                .unwrap_or_else(|| "unknown".into());
            self.receiver = attrs
                .get(ATTR_RECEIVER)
                .filter(|s| !s.is_empty())
                .cloned()
                .unwrap_or_else(|| "unknown".into());
        }
        let mut events = Vec::new();
        let answer = match request.request {
            Some(Phase::RequestHeaders(h)) => Answer::RequestHeaders(self.request_headers(&h, now)),
            Some(Phase::RequestBody(b)) => {
                let full_duplex = self.request_mode == BodySendMode::FullDuplexStreamed as i32;
                if self.recorded {
                    capture(&mut self.request_body, &mut self.request_truncated, &b.body);
                    if b.end_of_stream {
                        events.extend(self.finish_request(now));
                    }
                }
                Answer::RequestBody(body_answer(full_duplex, b.body, b.end_of_stream))
            }
            Some(Phase::ResponseHeaders(h)) => {
                // A buffered-partial request body over the gateway's limit sends only its
                // prefix, with no end of stream.
                events.extend(self.finish_request(now));
                let (answer, mode) = self.response_headers(&h);
                return (
                    ProcessingResponse {
                        response: Some(Answer::ResponseHeaders(answer)),
                        mode_override: mode,
                        ..Default::default()
                    },
                    events,
                );
            }
            Some(Phase::ResponseBody(b)) => {
                let full_duplex = self.response_mode == BodySendMode::FullDuplexStreamed as i32;
                if self.recorded {
                    capture(&mut self.response_body, &mut self.response_truncated, &b.body);
                    if b.end_of_stream {
                        events.extend(self.finish(now));
                    }
                }
                Answer::ResponseBody(body_answer(full_duplex, b.body, b.end_of_stream))
            }
            Some(Phase::RequestTrailers(_)) => Answer::RequestTrailers(TrailersResponse::default()),
            Some(Phase::ResponseTrailers(_)) => {
                events.extend(self.finish(now));
                Answer::ResponseTrailers(TrailersResponse::default())
            }
            None => Answer::RequestHeaders(HeadersResponse::default()),
        };
        (
            ProcessingResponse {
                response: Some(answer),
                ..Default::default()
            },
            events,
        )
    }

    /// Records whatever the exchange still owes: the gateway closed the stream, or the
    /// response never finished.
    pub fn finish(&mut self, now: SystemTime) -> Vec<Event> {
        let mut out = self.finish_request(now);
        // A reply is recorded once the receiver answered; a call that failed first has none.
        if self.recorded && self.response_seen && !self.reply_emitted && self.message_emitted && self.replies() {
            self.reply_emitted = true;
            out.push(self.reply_event(now));
        }
        out
    }

    fn request_headers(&mut self, h: &HttpHeaders, now: SystemTime) -> HeadersResponse {
        let headers = header_map(h);
        self.started_at = nanos(now);
        if headers.get(":method").map(String::as_str) != Some("POST") {
            // Agent cards and other reads aren't messages.
            return HeadersResponse::default();
        }
        self.recorded = true;
        self.header_trace = headers
            .get(mcp::HEADER_TRACEPARENT)
            .and_then(|t| mcp::parse_traceparent(t));
        self.header_parents = headers
            .get(mcp::HEADER_PARENTS)
            .map(|p| {
                p.split(',')
                    .map(str::trim)
                    .filter(|s| !s.is_empty() && s.len() <= 128)
                    .take(64)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default();
        let (trace_id, span) = random_ids();
        self.trace_id = self.header_trace.map(|t| t.trace_id).unwrap_or(trace_id);
        self.parent_span = self.header_trace.map(|t| t.span_id);
        self.message_span = span;
        self.message_id = uuid::Uuid::new_v4().to_string();
        // The receiver's next step can link to this message.
        let set = |key: &str, value: String| HeaderValueOption {
            header: Some(HeaderValue {
                key: key.into(),
                raw_value: value.into_bytes(),
                ..Default::default()
            }),
            append_action: HeaderAppendAction::OverwriteIfExistsOrAdd as i32,
            ..Default::default()
        };
        HeadersResponse {
            response: Some(CommonResponse {
                header_mutation: Some(HeaderMutation {
                    set_headers: vec![
                        set(
                            mcp::HEADER_TRACEPARENT,
                            format!(
                                "00-{}-{}-01",
                                hex::encode(self.trace_id),
                                hex::encode(self.message_span)
                            ),
                        ),
                        set(mcp::HEADER_PARENTS, self.message_id.clone()),
                    ],
                    remove_headers: vec![],
                }),
                ..Default::default()
            }),
        }
    }

    fn response_headers(&mut self, h: &HttpHeaders) -> (HeadersResponse, Option<ProcessingMode>) {
        let headers = header_map(h);
        self.response_seen = true;
        let streamed = headers
            .get("content-type")
            .is_some_and(|c| c.starts_with("text/event-stream"));
        if !self.recorded || !streamed {
            return (HeadersResponse::default(), None);
        }
        // Buffering a stream would hold it until it ends, so receive it in full duplex and
        // pass each chunk straight back.
        self.response_streamed = true;
        self.response_mode = BodySendMode::FullDuplexStreamed as i32;
        let mode = ProcessingMode {
            response_body_mode: BodySendMode::FullDuplexStreamed as i32,
            ..Default::default()
        };
        (HeadersResponse::default(), Some(mode))
    }

    fn replies(&self) -> bool {
        self.request
            .as_ref()
            .and_then(|r| r.method.as_deref())
            .is_some_and(a2a::replies)
    }

    fn finish_request(&mut self, now: SystemTime) -> Vec<Event> {
        if !self.recorded || self.request.is_some() {
            return vec![];
        }
        let request = a2a::parse_request(&self.request_body);
        let is_message = request
            .method
            .as_deref()
            .is_some_and(|m| a2a::MESSAGE_METHODS.contains(&m));
        self.request = Some(request);
        if !is_message {
            // tasks/get, GetTask and the like: no message goes out, but the reply still brings
            // content back.
            self.message_emitted = self.replies();
            return vec![];
        }
        self.message_emitted = true;
        vec![self.message_event(now)]
    }

    fn base(&self, id: &str, span_id: [u8; 8], parent_span: Option<[u8; 8]>, start: u64, end: u64) -> Event {
        let mut e = Event {
            trace_id: self.trace_id,
            span_id,
            parent_span_id: parent_span,
            start_time_unix_nano: start,
            end_time_unix_nano: end.max(start),
            ..Default::default()
        };
        e.set("wake.event.id", Value::Str(id.into()));
        e.set("wake.event.kind", Value::Str("agent_message".into()));
        e.set("wake.capture", Value::Str("proxy".into()));
        if let Some(r) = &self.request {
            for (key, value) in [
                ("wake.a2a.method", &r.method),
                ("wake.a2a.message_id", &r.message_id),
                ("wake.a2a.context_id", &r.context_id),
                ("wake.a2a.task_id", &r.task_id),
            ] {
                if let Some(v) = value {
                    e.set(key, Value::Str(v.clone()));
                }
            }
        }
        e
    }

    /// The message from sender to receiver, recorded where the receiver gets it.
    fn message_event(&self, now: SystemTime) -> Event {
        let request = self.request.clone().unwrap_or_default();
        let parents = if self.header_parents.is_empty() {
            request.parents.clone()
        } else {
            self.header_parents.clone()
        };
        let trace_from_metadata = self.header_trace.is_none().then_some(request.trace).flatten();
        let mut e = self.base(
            &self.message_id,
            self.message_span,
            self.parent_span,
            self.started_at,
            nanos(now),
        );
        if let Some(t) = trace_from_metadata {
            e.trace_id = t.trace_id;
            e.parent_span_id = Some(t.span_id);
        }
        e.name = format!("agent_message {} -> {}", self.sender, self.receiver);
        e.set("wake.agent.id", Value::Str(self.receiver.clone()));
        e.set("gen_ai.agent.name", Value::Str(self.receiver.clone()));
        e.set("wake.parents", Value::Strs(parents));
        e.set("wake.origins", Value::Strs(vec![format!("agent:{}", self.sender)]));
        e.set(
            "wake.payload",
            Value::Str(payload(
                &self.sender,
                &self.receiver,
                &self.request_body,
                false,
                self.request_truncated,
            )),
        );
        e
    }

    /// The receiver's reply, recorded where the sender gets it.
    fn reply_event(&self, now: SystemTime) -> Event {
        let (_, span) = random_ids();
        let reply = a2a::parse_reply(&self.response_body, self.response_streamed);
        let request = self.request.as_ref();
        let answers_message = request
            .and_then(|r| r.method.as_deref())
            .is_some_and(|m| a2a::MESSAGE_METHODS.contains(&m));
        if let (true, Some(task), Some(tasks)) = (answers_message, &reply.task_id, &self.tasks) {
            tasks.remember(task, &self.message_id);
        }
        let mut parents = reply.parents;
        if parents.is_empty() {
            if answers_message {
                // Coarse but true: the reply follows from the message it answers.
                parents.push(self.message_id.clone());
            } else if let Some(started_by) = request
                .and_then(|r| r.task_id.as_deref())
                .and_then(|t| self.tasks.as_ref()?.started(t))
            {
                // A task's later status follows from the message that started it.
                parents.push(started_by);
            }
        }
        let mut e = self.base(
            &uuid::Uuid::new_v4().to_string(),
            span,
            Some(self.message_span),
            self.started_at,
            nanos(now),
        );
        e.name = format!("agent_message {} -> {}", self.receiver, self.sender);
        e.set("wake.agent.id", Value::Str(self.sender.clone()));
        e.set("gen_ai.agent.name", Value::Str(self.sender.clone()));
        e.set("wake.parents", Value::Strs(parents));
        e.set("wake.origins", Value::Strs(vec![format!("agent:{}", self.receiver)]));
        e.set(
            "wake.payload",
            Value::Str(payload(
                &self.receiver,
                &self.sender,
                &self.response_body,
                self.response_streamed,
                self.response_truncated,
            )),
        );
        e
    }
}

fn body_answer(full_duplex: bool, body: Vec<u8>, end_of_stream: bool) -> BodyResponse {
    if !full_duplex {
        // Buffered: no mutation means the gateway forwards the body as it was.
        return BodyResponse::default();
    }
    // Full duplex: the gateway forwards only what comes back, so send each chunk back.
    BodyResponse {
        response: Some(CommonResponse {
            body_mutation: Some(BodyMutation {
                mutation: Some(body_mutation::Mutation::StreamedResponse(StreamedBodyResponse {
                    body,
                    end_of_stream,
                })),
            }),
            ..Default::default()
        }),
    }
}

fn capture(into: &mut Vec<u8>, truncated: &mut bool, chunk: &[u8]) {
    let room = MAX_CAPTURE.saturating_sub(into.len());
    if chunk.len() > room {
        *truncated = true;
    }
    into.extend_from_slice(&chunk[..chunk.len().min(room)]);
}

/// The payload of a message: who sent it to whom, and the body exactly as it went over
/// the wire when it's JSON, or as a string when it's a stream or not JSON.
fn payload(from: &str, to: &str, body: &[u8], streamed: bool, truncated: bool) -> String {
    let quote = |s: &str| serde_json::Value::String(s.to_string()).to_string();
    let content = match std::str::from_utf8(body) {
        Ok(s) if !streamed && !truncated && serde_json::from_str::<serde::de::IgnoredAny>(s).is_ok() => {
            s.trim().to_string()
        }
        _ => quote(&String::from_utf8_lossy(body)),
    };
    let mut out = format!(r#"{{"from":{},"to":{},"content":{}"#, quote(from), quote(to), content);
    if streamed {
        out.push_str(r#","streamed":true"#);
    }
    if truncated {
        out.push_str(r#","truncated":true"#);
    }
    out.push('}');
    out
}

fn header_map(h: &HttpHeaders) -> HashMap<String, String> {
    h.headers
        .as_ref()
        .map(|m| {
            m.headers
                .iter()
                .map(|v| {
                    let value = if v.raw_value.is_empty() {
                        v.value.clone()
                    } else {
                        String::from_utf8_lossy(&v.raw_value).into_owned()
                    };
                    (v.key.to_ascii_lowercase(), value)
                })
                .collect()
        })
        .unwrap_or_default()
}

fn attributes(request: &ProcessingRequest) -> HashMap<String, String> {
    request
        .attributes
        .get(ATTRIBUTES)
        .map(|s| {
            s.fields
                .iter()
                .filter_map(|(k, v)| match &v.kind {
                    Some(Kind::StringValue(s)) => Some((k.clone(), s.clone())),
                    _ => None,
                })
                .collect()
        })
        .unwrap_or_default()
}

fn nanos(t: SystemTime) -> u64 {
    t.duration_since(UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(0)
}

fn random_ids() -> ([u8; 16], [u8; 8]) {
    let mut span = [0u8; 8];
    span.copy_from_slice(&uuid::Uuid::new_v4().as_bytes()[..8]);
    (*uuid::Uuid::new_v4().as_bytes(), span)
}

/// The gRPC service. Each stream runs its exchange in its own task.
pub struct Processor {
    events: mpsc::Sender<Event>,
    counters: Arc<Counters>,
    tasks: Arc<a2a::Tasks>,
}

/// How many tasks the processor remembers the starting message of.
pub const TASKS_REMEMBERED: usize = 10_000;

impl Processor {
    pub fn new(events: mpsc::Sender<Event>, counters: Arc<Counters>) -> Self {
        Processor {
            events,
            counters,
            tasks: Arc::new(a2a::Tasks::new(TASKS_REMEMBERED)),
        }
    }

    pub fn server(self) -> pb::ext_proc::external_processor_server::ExternalProcessorServer<Self> {
        pb::ext_proc::external_processor_server::ExternalProcessorServer::new(self)
            .max_decoding_message_size(crate::extmcp::MAX_MESSAGE)
    }
}

#[tonic::async_trait]
impl ExternalProcessor for Processor {
    type ProcessStream = ReceiverStream<Result<ProcessingResponse, Status>>;

    async fn process(
        &self,
        request: Request<Streaming<ProcessingRequest>>,
    ) -> Result<Response<Self::ProcessStream>, Status> {
        let mut incoming = request.into_inner();
        let (tx, rx) = mpsc::channel(16);
        let events = self.events.clone();
        let counters = self.counters.clone();
        let tasks = self.tasks.clone();
        tokio::spawn(async move {
            let mut exchange = Exchange::with_tasks(tasks);
            let emit = |list: Vec<Event>| {
                for e in list {
                    if events.try_send(e).is_err() {
                        counters.export_dropped.fetch_add(1, Ordering::Relaxed);
                    }
                }
            };
            while let Some(message) = incoming.next().await {
                let Ok(message) = message else { break };
                let (answer, done) = exchange.handle(message, SystemTime::now());
                // Answer first: the gateway is waiting on it.
                if tx.send(Ok(answer)).await.is_err() {
                    break;
                }
                emit(done);
            }
            emit(exchange.finish(SystemTime::now()));
        });
        Ok(Response::new(ReceiverStream::new(rx)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pb::ext_proc::{HeaderMap, HttpBody, ProtocolConfiguration};
    use std::time::Duration;

    fn t(ms: u64) -> SystemTime {
        UNIX_EPOCH + Duration::from_millis(1_761_552_000_000 + ms)
    }

    fn headers(pairs: &[(&str, &str)]) -> HttpHeaders {
        HttpHeaders {
            headers: Some(HeaderMap {
                headers: pairs
                    .iter()
                    .map(|(k, v)| HeaderValue {
                        key: k.to_string(),
                        raw_value: v.as_bytes().to_vec(),
                        ..Default::default()
                    })
                    .collect(),
            }),
            ..Default::default()
        }
    }

    fn first(pairs: &[(&str, &str)]) -> ProcessingRequest {
        let fields = [
            (ATTR_SENDER, "spiffe://wake.local/agent/planner"),
            (ATTR_RECEIVER, "research"),
        ]
        .into_iter()
        .map(|(k, v)| {
            (
                k.to_string(),
                prost_types::Value {
                    kind: Some(Kind::StringValue(v.into())),
                },
            )
        })
        .collect();
        ProcessingRequest {
            request: Some(Phase::RequestHeaders(headers(pairs))),
            attributes: [(ATTRIBUTES.to_string(), prost_types::Struct { fields })].into(),
            protocol_config: Some(ProtocolConfiguration {
                request_body_mode: BodySendMode::BufferedPartial as i32,
                response_body_mode: BodySendMode::BufferedPartial as i32,
                ..Default::default()
            }),
            ..Default::default()
        }
    }

    fn phase(p: Phase) -> ProcessingRequest {
        ProcessingRequest {
            request: Some(p),
            ..Default::default()
        }
    }

    fn body(b: &[u8], end: bool) -> HttpBody {
        HttpBody {
            body: b.to_vec(),
            end_of_stream: end,
        }
    }

    const SEND: &[u8] = br#"{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{"role":"user","messageId":"m1","contextId":"c1","parts":[{"kind":"text","text":"Is Acme legit?"}]}}}"#;
    const REPLY: &[u8] = br#"{"jsonrpc":"2.0","id":1,"result":{"kind":"message","messageId":"r1","parts":[{"kind":"text","text":"Acme is unverified."}]}}"#;

    fn s<'a>(e: &'a Event, k: &str) -> &'a str {
        e.str(k).unwrap_or_else(|| panic!("{k}"))
    }

    fn set_headers(r: &ProcessingResponse) -> HashMap<String, String> {
        let Some(Answer::RequestHeaders(h)) = &r.response else {
            panic!("{r:?}")
        };
        h.response
            .as_ref()
            .and_then(|c| c.header_mutation.as_ref())
            .map(|m| {
                m.set_headers
                    .iter()
                    .filter_map(|o| o.header.as_ref())
                    .map(|h| (h.key.clone(), String::from_utf8(h.raw_value.clone()).unwrap()))
                    .collect()
            })
            .unwrap_or_default()
    }

    #[test]
    fn records_a_message_and_its_reply_and_links_the_receiver() {
        let mut x = Exchange::default();
        let (answer, events) = x.handle(
            first(&[
                (":method", "POST"),
                ("traceparent", "00-0102030405060708090a0b0c0d0e0f10-1112131415161718-01"),
                ("wake-parents", "planner-step-3"),
            ]),
            t(0),
        );
        assert!(events.is_empty());
        let injected = set_headers(&answer);

        let (answer, events) = x.handle(phase(Phase::RequestBody(body(SEND, true))), t(1));
        // Buffered: no mutation, so the gateway forwards the body as it was.
        assert!(matches!(
            answer.response,
            Some(Answer::RequestBody(BodyResponse { response: None }))
        ));
        let [message] = events.as_slice() else {
            panic!("{events:?}")
        };
        assert_eq!(s(message, "wake.event.kind"), "agent_message");
        assert_eq!(s(message, "wake.capture"), "proxy");
        assert_eq!(s(message, "wake.agent.id"), "research");
        assert_eq!(
            message.attributes["wake.origins"],
            Value::Strs(vec!["agent:spiffe://wake.local/agent/planner".into()])
        );
        assert_eq!(
            message.attributes["wake.parents"],
            Value::Strs(vec!["planner-step-3".into()])
        );
        assert_eq!(s(message, "wake.a2a.method"), "message/send");
        assert_eq!(s(message, "wake.a2a.message_id"), "m1");
        assert_eq!(hex::encode(message.trace_id), "0102030405060708090a0b0c0d0e0f10");
        assert_eq!(
            message.parent_span_id.map(hex::encode).as_deref(),
            Some("1112131415161718")
        );
        let payload: serde_json::Value = serde_json::from_str(s(message, "wake.payload")).unwrap();
        assert_eq!(
            payload["content"]["params"]["message"]["parts"][0]["text"],
            "Is Acme legit?"
        );

        // The receiver is told which event its next step follows from.
        assert_eq!(injected["wake-parents"], s(message, "wake.event.id"));
        assert_eq!(
            injected["traceparent"],
            format!(
                "00-0102030405060708090a0b0c0d0e0f10-{}-01",
                hex::encode(message.span_id)
            )
        );

        let (answer, _) = x.handle(
            phase(Phase::ResponseHeaders(headers(&[
                (":status", "200"),
                ("content-type", "application/json"),
            ]))),
            t(2),
        );
        assert!(answer.mode_override.is_none());
        let (_, events) = x.handle(phase(Phase::ResponseBody(body(REPLY, true))), t(3));
        let [reply] = events.as_slice() else {
            panic!("{events:?}")
        };
        assert_eq!(s(reply, "wake.agent.id"), "spiffe://wake.local/agent/planner");
        assert_eq!(
            reply.attributes["wake.origins"],
            Value::Strs(vec!["agent:research".into()])
        );
        assert_eq!(
            reply.attributes["wake.parents"],
            Value::Strs(vec![s(message, "wake.event.id").into()])
        );
        assert_eq!(reply.trace_id, message.trace_id);
        assert_eq!(reply.parent_span_id, Some(message.span_id));
        assert!(s(reply, "wake.payload").contains("Acme is unverified."));
        assert!(x.finish(t(4)).is_empty());
    }

    #[test]
    fn a_streamed_reply_is_passed_back_chunk_by_chunk() {
        let mut x = Exchange::default();
        x.handle(first(&[(":method", "POST")]), t(0));
        x.handle(
            phase(Phase::RequestBody(body(
                &SEND.to_vec().replace_method("message/stream"),
                true,
            ))),
            t(1),
        );
        let (answer, _) = x.handle(
            phase(Phase::ResponseHeaders(headers(&[
                (":status", "200"),
                ("content-type", "text/event-stream"),
            ]))),
            t(2),
        );
        assert_eq!(
            answer.mode_override.unwrap().response_body_mode,
            BodySendMode::FullDuplexStreamed as i32
        );

        let chunks: [&[u8]; 2] = [b"data: {\"result\":{\"kind\":\"status-update\",\"status\":{\"state\":\"working\"}}}\n\n", b"data: {\"result\":{\"kind\":\"message\",\"parts\":[{\"text\":\"done\"}],\"metadata\":{\"dev.wake/parents\":[\"research-step-9\"]}}}\n\n"];
        let mut events = vec![];
        for (i, chunk) in chunks.iter().enumerate() {
            let end = i == chunks.len() - 1;
            let (answer, done) = x.handle(phase(Phase::ResponseBody(body(chunk, end))), t(3 + i as u64));
            // Full duplex: the gateway forwards only what comes back.
            let Some(Answer::ResponseBody(BodyResponse { response: Some(c) })) = answer.response else {
                panic!()
            };
            let Some(body_mutation::Mutation::StreamedResponse(sent)) = c.body_mutation.unwrap().mutation else {
                panic!()
            };
            assert_eq!((sent.body.as_slice(), sent.end_of_stream), (*chunk, end));
            events.extend(done);
        }
        let [reply] = events.as_slice() else {
            panic!("{events:?}")
        };
        // The receiver's SDK named the step behind its reply.
        assert_eq!(
            reply.attributes["wake.parents"],
            Value::Strs(vec!["research-step-9".into()])
        );
        let payload: serde_json::Value = serde_json::from_str(s(reply, "wake.payload")).unwrap();
        assert_eq!(payload["streamed"], true);
        assert!(payload["content"].as_str().unwrap().contains("done"));
    }

    trait ReplaceMethod {
        fn replace_method(self, m: &str) -> Vec<u8>;
    }
    impl ReplaceMethod for Vec<u8> {
        fn replace_method(self, m: &str) -> Vec<u8> {
            String::from_utf8(self).unwrap().replace("message/send", m).into_bytes()
        }
    }

    /// One buffered exchange: a POST with this body, answered with this JSON.
    fn exchange(x: &mut Exchange, request: &[u8], reply: &[u8]) -> Vec<Event> {
        let mut out = Vec::new();
        out.extend(x.handle(first(&[(":method", "POST")]), t(0)).1);
        out.extend(x.handle(phase(Phase::RequestBody(body(request, true))), t(1)).1);
        out.extend(
            x.handle(
                phase(Phase::ResponseHeaders(headers(&[("content-type", "application/json")]))),
                t(2),
            )
            .1,
        );
        out.extend(x.handle(phase(Phase::ResponseBody(body(reply, true))), t(3)).1);
        out.extend(x.finish(t(4)));
        out
    }

    #[test]
    fn records_version_1_messages_and_replies() {
        let send = br#"{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"Is Acme legit?"}]}}}"#;
        let reply = br#"{"jsonrpc":"2.0","id":"1","result":{"message":{"messageId":"r1","role":"ROLE_AGENT","parts":[{"text":"No."}],"metadata":{"dev.wake/parents":["research-step-2"]}}}}"#;
        let events = exchange(&mut Exchange::default(), send, reply);
        let [message, answer] = events.as_slice() else {
            panic!("{events:?}")
        };
        assert_eq!(s(message, "wake.a2a.method"), "SendMessage");
        assert_eq!(s(message, "wake.a2a.message_id"), "m1");
        assert_eq!(s(answer, "wake.agent.id"), "spiffe://wake.local/agent/planner");
        assert_eq!(
            answer.attributes["wake.parents"],
            Value::Strs(vec!["research-step-2".into()])
        );
    }

    #[test]
    fn a_task_read_links_to_the_message_that_started_the_task() {
        let tasks = Arc::new(a2a::Tasks::new(16));
        let send = br#"{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{"messageId":"m1","parts":[{"text":"Research Acme"}]}}}"#;
        let started =
            br#"{"jsonrpc":"2.0","id":"1","result":{"task":{"id":"t1","status":{"state":"TASK_STATE_WORKING"}}}}"#;
        let events = exchange(&mut Exchange::with_tasks(tasks.clone()), send, started);
        let message_id = s(&events[0], "wake.event.id").to_string();

        // Later, in another exchange: the task's result.
        let get = br#"{"jsonrpc":"2.0","id":"2","method":"GetTask","params":{"id":"t1"}}"#;
        let done = br#"{"jsonrpc":"2.0","id":"2","result":{"id":"t1","status":{"state":"TASK_STATE_COMPLETED"},"artifacts":[{"parts":[{"text":"Acme is unverified."}]}]}}"#;
        let events = exchange(&mut Exchange::with_tasks(tasks.clone()), get, done);
        let [reply] = events.as_slice() else {
            panic!("{events:?}")
        };
        assert_eq!(s(reply, "wake.a2a.task_id"), "t1");
        assert_eq!(reply.attributes["wake.parents"], Value::Strs(vec![message_id]));

        // The same in v0.3, and a task nobody saw start has no parent.
        let send =
            br#"{"jsonrpc":"2.0","id":3,"method":"message/send","params":{"message":{"messageId":"m2","parts":[]}}}"#;
        let events = exchange(
            &mut Exchange::with_tasks(tasks.clone()),
            send,
            br#"{"result":{"kind":"task","id":"t2"}}"#,
        );
        let message_id = s(&events[0], "wake.event.id").to_string();
        let get = br#"{"jsonrpc":"2.0","id":4,"method":"tasks/get","params":{"id":"t2"}}"#;
        let events = exchange(
            &mut Exchange::with_tasks(tasks.clone()),
            get,
            br#"{"result":{"kind":"task","id":"t2"}}"#,
        );
        assert_eq!(events[0].attributes["wake.parents"], Value::Strs(vec![message_id]));
        let get = br#"{"jsonrpc":"2.0","id":5,"method":"GetTask","params":{"id":"t-unknown"}}"#;
        let events = exchange(
            &mut Exchange::with_tasks(tasks),
            get,
            br#"{"result":{"id":"t-unknown"}}"#,
        );
        assert_eq!(events[0].attributes["wake.parents"], Value::Strs(vec![]));
    }

    #[test]
    fn agent_cards_and_other_reads_are_left_alone() {
        let mut x = Exchange::default();
        let (answer, events) = x.handle(
            first(&[(":method", "GET"), (":path", "/.well-known/agent-card.json")]),
            t(0),
        );
        assert!(events.is_empty() && set_headers(&answer).is_empty());
        x.handle(phase(Phase::ResponseHeaders(headers(&[(":status", "200")]))), t(1));
        let (_, events) = x.handle(phase(Phase::ResponseBody(body(b"{}", true))), t(2));
        assert!(events.is_empty() && x.finish(t(3)).is_empty());
    }

    #[test]
    fn a_failed_call_records_the_message_but_no_reply() {
        let mut x = Exchange::default();
        x.handle(first(&[(":method", "POST")]), t(0));
        let (_, events) = x.handle(phase(Phase::RequestBody(body(SEND, true))), t(1));
        assert_eq!(events.len(), 1);
        // The gateway closes the stream without a response.
        assert!(x.finish(t(2)).is_empty());
    }

    #[test]
    fn a_task_read_records_the_reply_and_an_oversized_body_is_marked() {
        let mut x = Exchange::default();
        x.handle(first(&[(":method", "POST")]), t(0));
        let (_, events) = x.handle(
            phase(Phase::RequestBody(body(
                br#"{"jsonrpc":"2.0","id":2,"method":"tasks/get","params":{"id":"t1"}}"#,
                true,
            ))),
            t(1),
        );
        assert!(events.is_empty(), "no message goes out on tasks/get");
        x.handle(
            phase(Phase::ResponseHeaders(headers(&[("content-type", "application/json")]))),
            t(2),
        );
        let big = vec![b'a'; MAX_CAPTURE + 10];
        let (_, events) = x.handle(phase(Phase::ResponseBody(body(&big, true))), t(3));
        let [reply] = events.as_slice() else {
            panic!("{events:?}")
        };
        let payload: serde_json::Value = serde_json::from_str(s(reply, "wake.payload")).unwrap();
        assert_eq!(payload["truncated"], true);
        assert_eq!(payload["content"].as_str().unwrap().len(), MAX_CAPTURE);
    }

    #[test]
    fn a_body_over_the_gateways_buffer_is_recorded_from_its_prefix() {
        let mut x = Exchange::default();
        x.handle(first(&[(":method", "POST")]), t(0));
        // bufferedPartial over the limit: the prefix, with no end of stream.
        let (_, events) = x.handle(phase(Phase::RequestBody(body(&SEND[..40], false))), t(1));
        assert!(events.is_empty());
        let (_, events) = x.handle(
            phase(Phase::ResponseHeaders(headers(&[("content-type", "application/json")]))),
            t(2),
        );
        assert!(
            events.is_empty(),
            "a prefix that doesn't parse isn't known to be a message"
        );
    }
}
