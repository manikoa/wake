//! Reads the A2A JSON-RPC traffic agentgateway hands the processor. Like everything the
//! processor parses, the bytes come from agents, so nothing here panics or trusts a field
//! to be there or to have the right type. The fuzz targets in fuzz/ cover it.
//!
//! Both protocol versions in use are read: v0.3 (message/send, a bare Message or Task as
//! the result) and v1.0 (SendMessage, the result wrapping a message, task or update).

use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;

use serde_json::Value;

use crate::mcp::{TraceParent, parse_traceparent};

/// Methods whose request carries a message from one agent to another.
pub const MESSAGE_METHODS: [&str; 4] = ["message/send", "message/stream", "SendMessage", "SendStreamingMessage"];

/// Methods that ask about a task an earlier message started.
pub const TASK_METHODS: [&str; 4] = ["tasks/get", "tasks/resubscribe", "GetTask", "SubscribeToTask"];

/// Keys in an A2A message's metadata that link it to the events behind it, the same as in
/// MCP params._meta.
pub const META_TRACEPARENT: &str = "traceparent";
pub const META_PARENTS: &str = "dev.wake/parents";

/// Whether a method's response carries content from the remote agent: its reply, or a
/// task's status messages and artifacts.
pub fn replies(method: &str) -> bool {
    MESSAGE_METHODS.contains(&method) || TASK_METHODS.contains(&method)
}

/// What an A2A request says about itself.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Request {
    pub method: Option<String>,
    pub message_id: Option<String>,
    pub context_id: Option<String>,
    /// The message's task, or for a task method, the task it asks about.
    pub task_id: Option<String>,
    /// From params.message.metadata, set by a sender with a Wake SDK.
    pub trace: Option<TraceParent>,
    pub parents: Vec<String>,
}

/// Reads a JSON-RPC request. Anything malformed is treated as absent.
pub fn parse_request(body: &[u8]) -> Request {
    let Ok(v) = serde_json::from_slice::<Value>(body) else {
        return Request::default();
    };
    let method = v.get("method").and_then(Value::as_str).map(str::to_string);
    let params = v.get("params");
    let message = params.and_then(|p| p.get("message"));
    let field = |key: &str| {
        message
            .and_then(|m| m.get(key))
            .and_then(Value::as_str)
            .map(str::to_string)
    };
    let asked_about = method
        .as_deref()
        .filter(|m| TASK_METHODS.contains(m))
        .and_then(|_| params?.get("id")?.as_str())
        .map(str::to_string);
    let metadata = message.and_then(|m| m.get("metadata"));
    Request {
        method,
        message_id: field("messageId"),
        context_id: field("contextId"),
        task_id: field("taskId").or(asked_about),
        trace: metadata
            .and_then(|m| m.get(META_TRACEPARENT))
            .and_then(Value::as_str)
            .and_then(parse_traceparent),
        parents: parents_in(metadata),
    }
}

/// What a reply says about itself: the parents it names, from the metadata of the messages
/// it carries (set by a remote agent with a Wake SDK), and the task it's about.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Reply {
    pub parents: Vec<String>,
    pub task_id: Option<String>,
    /// The last task state it gives, as task_state spells it.
    pub state: Option<&'static str>,
    /// Whether any response in it is a JSON-RPC error.
    pub error: bool,
    /// Whether any response in it has a result.
    pub answered: bool,
}

/// The task states A2A defines, as `wake.a2a.task_state` records them: v0.3's spelling,
/// which v1.0's `TASK_STATE_INPUT_REQUIRED` and the like map onto.
pub const TASK_STATES: [&str; 9] = [
    "submitted",
    "working",
    "input-required",
    "completed",
    "canceled",
    "failed",
    "rejected",
    "auth-required",
    "unknown",
];

/// A task state as a reply spells it, in v0.3's spelling. None for anything A2A doesn't
/// define.
pub fn task_state(raw: &str) -> Option<&'static str> {
    let lower = raw.trim().to_ascii_lowercase();
    let state = lower.strip_prefix("task_state_").unwrap_or(&lower).replace('_', "-");
    let state = match state.as_str() {
        "cancelled" => "canceled",
        "unspecified" => "unknown",
        s => s,
    };
    TASK_STATES.into_iter().find(|s| *s == state)
}

impl Reply {
    /// Whether the call went well, as `wake.call.ok` records it: the receiver answered with
    /// a result, no JSON-RPC error, and no task that failed, was rejected or was canceled,
    /// with a 2xx. None when nothing in it could be read, as when it was cut short.
    pub fn ok(&self, status: Option<u16>, cut: bool) -> Option<bool> {
        let failed = matches!(self.state, Some("failed" | "rejected" | "canceled"));
        if self.error || failed || status.is_some_and(|s| !(200..300).contains(&s)) {
            return Some(false);
        }
        (self.answered && !cut).then_some(true)
    }
}

/// Reads a reply. body is a JSON-RPC response, or a text/event-stream of them.
pub fn parse_reply(body: &[u8], streamed: bool) -> Reply {
    let responses: Vec<Value> = if streamed {
        let text = String::from_utf8_lossy(body);
        text.lines()
            .filter_map(|l| l.strip_prefix("data:"))
            .filter_map(|d| serde_json::from_str(d.trim()).ok())
            .collect()
    } else {
        serde_json::from_slice(body).into_iter().collect()
    };
    let mut out = Reply::default();
    for r in responses {
        if r.get("error").is_some() {
            out.error = true;
        }
        let Some(result) = r.get("result") else { continue };
        out.answered = true;
        // v1.0 wraps the payload in a field naming its kind; v0.3 returns it bare. Each
        // comes with whether it is a Task, whose own id is the task's.
        let bare_task = result.get("kind").and_then(Value::as_str) == Some("task");
        let payloads = [
            (Some(result), bare_task),
            (result.get("message"), false),
            (result.get("task"), true),
            (result.get("statusUpdate"), false),
            (result.get("artifactUpdate"), false),
        ];
        for (payload, is_task) in payloads {
            let Some(payload) = payload else { continue };
            if out.task_id.is_none() {
                out.task_id = task_id_of(payload, is_task);
            }
            // The latest state wins: a stream's updates come in order.
            if let Some(state) = payload
                .get("status")
                .and_then(|s| s.get("state"))
                .and_then(Value::as_str)
                .and_then(task_state)
            {
                out.state = Some(state);
            }
            // A message, or a task or status update whose status carries one.
            let messages = [Some(payload), payload.get("status").and_then(|s| s.get("message"))];
            for message in messages.into_iter().flatten() {
                for p in parents_in(message.get("metadata")) {
                    if !out.parents.contains(&p) {
                        out.parents.push(p);
                    }
                }
            }
        }
        if out.parents.len() >= crate::mcp::MAX_PARENTS {
            break;
        }
    }
    out.parents.truncate(crate::mcp::MAX_PARENTS);
    out
}

/// A Task's own id, or the task an update or message belongs to.
fn task_id_of(payload: &Value, is_task: bool) -> Option<String> {
    let id = if is_task {
        payload.get("id")
    } else {
        payload.get("taskId")
    };
    id.and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty() && s.len() <= 256)
        .map(str::to_string)
}

fn parents_in(metadata: Option<&Value>) -> Vec<String> {
    metadata
        .and_then(|m| m.get(META_PARENTS))
        .and_then(Value::as_array)
        .map(|list| crate::mcp::event_ids(list.iter().filter_map(Value::as_str)))
        .unwrap_or_default()
}

/// Which message started each task, so a later tasks/get reply links back to it. Shared by
/// every exchange, bounded, oldest forgotten first. A task id is only unique at the agent
/// that made it, so each is kept under the agent the message went to: another agent's
/// task with the same id doesn't link to it.
pub struct Tasks {
    inner: Mutex<(HashMap<TaskKey, String>, VecDeque<TaskKey>)>,
    limit: usize,
}

/// The agent a task was made by, and its id there.
type TaskKey = (String, String);

impl Tasks {
    pub fn new(limit: usize) -> Self {
        Tasks {
            inner: Mutex::new((HashMap::new(), VecDeque::new())),
            limit,
        }
    }

    pub fn remember(&self, receiver: &str, task_id: &str, message_event: &str) {
        let Ok(mut guard) = self.inner.lock() else { return };
        let (map, order) = &mut *guard;
        let key = (receiver.to_string(), task_id.to_string());
        if map.insert(key.clone(), message_event.to_string()).is_none() {
            order.push_back(key);
        }
        while order.len() > self.limit {
            if let Some(old) = order.pop_front() {
                map.remove(&old);
            }
        }
    }

    pub fn started(&self, receiver: &str, task_id: &str) -> Option<String> {
        self.inner
            .lock()
            .ok()?
            .0
            .get(&(receiver.to_string(), task_id.to_string()))
            .cloned()
    }
}

#[cfg(test)]
mod outcome_tests {
    use super::*;

    #[test]
    fn task_states_read_in_either_version() {
        for (raw, state) in [
            ("completed", Some("completed")),
            ("TASK_STATE_COMPLETED", Some("completed")),
            ("TASK_STATE_INPUT_REQUIRED", Some("input-required")),
            ("input-required", Some("input-required")),
            ("TASK_STATE_AUTH_REQUIRED", Some("auth-required")),
            ("TASK_STATE_CANCELLED", Some("canceled")),
            ("TASK_STATE_UNSPECIFIED", Some("unknown")),
            (" Failed ", Some("failed")),
            ("done", None),
            ("", None),
        ] {
            assert_eq!(task_state(raw), state, "{raw}");
        }
    }

    #[test]
    fn a_reply_says_how_the_call_went() {
        let completed = parse_reply(
            br#"{"jsonrpc":"2.0","id":1,"result":{"kind":"task","id":"t1","status":{"state":"completed"}}}"#,
            false,
        );
        assert_eq!(completed.state, Some("completed"));
        assert_eq!(completed.ok(Some(200), false), Some(true));
        // A 5xx, or the body cut short, says otherwise or nothing.
        assert_eq!(completed.ok(Some(502), false), Some(false));
        assert_eq!(completed.ok(Some(200), true), None);

        // A stream's last state wins.
        let stream = b"data: {\"result\":{\"kind\":\"status-update\",\"status\":{\"state\":\"working\"}}}\n\ndata: {\"result\":{\"statusUpdate\":{\"taskId\":\"t2\",\"status\":{\"state\":\"TASK_STATE_FAILED\"}}}}\n\n";
        let failed = parse_reply(stream, true);
        assert_eq!(failed.state, Some("failed"));
        assert_eq!(failed.ok(Some(200), false), Some(false));

        let error = parse_reply(
            br#"{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"Task not found"}}"#,
            false,
        );
        assert!(error.error && !error.answered);
        assert_eq!(error.ok(Some(200), false), Some(false));

        // A plain message: answered, no task.
        let message = parse_reply(br#"{"result":{"kind":"message","parts":[]}}"#, false);
        assert_eq!((message.state, message.ok(None, false)), (None, Some(true)));
        assert_eq!(parse_reply(b"not json", false).ok(Some(200), false), None);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn reads_the_message_and_its_links() {
        let body = br#"{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{
            "role":"user","messageId":"m1","contextId":"c1","taskId":"t1",
            "parts":[{"kind":"text","text":"Is this vendor legit?"}],
            "metadata":{"traceparent":"00-0102030405060708090a0b0c0d0e0f10-1112131415161718-01","dev.wake/parents":["e1"," e2 ",""]}}}}"#;
        let r = parse_request(body);
        assert_eq!(r.method.as_deref(), Some("message/send"));
        assert_eq!(
            (r.message_id.as_deref(), r.context_id.as_deref(), r.task_id.as_deref()),
            (Some("m1"), Some("c1"), Some("t1"))
        );
        assert_eq!(r.parents, vec!["e1", "e2"]);
        assert_eq!(
            r.trace.map(|t| hex::encode(t.trace_id)).as_deref(),
            Some("0102030405060708090a0b0c0d0e0f10")
        );
        assert_eq!(parse_request(b"not json"), Request::default());
        assert_eq!(
            parse_request(br#"{"method":"tasks/get","params":{"id":"t1"}}"#)
                .method
                .as_deref(),
            Some("tasks/get")
        );
    }

    #[test]
    fn reads_a_replys_parents_from_messages_and_streams() {
        let parents = |body: &[u8], streamed| parse_reply(body, streamed).parents;
        let message = br#"{"jsonrpc":"2.0","id":1,"result":{"kind":"message","messageId":"r1","metadata":{"dev.wake/parents":["b1"]}}}"#;
        assert_eq!(parents(message, false), vec!["b1"]);
        let task = br#"{"jsonrpc":"2.0","id":1,"result":{"kind":"task","status":{"state":"completed","message":{"metadata":{"dev.wake/parents":["b2"]}}}}}"#;
        assert_eq!(parents(task, false), vec!["b2"]);
        let stream = b"event: message\ndata: {\"result\":{\"kind\":\"status-update\",\"status\":{\"message\":{\"metadata\":{\"dev.wake/parents\":[\"b3\"]}}}}}\n\ndata: {\"result\":{\"kind\":\"message\",\"metadata\":{\"dev.wake/parents\":[\"b3\",\"b4\"]}}}\n\n";
        assert_eq!(parents(stream, true), vec!["b3", "b4"]);
        assert!(parents(b"{", false).is_empty());
        assert!(parents(b"data: {\ndata: 7\n", true).is_empty());
    }

    #[test]
    fn reads_version_1_requests_and_replies() {
        let send = br#"{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{
            "messageId":"m1","contextId":"c1","role":"ROLE_USER","parts":[{"text":"Is this vendor legit?"}],
            "metadata":{"dev.wake/parents":["e1"]}}}}"#;
        let r = parse_request(send);
        assert!(MESSAGE_METHODS.contains(&r.method.as_deref().unwrap()));
        assert_eq!(
            (r.message_id.as_deref(), r.parents),
            (Some("m1"), vec!["e1".to_string()])
        );

        let message = br#"{"jsonrpc":"2.0","id":"1","result":{"message":{"messageId":"r1","role":"ROLE_AGENT",
            "parts":[{"text":"No."}],"metadata":{"dev.wake/parents":["b1"]}}}}"#;
        assert_eq!(parse_reply(message, false).parents, vec!["b1"]);
        let task = br#"{"jsonrpc":"2.0","id":"1","result":{"task":{"id":"t1","contextId":"c1",
            "status":{"state":"TASK_STATE_COMPLETED","message":{"metadata":{"dev.wake/parents":["b2"]}}}}}}"#;
        assert_eq!(
            parse_reply(task, false),
            Reply {
                parents: vec!["b2".into()],
                task_id: Some("t1".into()),
                state: Some("completed"),
                error: false,
                answered: true,
            }
        );
        let stream = b"data: {\"result\":{\"statusUpdate\":{\"taskId\":\"t2\",\"status\":{\"message\":{\"metadata\":{\"dev.wake/parents\":[\"b3\"]}}}}}}\n\n";
        assert_eq!(
            parse_reply(stream, true),
            Reply {
                parents: vec!["b3".into()],
                task_id: Some("t2".into()),
                state: None,
                error: false,
                answered: true,
            }
        );

        let get = parse_request(br#"{"jsonrpc":"2.0","id":"2","method":"GetTask","params":{"id":"t1"}}"#);
        assert_eq!(get.task_id.as_deref(), Some("t1"));
        assert!(replies("GetTask") && !MESSAGE_METHODS.contains(&"GetTask"));
        let v03 = parse_request(br#"{"method":"tasks/get","params":{"id":"t9"}}"#);
        assert_eq!(v03.task_id.as_deref(), Some("t9"));
        assert_eq!(
            parse_reply(br#"{"result":{"kind":"task","id":"t9"}}"#, false)
                .task_id
                .as_deref(),
            Some("t9")
        );
    }

    #[test]
    fn tasks_forget_the_oldest() {
        let tasks = Tasks::new(2);
        tasks.remember("research", "t1", "m1");
        tasks.remember("research", "t2", "m2");
        tasks.remember("research", "t1", "m1b");
        assert_eq!(tasks.started("research", "t1").as_deref(), Some("m1b"));
        tasks.remember("research", "t3", "m3");
        assert_eq!(tasks.started("research", "t1"), None);
        assert_eq!(tasks.started("research", "t3").as_deref(), Some("m3"));
    }

    #[test]
    fn a_task_id_belongs_to_the_agent_that_made_it() {
        let tasks = Tasks::new(8);
        tasks.remember("research", "1", "m-research");
        assert_eq!(tasks.started("billing", "1"), None, "billing's task 1 is another task");
        tasks.remember("billing", "1", "m-billing");
        assert_eq!(tasks.started("research", "1").as_deref(), Some("m-research"));
        assert_eq!(tasks.started("billing", "1").as_deref(), Some("m-billing"));
    }

    proptest! {
        #[test]
        fn never_panics(body in proptest::collection::vec(any::<u8>(), 0..512)) {
            let _ = parse_request(&body);
            let _ = parse_reply(&body, false);
            let _ = parse_reply(&body, true);
        }

        #[test]
        fn never_panics_on_json_shaped_input(s in r#"\{"(method|params|result|message|task|statusUpdate|metadata|status|id)":(\{.{0,40}\}|"[^"]{0,10}"|\[\]|null)\}"#) {
            let _ = parse_request(s.as_bytes());
            let _ = parse_reply(s.as_bytes(), false);
        }
    }
}
