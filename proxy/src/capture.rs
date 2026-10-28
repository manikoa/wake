//! Turns the request and response hooks agentgateway calls into one Wake event per MCP
//! call.
//!
//! agentgateway calls the processor once before a call reaches the MCP server and once
//! after the result comes back, with no id tying the two together. The request hook hands
//! agentgateway the event's id as metadata, and the gateway config passes it back to the
//! response hook (see agentgateway.yaml). When it doesn't come back, responses are paired
//! with requests in order within the same session, method and server.
//!
//! JSON-RPC errors skip the response hook, so a request with no response within the
//! timeout becomes an event with no output.
//!
//! Params and results are parsed whole, then kept only up to budget::MAX_CAPTURE, and the
//! requests waiting for a response are bounded by bytes as well as by count.

use std::collections::{HashMap, VecDeque};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::budget::MAX_CAPTURE;
use crate::canonical::{Event, Value};
use crate::mcp;

/// A call into the processor, already read off the wire.
#[derive(Clone, Debug)]
pub enum Hook {
    Request {
        event_id: String,
        method: String,
        server: String,
        metadata: HashMap<String, String>,
        params: Option<Vec<u8>>,
        /// The request headers the gateway forwarded.
        headers: Vec<(String, Vec<u8>)>,
        at: SystemTime,
    },
    Response {
        method: String,
        server: String,
        metadata: HashMap<String, String>,
        result: Vec<u8>,
        at: SystemTime,
    },
}

impl Hook {
    /// About how many bytes the hook holds.
    pub fn size(&self) -> usize {
        let strings = |m: &HashMap<String, String>| m.iter().map(|(k, v)| k.len() + v.len()).sum::<usize>();
        match self {
            Hook::Request {
                metadata,
                params,
                headers,
                ..
            } => {
                256 + strings(metadata)
                    + params.as_ref().map_or(0, Vec::len)
                    + headers.iter().map(|(k, v)| k.len() + v.len()).sum::<usize>()
            }
            Hook::Response { metadata, result, .. } => 256 + strings(metadata) + result.len(),
        }
    }
}

/// Bytes of params the requests waiting for a response may hold, by default.
pub const PENDING_BYTES: usize = 64 << 20;

/// Metadata keys the gateway config fills from CEL.
pub const META_EVENT_ID: &str = "wake_event_id";
pub const META_AGENT: &str = "agent";
pub const META_SESSION: &str = "session";

struct Pending {
    method: String,
    server: String,
    agent: String,
    session: String,
    params: Option<Vec<u8>>,
    params_cut: bool,
    request: mcp::Request,
    at: SystemTime,
}

type Key = (String, String, String); // session, method, server

pub struct Capture {
    /// Responses paired by the event id the gateway passed back, and by order. If only the
    /// second grows, the gateway config isn't passing the id back.
    pub paired_by_id: u64,
    pub paired_in_order: u64,
    timeout: Duration,
    limit: usize,
    max_bytes: usize,
    bytes: usize, // params held by pending requests
    pending: HashMap<String, Pending>,
    order: VecDeque<String>, // event ids, oldest first
    queues: HashMap<Key, VecDeque<String>>,
}

impl Capture {
    /// timeout is how long a request waits for its response; limit bounds how many wait,
    /// and PENDING_BYTES how many bytes of params they hold.
    pub fn new(timeout: Duration, limit: usize) -> Self {
        Capture {
            paired_by_id: 0,
            paired_in_order: 0,
            timeout,
            limit,
            max_bytes: PENDING_BYTES,
            bytes: 0,
            pending: HashMap::new(),
            order: VecDeque::new(),
            queues: HashMap::new(),
        }
    }

    /// Bounds the bytes of params the waiting requests hold.
    pub fn with_max_bytes(mut self, max_bytes: usize) -> Self {
        self.max_bytes = max_bytes;
        self
    }

    pub fn waiting(&self) -> usize {
        self.pending.len()
    }

    /// Bytes of params the waiting requests hold.
    pub fn held(&self) -> usize {
        self.bytes
    }

    /// Takes one hook and returns any events it completes.
    pub fn handle(&mut self, hook: Hook) -> Vec<Event> {
        match hook {
            Hook::Request {
                event_id,
                method,
                server,
                metadata,
                params,
                headers,
                at,
            } => {
                let request = mcp::parse_request(&method, params.as_deref(), &headers);
                let (params, params_cut) = match params.map(|p| mcp::cap(p, MAX_CAPTURE)) {
                    Some((p, cut)) => (Some(p), cut),
                    None => (None, false),
                };
                // Pairing uses the session from metadata, the only place the response hook
                // can carry it too.
                let session = metadata.get(META_SESSION).cloned().unwrap_or_default();
                let agent = metadata
                    .get(META_AGENT)
                    .filter(|a| !a.is_empty())
                    .cloned()
                    .unwrap_or_else(|| "unknown".into());
                self.queues
                    .entry((session.clone(), method.clone(), server.clone()))
                    .or_default()
                    .push_back(event_id.clone());
                self.order.push_back(event_id.clone());
                self.bytes += params.as_ref().map_or(0, Vec::len);
                if let Some(old) = self.pending.insert(
                    event_id,
                    Pending {
                        method,
                        server,
                        agent,
                        session,
                        params,
                        params_cut,
                        request,
                        at,
                    },
                ) {
                    // The same id twice: only the newer one waits.
                    self.bytes = self.bytes.saturating_sub(old.params.as_ref().map_or(0, Vec::len));
                }
                // Never hold more than limit, or more than max_bytes of params: the oldest
                // goes out without its response.
                let mut out = Vec::new();
                while self.pending.len() > self.limit || self.bytes > self.max_bytes {
                    match self.order.pop_front() {
                        Some(id) => out.extend(self.finish(
                            &id,
                            None,
                            at,
                            Some("no response seen before the processor's limit"),
                        )),
                        None => break,
                    }
                }
                out
            }
            Hook::Response {
                method,
                server,
                metadata,
                result,
                at,
            } => {
                let (result, result_cut) = mcp::cap(result, MAX_CAPTURE);
                let id = match metadata.get(META_EVENT_ID).filter(|id| !id.is_empty()) {
                    Some(id) if self.pending.contains_key(id) => {
                        self.paired_by_id += 1;
                        Some(id.clone())
                    }
                    // An id this processor doesn't hold: its request was already recorded
                    // without it, or went through another replica. Pairing it in order
                    // would give some other call this response.
                    Some(_) => None,
                    None => {
                        let session = metadata.get(META_SESSION).cloned().unwrap_or_default();
                        let next = self
                            .queues
                            .get_mut(&(session, method.clone(), server.clone()))
                            .and_then(|q| q.pop_front());
                        if next.is_some() {
                            self.paired_in_order += 1;
                        }
                        next
                    }
                };
                match id {
                    Some(id) => self
                        .finish(&id, Some((&result, result_cut)), at, None)
                        .into_iter()
                        .collect(),
                    // A response with no request seen, such as when only the response
                    // phase is configured. Still recorded.
                    None => vec![orphan(&method, &server, &metadata, (&result, result_cut), at)],
                }
            }
        }
    }

    /// Emits requests that have waited longer than the timeout.
    pub fn expire(&mut self, now: SystemTime) -> Vec<Event> {
        let mut out = Vec::new();
        while let Some(id) = self.order.front().cloned() {
            match self.pending.get(&id) {
                None => {
                    self.order.pop_front();
                }
                Some(p) if now.duration_since(p.at).unwrap_or_default() >= self.timeout => {
                    self.order.pop_front();
                    out.extend(self.finish(
                        &id,
                        None,
                        now,
                        Some("no response seen: the call failed or the response phase isn't configured"),
                    ));
                }
                Some(_) => break,
            }
        }
        out
    }

    fn finish(
        &mut self,
        id: &str,
        result: Option<(&[u8], bool)>,
        at: SystemTime,
        error: Option<&str>,
    ) -> Option<Event> {
        let p = self.pending.remove(id)?;
        self.bytes = self.bytes.saturating_sub(p.params.as_ref().map_or(0, Vec::len));
        if let Some(q) = self
            .queues
            .get_mut(&(p.session.clone(), p.method.clone(), p.server.clone()))
        {
            q.retain(|x| x != id);
            if q.is_empty() {
                self.queues
                    .remove(&(p.session.clone(), p.method.clone(), p.server.clone()));
            }
        }
        Some(build(id, &p, result, at, error))
    }
}

fn nanos(t: SystemTime) -> u64 {
    t.duration_since(UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(0)
}

fn random_trace() -> ([u8; 16], [u8; 8]) {
    let a = uuid::Uuid::new_v4();
    let b = uuid::Uuid::new_v4();
    let mut span = [0u8; 8];
    span.copy_from_slice(&b.as_bytes()[..8]);
    (*a.as_bytes(), span)
}

fn build(id: &str, p: &Pending, result: Option<(&[u8], bool)>, end: SystemTime, error: Option<&str>) -> Event {
    let (trace_id, span_id) = random_trace();
    let mut event = Event {
        trace_id: p.request.trace.map(|t| t.trace_id).unwrap_or(trace_id),
        span_id,
        parent_span_id: p.request.trace.map(|t| t.span_id),
        start_time_unix_nano: nanos(p.at),
        end_time_unix_nano: nanos(end).max(nanos(p.at)),
        ..Default::default()
    };
    let target = p.request.target.clone().unwrap_or_default();
    event.name = if p.method == "tools/call" {
        format!("execute_tool {target}")
    } else {
        format!("{} {target}", p.method)
    };
    event.set("wake.event.id", Value::Str(id.into()));
    event.set("wake.event.kind", Value::Str("tool_call".into()));
    event.set("wake.agent.id", Value::Str(p.agent.clone()));
    event.set("wake.capture", Value::Str("proxy".into()));
    event.set("wake.parents", Value::Strs(p.request.parents.clone()));
    // What came back is outside content entering the system here.
    let origin = if target.is_empty() {
        format!("tool:{}", p.server)
    } else {
        format!("tool:{target}")
    };
    event.set("wake.origins", Value::Strs(vec![origin]));
    event.set("gen_ai.operation.name", Value::Str("execute_tool".into()));
    if p.method == "tools/call" && !target.is_empty() {
        event.set("gen_ai.tool.name", Value::Str(target));
    }
    event.set("wake.mcp.method", Value::Str(p.method.clone()));
    event.set("wake.mcp.server", Value::Str(p.server.clone()));
    let session = if p.session.is_empty() {
        p.request.session.clone().unwrap_or_default()
    } else {
        p.session.clone()
    };
    if !session.is_empty() {
        event.set("wake.mcp.session", Value::Str(session));
    }
    event.set(
        "wake.payload",
        Value::Str(mcp::capped_payload(
            (p.params.as_deref(), p.params_cut),
            (result.map(|(r, _)| r), result.is_some_and(|(_, cut)| cut)),
            error,
        )),
    );
    event
}

fn orphan(
    method: &str,
    server: &str,
    metadata: &HashMap<String, String>,
    result: (&[u8], bool),
    at: SystemTime,
) -> Event {
    let p = Pending {
        method: method.into(),
        server: server.into(),
        agent: metadata
            .get(META_AGENT)
            .filter(|a| !a.is_empty())
            .cloned()
            .unwrap_or_else(|| "unknown".into()),
        session: metadata.get(META_SESSION).cloned().unwrap_or_default(),
        params: None,
        params_cut: false,
        request: mcp::Request::default(),
        at,
    };
    build(
        &uuid::Uuid::new_v4().to_string(),
        &p,
        Some(result),
        at,
        Some("response with no request seen"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(secs: u64) -> SystemTime {
        UNIX_EPOCH + Duration::from_secs(1_761_552_000 + secs)
    }

    fn meta(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
    }

    fn request(id: &str, session: &str, tool: &str, at: SystemTime) -> Hook {
        Hook::Request {
            event_id: id.into(),
            method: "tools/call".into(),
            server: "inbox".into(),
            metadata: meta(&[(META_AGENT, "spiffe://wake.local/agent/email"), (META_SESSION, session)]),
            params: Some(format!(r#"{{"name":"{tool}","arguments":{{}}}}"#).into_bytes()),
            headers: vec![],
            at,
        }
    }

    fn response(session: &str, event_id: Option<&str>, result: &str, at: SystemTime) -> Hook {
        let mut m = meta(&[(META_SESSION, session)]);
        if let Some(id) = event_id {
            m.insert(META_EVENT_ID.into(), id.into());
        }
        Hook::Response {
            method: "tools/call".into(),
            server: "inbox".into(),
            metadata: m,
            result: result.as_bytes().to_vec(),
            at,
        }
    }

    #[test]
    fn pairs_by_the_event_id_the_gateway_passes_back() {
        let mut c = Capture::new(Duration::from_secs(60), 100);
        assert!(c.handle(request("a", "s1", "read_email", t(0))).is_empty());
        assert!(c.handle(request("b", "s1", "read_email", t(1))).is_empty());
        // b's response arrives first; the id says which call it belongs to.
        let out = c.handle(response("s1", Some("b"), r#"{"content":"second"}"#, t(2)));
        assert_eq!(out.len(), 1);
        let e = &out[0];
        assert_eq!(e.str("wake.event.id"), Some("b"));
        assert_eq!(e.str("wake.capture"), Some("proxy"));
        assert_eq!(e.str("wake.agent.id"), Some("spiffe://wake.local/agent/email"));
        assert_eq!(e.str("gen_ai.tool.name"), Some("read_email"));
        assert_eq!(
            e.attributes["wake.origins"],
            Value::Strs(vec!["tool:read_email".into()])
        );
        assert_eq!(
            e.str("wake.payload"),
            Some(r#"{"input":{"name":"read_email","arguments":{}},"output":{"content":"second"}}"#)
        );
        assert_eq!(
            (e.start_time_unix_nano, e.end_time_unix_nano),
            (nanos(t(1)), nanos(t(2)))
        );
        assert_eq!(c.waiting(), 1);
        assert_eq!((c.paired_by_id, c.paired_in_order), (1, 0));
    }

    #[test]
    fn pairs_in_order_within_a_session_without_the_id() {
        let mut c = Capture::new(Duration::from_secs(60), 100);
        c.handle(request("a", "s1", "read_email", t(0)));
        c.handle(request("x", "s2", "read_email", t(0)));
        c.handle(request("b", "s1", "read_email", t(1)));
        let first = c.handle(response("s1", None, "1", t(2)));
        let second = c.handle(response("s1", None, "2", t(3)));
        assert_eq!(first[0].str("wake.event.id"), Some("a"));
        assert_eq!(second[0].str("wake.event.id"), Some("b"));
        assert_eq!(c.waiting(), 1);
        assert_eq!((c.paired_by_id, c.paired_in_order), (0, 2));
    }

    #[test]
    fn a_request_with_no_response_is_recorded_after_the_timeout() {
        let mut c = Capture::new(Duration::from_secs(60), 100);
        c.handle(request("a", "s1", "wire_money", t(0)));
        assert!(c.expire(t(59)).is_empty());
        let out = c.expire(t(60));
        assert_eq!(out.len(), 1);
        let payload = out[0].str("wake.payload").unwrap();
        assert!(
            payload.contains(r#""output":null"#) && payload.contains("no response seen"),
            "{payload}"
        );
        // A late response then has nothing to pair with and is recorded on its own.
        let late = c.handle(response("s1", Some("a"), "{}", t(61)));
        assert_eq!(late.len(), 1);
        assert_ne!(late[0].str("wake.event.id"), Some("a"));
        assert_eq!(c.waiting(), 0);
    }

    #[test]
    fn waiting_requests_are_bounded() {
        let mut c = Capture::new(Duration::from_secs(60), 2);
        c.handle(request("a", "s1", "t", t(0)));
        c.handle(request("b", "s1", "t", t(1)));
        let out = c.handle(request("c", "s1", "t", t(2)));
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].str("wake.event.id"), Some("a"));
        assert_eq!(c.waiting(), 2);
        // The pairing queue forgot a, so the next response pairs with b.
        assert_eq!(
            c.handle(response("s1", None, "{}", t(3)))[0].str("wake.event.id"),
            Some("b")
        );
    }

    #[test]
    fn a_response_naming_an_id_it_doesnt_hold_isnt_paired_in_order() {
        let mut c = Capture::new(Duration::from_secs(60), 100);
        c.handle(request("a", "s1", "read_email", t(0)));
        // An id from another replica, or from a request already recorded without it.
        let out = c.handle(response("s1", Some("elsewhere"), "{}", t(1)));
        assert_eq!(out.len(), 1);
        assert_ne!(out[0].str("wake.event.id"), Some("a"));
        assert!(
            out[0]
                .str("wake.payload")
                .unwrap()
                .contains("response with no request seen")
        );
        assert_eq!(c.waiting(), 1, "a still waits for its own response");
        assert_eq!((c.paired_by_id, c.paired_in_order), (0, 0));
        // An empty id is no id: paired in order.
        let out = c.handle(response("s1", Some(""), "{}", t(2)));
        assert_eq!(out[0].str("wake.event.id"), Some("a"));
        assert_eq!(c.paired_in_order, 1);
    }

    #[test]
    fn params_and_results_are_kept_up_to_the_cap() {
        let mut c = Capture::new(Duration::from_secs(60), 100);
        let big = format!(
            r#"{{"name":"upload","arguments":{{"blob":"{}"}}}}"#,
            "x".repeat(MAX_CAPTURE)
        );
        c.handle(Hook::Request {
            event_id: "a".into(),
            method: "tools/call".into(),
            server: "files".into(),
            metadata: HashMap::new(),
            params: Some(big.into_bytes()),
            headers: vec![],
            at: t(0),
        });
        assert_eq!(c.held(), MAX_CAPTURE);
        let out = c.handle(response("", Some("a"), &"y".repeat(MAX_CAPTURE + 1), t(1)));
        let e = &out[0];
        // Parsed whole, so the tool's name survives the cut.
        assert_eq!(e.str("gen_ai.tool.name"), Some("upload"));
        let payload: serde_json::Value = serde_json::from_str(e.str("wake.payload").unwrap()).unwrap();
        assert_eq!(payload["truncated"], true);
        assert_eq!(payload["input"].as_str().unwrap().len(), MAX_CAPTURE);
        assert_eq!(payload["output"].as_str().unwrap().len(), MAX_CAPTURE);
        assert_eq!(c.held(), 0);
    }

    #[test]
    fn waiting_requests_are_bounded_by_bytes() {
        let mut c = Capture::new(Duration::from_secs(60), 100).with_max_bytes(100);
        let hook = |id: &str, size: usize| Hook::Request {
            event_id: id.into(),
            method: "tools/call".into(),
            server: "s".into(),
            metadata: HashMap::new(),
            params: Some(vec![b'1'; size]),
            headers: vec![],
            at: t(0),
        };
        assert!(c.handle(hook("a", 60)).is_empty());
        let out = c.handle(hook("b", 60));
        assert_eq!(out.len(), 1, "a goes out without its response");
        assert_eq!(out[0].str("wake.event.id"), Some("a"));
        assert_eq!((c.waiting(), c.held()), (1, 60));
    }

    #[test]
    fn joins_the_callers_trace_and_parents() {
        let mut c = Capture::new(Duration::from_secs(60), 100);
        c.handle(Hook::Request {
            event_id: "a".into(),
            method: "resources/read".into(),
            server: "files".into(),
            metadata: HashMap::new(),
            params: Some(br#"{"uri":"file:///inbox/1.eml","_meta":{"traceparent":"00-0102030405060708090a0b0c0d0e0f10-1112131415161718-01","dev.wake/parents":["model-call-1"]}}"#.to_vec()),
            headers: vec![("mcp-session-id".into(), b"s9".to_vec())],
            at: t(0),
        });
        let out = c.handle(Hook::Response {
            method: "resources/read".into(),
            server: "files".into(),
            metadata: meta(&[(META_EVENT_ID, "a")]),
            result: b"{}".to_vec(),
            at: t(1),
        });
        let e = &out[0];
        assert_eq!(hex::encode(e.trace_id), "0102030405060708090a0b0c0d0e0f10");
        assert_eq!(e.parent_span_id.map(hex::encode).as_deref(), Some("1112131415161718"));
        assert_eq!(e.attributes["wake.parents"], Value::Strs(vec!["model-call-1".into()]));
        assert_eq!(
            e.attributes["wake.origins"],
            Value::Strs(vec!["tool:file:///inbox/1.eml".into()])
        );
        assert_eq!(e.str("wake.agent.id"), Some("unknown"));
        assert_eq!(e.str("wake.mcp.session"), Some("s9"));
        assert_eq!(e.str("gen_ai.tool.name"), None);
    }
}
