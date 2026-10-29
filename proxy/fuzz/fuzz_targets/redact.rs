//! Redacting any payload: a JSON payload stays JSON, and so does the JSON in its strings,
//! nothing redacted is redacted again, and every kind listed names a placeholder in the
//! payload.
#![no_main]
use libfuzzer_sys::fuzz_target;
use wake_proxy::redact::redact;

/// JSON's non-ASCII escaped as \uXXXX, as Python's json.dumps writes it by default.
fn ascii(json: &str) -> String {
    let mut out = String::with_capacity(json.len());
    for c in json.chars() {
        if c.is_ascii() {
            out.push(c);
        } else {
            for unit in c.encode_utf16(&mut [0; 2]) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}

fuzz_target!(|data: &[u8]| {
    let text = String::from_utf8_lossy(data).into_owned();
    // As the proxy sees it: anything an agent sends, inside a JSON payload, with non-ASCII as
    // it is, and escaped, as an MCP server in Python sends it.
    let nested = serde_json::json!({"text": text}).to_string();
    let plain = serde_json::json!({"output": text, "nested": nested}).to_string();
    let escaped = ascii(&serde_json::json!({"output": text, "nested": ascii(&nested)}).to_string());
    for payload in [plain, escaped] {
        let (out, kinds) = redact(&payload);
        let doc: serde_json::Value = serde_json::from_str(&out).expect("a redacted JSON payload is JSON");
        let nested = doc["nested"].as_str().expect("nested is a string");
        serde_json::from_str::<serde_json::Value>(nested).expect("the JSON in its string is still JSON");
        assert_eq!(redact(&out).0, out, "redacting twice changes nothing more");
        for kind in &kinds {
            assert!(
                out.contains(&format!("[redacted {kind} sha256:")),
                "{kind} listed but not in the payload"
            );
        }
    }
    // Raw, not JSON: still never panics, and still idempotent.
    let (raw, _) = redact(&text);
    assert_eq!(redact(&raw).0, raw);
});
