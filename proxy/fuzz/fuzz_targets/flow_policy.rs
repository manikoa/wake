//! Fuzz target for FlowPolicy JSON parsing, wake-origins header extraction, and policy
//! denial recording at ext_authz.
#![no_main]
use std::collections::HashMap;

use libfuzzer_sys::fuzz_target;
use wake_proxy::agents::Agents;
use wake_proxy::authz::{self, Call};
use wake_proxy::policy::{FlowPolicy, HEADER_ORIGINS};

fuzz_target!(|data: &[u8]| {
    let raw = String::from_utf8_lossy(data).into_owned();
    let mut headers = HashMap::new();
    headers.insert(HEADER_ORIGINS.to_string(), raw);
    let call = Call {
        caller: Some("billing".into()),
        receiver: Some("tools".into()),
        method: "POST".into(),
        host: "agentgateway:3000".into(),
        path: "/mcp/pay".into(),
        headers,
    };
    let origins = FlowPolicy::origins_of(&call);
    assert!(origins.len() <= 64);

    if let Ok(policy) = serde_json::from_slice::<FlowPolicy>(data)
        && let Some(halt) = policy.deciding(&call)
    {
        let event = authz::denied_event(&call, &[halt], &Agents::none(), 1);
        assert!(serde_json::from_str::<serde_json::Value>(event.str("wake.payload").unwrap()).is_ok());
    }
});
