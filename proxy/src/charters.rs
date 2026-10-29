//! Run charters (Contain): the stop conditions written before a run starts, which the proxy
//! holds the run's calls to. See docs/proxy.md, "Run charters".
//!
//! A charter names a run's calls as a halt names an agent's: by the identities they're made
//! under (an agent, or a sub-agent calling under an identity of its own) and by the tokens
//! issued for the run. Never by anything the caller sends, such as its trace, so a run can't
//! step outside its charter by leaving a header out. A caller nobody can name or place, or
//! one whose token's chain was cut, could be the run's, so a charter naming agents covers
//! it, as an agent halt does (halts.rs). Within that scope:
//! - `never` is Cedar: forbid policies over the Wake schema (SCHEMA). A call one of them
//!   forbids is denied. So is a call one can't be evaluated for: Cedar skips a policy that
//!   errors, and a skipped forbid would let the call through.
//! - `budget` limits the MCP calls the run makes, all of them or those to the tools a limit
//!   names. A call past a limit is denied, and a denied call isn't counted. Counts are kept
//!   in memory, by each proxy for the calls through it.
//! - From `expires_at` on, every call is denied.
//!
//! Charters are kept in the halts' state, so they're on disk and versioned, and the brake
//! confirms a proxy holds one as it does a halt. A call a charter denies is denied under
//! `charter:<id>`.

use std::collections::HashMap;
use std::str::FromStr;
use std::sync::{Arc, LazyLock, Mutex};

use cedar_policy::{
    Authorizer, Context, Decision, Effect, Entities, EntityId, EntityTypeName, EntityUid, Policy, PolicyId, PolicySet,
    Request, Schema, ValidationMode, Validator,
};
use serde::{Deserialize, Serialize};

use crate::halts::{Halt, Scope};

/// The Wake schema a charter's `never` is written over and checked against: the entities and
/// action of the dashboard's Cedar for flow policies, without their context.
pub const SCHEMA: &str = r#"namespace Wake {
  // An agent, under the name the proxy records it by.
  entity Agent;
  // An MCP call's target: a tool name, a resource URI or a prompt name.
  entity Tool;
  // A call through the proxy: an MCP call, whose resource is the Tool it calls, or an A2A
  // request, whose resource is the Agent its route reaches.
  action "call" appliesTo {
    principal: [Agent],
    resource: [Agent, Tool]
  };
}"#;

static WAKE: LazyLock<Schema> =
    LazyLock::new(|| Schema::from_cedarschema_str(SCHEMA).expect("the Wake schema parses").0);

/// The action every call is.
static CALL: LazyLock<EntityUid> = LazyLock::new(|| uid("Wake::Action", "call"));

/// The id of the permit every `never` is evaluated with, so that only its forbids decide.
const PERMIT: &str = "wake-permits-the-rest";

/// The most policies, budget limits, names in scope and tool patterns a charter may hold,
/// and the longest name and `never`.
pub const MAX_POLICIES: usize = 100;
pub const MAX_LIMITS: usize = 32;
pub const MAX_NAMES: usize = 100;
pub const MAX_NAME: usize = 1024;
pub const MAX_NEVER: usize = 64 * 1024;

/// One run's charter, as the brake puts it.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Charter {
    pub id: String,
    /// The run's calls: those made under these agents' or sub-agents' identities, and with
    /// these tokens. Never the fleet.
    pub scope: Scope,
    /// Unix seconds: from then on, every call in scope is denied.
    pub expires_at: u64,
    /// Cedar forbid policies over SCHEMA: what the run must never do.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub never: String,
    /// Limits on the run's MCP calls.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub budget: Vec<Limit>,
    /// When the brake issued it, in Unix nanoseconds.
    pub issued_at: u64,
    /// When this proxy put it in force, in Unix nanoseconds.
    #[serde(default)]
    pub applied_at: u64,
}

impl Charter {
    /// The halt a call this charter denies is denied under.
    pub fn halt(&self) -> Halt {
        Halt {
            id: format!("charter:{}", self.id),
            scope: self.scope.clone(),
            issued_at: self.issued_at,
            applied_at: self.applied_at,
        }
    }

    /// Why the brake can't put it, if it can't: what PUT /v1/charters refuses.
    pub fn check(&self) -> Result<(), String> {
        let names = self.scope.agents.iter().chain(&self.scope.tokens);
        if self.scope.fleet {
            return Err(
                "a charter is one run's: name the agents or sub-agents it calls under, or its tokens, not the fleet"
                    .into(),
            );
        }
        if self.scope.is_empty() {
            return Err("a charter needs a scope: the agents or sub-agents its run calls under, or its tokens".into());
        }
        if self.scope.agents.len() + self.scope.tokens.len() > MAX_NAMES {
            return Err(format!("a charter names at most {MAX_NAMES} agents and tokens"));
        }
        for name in names {
            if name.trim().is_empty() || name.len() > MAX_NAME {
                return Err(format!(
                    "a charter's agents and tokens are 1 to {MAX_NAME} bytes, and not blank"
                ));
            }
        }
        if self.expires_at == 0 {
            return Err("a charter needs expires_at, in Unix seconds".into());
        }
        if self.budget.len() > MAX_LIMITS {
            return Err(format!("a charter's budget has at most {MAX_LIMITS} limits"));
        }
        for limit in &self.budget {
            if limit.tools.len() > MAX_NAMES {
                return Err(format!("a budget limit names at most {MAX_NAMES} tools"));
            }
            for tool in &limit.tools {
                let star = tool.find('*');
                if tool.is_empty() || tool.len() > MAX_NAME || star.is_some_and(|i| i != tool.len() - 1) {
                    return Err(format!(
                        "a budget limit's tools are exact, or a prefix ending in *, of 1 to {MAX_NAME} bytes: {tool:?}"
                    ));
                }
            }
        }
        compile(&self.never).map(|_| ())
    }
}

/// A limit on a run's MCP calls.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Limit {
    /// The tools, resources and prompts it counts, as the proxy records them: exact, or a
    /// prefix ending in `*`. Empty counts every MCP call.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tools: Vec<String>,
    /// How many it allows.
    pub calls: u64,
}

impl Limit {
    fn counts(&self, tool: &str) -> bool {
        self.tools.is_empty()
            || self.tools.iter().any(|p| match p.strip_suffix('*') {
                Some(prefix) => tool.starts_with(prefix),
                None => p == tool,
            })
    }

    fn text(&self) -> String {
        if self.tools.is_empty() {
            format!("{} MCP calls", self.calls)
        } else {
            format!("{} calls to {}", self.calls, self.tools.join(", "))
        }
    }
}

fn uid(kind: &str, id: &str) -> EntityUid {
    EntityUid::from_type_name_and_id(
        EntityTypeName::from_str(kind).expect("a Wake entity type parses"),
        EntityId::new(id),
    )
}

/// A charter's `never`, ready to evaluate: its forbids with a permit for everything else, and
/// each forbid's name, its `@id` annotation or its place in the text (`policy0` on).
pub struct Compiled {
    policies: PolicySet,
    names: HashMap<PolicyId, String>,
}

/// Parses and checks never: Cedar forbid policies, without templates, that the Wake schema
/// validates strictly. A policy that doesn't validate, such as one naming `Wake::Tol`, would
/// match no call and forbid nothing, so it's refused rather than kept.
pub fn compile(never: &str) -> Result<Compiled, String> {
    if never.len() > MAX_NEVER {
        return Err(format!("never is {} bytes, over {MAX_NEVER}", never.len()));
    }
    let mut policies = PolicySet::from_str(never).map_err(|e| format!("never isn't Cedar: {e}"))?;
    if policies.templates().next().is_some() {
        return Err("never can't hold templates: write each policy out".into());
    }
    let mut names = HashMap::new();
    for p in policies.policies() {
        let name = p
            .annotation("id")
            .map(str::to_string)
            .unwrap_or_else(|| p.id().to_string());
        if p.effect() != Effect::Forbid {
            return Err(format!(
                "never holds only forbid policies, and {name} is a permit: what isn't forbidden is allowed already"
            ));
        }
        names.insert(p.id().clone(), name);
    }
    if names.len() > MAX_POLICIES {
        return Err(format!("never holds {} policies, over {MAX_POLICIES}", names.len()));
    }
    let checked = Validator::new(WAKE.clone()).validate(&policies, ValidationMode::Strict);
    if !checked.validation_passed() {
        let errors: Vec<String> = checked.validation_errors().take(5).map(|e| e.to_string()).collect();
        return Err(format!("never doesn't fit the Wake schema: {}", errors.join("; ")));
    }
    let permit = Policy::parse(Some(PolicyId::new(PERMIT)), "permit(principal, action, resource);")
        .map_err(|e| e.to_string())?;
    policies.add(permit).map_err(|e| e.to_string())?;
    Ok(Compiled { policies, names })
}

impl Compiled {
    /// The forbids that deny principal calling resource, by name: none when it may go ahead.
    /// An error when the policies couldn't be evaluated for it.
    pub fn forbids(&self, principal: &str, resource: Resource<'_>) -> Result<Vec<String>, String> {
        let resource = match resource {
            Resource::Tool(tool) => uid("Wake::Tool", tool),
            Resource::Agent(agent) => uid("Wake::Agent", agent),
        };
        let request = Request::new(
            uid("Wake::Agent", principal),
            CALL.clone(),
            resource,
            Context::empty(),
            Some(&WAKE),
        )
        .map_err(|e| e.to_string())?;
        let response = Authorizer::new().is_authorized(&request, &self.policies, &Entities::empty());
        let errors: Vec<String> = response.diagnostics().errors().map(|e| e.to_string()).collect();
        if !errors.is_empty() {
            return Err(errors.join("; "));
        }
        if response.decision() == Decision::Allow {
            return Ok(vec![]);
        }
        let mut names: Vec<String> = response
            .diagnostics()
            .reason()
            .map(|id| self.names.get(id).cloned().unwrap_or_else(|| id.to_string()))
            .collect();
        names.sort();
        Ok(names)
    }
}

/// What a call reaches, as a charter checks it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Resource<'a> {
    /// An MCP tool, resource or prompt, as the proxy records it: checked against never and
    /// counted against the budget. The MCP hook sees it.
    Tool(&'a str),
    /// The agent an A2A route reaches: checked against never. ext_authz sees it.
    Agent(&'a str),
}

/// Why a charter denied a call.
#[derive(Clone, Debug, PartialEq)]
pub struct Denial {
    /// `charter:<id>`, the halt the call is denied under.
    pub halt: Halt,
    /// `expired`, `never`, `budget`, or `unreadable` for a never that can't be compiled.
    pub why: &'static str,
    /// In words: when it expired, the forbids that deny the call, or the limit it's past.
    pub detail: String,
}

/// A never compiled, or why it couldn't be.
type CompiledNever = Arc<Result<Compiled, String>>;

/// What a proxy keeps of each charter beside the state, in memory: its never, compiled once,
/// and what its budget has used.
#[derive(Default)]
pub struct Runtime {
    /// Each charter's never, as the text it was compiled from.
    compiled: Mutex<HashMap<String, (String, CompiledNever)>>,
    used: Mutex<HashMap<String, Vec<u64>>>,
}

impl Runtime {
    fn compiled(&self, c: &Charter) -> CompiledNever {
        let mut cache = self.compiled.lock().unwrap_or_else(|p| p.into_inner());
        if let Some((never, compiled)) = cache.get(&c.id)
            && *never == c.never
        {
            return compiled.clone();
        }
        let compiled = Arc::new(compile(&c.never));
        cache.insert(c.id.clone(), (c.never.clone(), compiled.clone()));
        compiled
    }

    /// Whether the charters whose scope covers a call deny it, made by principal (the
    /// caller's name) to resource, if the hook knows what the call reaches. Charters are
    /// checked in the order they were put, and the first that denies is named. Only a call
    /// none denies is counted, against every limit that counts it.
    pub fn decide(
        &self,
        charters: &[&Charter],
        principal: &str,
        resource: Option<Resource<'_>>,
        now_secs: u64,
    ) -> Option<Denial> {
        if charters.is_empty() {
            return None;
        }
        let mut used = self.used.lock().unwrap_or_else(|p| p.into_inner());
        let mut counted: Vec<(&str, usize)> = Vec::new();
        for c in charters {
            let deny = |why, detail: String| {
                Some(Denial {
                    halt: c.halt(),
                    why,
                    detail,
                })
            };
            if now_secs >= c.expires_at {
                return deny(
                    "expired",
                    format!("the charter expired at {} (Unix seconds)", c.expires_at),
                );
            }
            let compiled = (!c.never.is_empty()).then(|| self.compiled(c));
            if let Some(Err(e)) = compiled.as_deref() {
                return deny(
                    "unreadable",
                    format!("the charter's never can't be read, so every call it covers is denied: {e}"),
                );
            }
            let Some(resource) = resource else { continue };
            if let Some(Ok(compiled)) = compiled.as_deref() {
                match compiled.forbids(principal, resource) {
                    Err(e) => {
                        return deny(
                            "never",
                            format!("a never policy couldn't be evaluated for this call, so it's denied: {e}"),
                        );
                    }
                    Ok(forbids) if !forbids.is_empty() => {
                        return deny("never", format!("forbidden by {}", forbids.join(", ")));
                    }
                    Ok(_) => {}
                }
            }
            let Resource::Tool(tool) = resource else { continue };
            let counts = used.entry(c.id.clone()).or_default();
            counts.resize(c.budget.len(), 0);
            for (i, limit) in c.budget.iter().enumerate() {
                if !limit.counts(tool) {
                    continue;
                }
                if counts[i] >= limit.calls {
                    return deny("budget", format!("its budget of {} is used up", limit.text()));
                }
                counted.push((&c.id, i));
            }
        }
        for (id, i) in counted {
            if let Some(n) = used.get_mut(id).and_then(|counts| counts.get_mut(i)) {
                *n += 1;
            }
        }
        None
    }

    /// What each charter's budget has used, limit by limit.
    pub fn used(&self) -> HashMap<String, Vec<u64>> {
        self.used.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }

    /// Forgets what the proxy kept of a charter that's ended.
    pub fn forget(&self, id: &str) {
        self.compiled.lock().unwrap_or_else(|p| p.into_inner()).remove(id);
        self.used.lock().unwrap_or_else(|p| p.into_inner()).remove(id);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn charter(never: &str, budget: Vec<Limit>) -> Charter {
        Charter {
            id: "refund-4711".into(),
            scope: Scope {
                agents: vec!["support/refunder#toolu_1".into()],
                ..Default::default()
            },
            expires_at: 2_000_000_000,
            never: never.into(),
            budget,
            issued_at: 1,
            applied_at: 2,
        }
    }

    const NOW: u64 = 1_800_000_000;

    #[test]
    fn never_forbids_what_it_names_and_nothing_else() {
        let c = charter(
            r#"@id("no-deletes") forbid(principal, action, resource == Wake::Tool::"delete_customer");
               forbid(principal, action, resource == Wake::Agent::"payments");"#,
            vec![],
        );
        c.check().unwrap();
        let rt = Runtime::default();
        let d = rt
            .decide(&[&c], "support", Some(Resource::Tool("delete_customer")), NOW)
            .unwrap();
        assert_eq!((d.halt.id.as_str(), d.why), ("charter:refund-4711", "never"));
        assert_eq!(d.detail, "forbidden by no-deletes");
        let d = rt
            .decide(&[&c], "support", Some(Resource::Agent("payments")), NOW)
            .unwrap();
        assert_eq!(d.detail, "forbidden by policy1");
        // A tool and an agent of the same name are different things.
        assert_eq!(
            rt.decide(&[&c], "support", Some(Resource::Agent("delete_customer")), NOW),
            None
        );
        assert_eq!(rt.decide(&[&c], "support", Some(Resource::Tool("refund")), NOW), None);
        // With nothing to check never against, only expiry decides.
        assert_eq!(rt.decide(&[&c], "support", None, NOW), None);
    }

    #[test]
    fn never_can_say_which_caller_and_match_tools_by_their_name() {
        let c = charter(
            r#"forbid(principal == Wake::Agent::"support/refunder#toolu_1", action, resource);"#,
            vec![],
        );
        c.check().unwrap();
        let rt = Runtime::default();
        assert!(
            rt.decide(&[&c], "support/refunder#toolu_1", Some(Resource::Tool("refund")), NOW)
                .is_some()
        );
        assert!(
            rt.decide(&[&c], "support", Some(Resource::Tool("refund")), NOW)
                .is_none()
        );
    }

    #[test]
    fn only_forbids_that_fit_the_schema_are_taken() {
        for (never, why) in [
            ("forbid(principal, action, resource", "isn't Cedar"),
            (
                r#"permit(principal, action, resource == Wake::Tool::"refund");"#,
                "is a permit",
            ),
            ("forbid(principal == ?principal, action, resource);", "templates"),
            // A misspelled type would match no call: refused, not kept.
            (
                r#"forbid(principal, action, resource == Wake::Tol::"refund");"#,
                "doesn't fit the Wake schema",
            ),
            (
                r#"forbid(principal, action == Wake::Action::"read", resource);"#,
                "doesn't fit the Wake schema",
            ),
            (
                r#"forbid(principal, action, resource) when { context.origins.isEmpty() };"#,
                "doesn't fit the Wake schema",
            ),
        ] {
            let err = charter(never, vec![]).check().unwrap_err();
            assert!(err.contains(why), "{never}: {err}");
        }
        let many = "forbid(principal, action, resource);\n".repeat(MAX_POLICIES + 1);
        assert!(charter(&many, vec![]).check().unwrap_err().contains("over 100"));
        assert!(
            charter(&" ".repeat(MAX_NEVER + 1), vec![])
                .check()
                .unwrap_err()
                .contains("bytes")
        );
        // No never at all is a charter of expiry and budget alone.
        charter("", vec![]).check().unwrap();
    }

    #[test]
    fn a_charter_is_one_runs() {
        let mut c = charter("", vec![]);
        c.scope = Scope {
            fleet: true,
            ..Default::default()
        };
        assert!(c.check().unwrap_err().contains("not the fleet"));
        c.scope = Scope::default();
        assert!(c.check().unwrap_err().contains("needs a scope"));
        c.scope.tokens = vec![" ".into()];
        assert!(c.check().unwrap_err().contains("not blank"));
        let mut c = charter("", vec![]);
        c.expires_at = 0;
        assert!(c.check().unwrap_err().contains("expires_at"));
        let c = charter(
            "",
            vec![Limit {
                tools: vec!["re*fund".into()],
                calls: 1,
            }],
        );
        assert!(c.check().unwrap_err().contains("prefix ending in *"));
    }

    #[test]
    fn a_budget_counts_only_calls_that_go_ahead() {
        let c = charter(
            r#"forbid(principal, action, resource == Wake::Tool::"wire");"#,
            vec![
                Limit {
                    tools: vec!["refund".into()],
                    calls: 1,
                },
                Limit {
                    tools: vec![],
                    calls: 3,
                },
            ],
        );
        c.check().unwrap();
        let rt = Runtime::default();
        let call = |tool| rt.decide(&[&c], "support", Some(Resource::Tool(tool)), NOW);
        assert_eq!(call("refund"), None);
        // The second refund is past its limit, and isn't counted against the rest.
        let d = call("refund").unwrap();
        assert_eq!(
            (d.why, d.detail.as_str()),
            ("budget", "its budget of 1 calls to refund is used up")
        );
        // A forbidden call isn't counted either.
        assert_eq!(call("wire").unwrap().why, "never");
        assert_eq!(rt.used()["refund-4711"], vec![1, 1]);
        assert_eq!(call("lookup"), None);
        assert_eq!(call("lookup_*"), None);
        let d = call("lookup").unwrap();
        assert_eq!(d.detail, "its budget of 3 MCP calls is used up");
        assert_eq!(rt.used()["refund-4711"], vec![1, 3]);
        // An A2A request isn't counted: only MCP calls are.
        assert_eq!(rt.decide(&[&c], "support", Some(Resource::Agent("billing")), NOW), None);

        // A call two charters cover is counted by both only when neither denies it.
        let mut other = charter(
            "",
            vec![Limit {
                tools: vec![],
                calls: 1,
            }],
        );
        other.id = "other".into();
        let rt = Runtime::default();
        assert_eq!(
            rt.decide(&[&other, &c], "support", Some(Resource::Tool("refund")), NOW),
            None
        );
        assert_eq!(
            rt.decide(&[&other, &c], "support", Some(Resource::Tool("refund")), NOW)
                .unwrap()
                .halt
                .id,
            "charter:other"
        );
        assert_eq!(rt.used()["refund-4711"], vec![1, 1]);
        rt.forget("other");
        assert!(!rt.used().contains_key("other"));
    }

    #[test]
    fn an_expired_charter_denies_everything_it_covers() {
        let c = charter("", vec![]);
        let rt = Runtime::default();
        assert_eq!(rt.decide(&[&c], "support", None, c.expires_at - 1), None);
        for resource in [None, Some(Resource::Tool("lookup")), Some(Resource::Agent("billing"))] {
            let d = rt.decide(&[&c], "support", resource, c.expires_at).unwrap();
            assert_eq!(d.why, "expired");
        }
    }

    #[test]
    fn a_never_that_cant_be_read_denies_everything_it_covers() {
        // Only a state file edited by hand can hold one: PUT refuses it.
        let c = charter("forbid(", vec![]);
        let d = Runtime::default().decide(&[&c], "support", None, NOW).unwrap();
        assert_eq!(d.why, "unreadable");
    }

    #[test]
    fn names_that_arent_cedar_identifiers_still_match() {
        let c = charter(
            r#"forbid(principal, action, resource == Wake::Tool::"file:///etc/passwd");
               forbid(principal, action, resource == Wake::Tool::"weird \"quoted\" tool");"#,
            vec![],
        );
        c.check().unwrap();
        let rt = Runtime::default();
        assert!(
            rt.decide(&[&c], "sp\"iffe://x", Some(Resource::Tool("file:///etc/passwd")), NOW)
                .is_some()
        );
        assert!(
            rt.decide(&[&c], "x", Some(Resource::Tool(r#"weird "quoted" tool"#)), NOW)
                .is_some()
        );
        assert!(
            rt.decide(&[&c], "x", Some(Resource::Tool("file:///etc/hosts")), NOW)
                .is_none()
        );
    }
}
