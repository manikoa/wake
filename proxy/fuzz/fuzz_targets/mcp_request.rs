//! A request's params and a call's result, as an agent or MCP server sends them.
#![no_main]
use libfuzzer_sys::fuzz_target;
use wake_proxy::mcp;

fuzz_target!(|data: &[u8]| {
    let headers = vec![
        (mcp::HEADER_TRACEPARENT.to_string(), data.to_vec()),
        (mcp::HEADER_PARENTS.to_string(), data.to_vec()),
        (mcp::HEADER_SESSION.to_string(), data.to_vec()),
    ];
    for method in mcp::CAPTURED {
        let _ = mcp::parse_request(method, Some(data), &headers);
    }
    if let Ok(s) = std::str::from_utf8(data) {
        let _ = mcp::parse_traceparent(s);
    }
    // The payload is always valid JSON, whatever came in.
    let payload = mcp::payload(Some(data), Some(data), Some("e"));
    assert!(serde_json::from_str::<serde_json::Value>(&payload).is_ok());
});
