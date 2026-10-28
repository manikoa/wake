//! Origin flow policies (Contain): rules that keep content from untrusted origins, such as a
//! web page a tool fetched or another agent's message, from triggering privileged calls.
//! See docs/proxy.md for the file and what each rule means.
//!
//! A rule decides on what entered the caller through the proxy within the policy's window
//! (origins.rs), never on anything the caller sends: an agent can't leave a header out, or
//! send one, to get past a rule. Rules that name MCP tools are checked by the MCP hook,
//! which sees the tool; the rest by ext_authz, on every request.
//!
//! Like `Halts`, a policy fails closed: a `--policy-file` that can't be read, or isn't a
//! valid policy, denies every call under `policy:unreadable-policy` until it's fixed. The
//! file is read again when it changes.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};
use std::time::Duration;

use serde::Deserialize;

use crate::agents::Agents;
use crate::halts::{Halt, Scope};
use crate::origins::{MAX_WINDOW_SECS, Observed, Seen, Who};

/// The halt id a policy that can't be read denies under.
pub const UNREADABLE: &str = "policy:unreadable-policy";
/// The window when a policy doesn't set one, in seconds.
pub const DEFAULT_WINDOW_SECS: u64 = 900;
/// How often the policy file is checked for changes.
pub const RELOAD_EVERY: Duration = Duration::from_secs(2);

/// One rule: the calls it targets, and the origins it keeps from them.
#[derive(Clone, Debug, Default, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FlowRule {
    pub id: String,
    /// The calling agents it applies to, by name or identity. Empty means every caller.
    #[serde(default)]
    pub callers: Vec<String>,
    /// The agents an A2A route reaches that it applies to. Empty means every receiver.
    #[serde(default)]
    pub receivers: Vec<String>,
    /// The request paths it applies to, by prefix. Empty means every path.
    #[serde(default)]
    pub path_prefixes: Vec<String>,
    /// The MCP tools, resources and prompts it applies to, as the proxy records them: a
    /// tool's or prompt's name, a resource's URI. Exact, or a prefix ending in `*`.
    #[serde(default)]
    pub tools: Vec<String>,
    /// Origins that may not be behind a call it targets: denied when any origin that
    /// entered the caller matches one. Exact, or a prefix ending in `*`.
    #[serde(default)]
    pub forbid_origins: Vec<String>,
    /// The only origins that may be behind a call it targets: denied when an origin that
    /// entered the caller matches none. A caller nothing entered passes.
    #[serde(default)]
    pub allow_origins: Vec<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct File {
    #[serde(default = "default_window")]
    window_seconds: u64,
    #[serde(default)]
    rules: Vec<FlowRule>,
}

fn default_window() -> u64 {
    DEFAULT_WINDOW_SECS
}

/// A parsed policy: its rules, in the order they're checked, and its window.
#[derive(Clone, Debug, PartialEq)]
pub struct FlowPolicy {
    pub rules: Vec<FlowRule>,
    pub window_seconds: u64,
    /// Why the file couldn't be used, when it couldn't: every call is then denied.
    pub unreadable: Option<String>,
}

impl Default for FlowPolicy {
    fn default() -> Self {
        FlowPolicy {
            rules: vec![],
            window_seconds: DEFAULT_WINDOW_SECS,
            unreadable: None,
        }
    }
}

/// What a call is, as a rule targets it.
#[derive(Clone, Copy, Debug)]
pub struct Target<'a> {
    pub caller: Who<'a>,
    /// The agent an A2A route reaches, when there is one.
    pub receiver: Option<&'a str>,
    /// The request path, at ext_authz.
    pub path: Option<&'a str>,
    /// The MCP tool, resource or prompt called, at the MCP hook.
    pub tool: Option<&'a str>,
}

/// Where a call is checked: ext_authz checks the rules that name no tools, and the MCP
/// hook those that do.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum At {
    Authz,
    Mcp,
}

/// A rule denying a call: the halt it's denied under, and why.
#[derive(Clone, Debug, PartialEq)]
pub struct Denial {
    pub halt: Halt,
    /// The origins behind the call the rule keeps from it, or `*` for every origin.
    pub origins: Vec<String>,
    /// Why the caller counts as having every origin, when it does.
    pub every: Option<&'static str>,
}

impl FlowPolicy {
    /// No rules: nothing is denied.
    pub fn empty() -> Self {
        FlowPolicy::default()
    }

    /// Reads a policy from JSON. Any problem makes it unusable.
    pub fn parse(bytes: &[u8]) -> Result<Self, String> {
        // serde would also read a struct from an array, by position: a policy and its rules
        // are objects, so every field is named.
        let value: serde_json::Value = serde_json::from_slice(bytes).map_err(|e| e.to_string())?;
        let rules = value.get("rules").and_then(|r| r.as_array());
        if !value.is_object() || rules.is_some_and(|r| r.iter().any(|rule| !rule.is_object())) {
            return Err("a policy and each of its rules must be a JSON object".into());
        }
        // Read from the bytes, not the value, which keeps only the last of a field given
        // twice: which one the writer meant isn't known, so it's refused.
        let file: File = serde_json::from_slice(bytes).map_err(|e| e.to_string())?;
        if !(1..=MAX_WINDOW_SECS).contains(&file.window_seconds) {
            return Err(format!(
                "window_seconds must be from 1 to {MAX_WINDOW_SECS}, not {}",
                file.window_seconds
            ));
        }
        let mut ids = HashSet::new();
        for rule in &file.rules {
            rule.validate()?;
            if !ids.insert(rule.id.as_str()) {
                return Err(format!("two rules are {:?}", rule.id));
            }
        }
        Ok(FlowPolicy {
            rules: file.rules,
            window_seconds: file.window_seconds,
            unreadable: None,
        })
    }

    /// Loads a policy file. One that can't be read or isn't valid denies every call.
    pub fn load(path: &Path) -> Self {
        match std::fs::read(path)
            .map_err(|e| e.to_string())
            .and_then(|bytes| Self::parse(&bytes))
        {
            Ok(policy) => policy,
            Err(error) => Self::unusable(path, &error),
        }
    }

    fn unusable(path: &Path, error: &str) -> Self {
        tracing::error!(
            path = %path.display(),
            error,
            "can't use the flow policy; denying every call until it's fixed"
        );
        FlowPolicy {
            unreadable: Some(error.to_string()),
            ..Default::default()
        }
    }

    /// The first rule checked at at that denies the call, given what entered its caller.
    pub fn deciding(&self, at: At, target: &Target<'_>, seen: &Seen, agents: &Agents) -> Option<Denial> {
        if self.unreadable.is_some() {
            return (at == At::Authz).then(|| Denial {
                halt: halt(UNREADABLE.into(), true, None),
                origins: vec![],
                every: None,
            });
        }
        let caller = match target.caller {
            Who::Agent(name) | Who::Unlisted(name) => Some(name),
            Who::Nobody => None,
        };
        for rule in &self.rules {
            // The MCP hook checks the rules that name tools, and ext_authz the rest.
            let names_tools = !rule.tools.is_empty();
            if (at == At::Mcp) != names_tools || !rule.targets(target, agents) {
                continue;
            }
            let (origins, every) = match seen {
                Seen::Every(why) => (vec!["*".to_string()], Some(*why)),
                Seen::These(origins) => {
                    let kept: Vec<String> = origins
                        .iter()
                        .filter(|o| {
                            rule.forbid_origins.iter().any(|p| matches(p, o))
                                || (!rule.allow_origins.is_empty() && !rule.allow_origins.iter().any(|p| matches(p, o)))
                        })
                        .cloned()
                        .collect();
                    if kept.is_empty() {
                        continue;
                    }
                    (kept, None)
                }
            };
            return Some(Denial {
                halt: halt(format!("policy:{}", rule.id), false, caller),
                origins,
                every,
            });
        }
        None
    }
}

fn halt(id: String, fleet: bool, caller: Option<&str>) -> Halt {
    Halt {
        id,
        scope: Scope {
            fleet,
            agents: caller.iter().map(|c| c.to_string()).collect(),
            tokens: vec![],
        },
        issued_at: 0,
        applied_at: 0,
    }
}

impl FlowRule {
    fn validate(&self) -> Result<(), String> {
        let id = &self.id;
        if id.is_empty()
            || id.len() > 64
            || !id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'-'))
        {
            return Err(format!("rule id {id:?} must be 1 to 64 of A-Z a-z 0-9 . _ : -"));
        }
        if self.forbid_origins.is_empty() && self.allow_origins.is_empty() {
            return Err(format!(
                "rule {id} keeps nothing: give it forbid_origins or allow_origins"
            ));
        }
        if !self.tools.is_empty() && (!self.receivers.is_empty() || !self.path_prefixes.is_empty()) {
            return Err(format!(
                "rule {id} names tools with receivers or path_prefixes: the MCP hook, which sees the tool, sees neither"
            ));
        }
        let lists = [
            ("callers", &self.callers, false),
            ("receivers", &self.receivers, false),
            ("path_prefixes", &self.path_prefixes, false),
            ("tools", &self.tools, true),
            ("forbid_origins", &self.forbid_origins, true),
            ("allow_origins", &self.allow_origins, true),
        ];
        for (name, list, patterns) in lists {
            for entry in list.iter() {
                let wildcard = entry.find('*');
                if entry.is_empty()
                    || (!patterns && wildcard.is_some())
                    || wildcard.is_some_and(|i| i != entry.len() - 1)
                {
                    let allowed = if patterns { ", or a prefix ending in *" } else { "" };
                    return Err(format!("rule {id}: {name} entry {entry:?} must be exact{allowed}"));
                }
            }
        }
        Ok(())
    }

    fn targets(&self, target: &Target<'_>, agents: &Agents) -> bool {
        let callers = match target.caller {
            // A caller the identity map doesn't name could be any agent.
            Who::Unlisted(_) | Who::Nobody => true,
            Who::Agent(name) => self.callers.is_empty() || self.callers.iter().any(|c| agents.named(c) == name),
        };
        let receivers = self.receivers.is_empty()
            || target
                .receiver
                .filter(|r| !r.is_empty())
                .is_some_and(|r| self.receivers.iter().any(|x| agents.named(x) == agents.named(r)));
        let paths = self.path_prefixes.is_empty()
            || target
                .path
                .is_some_and(|p| self.path_prefixes.iter().any(|x| p.starts_with(x.as_str())));
        let tools = self.tools.is_empty() || target.tool.is_some_and(|t| self.tools.iter().any(|x| matches(x, t)));
        callers && receivers && paths && tools
    }
}

fn matches(pattern: &str, value: &str) -> bool {
    match pattern.strip_suffix('*') {
        Some(prefix) => value.starts_with(prefix),
        None => value == pattern,
    }
}

/// The policy in force, read again from its file when that changes.
pub struct Policy {
    path: Option<PathBuf>,
    current: RwLock<Arc<FlowPolicy>>,
    stamp: RwLock<Option<[u8; 32]>>,
}

impl Policy {
    /// A policy that never changes.
    pub fn fixed(policy: FlowPolicy) -> Self {
        Policy {
            path: None,
            current: RwLock::new(Arc::new(policy)),
            stamp: RwLock::new(None),
        }
    }

    /// The policy in path, as it is now.
    pub fn from_file(path: &Path) -> Self {
        let policy = Policy {
            path: Some(path.to_path_buf()),
            current: RwLock::new(Arc::new(FlowPolicy::empty())),
            stamp: RwLock::new(None),
        };
        *policy.current.write().unwrap_or_else(|e| e.into_inner()) = Arc::new(FlowPolicy::load(path));
        *policy.stamp.write().unwrap_or_else(|e| e.into_inner()) = stamp(path);
        policy
    }

    /// Whether calls are checked at all: a policy file is set, which may gain rules, or
    /// rules were given. Otherwise the hooks note nothing, since nothing will ever decide on
    /// it.
    pub fn enforcing(&self) -> bool {
        self.path.is_some() || !self.current().rules.is_empty()
    }

    /// The rules in force now.
    pub fn current(&self) -> Arc<FlowPolicy> {
        self.current.read().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// Reads the file again if it changed since it was last read, and returns whether it
    /// did. The window observed takes the new one first, so no check sees a longer window
    /// than what was kept.
    pub fn reload(&self, observed: &Observed, at: u64) -> bool {
        let Some(path) = &self.path else { return false };
        let now = stamp(path);
        if *self.stamp.read().unwrap_or_else(|e| e.into_inner()) == now {
            return false;
        }
        let policy = FlowPolicy::load(path);
        observed.set_window(at, policy.window_seconds);
        tracing::info!(
            path = %path.display(),
            rules = policy.rules.len(),
            window_seconds = policy.window_seconds,
            usable = policy.unreadable.is_none(),
            "flow policy read"
        );
        *self.current.write().unwrap_or_else(|e| e.into_inner()) = Arc::new(policy);
        *self.stamp.write().unwrap_or_else(|e| e.into_inner()) = now;
        true
    }

    /// Checks the file for changes every RELOAD_EVERY until shutdown resolves.
    pub async fn watch(self: Arc<Self>, observed: Arc<Observed>, shutdown: impl std::future::Future<Output = ()>) {
        if self.path.is_none() {
            return;
        }
        tokio::pin!(shutdown);
        let mut tick = tokio::time::interval(RELOAD_EVERY);
        loop {
            tokio::select! {
                _ = &mut shutdown => return,
                _ = tick.tick() => {
                    self.reload(&observed, crate::halts::now());
                }
            }
        }
    }
}

/// A digest of what the file holds, so any edit is read again; None when it can't be read.
fn stamp(path: &Path) -> Option<[u8; 32]> {
    use sha2::{Digest, Sha256};
    std::fs::read(path).ok().map(|bytes| Sha256::digest(&bytes).into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn policy(json: &str) -> FlowPolicy {
        FlowPolicy::parse(json.as_bytes()).unwrap()
    }

    fn mcp(caller: &str, tool: &str) -> Target<'static> {
        Target {
            caller: Who::Agent(Box::leak(caller.to_string().into_boxed_str())),
            receiver: None,
            path: None,
            tool: Some(Box::leak(tool.to_string().into_boxed_str())),
        }
    }

    fn a2a(caller: &str, receiver: &str) -> Target<'static> {
        Target {
            caller: Who::Agent(Box::leak(caller.to_string().into_boxed_str())),
            receiver: Some(Box::leak(receiver.to_string().into_boxed_str())),
            path: Some("/a2a"),
            tool: None,
        }
    }

    fn seen(origins: &[&str]) -> Seen {
        Seen::These(origins.iter().map(|o| o.to_string()).collect())
    }

    const PAY: &str = r#"{"window_seconds": 600, "rules": [
        {"id": "no-pay-after-web", "callers": ["billing"], "tools": ["pay", "wire_*"],
         "forbid_origins": ["tool:fetch_page", "agent:*"]},
        {"id": "billing-hears-support-only", "receivers": ["billing"], "allow_origins": ["agent:support", "tool:search"]}
    ]}"#;

    #[test]
    fn forbids_what_entered_the_caller_from_a_tool_it_names() {
        let p = policy(PAY);
        assert_eq!(p.window_seconds, 600);
        let none = Agents::none();
        // Nothing untrusted entered billing: it may pay.
        assert_eq!(
            p.deciding(At::Mcp, &mcp("billing", "pay"), &seen(&["tool:search"]), &none),
            None
        );
        // A fetched page entered it: no paying, and no wiring.
        for tool in ["pay", "wire_eur"] {
            let d = p
                .deciding(
                    At::Mcp,
                    &mcp("billing", tool),
                    &seen(&["tool:search", "tool:fetch_page"]),
                    &none,
                )
                .unwrap();
            assert_eq!(d.halt.id, "policy:no-pay-after-web");
            assert_eq!(d.halt.scope.agents, vec!["billing".to_string()]);
            assert_eq!(d.origins, vec!["tool:fetch_page".to_string()]);
        }
        // Any one of the forbidden origins is enough.
        assert!(
            p.deciding(At::Mcp, &mcp("billing", "pay"), &seen(&["agent:research"]), &none)
                .is_some()
        );
        // Other tools, and other callers, aren't covered by this rule.
        assert_eq!(
            p.deciding(At::Mcp, &mcp("billing", "lookup"), &seen(&["tool:fetch_page"]), &none),
            None
        );
        assert_eq!(
            p.deciding(At::Mcp, &mcp("support", "pay"), &seen(&["tool:fetch_page"]), &none),
            None
        );
        // A rule that names tools isn't checked at ext_authz, which doesn't see them.
        assert_eq!(
            p.deciding(At::Authz, &mcp("billing", "pay"), &seen(&["tool:fetch_page"]), &none),
            None
        );
    }

    #[test]
    fn an_allowlist_denies_any_origin_it_doesnt_name() {
        let p = policy(PAY);
        let none = Agents::none();
        // A message to billing from a caller only support and search fed is fine.
        assert_eq!(
            p.deciding(At::Authz, &a2a("planner", "billing"), &seen(&["agent:support"]), &none),
            None
        );
        // Nothing entered at all: fine.
        assert_eq!(
            p.deciding(At::Authz, &a2a("planner", "billing"), &seen(&[]), &none),
            None
        );
        // One origin it doesn't name is enough to deny, and the denial says which.
        let d = p
            .deciding(
                At::Authz,
                &a2a("planner", "billing"),
                &seen(&["agent:support", "tool:fetch_page"]),
                &none,
            )
            .unwrap();
        assert_eq!(d.halt.id, "policy:billing-hears-support-only");
        assert_eq!(d.origins, vec!["tool:fetch_page".to_string()]);
        // The rule names a receiver: a call to another agent isn't covered.
        assert_eq!(
            p.deciding(
                At::Authz,
                &a2a("planner", "research"),
                &seen(&["tool:fetch_page"]),
                &none
            ),
            None
        );
    }

    #[test]
    fn where_it_cant_know_it_assumes_every_origin() {
        let p = policy(PAY);
        let none = Agents::none();
        let every = Seen::Every("wake-proxy hasn't watched for a whole window yet");
        let d = p.deciding(At::Mcp, &mcp("billing", "pay"), &every, &none).unwrap();
        assert_eq!(d.origins, vec!["*".to_string()]);
        assert_eq!(d.every, Some("wake-proxy hasn't watched for a whole window yet"));
        // A call the rules don't target is still allowed.
        assert_eq!(p.deciding(At::Mcp, &mcp("billing", "lookup"), &every, &none), None);
    }

    #[test]
    fn a_caller_the_map_doesnt_name_could_be_any_agent() {
        let p = policy(PAY);
        let agents = Agents::from_json(br#"{"billing": ["spiffe://wake.local/agent/billing"]}"#).unwrap();
        let unlisted = Target {
            caller: Who::Unlisted("spiffe://wake.local/agent/intruder"),
            receiver: None,
            path: None,
            tool: Some("pay"),
        };
        let d = p
            .deciding(At::Mcp, &unlisted, &seen(&["tool:fetch_page"]), &agents)
            .unwrap();
        assert_eq!(
            d.halt.scope.agents,
            vec!["spiffe://wake.local/agent/intruder".to_string()]
        );
        let nobody = Target {
            caller: Who::Nobody,
            ..unlisted
        };
        assert!(
            p.deciding(At::Mcp, &nobody, &Seen::Every("the caller has no identity"), &agents)
                .is_some()
        );
    }

    #[test]
    fn rules_name_agents_by_name_or_identity() {
        let agents = Agents::from_json(br#"{"billing": ["spiffe://wake.local/agent/billing"]}"#).unwrap();
        let by_identity = policy(
            r#"{"rules": [{"id": "r", "callers": ["spiffe://wake.local/agent/billing"], "tools": ["pay"], "forbid_origins": ["tool:*"]}]}"#,
        );
        assert!(
            by_identity
                .deciding(At::Mcp, &mcp("billing", "pay"), &seen(&["tool:fetch_page"]), &agents)
                .is_some()
        );
    }

    #[test]
    fn refuses_a_policy_it_cant_be_sure_of() {
        for (bad, why) in [
            (
                r#"{"rules": [{"id": "r", "forbid_origin": ["tool:x"]}]}"#,
                "unknown field",
            ),
            (r#"{"rule": []}"#, "unknown field"),
            (
                r#"{"rules": [{"id": "r", "require_origins": ["user"]}]}"#,
                "unknown field",
            ),
            (r#"{"rules": [{"id": "r"}]}"#, "keeps nothing"),
            (r#"{"rules": [{"id": "", "forbid_origins": ["x"]}]}"#, "must be 1 to 64"),
            (
                r#"{"rules": [{"id": "a b", "forbid_origins": ["x"]}]}"#,
                "must be 1 to 64",
            ),
            (
                r#"{"rules": [{"id": "r", "forbid_origins": ["x"]}, {"id": "r", "allow_origins": ["y"]}]}"#,
                "two rules",
            ),
            (
                r#"{"rules": [{"id": "r", "forbid_origins": ["tool:*:x"]}]}"#,
                "must be exact",
            ),
            (r#"{"rules": [{"id": "r", "forbid_origins": [""]}]}"#, "must be exact"),
            (
                r#"{"rules": [{"id": "r", "callers": ["bill*"], "forbid_origins": ["x"]}]}"#,
                "must be exact",
            ),
            (
                r#"{"rules": [{"id": "r", "tools": ["pay"], "receivers": ["b"], "forbid_origins": ["x"]}]}"#,
                "sees neither",
            ),
            (r#"{"window_seconds": 0, "rules": []}"#, "window_seconds"),
            (r#"{"window_seconds": 86401, "rules": []}"#, "window_seconds"),
            ("[]", "must be a JSON object"),
            (r#"{"window_seconds": 60, "window_seconds": 900}"#, "duplicate field"),
            (
                r#"{"rules": [{"id": "r", "forbid_origins": ["x"], "forbid_origins": []}]}"#,
                "duplicate field",
            ),
            (
                r#"{"rules": [["r", [], [], [], [], ["x"], []]]}"#,
                "must be a JSON object",
            ),
            ("{", "EOF"),
        ] {
            let err = FlowPolicy::parse(bad.as_bytes()).unwrap_err();
            assert!(err.contains(why), "{bad}: {err}");
        }
        assert_eq!(policy("{}"), FlowPolicy::empty());
    }

    #[test]
    fn a_policy_it_cant_use_denies_every_call() {
        let missing = std::env::temp_dir().join("wake-nonexistent-policy.json");
        let p = FlowPolicy::load(&missing);
        let d = p
            .deciding(At::Authz, &a2a("support", "research"), &seen(&[]), &Agents::none())
            .unwrap();
        assert_eq!(d.halt.id, UNREADABLE);
        assert!(d.halt.scope.fleet);
    }

    #[test]
    fn reads_the_file_again_when_it_changes() {
        let dir = std::env::temp_dir().join(format!("wake-policy-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("policy.json");
        std::fs::write(&path, r#"{"rules": []}"#).unwrap();
        let observed = Observed::starting(0, DEFAULT_WINDOW_SECS);
        let p = Policy::from_file(&path);
        assert!(p.current().rules.is_empty());
        assert!(!p.reload(&observed, 1), "unchanged");
        std::fs::write(&path, PAY).unwrap();
        assert!(p.reload(&observed, 2));
        assert_eq!(p.current().rules.len(), 2);
        // A broken edit fails closed, and fixing it lifts that.
        std::fs::write(&path, "{").unwrap();
        assert!(p.reload(&observed, 3));
        assert!(p.current().unreadable.is_some());
        std::fs::write(&path, PAY).unwrap();
        assert!(p.reload(&observed, 4));
        assert!(p.current().unreadable.is_none());
        // A file that's gone fails closed too.
        std::fs::remove_file(&path).unwrap();
        assert!(p.reload(&observed, 5));
        assert!(p.current().unreadable.is_some());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    proptest! {
        #[test]
        fn parsing_any_bytes_never_panics(data in proptest::collection::vec(any::<u8>(), 0..512)) {
            let _ = FlowPolicy::parse(&data);
        }

        #[test]
        fn a_forbidden_prefix_always_denies_a_matching_origin(
            prefix in "[a-z]{1,12}:",
            suffix in "[a-z0-9_-]{1,20}",
            caller in "[a-z]{1,10}",
            tool in "[a-z_]{1,10}",
            others in proptest::collection::vec("[a-z]{1,8}:[a-z]{1,8}", 0..8),
        ) {
            let p = policy(&format!(
                r#"{{"rules": [{{"id": "fuzz-forbid", "callers": ["{caller}"], "tools": ["{tool}"], "forbid_origins": ["{prefix}*"]}}]}}"#
            ));
            let forbidden = format!("{prefix}{suffix}");
            let mut origins = others.clone();
            origins.push(forbidden.clone());
            let seen = Seen::These(origins);
            let d = p.deciding(At::Mcp, &mcp(&caller, &tool), &seen, &Agents::none());
            prop_assert!(d.is_some());
            let d = d.unwrap();
            prop_assert_eq!(d.halt.id.as_str(), "policy:fuzz-forbid");
            prop_assert!(d.origins.contains(&forbidden));
        }

        #[test]
        fn an_allowlist_never_denies_only_what_it_names(
            allowed in proptest::collection::vec("[a-z]{1,8}:[a-z]{1,8}", 1..6),
            picks in proptest::collection::vec(any::<prop::sample::Index>(), 0..6),
        ) {
            let list = allowed.iter().map(|a| format!("{a:?}")).collect::<Vec<_>>().join(",");
            let p = policy(&format!(r#"{{"rules": [{{"id": "allow", "allow_origins": [{list}]}}]}}"#));
            let origins: Vec<String> = picks.iter().map(|i| allowed[i.index(allowed.len())].clone()).collect();
            prop_assert_eq!(
                p.deciding(At::Authz, &a2a("a", "b"), &Seen::These(origins), &Agents::none()),
                None
            );
        }
    }
}
