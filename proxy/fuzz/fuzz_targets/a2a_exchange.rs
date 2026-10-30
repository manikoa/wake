//! An A2A exchange in any order: headers, bodies and trailers with any bytes, in either
//! body mode. Every message must be answered, and nothing may panic.
#![no_main]
use std::time::{Duration, UNIX_EPOCH};

use libfuzzer_sys::fuzz_target;
use wake_proxy::a2a;
use wake_proxy::extproc::Exchange;
use wake_proxy::extproc::pb::common::HeaderValue;
use wake_proxy::extproc::pb::ext_proc::{
    HeaderMap, HttpBody, HttpHeaders, HttpTrailers, ProcessingRequest, ProtocolConfiguration,
    processing_request::Request as Phase,
};

fuzz_target!(|data: &[u8]| {
    let _ = a2a::parse_request(data);
    let _ = a2a::parse_prefix(data);
    let _ = a2a::parse_reply(data, false);
    let _ = a2a::parse_reply(data, true);

    // Shared task memory, as in the processor: a reply can name a task a request asks about.
    let mut x = Exchange::with_tasks(std::sync::Arc::new(a2a::Tasks::new(4)));
    for (i, chunk) in data.chunks(23).enumerate() {
        let b = chunk.first().copied().unwrap_or(0);
        let text = String::from_utf8_lossy(chunk).into_owned();
        let headers = HttpHeaders {
            headers: Some(HeaderMap {
                headers: vec![
                    HeaderValue {
                        key: ":method".into(),
                        value: if b & 1 == 0 { "POST".into() } else { "GET".into() },
                        ..Default::default()
                    },
                    HeaderValue {
                        key: "content-type".into(),
                        value: if b & 2 == 0 {
                            "text/event-stream".into()
                        } else {
                            text.clone()
                        },
                        ..Default::default()
                    },
                    HeaderValue {
                        key: "traceparent".into(),
                        raw_value: chunk.to_vec(),
                        ..Default::default()
                    },
                    HeaderValue {
                        key: "wake-parents".into(),
                        raw_value: chunk.to_vec(),
                        ..Default::default()
                    },
                ],
            }),
            end_of_stream: b & 4 != 0,
        };
        let body = HttpBody {
            body: chunk.to_vec(),
            end_of_stream: b & 8 != 0,
        };
        let phase = match b % 6 {
            0 => Phase::RequestHeaders(headers),
            1 => Phase::RequestBody(body),
            2 => Phase::ResponseHeaders(headers),
            3 => Phase::ResponseBody(body),
            4 => Phase::RequestTrailers(HttpTrailers::default()),
            _ => Phase::ResponseTrailers(HttpTrailers::default()),
        };
        let protocol_config = (i == 0).then(|| ProtocolConfiguration {
            request_body_mode: (b % 5) as i32,
            response_body_mode: (b / 5 % 5) as i32,
            ..Default::default()
        });
        let (answer, events) = x.handle(
            ProcessingRequest {
                request: Some(phase),
                protocol_config,
                ..Default::default()
            },
            UNIX_EPOCH + Duration::from_secs(i as u64),
        );
        assert!(answer.response.is_some());
        for e in events {
            assert!(serde_json::from_str::<serde_json::Value>(e.str("wake.payload").unwrap()).is_ok());
        }
    }
    let _ = x.finish(UNIX_EPOCH);
});
