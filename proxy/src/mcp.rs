//! Reads the MCP calls agentgateway hands the processor. Everything here parses bytes an
//! agent or an MCP server controls, so it never panics and never trusts a field to be
//! there or to have the right type. The fuzz targets in fuzz/ cover it.

use serde_json::Value;

/// The MCP methods the proxy records. Each brings outside content into the system.
pub const CAPTURED: [&str; 3] = ["tools/call", "resources/read", "prompts/get"];

pub fn captured(method: &str) -> bool {
    CAPTURED.contains(&method)
}

/// What a request says about itself.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Request {
    /// The tool, resource or prompt called: params.name, or params.uri for resources/read.
    pub target: Option<String>,
    /// W3C trace context, so the event joins the caller's trace: the traceparent header,
    /// else params._meta.traceparent.
    pub trace: Option<TraceParent>,
    /// Ids of the events that led to this call: the wake-parents header, comma
    /// separated, else params._meta["dev.wake/parents"].
    pub parents: Vec<String>,
    /// The MCP session, from the mcp-session-id header.
    pub session: Option<String>,
}

/// Header names the processor reads. agentgateway v1.6.0 drops params._meta before
/// calling the processor, so through it, a caller links a call with headers.
pub const HEADER_TRACEPARENT: &str = "traceparent";
pub const HEADER_PARENTS: &str = "wake-parents";
pub const HEADER_SESSION: &str = "mcp-session-id";

/// The most parents a link may name (docs/linking.md).
pub const MAX_PARENTS: usize = 64;

/// Reads the event ids a link names, as docs/linking.md says: each trimmed, 1 to 128
/// characters from A-Z a-z 0-9 . _ : -, duplicates dropped, at most MAX_PARENTS.
pub fn event_ids<'a>(ids: impl IntoIterator<Item = &'a str>) -> Vec<String> {
    let valid = |s: &str| {
        (1..=128).contains(&s.len())
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'-'))
    };
    let mut out: Vec<String> = Vec::new();
    for id in ids.into_iter().map(str::trim) {
        if out.len() == MAX_PARENTS {
            break;
        }
        if valid(id) && !out.iter().any(|o| o == id) {
            out.push(id.to_string());
        }
    }
    out
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TraceParent {
    pub trace_id: [u8; 16],
    pub span_id: [u8; 8],
}

/// Reads a request's params and the headers the gateway forwarded. Anything malformed is
/// treated as absent.
pub fn parse_request(method: &str, params: Option<&[u8]>, headers: &[(String, Vec<u8>)]) -> Request {
    let header = |name: &str| {
        headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .and_then(|(_, v)| std::str::from_utf8(v).ok())
    };
    let mut request = parse_params(method, params);
    if let Some(trace) = header(HEADER_TRACEPARENT).and_then(parse_traceparent) {
        request.trace = Some(trace);
    }
    if let Some(parents) = header(HEADER_PARENTS) {
        let parents = event_ids(parents.split(','));
        if !parents.is_empty() {
            request.parents = parents;
        }
    }
    request.session = header(HEADER_SESSION)
        .map(str::trim)
        .filter(|s| !s.is_empty() && s.len() <= 256)
        .map(str::to_string);
    request
}

fn parse_params(method: &str, params: Option<&[u8]>) -> Request {
    let Some(params) = params.and_then(|p| serde_json::from_slice::<Value>(p).ok()) else {
        return Request::default();
    };
    let field = |name: &str| params.get(name).and_then(Value::as_str).map(str::to_string);
    let target = match method {
        "resources/read" => field("uri"),
        _ => field("name"),
    };
    let meta = params.get("_meta");
    let trace = meta
        .and_then(|m| m.get("traceparent"))
        .and_then(Value::as_str)
        .and_then(parse_traceparent);
    let parents = meta
        .and_then(|m| m.get("dev.wake/parents"))
        .and_then(Value::as_array)
        .map(|list| event_ids(list.iter().filter_map(Value::as_str)))
        .unwrap_or_default();
    Request {
        target,
        trace,
        parents,
        session: None,
    }
}

/// Parses a W3C traceparent: version-traceid-spanid-flags, lowercase hex. An all-zero
/// trace or span id is invalid.
pub fn parse_traceparent(s: &str) -> Option<TraceParent> {
    let parts: Vec<&str> = s.split('-').collect();
    // Version 00 has exactly four fields; a later version may add more after them.
    if parts.len() < 4 || (parts[0] == "00" && parts.len() != 4) {
        return None;
    }
    if parts[0].len() != 2 || parts[0] == "ff" || parts[3].len() != 2 {
        return None;
    }
    let lower_hex = |p: &str| p.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
    if !parts[..4].iter().all(|p| lower_hex(p)) {
        return None;
    }
    let mut trace_id = [0u8; 16];
    let mut span_id = [0u8; 8];
    hex::decode_to_slice(parts[1], &mut trace_id).ok()?;
    hex::decode_to_slice(parts[2], &mut span_id).ok()?;
    if trace_id == [0; 16] || span_id == [0; 8] {
        return None;
    }
    Some(TraceParent { trace_id, span_id })
}

/// Whether an MCP call went well, as `wake.call.ok` records it: a result came back that
/// doesn't say `isError`. One with no response seen didn't, since agentgateway skips the
/// response hook on a JSON-RPC error. None when the result was cut short or isn't a JSON
/// object, so nothing can be read from it.
pub fn call_ok(result: Option<(&[u8], bool)>) -> Option<bool> {
    let Some((bytes, cut)) = result else {
        return Some(false);
    };
    if cut {
        return None;
    }
    let v: serde_json::Value = serde_json::from_slice(bytes).ok()?;
    if !v.is_object() {
        return None;
    }
    // The result alone, or a whole JSON-RPC response around it.
    if v.get("error").is_some() && v.get("result").is_none() {
        return Some(false);
    }
    let result = v.get("result").unwrap_or(&v);
    Some(result.get("isError").and_then(serde_json::Value::as_bool) != Some(true))
}

/// The event's payload: what the call sent and what came back, kept byte for byte when
/// they're JSON, and as a string when they aren't.
pub fn payload(input: Option<&[u8]>, output: Option<&[u8]>, error: Option<&str>) -> String {
    capped_payload((input, false), (output, false), error)
}

/// The payload of a call whose params or result may have been cut to a prefix (see
/// budget::MAX_CAPTURE). A prefix goes in as a string, since it may not be JSON, or may be
/// JSON that says something else, and the payload is marked truncated.
pub fn capped_payload(
    (input, input_cut): (Option<&[u8]>, bool),
    (output, output_cut): (Option<&[u8]>, bool),
    error: Option<&str>,
) -> String {
    let mut out = String::from(r#"{"input":"#);
    push_json(&mut out, input, input_cut);
    out.push_str(r#","output":"#);
    push_json(&mut out, output, output_cut);
    if let Some(error) = error {
        out.push_str(r#","error":"#);
        out.push_str(&Value::String(error.to_string()).to_string());
    }
    if input_cut || output_cut {
        out.push_str(r#","truncated":true"#);
    }
    out.push('}');
    out
}

fn push_json(out: &mut String, bytes: Option<&[u8]>, cut: bool) {
    let quoted = |b: &[u8]| Value::String(String::from_utf8_lossy(b).into_owned()).to_string();
    match bytes {
        None => out.push_str("null"),
        Some(b) if cut => out.push_str(&quoted(b)),
        Some(b) => match as_json(b) {
            Some(json) => out.push_str(json),
            None => out.push_str(&quoted(b)),
        },
    }
}

/// At most limit bytes, and whether any were cut off.
pub fn cap(mut bytes: Vec<u8>, limit: usize) -> (Vec<u8>, bool) {
    if bytes.len() <= limit {
        return (bytes, false);
    }
    bytes.truncate(limit);
    bytes.shrink_to_fit();
    (bytes, true)
}

/// How deep JSON may nest to go into a payload as it is. Parsers cap nesting (serde_json
/// at 128), and a payload wraps what it carries at least one level deeper.
const MAX_EMBEDDED_DEPTH: usize = 100;

/// The bytes as they are, trimmed, if they can go into a payload byte for byte: JSON that
/// parses as a value, numbers in range included, nested no deeper than MAX_EMBEDDED_DEPTH.
/// Anything else, such as `1e999`, which is JSON by its grammar but no parser's value,
/// goes in quoted instead, so a payload is always JSON every consumer can read.
pub fn as_json(bytes: &[u8]) -> Option<&str> {
    fn depth(v: &Value) -> usize {
        match v {
            Value::Array(items) => 1 + items.iter().map(depth).max().unwrap_or(0),
            Value::Object(fields) => 1 + fields.values().map(depth).max().unwrap_or(0),
            _ => 0,
        }
    }
    let s = std::str::from_utf8(bytes).ok()?;
    let value: Value = serde_json::from_str(s).ok()?;
    (depth(&value) <= MAX_EMBEDDED_DEPTH).then(|| s.trim())
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn reads_event_ids_as_docs_linking_says() {
        assert_eq!(event_ids("a, b,a,  ,bad id!,c:1".split(',')), vec!["a", "b", "c:1"]);
        let long = "x".repeat(129);
        assert!(event_ids([long.as_str(), "", "é"]).is_empty());
        let many: Vec<String> = (0..500).map(|i| format!("e{i}")).collect();
        assert_eq!(event_ids(many.iter().map(String::as_str)).len(), MAX_PARENTS);
    }

    #[test]
    fn reads_traceparents_as_w3c_says() {
        let ok = "00-0102030405060708090a0b0c0d0e0f10-1112131415161718-01";
        assert!(parse_traceparent(ok).is_some());
        assert!(
            parse_traceparent(&format!("{ok}-extra")).is_none(),
            "version 00 has four fields"
        );
        assert!(parse_traceparent("01-0102030405060708090a0b0c0d0e0f10-1112131415161718-01-future").is_some());
        assert!(parse_traceparent("ff-0102030405060708090a0b0c0d0e0f10-1112131415161718-01").is_none());
        assert!(parse_traceparent(&ok.to_uppercase()).is_none(), "lowercase hex only");
        assert!(parse_traceparent("00-00000000000000000000000000000000-1112131415161718-01").is_none());
    }

    #[test]
    fn reads_the_target_trace_and_parents() {
        let params = br#"{"name":"read_email","arguments":{"subject":"invoice"},
            "_meta":{"traceparent":"00-0102030405060708090a0b0c0d0e0f10-1112131415161718-01",
                     "dev.wake/parents":["e1","",42,"e2"]}}"#;
        let r = parse_request("tools/call", Some(params), &[]);
        assert_eq!(r.target.as_deref(), Some("read_email"));
        assert_eq!(r.parents, vec!["e1", "e2"]);
        let trace = r.trace.unwrap();
        assert_eq!(hex::encode(trace.trace_id), "0102030405060708090a0b0c0d0e0f10");
        assert_eq!(hex::encode(trace.span_id), "1112131415161718");

        let r = parse_request("resources/read", Some(br#"{"uri":"file:///inbox/1.eml"}"#), &[]);
        assert_eq!(r.target.as_deref(), Some("file:///inbox/1.eml"));
    }

    #[test]
    fn headers_link_the_call_when_meta_is_dropped() {
        let headers = vec![
            (
                "Traceparent".to_string(),
                b"00-0102030405060708090a0b0c0d0e0f10-1112131415161718-01".to_vec(),
            ),
            ("wake-parents".to_string(), b" e1, ,e2 ".to_vec()),
            ("mcp-session-id".to_string(), b"s1".to_vec()),
            ("authorization".to_string(), b"secret".to_vec()),
        ];
        let r = parse_request("tools/call", Some(br#"{"name":"read_email"}"#), &headers);
        assert_eq!(r.parents, vec!["e1", "e2"]);
        assert_eq!(r.session.as_deref(), Some("s1"));
        assert_eq!(
            r.trace.map(|t| hex::encode(t.trace_id)).as_deref(),
            Some("0102030405060708090a0b0c0d0e0f10")
        );
        // A malformed header leaves what _meta said.
        let meta = br#"{"name":"t","_meta":{"dev.wake/parents":["m1"]}}"#;
        let r = parse_request(
            "tools/call",
            Some(meta),
            &[
                ("wake-parents".into(), b" , ".to_vec()),
                ("traceparent".into(), b"bad".to_vec()),
            ],
        );
        assert_eq!(r.parents, vec!["m1"]);
        assert_eq!(r.trace, None);
        let r = parse_request("tools/call", None, &[("wake-parents".into(), vec![0xff])]);
        assert!(r.parents.is_empty());
    }

    #[test]
    fn treats_anything_malformed_as_absent() {
        for params in [&b""[..], b"null", b"[]", b"{", b"{\"name\":7}", b"\xff\xfe"] {
            let r = parse_request("tools/call", Some(params), &[]);
            assert_eq!(r.target, None, "{:?}", params);
        }
        for tp in [
            "",
            "00-00000000000000000000000000000000-1112131415161718-01",
            "00-0102030405060708090a0b0c0d0e0f10-0000000000000000-01",
            "ff-0102030405060708090a0b0c0d0e0f10-1112131415161718-01",
            "00-0102030405060708090A0B0C0D0E0F10-1112131415161718-01",
            "00-0102-1112131415161718-01",
        ] {
            assert_eq!(parse_traceparent(tp), None, "{tp:?}");
        }
    }

    #[test]
    fn payload_keeps_json_exactly_and_quotes_the_rest() {
        assert_eq!(
            payload(Some(br#"{"a": 1}"#), Some(br#"{"content":[]}"#), None),
            r#"{"input":{"a": 1},"output":{"content":[]}}"#
        );
        assert_eq!(
            payload(Some(b"not json"), None, Some("no response")),
            r#"{"input":"not json","output":null,"error":"no response"}"#
        );
        let p = payload(Some(b"\xff"), Some(b"{"), None);
        assert!(serde_json::from_str::<Value>(&p).is_ok(), "{p}");
    }

    #[test]
    fn a_cut_payload_is_quoted_and_marked() {
        let (cut, truncated) = cap(br#"{"content":[{"text":"12345"}]}"#.to_vec(), 13);
        assert!(truncated);
        let p = capped_payload((Some(b"12"), false), (Some(&cut), true), None);
        assert_eq!(p, r#"{"input":12,"output":"{\"content\":[{","truncated":true}"#);
        // A prefix that happens to be JSON still goes in as a string.
        let p = capped_payload((Some(b"12"), true), (None, false), None);
        assert_eq!(p, r#"{"input":"12","output":null,"truncated":true}"#);
        assert_eq!(cap(b"abc".to_vec(), 3), (b"abc".to_vec(), false));
    }

    #[test]
    fn payload_quotes_json_no_parser_can_hold() {
        // Found by fuzzing: a number out of a float's range is JSON by the grammar, but
        // serde_json and Go's encoding/json both refuse it.
        let p = payload(Some(b"6441e626"), Some(b"[1e999]"), None);
        assert_eq!(p, r#"{"input":"6441e626","output":"[1e999]"}"#);
        // Valid JSON nested so deep that wrapping it would pass serde_json's limit.
        let deep = format!("{}{}", "[".repeat(128), "]".repeat(128));
        let p = payload(Some(deep.as_bytes()), None, None);
        assert!(serde_json::from_str::<Value>(&p).is_ok(), "{p}");
        let fine = format!("{}{}", "[".repeat(100), "]".repeat(100));
        assert_eq!(as_json(fine.as_bytes()), Some(fine.as_str()));
    }

    proptest! {
        #[test]
        fn never_panics_and_payload_is_always_json(method in "[a-z/]{0,20}", params in proptest::collection::vec(any::<u8>(), 0..512)) {
            let headers = vec![("wake-parents".to_string(), params.clone()), ("traceparent".to_string(), params.clone())];
            let _ = parse_request(&method, Some(&params), &headers);
            let p = payload(Some(&params), Some(&params), Some("e"));
            prop_assert!(serde_json::from_str::<Value>(&p).is_ok());
        }

        #[test]
        fn never_panics_on_json_shaped_input(s in r#"\{("[a-z_/.]{0,12}":("[^"]{0,20}"|[0-9]{1,5}|\[\]|\{\}|null),?){0,6}\}"#) {
            let _ = parse_request("tools/call", Some(s.as_bytes()), &[]);
            let _ = parse_request("resources/read", Some(s.as_bytes()), &[]);
        }
    }
}
