//! Which agent each identity belongs to: the one place where an identity the gateway
//! authenticated, an X.509-SVID's SPIFFE ID or a JWT's (the act.sub of the agent a delegated
//! token names as acting, else its subject), becomes the short name the
//! brake halts by and the SDKs record under (`--agents`, see docs/proxy.md).
//!
//! ```json
//! {"billing": ["spiffe://wake.local/agent/billing", "0oa1billingagent"], "research": ["spiffe://wake.local/agent/research"]}
//! ```
//!
//! Without a map, an identity is its own name, as when the gateway already sends short
//! names. With one, an identity the map doesn't list belongs to no agent: the deny treats
//! its calls as a caller it doesn't know, which every agent halt denies, unless its own
//! token places it under an agent the map lists, as a sub-agent's does (halts.rs).
//! Sub-agents needn't be listed.

use std::collections::{BTreeMap, HashMap};

#[derive(Debug, Default)]
pub struct Agents {
    by_identity: HashMap<String, String>,
}

impl Agents {
    /// No map: every identity is its own name.
    pub fn none() -> Self {
        Agents::default()
    }

    /// Reads a map from agent names to the identities each authenticates with. A name is
    /// what the brake halts by; an identity may belong to only one agent.
    pub fn from_json(bytes: &[u8]) -> Result<Self, String> {
        let map: BTreeMap<String, Vec<String>> = serde_json::from_slice(bytes).map_err(|e| format!("agents: {e}"))?;
        let mut by_identity = HashMap::new();
        for (name, identities) in map {
            if name.trim().is_empty() || name.trim() != name {
                return Err(format!("agents: {name:?} isn't a name"));
            }
            for identity in identities {
                if identity.trim().is_empty() {
                    return Err(format!("agents: {name} has an empty identity"));
                }
                if let Some(other) = by_identity.insert(identity.clone(), name.clone()) {
                    return Err(format!("agents: {identity} belongs to both {other} and {name}"));
                }
            }
        }
        Ok(Agents { by_identity })
    }

    pub fn is_empty(&self) -> bool {
        self.by_identity.is_empty()
    }

    /// The agent making a call, from the identity the gateway authenticated. None when the
    /// caller is unknown: no identity, or one the map doesn't list.
    pub fn caller<'a>(&'a self, identity: Option<&'a str>) -> Option<&'a str> {
        let identity = identity.filter(|i| !i.is_empty())?;
        if self.is_empty() {
            return Some(identity);
        }
        self.by_identity.get(identity).map(String::as_str)
    }

    /// The agent a name in a halt's scope means: the brake may name an agent or one of its
    /// identities.
    pub fn named<'a>(&'a self, name: &'a str) -> &'a str {
        self.by_identity.get(name).map(String::as_str).unwrap_or(name)
    }

    /// Whether the map lists this identity, as one of an agent's.
    pub fn knows(&self, identity: &str) -> bool {
        self.by_identity.contains_key(identity)
    }

    /// The name to record an identity under: its agent's, when the map lists it, else the
    /// identity as it is.
    pub fn record<'a>(&'a self, identity: &'a str) -> &'a str {
        self.named(identity)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MAP: &[u8] = br#"{"billing": ["spiffe://wake.local/agent/billing", "0oa1billing"], "research": ["spiffe://wake.local/agent/research"]}"#;

    #[test]
    fn maps_each_identity_to_one_agent() {
        let agents = Agents::from_json(MAP).unwrap();
        assert_eq!(
            agents.caller(Some("spiffe://wake.local/agent/billing")),
            Some("billing")
        );
        assert_eq!(agents.caller(Some("0oa1billing")), Some("billing"));
        assert_eq!(
            agents.caller(Some("spiffe://wake.local/agent/intruder")),
            None,
            "an identity the map doesn't list is no agent"
        );
        assert_eq!(agents.caller(Some("billing")), None, "a bare name isn't an identity");
        assert_eq!(agents.caller(None), None);
        assert_eq!(agents.named("spiffe://wake.local/agent/research"), "research");
        assert_eq!(agents.named("billing"), "billing");
        assert_eq!(
            agents.record("spiffe://wake.local/agent/intruder"),
            "spiffe://wake.local/agent/intruder"
        );
    }

    #[test]
    fn without_a_map_an_identity_is_its_name() {
        let agents = Agents::none();
        assert_eq!(agents.caller(Some("billing")), Some("billing"));
        assert_eq!(agents.caller(Some("")), None);
        assert_eq!(
            agents.named("spiffe://wake.local/agent/billing"),
            "spiffe://wake.local/agent/billing"
        );
    }

    #[test]
    fn refuses_an_identity_under_two_agents() {
        let err = Agents::from_json(br#"{"a": ["x"], "b": ["x"]}"#).unwrap_err();
        assert!(err.contains("both"), "{err}");
        assert!(Agents::from_json(br#"{"a": [""]}"#).is_err());
        assert!(Agents::from_json(br#"{" a": ["x"]}"#).is_err());
        assert!(Agents::from_json(b"[]").is_err());
    }
}
