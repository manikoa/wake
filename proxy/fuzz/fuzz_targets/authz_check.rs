//! An ext_authz check with any attributes: what the deny lever reads from agent traffic.
//! Deciding and recording must never panic, a halt that names the caller, the agent its
//! token's chain started from or the token itself always covers the call, and a denied or
//! cut call's payload is always JSON.
#![no_main]
use libfuzzer_sys::fuzz_target;
use prost::Message;
use wake_proxy::agents::Agents;
use wake_proxy::authz::{self, pb::auth::CheckRequest};
use wake_proxy::halts::{Halts, Scope};

fuzz_target!(|data: &[u8]| {
    let Ok(request) = CheckRequest::decode(data) else {
        return;
    };
    let call = authz::read_call(&request);
    let token = call.token.as_ref();
    let halts = Halts::in_memory();
    let scope = Scope {
        fleet: data.first().is_some_and(|b| b & 1 == 1),
        agents: call
            .caller
            .iter()
            .cloned()
            .chain(token.and_then(|t| t.actors.last().cloned()))
            .collect(),
        tokens: token.and_then(|t| t.id.clone()).into_iter().collect(),
    };
    if !scope.is_empty() {
        halts.put("fuzz", scope.clone(), 0).unwrap();
    }
    halts.note_caller(call.caller.as_deref(), token, 1);
    let deciding = halts.deciding_with(call.caller.as_deref(), call.receiver.as_deref(), token);
    assert_eq!(deciding.is_empty(), scope.is_empty(), "{scope:?} {call:?}");
    if !deciding.is_empty() {
        let event = authz::denied_event(&call, &deciding, &Agents::none(), 1);
        assert!(serde_json::from_str::<serde_json::Value>(event.str("wake.payload").unwrap()).is_ok());
        let cut = authz::cut_event(&call, &deciding, &Agents::none(), 1, call.caller.as_deref());
        let payload: serde_json::Value = serde_json::from_str(cut.str("wake.payload").unwrap()).unwrap();
        assert_eq!(payload["cut"], true);
        let _ = authz::deny(&deciding);
    }
});
