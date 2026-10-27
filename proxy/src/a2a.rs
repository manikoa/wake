//! Reads the A2A JSON-RPC traffic agentgateway hands the processor. Like everything the
//! processor parses, the bytes come from agents, so nothing here panics or trusts a field
//! to be there or to have the right type. The fuzz targets in fuzz/ cover it.

use serde_json::Value;

use crate::mcp::{TraceParent, parse_traceparent};

/// Methods whose request carries a message from one agent to another.
pub const MESSAGE_METHODS: [&str; 2] = ["message/send", "message/stream"];

/// Methods whose response carries content from the remote agent: its reply, or a task's
/// status messages and artifacts.
pub const REPLY_METHODS: [&str; 4] = ["message/send", "message/stream", "tasks/get", "tasks/resubscribe"];

/// Keys in an A2A message's metadata that link it to the events behind it, the same as in
/// MCP params._meta.
pub const META_TRACEPARENT: &str = "traceparent";
pub const META_PARENTS: &str = "dev.wake/parents";

/// What an A2A request says about itself.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Request {
    pub method: Option<String>,
    pub message_id: Option<String>,
    pub context_id: Option<String>,
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
    let message = v.get("params").and_then(|p| p.get("message"));
    let field = |key: &str| message.and_then(|m| m.get(key)).and_then(Value::as_str).map(str::to_string);
    let metadata = message.and_then(|m| m.get("metadata"));
    Request {
        method: v.get("method").and_then(Value::as_str).map(str::to_string),
        message_id: field("messageId"),
        context_id: field("contextId"),
        task_id: field("taskId"),
        trace: metadata.and_then(|m| m.get(META_TRACEPARENT)).and_then(Value::as_str).and_then(parse_traceparent),
        parents: parents_in(metadata),
    }
}

/// Parents a reply names for itself, from the metadata of the message it carries, set by a
/// remote agent with a Wake SDK. body is a JSON-RPC response, or a text/event-stream of
/// them.
pub fn reply_parents(body: &[u8], streamed: bool) -> Vec<String> {
    let responses: Vec<Value> = if streamed {
        let text = String::from_utf8_lossy(body);
        text.lines()
            .filter_map(|l| l.strip_prefix("data:"))
            .filter_map(|d| serde_json::from_str(d.trim()).ok())
            .collect()
    } else {
        serde_json::from_slice(body).into_iter().collect()
    };
    let mut out = Vec::new();
    for r in responses {
        let Some(result) = r.get("result") else { continue };
        // A Message, a Task whose status carries one, or a status update event.
        for message in [Some(result), result.get("status").and_then(|s| s.get("message"))].into_iter().flatten() {
            for p in parents_in(message.get("metadata")) {
                if !out.contains(&p) {
                    out.push(p);
                }
            }
        }
        if out.len() >= 64 {
            break;
        }
    }
    out.truncate(64);
    out
}

fn parents_in(metadata: Option<&Value>) -> Vec<String> {
    metadata
        .and_then(|m| m.get(META_PARENTS))
        .and_then(Value::as_array)
        .map(|list| {
            list.iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty() && s.len() <= 128)
                .take(64)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
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
        assert_eq!((r.message_id.as_deref(), r.context_id.as_deref(), r.task_id.as_deref()), (Some("m1"), Some("c1"), Some("t1")));
        assert_eq!(r.parents, vec!["e1", "e2"]);
        assert_eq!(r.trace.map(|t| hex::encode(t.trace_id)).as_deref(), Some("0102030405060708090a0b0c0d0e0f10"));
        assert_eq!(parse_request(b"not json"), Request::default());
        assert_eq!(parse_request(br#"{"method":"tasks/get","params":{"id":"t1"}}"#).method.as_deref(), Some("tasks/get"));
    }

    #[test]
    fn reads_a_replys_parents_from_messages_and_streams() {
        let message = br#"{"jsonrpc":"2.0","id":1,"result":{"kind":"message","messageId":"r1","metadata":{"dev.wake/parents":["b1"]}}}"#;
        assert_eq!(reply_parents(message, false), vec!["b1"]);
        let task = br#"{"jsonrpc":"2.0","id":1,"result":{"kind":"task","status":{"state":"completed","message":{"metadata":{"dev.wake/parents":["b2"]}}}}}"#;
        assert_eq!(reply_parents(task, false), vec!["b2"]);
        let stream = b"event: message\ndata: {\"result\":{\"kind\":\"status-update\",\"status\":{\"message\":{\"metadata\":{\"dev.wake/parents\":[\"b3\"]}}}}}\n\ndata: {\"result\":{\"kind\":\"message\",\"metadata\":{\"dev.wake/parents\":[\"b3\",\"b4\"]}}}\n\n";
        assert_eq!(reply_parents(stream, true), vec!["b3", "b4"]);
        assert!(reply_parents(b"{", false).is_empty());
        assert!(reply_parents(b"data: {\ndata: 7\n", true).is_empty());
    }

    proptest! {
        #[test]
        fn never_panics(body in proptest::collection::vec(any::<u8>(), 0..512)) {
            let _ = parse_request(&body);
            let _ = reply_parents(&body, false);
            let _ = reply_parents(&body, true);
        }

        #[test]
        fn never_panics_on_json_shaped_input(s in r#"\{"(method|params|result|message|metadata|status)":(\{.{0,40}\}|"[^"]{0,10}"|\[\]|null)\}"#) {
            let _ = parse_request(s.as_bytes());
            let _ = reply_parents(s.as_bytes(), false);
        }
    }
}
