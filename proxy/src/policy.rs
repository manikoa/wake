//! Origin flow policies (Contain): rules enforced at the proxy's `ext_authz` boundary that
//! forbid calls carrying untrusted origins (such as external tool outputs or unverified
//! memory) from reaching sensitive receivers or paths.
//!
//! Like `Halts`, a flow policy fails closed when configured: if `--policy-file` is given and
//! cannot be read or parsed, every call is denied under `policy:unreadable-policy` until the
//! file is fixed.

use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::authz::Call;
use crate::halts::{Halt, Scope};

/// Header carrying comma-separated causal origins across HTTP/A2A/MCP hops.
pub const HEADER_ORIGINS: &str = "wake-origins";

/// One flow rule evaluated against an outbound call.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct FlowRule {
    pub id: String,
    /// Callers this rule applies to (empty means every caller).
    #[serde(default)]
    pub callers: Vec<String>,
    /// Receivers this rule applies to (empty means every receiver).
    #[serde(default)]
    pub receivers: Vec<String>,
    /// Path prefixes this rule applies to (empty means every path).
    #[serde(default)]
    pub path_prefixes: Vec<String>,
    /// Origins (exact or ending with `*` prefix wildcard, e.g. `tool:*`) that are forbidden
    /// from flowing into a matching call.
    #[serde(default)]
    pub forbid_origins: Vec<String>,
    /// Origins (exact or ending with `*`) at least one of which must be present on a matching
    /// call (for example `["user"]`).
    #[serde(default)]
    pub require_origins: Vec<String>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct FlowPolicy {
    #[serde(default)]
    pub rules: Vec<FlowRule>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unreadable: Option<String>,
}

impl FlowPolicy {
    pub fn empty() -> Self {
        FlowPolicy::default()
    }

    /// Loads a JSON flow policy from disk. Fails closed if the file cannot be read or parsed.
    pub fn load(path: &Path) -> Self {
        match std::fs::read(path) {
            Ok(bytes) => match serde_json::from_slice::<FlowPolicy>(&bytes) {
                Ok(policy) => policy,
                Err(e) => Self::fail_closed(path, &e.to_string()),
            },
            Err(e) => Self::fail_closed(path, &e.to_string()),
        }
    }

    fn fail_closed(path: &Path, error: &str) -> Self {
        tracing::error!(
            path = %path.display(),
            error,
            "can't read flow policy; denying every call until replaced"
        );
        FlowPolicy {
            rules: vec![],
            unreadable: Some(error.to_string()),
        }
    }

    /// Extracts the normalized list of origins from the `wake-origins` header on a call.
    pub fn origins_of(call: &Call) -> Vec<String> {
        call.headers
            .get(HEADER_ORIGINS)
            .map(|raw| {
                raw.split(',')
                    .map(str::trim)
                    .filter(|s| !s.is_empty() && s.len() <= 256)
                    .take(64)
                    .map(String::from)
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Returns a synthetic `Halt` (`id = "policy:<rule_id>"`) when a flow rule denies `call`.
    pub fn deciding(&self, call: &Call) -> Option<Halt> {
        if self.unreadable.is_some() {
            return Some(Halt {
                id: "policy:unreadable-policy".into(),
                scope: Scope {
                    fleet: true,
                    agents: vec![],
                },
                issued_at: 0,
                applied_at: 0,
            });
        }
        let origins = Self::origins_of(call);
        for rule in &self.rules {
            if !rule.matches_target(call) {
                continue;
            }
            let forbidden = !rule.forbid_origins.is_empty()
                && origins
                    .iter()
                    .any(|o| rule.forbid_origins.iter().any(|pat| origin_matches(pat, o)));
            let missing_required = !rule.require_origins.is_empty()
                && !origins
                    .iter()
                    .any(|o| rule.require_origins.iter().any(|pat| origin_matches(pat, o)));
            if forbidden || missing_required {
                return Some(Halt {
                    id: format!("policy:{}", rule.id),
                    scope: Scope {
                        fleet: false,
                        agents: call.caller.iter().cloned().collect(),
                    },
                    issued_at: 0,
                    applied_at: 0,
                });
            }
        }
        None
    }
}

impl FlowRule {
    fn matches_target(&self, call: &Call) -> bool {
        if !self.callers.is_empty() {
            let Some(caller) = call.caller.as_deref() else {
                return false;
            };
            if !self.callers.iter().any(|c| c == caller) {
                return false;
            }
        }
        if !self.receivers.is_empty() {
            let Some(receiver) = call.receiver.as_deref() else {
                return false;
            };
            if !self.receivers.iter().any(|r| r == receiver) {
                return false;
            }
        }
        if !self.path_prefixes.is_empty() && !self.path_prefixes.iter().any(|p| call.path.starts_with(p.as_str())) {
            return false;
        }
        true
    }
}

fn origin_matches(pattern: &str, origin: &str) -> bool {
    if let Some(prefix) = pattern.strip_suffix('*') {
        origin.starts_with(prefix)
    } else {
        origin == pattern
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use std::collections::HashMap;

    fn call(caller: &str, receiver: &str, path: &str, origins: &str) -> Call {
        let mut headers = HashMap::new();
        if !origins.is_empty() {
            headers.insert(HEADER_ORIGINS.to_string(), origins.to_string());
        }
        Call {
            caller: Some(caller.into()),
            receiver: Some(receiver.into()),
            method: "POST".into(),
            host: "agentgateway:3000".into(),
            path: path.into(),
            headers,
        }
    }

    #[test]
    fn forbids_untrusted_origins_and_enforces_required_origins() {
        let policy = FlowPolicy {
            rules: vec![
                FlowRule {
                    id: "no-web-fetch-to-billing".into(),
                    callers: vec![],
                    receivers: vec!["billing".into()],
                    path_prefixes: vec![],
                    forbid_origins: vec!["tool:fetch_page".into(), "memory:untrusted/*".into()],
                    require_origins: vec![],
                },
                FlowRule {
                    id: "require-user-origin-for-payments".into(),
                    callers: vec!["billing".into()],
                    receivers: vec![],
                    path_prefixes: vec!["/mcp/pay".into()],
                    forbid_origins: vec![],
                    require_origins: vec!["user".into()],
                },
            ],
            unreadable: None,
        };

        assert!(
            policy
                .deciding(&call("researcher", "billing", "/agents/billing", "user"))
                .is_none()
        );

        let denied = policy
            .deciding(&call(
                "researcher",
                "billing",
                "/agents/billing",
                "user,tool:fetch_page",
            ))
            .unwrap();
        assert_eq!(denied.id, "policy:no-web-fetch-to-billing");

        let denied_mem = policy
            .deciding(&call(
                "researcher",
                "billing",
                "/agents/billing",
                "memory:untrusted/note",
            ))
            .unwrap();
        assert_eq!(denied_mem.id, "policy:no-web-fetch-to-billing");

        let denied_req = policy
            .deciding(&call("billing", "tools", "/mcp/pay", "agent:researcher"))
            .unwrap();
        assert_eq!(denied_req.id, "policy:require-user-origin-for-payments");

        assert!(
            policy
                .deciding(&call("billing", "tools", "/mcp/pay", "user,agent:researcher"))
                .is_none()
        );
    }

    #[test]
    fn unreadable_policy_file_fails_closed() {
        let missing = std::env::temp_dir().join("wake-nonexistent-policy.json");
        let policy = FlowPolicy::load(&missing);
        let denied = policy
            .deciding(&call("support", "research", "/agents/research", "user"))
            .unwrap();
        assert_eq!(denied.id, "policy:unreadable-policy");
    }

    proptest! {
        #[test]
        fn fuzz_origins_header_never_panics_and_enforces_bounds(raw in ".*") {
            let c = call("caller", "receiver", "/mcp", &raw);
            let parsed = FlowPolicy::origins_of(&c);
            prop_assert!(parsed.len() <= 64);
            for o in &parsed {
                prop_assert!(!o.is_empty() && o.len() <= 256);
            }
        }

        #[test]
        fn fuzz_forbid_prefix_always_denies_matching_origin(
            prefix in "[a-z]{1,12}:",
            suffix in "[a-z0-9_-]{1,20}",
            caller in "[a-z]{1,10}",
            receiver in "[a-z]{1,10}",
            path in "/[a-z/]{1,20}",
        ) {
            let rule = FlowRule {
                id: "fuzz-forbid".into(),
                callers: vec![caller.clone()],
                receivers: vec![receiver.clone()],
                path_prefixes: vec!["/".into()],
                forbid_origins: vec![format!("{prefix}*")],
                require_origins: vec![],
            };
            let policy = FlowPolicy {
                rules: vec![rule],
                unreadable: None,
            };
            let matching_origin = format!("user,{prefix}{suffix}");
            let denied = policy.deciding(&call(&caller, &receiver, &path, &matching_origin));
            prop_assert!(denied.is_some());
            prop_assert_eq!(denied.unwrap().id, "policy:fuzz-forbid");
        }
    }
}
