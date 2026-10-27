//! An ext_authz check with any attributes: what the deny lever reads from agent traffic.
//! Deciding and recording must never panic, and a denied call's payload is always JSON.
#![no_main]
use libfuzzer_sys::fuzz_target;
use prost::Message;
use wake_proxy::authz::{self, pb::auth::CheckRequest};
use wake_proxy::halts::{Halts, Scope};

fuzz_target!(|data: &[u8]| {
    let Ok(request) = CheckRequest::decode(data) else {
        return;
    };
    let call = authz::read_call(&request);
    let halts = Halts::in_memory();
    let scope = Scope {
        fleet: data.first().is_some_and(|b| b & 1 == 1),
        agents: call.caller.iter().cloned().collect(),
    };
    if !scope.is_empty() {
        halts.put("fuzz", scope, 0).unwrap();
    }
    if let Some(halt) = halts.deciding(call.caller.as_deref(), call.receiver.as_deref()) {
        let event = authz::denied_event(&call, &halt, 1);
        assert!(serde_json::from_str::<serde_json::Value>(event.str("wake.payload").unwrap()).is_ok());
        let _ = authz::deny(&halt);
    }
});
