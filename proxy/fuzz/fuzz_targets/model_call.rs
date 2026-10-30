//! A model call in any order, under a flow policy: headers, bodies and trailers with any
//! bytes, in any body mode, from an agent and from the provider. Every message must be
//! answered, each refusal's record must be JSON, and nothing may panic.
#![no_main]
use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use libfuzzer_sys::fuzz_target;
use wake_proxy::budget::Budget;
use wake_proxy::export::Counters;
use wake_proxy::extproc::pb::common::HeaderValue;
use wake_proxy::extproc::pb::ext_proc::{
    HeaderMap, HttpBody, HttpHeaders, HttpTrailers, ProcessingRequest, ProtocolConfiguration,
    processing_request::Request as Phase,
};
use wake_proxy::extproc::{ATTR_MODEL, ATTR_SENDER, ATTRIBUTES, Exchange};
use wake_proxy::models::{self, Approvals, Provider, Shared};
use wake_proxy::origins::Observed;
use wake_proxy::policy::{FlowPolicy, Policy};

const RULES: &[u8] = br#"{"rules": [
    {"id": "no-web-after-fetch", "callers": ["research"], "tools": ["web_search", "web_fetch"], "forbid_origins": ["tool:fetch_page"]},
    {"id": "no-refunds-after-web", "tools": ["refund", "code_interpreter"], "forbid_origins": ["tool:web_*"]}
]}"#;

fuzz_target!(|data: &[u8]| {
    for provider in [Some(Provider::OpenAi), Some(Provider::Anthropic), None] {
        let _ = models::read_request(provider, data);
    }
    if let Ok(value) = serde_json::from_slice(data) {
        for provider in [Provider::OpenAi, Provider::Anthropic] {
            let _ = models::read_response(provider, &value, &mut Default::default());
        }
    }

    let shared = Shared {
        policy: Arc::new(Policy::fixed(FlowPolicy::parse(RULES).unwrap())),
        observed: Arc::new(Observed::starting(0, 900)),
        approvals: Arc::new(Approvals::new(4)),
        counters: Arc::new(Counters::default()),
        held: Budget::new(1 << 16),
    };
    let mut x = Exchange::default().with_models(shared);
    for (i, chunk) in data.chunks(31).enumerate() {
        let b = chunk.first().copied().unwrap_or(0);
        let text = String::from_utf8_lossy(chunk).into_owned();
        let header = |key: &str, value: String| HeaderValue {
            key: key.into(),
            value,
            ..Default::default()
        };
        let headers = HttpHeaders {
            headers: Some(HeaderMap {
                headers: vec![
                    header(":method", "POST".into()),
                    header(":path", "/models/openai/v1/responses".into()),
                    header(":status", if b & 1 == 0 { "200".into() } else { text.clone() }),
                    header(
                        "content-type",
                        if b & 2 == 0 {
                            "text/event-stream".into()
                        } else {
                            "application/json".into()
                        },
                    ),
                    header("content-encoding", if b & 64 == 0 { "identity".into() } else { text }),
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
        let first = i == 0;
        let attributes = first.then(|| {
            let provider = ["openai", "anthropic", "gemini"][(b as usize / 3) % 3];
            let fields = [(ATTR_SENDER, "research"), (ATTR_MODEL, provider)]
                .into_iter()
                .map(|(k, v)| {
                    (
                        k.to_string(),
                        prost_types::Value {
                            kind: Some(prost_types::value::Kind::StringValue(v.into())),
                        },
                    )
                })
                .collect();
            (ATTRIBUTES.to_string(), prost_types::Struct { fields })
        });
        let (answer, events) = x.handle(
            ProcessingRequest {
                request: Some(phase),
                attributes: attributes.into_iter().collect(),
                protocol_config: first.then(|| ProtocolConfiguration {
                    request_body_mode: (b % 5) as i32,
                    response_body_mode: (b / 5 % 5) as i32,
                    ..Default::default()
                }),
                ..Default::default()
            },
            UNIX_EPOCH + Duration::from_secs(1_800_000_000 + i as u64),
        );
        assert!(answer.response.is_some());
        assert!(x.is_model());
        for e in events {
            assert!(serde_json::from_str::<serde_json::Value>(e.str("wake.payload").unwrap()).is_ok());
        }
    }
    assert!(
        x.finish(UNIX_EPOCH).is_empty(),
        "a model call records nothing but refusals"
    );
});
