//! The exact bytes a Wake event's signature covers, as defined in docs/events.md.
//!
//! The collector rebuilds these bytes in Go to check the proxy's signatures, so they must
//! match Go's encoding/json byte for byte: keys sorted, no whitespace, no HTML escaping,
//! and Go's rules for which characters are escaped and how. The shared vectors in
//! collector/event/testdata/canonical.json hold the two encoders to the same output.

use std::collections::BTreeMap;

/// An attribute value. These are the only kinds the proxy emits.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Str(String),
    Int(i64),
    Bool(bool),
    Strs(Vec<String>),
}

/// A Wake event: an OpenTelemetry span with Wake's attributes.
#[derive(Clone, Debug, Default)]
pub struct Event {
    pub trace_id: [u8; 16],
    pub span_id: [u8; 8],
    pub parent_span_id: Option<[u8; 8]>,
    pub name: String,
    pub start_time_unix_nano: u64,
    pub end_time_unix_nano: u64,
    pub attributes: BTreeMap<String, Value>,
}

impl Event {
    pub fn set(&mut self, key: &str, value: Value) {
        self.attributes.insert(key.to_string(), value);
    }

    pub fn str(&self, key: &str) -> Option<&str> {
        match self.attributes.get(key) {
            Some(Value::Str(s)) => Some(s),
            _ => None,
        }
    }
}

/// Whether an attribute is covered by the signature: every wake.* and gen_ai.* attribute
/// except the signer, the signature fields and the payload, which its digest covers.
pub fn signed(key: &str) -> bool {
    if key == "wake.signer" || key == "wake.payload" || key.starts_with("wake.signature") {
        return false;
    }
    key.starts_with("wake.") || key.starts_with("gen_ai.")
}

/// The canonical bytes of an event.
pub fn canonical(event: &Event) -> Vec<u8> {
    let mut out = String::new();
    out.push_str(r#"{"attributes":{"#);
    // BTreeMap iterates in byte order of the keys, which is the order Go sorts map keys.
    let mut first = true;
    for (key, value) in event.attributes.iter().filter(|(k, _)| signed(k)) {
        if !first {
            out.push(',');
        }
        first = false;
        write_string(&mut out, key);
        out.push(':');
        write_value(&mut out, value);
    }
    out.push_str(r#"},"end_time_unix_nano":"#);
    out.push_str(&event.end_time_unix_nano.to_string());
    out.push_str(r#","span_id":"#);
    write_string(&mut out, &hex::encode(event.span_id));
    out.push_str(r#","start_time_unix_nano":"#);
    out.push_str(&event.start_time_unix_nano.to_string());
    out.push_str(r#","trace_id":"#);
    write_string(&mut out, &hex::encode(event.trace_id));
    out.push('}');
    out.into_bytes()
}

fn write_value(out: &mut String, value: &Value) {
    match value {
        Value::Str(s) => write_string(out, s),
        Value::Int(n) => out.push_str(&n.to_string()),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Strs(list) => {
            out.push('[');
            for (i, s) in list.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(out, s);
            }
            out.push(']');
        }
    }
}

/// Writes s as a JSON string the way Go's encoding/json does with HTML escaping off.
fn write_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            // Go always escapes the two line separators JavaScript can't hold in a string.
            '\u{2028}' => out.push_str("\\u2028"),
            '\u{2029}' => out.push_str("\\u2029"),
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Deserialize)]
    struct Vector {
        name: String,
        trace_id: String,
        span_id: String,
        start_time_unix_nano: u64,
        end_time_unix_nano: u64,
        attributes: Vec<Attr>,
        canonical: String,
    }

    #[derive(Deserialize)]
    struct Attr {
        key: String,
        string: Option<String>,
        int: Option<i64>,
        bool: Option<bool>,
        strings: Option<Vec<String>>,
    }

    proptest::proptest! {
        // The same property the canonical fuzz target checks, on stable Rust.
        #[test]
        fn canonical_bytes_are_json_that_reads_back_the_same(text in ".*", n in proptest::prelude::any::<i64>()) {
            let mut event = Event::default();
            event.set("wake.text", Value::Str(text.clone()));
            event.set(&format!("wake.{text}"), Value::Strs(vec![text.clone()]));
            event.set("wake.n", Value::Int(n));
            let parsed: serde_json::Value = serde_json::from_slice(&canonical(&event)).unwrap();
            proptest::prop_assert_eq!(&parsed["attributes"]["wake.text"], &serde_json::Value::String(text));
            proptest::prop_assert_eq!(&parsed["attributes"]["wake.n"], &serde_json::json!(n));
        }
    }

    #[test]
    fn matches_the_collectors_canonical_bytes() {
        let data = include_str!("../../collector/event/testdata/canonical.json");
        let vectors: Vec<Vector> = serde_json::from_str(data).unwrap();
        assert!(!vectors.is_empty());
        for v in vectors {
            let mut event = Event {
                start_time_unix_nano: v.start_time_unix_nano,
                end_time_unix_nano: v.end_time_unix_nano,
                ..Default::default()
            };
            hex::decode_to_slice(&v.trace_id, &mut event.trace_id).unwrap();
            hex::decode_to_slice(&v.span_id, &mut event.span_id).unwrap();
            for a in v.attributes {
                let value = match (a.string, a.int, a.bool, a.strings) {
                    (Some(s), ..) => Value::Str(s),
                    (_, Some(n), ..) => Value::Int(n),
                    (_, _, Some(b), _) => Value::Bool(b),
                    (_, _, _, Some(list)) => Value::Strs(list),
                    _ => Value::Strs(vec![]),
                };
                event.set(&a.key, value);
            }
            let got = String::from_utf8(canonical(&event)).unwrap();
            assert_eq!(got, v.canonical, "vector {:?}", v.name);
        }
    }
}
