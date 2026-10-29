//! Canonical bytes of attributes holding any strings: always JSON that reads back the same.
#![no_main]
use libfuzzer_sys::fuzz_target;
use wake_proxy::canonical::{Event, Value, canonical};

fuzz_target!(|data: &[u8]| {
    let text = String::from_utf8_lossy(data).into_owned();
    let mut event = Event::default();
    event.set("wake.text", Value::Str(text.clone()));
    // A key of its own, so no input names wake.text and overwrites it.
    event.set(&format!("wake.key.{text}"), Value::Strs(vec![text.clone()]));
    let bytes = canonical(&event);
    let parsed: serde_json::Value = serde_json::from_slice(&bytes).expect("canonical bytes are JSON");
    assert_eq!(parsed["attributes"]["wake.text"], serde_json::Value::String(text));
});
