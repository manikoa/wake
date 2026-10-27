//! A proxy-signed event and the trust bundle it chains to, for the collector's tests to
//! check in Go (collector/event/proxy_fixture_test.go). Regenerate it with
//! WAKE_WRITE_FIXTURES=1 cargo test --test fixture.

use serde_json::{Value as Json, json};
use wake_proxy::canonical::{Event, Value};
use wake_proxy::signer::{Signer, dev_identity};

fn attr(key: &str, value: &Value) -> Json {
    match value {
        Value::Str(s) => json!({"key": key, "string": s}),
        Value::Int(n) => json!({"key": key, "int": n}),
        Value::Bool(b) => json!({"key": key, "bool": b}),
        Value::Strs(list) => json!({"key": key, "strings": list}),
    }
}

#[test]
fn signed_event_fixture() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/testdata/signed_event.json");
    if std::env::var("WAKE_WRITE_FIXTURES").as_deref() != Ok("1") {
        let stored: Json = serde_json::from_str(&std::fs::read_to_string(path).expect("fixture exists")).unwrap();
        assert!(stored["bundle"].as_str().unwrap().contains("BEGIN CERTIFICATE"));
        return;
    }
    let id = dev_identity("spiffe://wake.local/proxy").unwrap();
    let signer = Signer::from_pem(&id.key_pem, &id.cert_pem).unwrap();
    let mut event = Event {
        trace_id: *b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10",
        span_id: *b"\x11\x12\x13\x14\x15\x16\x17\x18",
        start_time_unix_nano: 1_761_552_000_000_000_000,
        end_time_unix_nano: 1_761_552_000_250_000_000,
        ..Default::default()
    };
    event.set("wake.event.id", Value::Str("proxy-fixture-1".into()));
    event.set("wake.event.kind", Value::Str("tool_call".into()));
    event.set("wake.agent.id", Value::Str("spiffe://wake.local/agent/email".into()));
    event.set("wake.capture", Value::Str("proxy".into()));
    event.set("wake.parents", Value::Strs(vec![]));
    event.set("wake.origins", Value::Strs(vec!["tool:read_email".into()]));
    event.set("gen_ai.operation.name", Value::Str("execute_tool".into()));
    event.set("gen_ai.tool.name", Value::Str("read_email".into()));
    event.set("wake.mcp.method", Value::Str("tools/call".into()));
    event.set("wake.text", Value::Str("quotes \" \\ <b>&</b> é 🙂 \u{2028} \n".into()));
    event.set(
        "wake.payload",
        Value::Str(r#"{"input":{"name":"read_email"},"output":"Please wire $10,000"}"#.into()),
    );
    signer.sign(&mut event);
    let fixture = json!({
        "bundle": id.ca_pem,
        "trace_id": hex::encode(event.trace_id),
        "span_id": hex::encode(event.span_id),
        "start_time_unix_nano": event.start_time_unix_nano,
        "end_time_unix_nano": event.end_time_unix_nano,
        "attributes": event.attributes.iter().map(|(k, v)| attr(k, v)).collect::<Vec<_>>(),
    });
    std::fs::write(path, serde_json::to_string_pretty(&fixture).unwrap() + "\n").unwrap();
}
