//! A stream of request and response hooks in any order, with any metadata and bodies.
#![no_main]
use std::collections::HashMap;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use libfuzzer_sys::fuzz_target;
use wake_proxy::capture::{Capture, Hook, META_EVENT_ID, META_SESSION};

fuzz_target!(|data: &[u8]| {
    let mut capture = Capture::new(Duration::from_secs(5), 16);
    for (i, chunk) in data.chunks(17).enumerate() {
        let at = UNIX_EPOCH + Duration::from_secs(1_761_552_000 + i as u64);
        let b = chunk.first().copied().unwrap_or(0);
        let mut metadata = HashMap::new();
        metadata.insert(META_SESSION.to_string(), format!("s{}", b % 3));
        if b & 0x10 != 0 {
            metadata.insert(META_EVENT_ID.to_string(), format!("id{}", b % 7));
        }
        let method = ["tools/call", "resources/read", "prompts/get"][(b % 3) as usize].to_string();
        let hook = if b & 0x80 == 0 {
            Hook::Request {
                event_id: format!("id{}", b % 7),
                method,
                server: "s".into(),
                metadata,
                token: None,
                params: Some(chunk.to_vec()),
                headers: vec![
                    ("wake-parents".into(), chunk.to_vec()),
                    ("mcp-session-id".into(), chunk.to_vec()),
                ],
                at,
            }
        } else {
            Hook::Response {
                method,
                server: "s".into(),
                metadata,
                result: chunk.to_vec(),
                at,
            }
        };
        for event in capture.handle(hook) {
            assert!(serde_json::from_str::<serde_json::Value>(event.str("wake.payload").unwrap()).is_ok());
        }
        let _ = capture.expire(at);
        assert!(capture.waiting() <= 16);
    }
    let _ = capture.expire(SystemTime::now());
});
