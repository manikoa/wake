//! Redacting any payload: a JSON payload stays JSON, nothing redacted is redacted again, and
//! every kind listed names a placeholder in the payload.
#![no_main]
use libfuzzer_sys::fuzz_target;
use wake_proxy::redact::redact;

fuzz_target!(|data: &[u8]| {
    let text = String::from_utf8_lossy(data).into_owned();
    // As the proxy sees it: anything an agent sends, inside a JSON payload.
    let payload =
        serde_json::json!({"output": text, "nested": serde_json::json!({"text": text}).to_string()}).to_string();
    let (out, kinds) = redact(&payload);
    serde_json::from_str::<serde_json::Value>(&out).expect("a redacted JSON payload is JSON");
    assert_eq!(redact(&out).0, out, "redacting twice changes nothing more");
    for kind in &kinds {
        assert!(
            out.contains(&format!("[redacted {kind} sha256:")),
            "{kind} listed but not in the payload"
        );
    }
    // Raw, not JSON: still never panics, and still idempotent.
    let (raw, _) = redact(&text);
    assert_eq!(redact(&raw).0, raw);
});
