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

fn parent_id(s: &str) -> Option<String> {
    let s = s.trim();
    (!s.is_empty() && s.len() <= 128).then(|| s.to_string())
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
        let parents: Vec<String> = parents.split(',').filter_map(parent_id).take(64).collect();
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
        .map(|list| {
            list.iter()
                .filter_map(Value::as_str)
                .filter_map(parent_id)
                .take(64)
                .collect()
        })
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
    if parts.len() < 4 || parts[0].len() != 2 || parts[0] == "ff" || parts[3].len() != 2 {
        return None;
    }
    let lower_hex = |p: &str| p.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
    if !parts.iter().all(|p| lower_hex(p)) {
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

/// The event's payload: what the call sent and what came back, kept byte for byte when
/// they're JSON, and as a string when they aren't.
pub fn payload(input: Option<&[u8]>, output: Option<&[u8]>, error: Option<&str>) -> String {
    let mut out = String::from(r#"{"input":"#);
    push_json(&mut out, input);
    out.push_str(r#","output":"#);
    push_json(&mut out, output);
    if let Some(error) = error {
        out.push_str(r#","error":"#);
        out.push_str(&Value::String(error.to_string()).to_string());
    }
    out.push('}');
    out
}

fn push_json(out: &mut String, bytes: Option<&[u8]>) {
    match bytes {
        None => out.push_str("null"),
        Some(b) => match std::str::from_utf8(b) {
            Ok(s) if serde_json::from_str::<serde::de::IgnoredAny>(s).is_ok() => out.push_str(s.trim()),
            _ => out.push_str(&Value::String(String::from_utf8_lossy(b).into_owned()).to_string()),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

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
