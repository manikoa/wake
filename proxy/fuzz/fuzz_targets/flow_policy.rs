//! Fuzz target for flow policies: parsing any policy file, deciding on any origins that
//! entered the caller, at ext_authz and at the MCP hook, and recording the denial.
#![no_main]
use std::collections::HashMap;

use libfuzzer_sys::fuzz_target;
use wake_proxy::agents::Agents;
use wake_proxy::authz::{self, Call};
use wake_proxy::origins::{Seen, Who};
use wake_proxy::policy::{At, FlowPolicy, Target};

fuzz_target!(|data: &[u8]| {
    // The first line is the policy, and each later one an origin that entered the caller.
    let mut lines = data.split(|b| *b == b'\n');
    let Ok(policy) = FlowPolicy::parse(lines.next().unwrap_or_default()) else {
        return;
    };
    let seen = Seen::These(lines.map(|l| String::from_utf8_lossy(l).into_owned()).collect());
    let agents = Agents::none();
    let call = Call {
        caller: Some("billing".into()),
        receiver: Some("payments".into()),
        method: "POST".into(),
        host: "agentgateway:3000".into(),
        path: "/a2a/payments".into(),
        headers: HashMap::new(),
    };
    for (at, receiver, path, tool) in [
        (At::Authz, Some("payments"), Some("/a2a/payments"), None),
        (At::Mcp, None, None, Some("pay")),
    ] {
        for caller in [Who::Agent("billing"), Who::Unlisted("spiffe://x/other"), Who::Nobody] {
            let target = Target {
                caller,
                receiver,
                path,
                tool,
            };
            for seen in [&seen, &Seen::Every("fuzz")] {
                if let Some(denial) = policy.deciding(at, &target, seen, &agents) {
                    assert!(denial.halt.id.starts_with("policy:"));
                    assert!(!denial.origins.is_empty());
                    let event = authz::policy_denied_event(&call, &denial, &agents, 1);
                    assert!(serde_json::from_str::<serde_json::Value>(event.str("wake.payload").unwrap()).is_ok());
                }
            }
        }
    }
});
