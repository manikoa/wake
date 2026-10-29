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
//!   errors, and a skipped forbid would let the call through. The caller is `in` each agent
//!   its call is made under, so `principal in Wake::Agent::"support"` covers support's
//!   sub-agents. A caller nobody can name or place is held to a policy if it could be any
//!   agent the charter names.
//! - `budget` limits the MCP calls the run makes, all of them or those to the tools a limit
//!   names. A call past a limit is denied, and a denied call isn't counted. Each proxy
//!   counts the calls through it, and writes its counts beside the state once a second, so a
//!   restart counts again at most a second's calls. A caller nobody can name or place isn't
//!   counted, so one outside the run can't use the run's budget up.
//! - From `expires_at` on, every call is denied.
//!
//! Charters are kept in the halts' state, so they're on disk and versioned, and the brake
//! confirms a proxy holds one as it does a halt. A call a charter denies is denied under
//! `charter:<id>`.

use std::collections::{HashMap, HashSet};
use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, LazyLock, Mutex};

use cedar_policy::{
    Authorizer, Context, Decision, Effect, Entities, Entity, EntityId, EntityTypeName, EntityUid, Policy, PolicyId,
    PolicySet, Request, Schema, ValidationMode, Validator,
};
use serde::{Deserialize, Serialize};

use crate::halts::{Halt, Scope};

/// The Wake schema a charter's `never` is written over and checked against: the entities and
/// action of the dashboard's Cedar for flow policies, without their context.
pub const SCHEMA: &str = r#"namespace Wake {
  // An agent, under the name the proxy records it by. A caller is in each agent its call
  // is made under: a sub-agent is in the agent it acts for.
  entity Agent in [Agent];
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
pub const MAX_NEVER: usize = 16 * 1024;
/// The deepest a `never` may nest brackets and if-then-else. Cedar's parser recurses once
/// per level, and a couple of hundred levels overflow a hook's stack, aborting the proxy.
pub const MAX_NESTING: usize = 32;
/// The most charters a proxy holds at once. Each is in the state the brake reads back after
/// every change, so they're bounded with it.
pub const MAX_CHARTERS: usize = 256;
/// The latest `expires_at` taken, in Unix seconds (in the year 2286): a later one is in
/// milliseconds or nanoseconds by mistake, and would never come.
pub const MAX_EXPIRES_AT: u64 = 10_000_000_000;

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
        if self.expires_at > MAX_EXPIRES_AT {
            return Err(format!(
                "expires_at {} is past the year 2286: it's in Unix seconds, not milliseconds",
                self.expires_at
            ));
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
    // Checked before Cedar reads it: past the stack, Cedar's parser doesn't fail, it aborts.
    let deepest = nesting(never);
    if deepest > MAX_NESTING {
        return Err(format!(
            "never nests {deepest} levels of brackets and if-then-else, over {MAX_NESTING}"
        ));
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

/// How deep never nests: its parentheses, brackets and braces, and its if-then-else, outside
/// its strings and comments.
fn nesting(never: &str) -> usize {
    let text = never.as_bytes();
    let (mut depth, mut deepest, mut i) = (0usize, 0usize, 0usize);
    while i < text.len() {
        match text[i] {
            b'"' => {
                i += 1;
                while i < text.len() && text[i] != b'"' {
                    i += if text[i] == b'\\' { 2 } else { 1 };
                }
            }
            b'/' if text.get(i + 1) == Some(&b'/') => {
                while i < text.len() && text[i] != b'\n' {
                    i += 1;
                }
            }
            b'(' | b'[' | b'{' => {
                depth += 1;
                deepest = deepest.max(depth);
            }
            b')' | b']' | b'}' => depth = depth.saturating_sub(1),
            c if c.is_ascii_alphabetic() || c == b'_' => {
                let start = i;
                while i < text.len() && (text[i].is_ascii_alphanumeric() || text[i] == b'_') {
                    i += 1;
                }
                match &text[start..i] {
                    b"if" => {
                        depth += 1;
                        deepest = deepest.max(depth);
                    }
                    b"else" => depth = depth.saturating_sub(1),
                    _ => {}
                }
                continue;
            }
            _ => {}
        }
        i += 1;
    }
    deepest
}

impl Compiled {
    /// The forbids that deny principal, made under the agents in under, calling resource, by
    /// name: none when it may go ahead. An error when the policies couldn't be evaluated for it.
    pub fn forbids(&self, principal: &str, under: &[&str], resource: Resource<'_>) -> Result<Vec<String>, String> {
        let principal = uid("Wake::Agent", principal);
        let parents: HashSet<EntityUid> = under
            .iter()
            .map(|a| uid("Wake::Agent", a))
            .filter(|a| *a != principal)
            .collect();
        let entities = Entities::from_entities([Entity::new_no_attrs(principal.clone(), parents)], Some(&WAKE))
            .map_err(|e| e.to_string())?;
        let resource = match resource {
            Resource::Tool(tool) => uid("Wake::Tool", tool),
            Resource::Agent(agent) => uid("Wake::Agent", agent),
        };
        let request = Request::new(principal, CALL.clone(), resource, Context::empty(), Some(&WAKE))
            .map_err(|e| e.to_string())?;
        let response = Authorizer::new().is_authorized(&request, &self.policies, &entities);
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

/// Who makes a call, as a charter holds it.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Caller<'a> {
    /// The caller's name, as the proxy records it, or `unknown` without one.
    pub name: &'a str,
    /// The agents the call is made under, by their names (Token::under): the caller is `in`
    /// each of them.
    pub under: Vec<&'a str>,
    /// Nobody can name or place it, or its token's chain was cut: it could be any agent a
    /// charter names. It's held to a charter's never as each of them, and to its expiry, but
    /// its calls aren't counted against a budget.
    pub unknown: bool,
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

/// What a proxy keeps of each charter beside the state: its never, compiled once, and what
/// its budget has used, which the halts write to a file of their own (halts::COUNTS_FILE).
#[derive(Default)]
pub struct Runtime {
    /// Each charter's never, as the text it was compiled from.
    compiled: Mutex<HashMap<String, (String, CompiledNever)>>,
    used: Mutex<HashMap<String, Vec<u64>>>,
    /// Moves on each time what the budgets used changes, so a writer can tell there's
    /// something new to write.
    changes: AtomicU64,
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

    /// Whether the charters whose scope covers a call deny it, made by caller to resource, if
    /// the hook knows what the call reaches. named gives the name an agent in a scope is
    /// recorded by. Charters are checked in the order they were put, and the first that
    /// denies is named. Only a call none denies is counted, against every limit that counts it.
    pub fn decide(
        &self,
        charters: &[&Charter],
        caller: &Caller<'_>,
        named: impl Fn(&str) -> String,
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
                // A caller nobody can name or place could be any agent the charter names.
                let as_any: Vec<String> = if caller.unknown {
                    c.scope.agents.iter().map(|a| named(a)).collect()
                } else {
                    vec![]
                };
                for principal in std::iter::once(caller.name).chain(as_any.iter().map(String::as_str)) {
                    match compiled.forbids(principal, &caller.under, resource) {
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
            }
            let Resource::Tool(tool) = resource else { continue };
            // Counting a caller nobody can name or place would let one outside the run use
            // the run's budget up.
            if caller.unknown {
                continue;
            }
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
        if !counted.is_empty() {
            self.changes.fetch_add(1, Ordering::Relaxed);
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

    /// How many times what the budgets used has changed.
    pub fn changes(&self) -> u64 {
        self.changes.load(Ordering::Relaxed)
    }

    /// Takes up what budgets used before a restart, for charters this proxy hasn't counted
    /// anything for since.
    pub fn take_up(&self, counts: HashMap<String, Vec<u64>>) {
        let mut used = self.used.lock().unwrap_or_else(|p| p.into_inner());
        for (id, counts) in counts {
            used.entry(id).or_insert(counts);
        }
    }

    /// Forgets what the proxy kept of a charter that's ended.
    pub fn forget(&self, id: &str) {
        self.compiled.lock().unwrap_or_else(|p| p.into_inner()).remove(id);
        self.used.lock().unwrap_or_else(|p| p.into_inner()).remove(id);
        self.changes.fetch_add(1, Ordering::Relaxed);
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

    fn by(name: &str) -> Caller<'_> {
        Caller {
            name,
            ..Default::default()
        }
    }

    fn same(name: &str) -> String {
        name.to_string()
    }

    #[test]
    fn a_never_nested_too_deep_is_refused_before_cedar_reads_it() {
        // Past a couple of hundred levels Cedar's parser overflows the stack, which aborts
        // the proxy: each is refused first. On a thread with a hook's stack, 2 MiB.
        let at = |n: usize| {
            let set = format!("{}1{}", "[".repeat(n), "]".repeat(n));
            let record = format!("{}1{}", "{a:".repeat(n), "}".repeat(n));
            [
                format!("{}true{}", "(".repeat(n), ")".repeat(n)),
                format!("{set} == {set}"),
                format!("{record} == {record}"),
                format!("{}true{}", "if true then ".repeat(n), " else false".repeat(n)),
            ]
            .map(|body| format!("forbid(principal, action, resource) when {{ {body} }};"))
        };
        std::thread::Builder::new()
            .stack_size(2 << 20)
            .spawn(move || {
                // when { } is a level, and each shape adds n.
                for never in at(MAX_NESTING - 1) {
                    compile(&never).unwrap_or_else(|e| panic!("{never}: {e}"));
                }
                for never in at(MAX_NESTING).into_iter().chain(at(600)) {
                    let err = compile(&never).err().unwrap();
                    assert!(err.contains("nests"), "{err}");
                }
            })
            .unwrap()
            .join()
            .unwrap();
        // Strings and comments don't count, and a long chain isn't deep.
        let flat = format!(
            r#"forbid(principal, action, resource == Wake::Tool::"((((((((((((((((((((((((((((((((((") // [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[
               when {{ {} }};"#,
            vec!["true"; 1000].join(" && ")
        );
        compile(&flat).unwrap();
    }

    #[test]
    fn principal_in_an_agent_covers_its_sub_agents() {
        let c = charter(
            r#"@id("support-never-pays") forbid(principal in Wake::Agent::"support", action, resource == Wake::Tool::"pay");"#,
            vec![],
        );
        c.check().unwrap();
        let rt = Runtime::default();
        let pay = Some(Resource::Tool("pay"));
        let sub_agent = Caller {
            name: "support/refunder#toolu_1",
            under: vec!["support/refunder#toolu_1", "support", "user-42"],
            unknown: false,
        };
        assert_eq!(rt.decide(&[&c], &by("support"), same, pay, NOW).unwrap().why, "never");
        assert_eq!(rt.decide(&[&c], &sub_agent, same, pay, NOW).unwrap().why, "never");
        let billing = Caller {
            name: "billing/payer#toolu_2",
            under: vec!["billing/payer#toolu_2", "billing"],
            unknown: false,
        };
        assert_eq!(rt.decide(&[&c], &billing, same, pay, NOW), None);
    }

    #[test]
    fn a_caller_nobody_can_place_is_held_as_any_agent_named_but_not_counted() {
        let c = charter(
            r#"forbid(principal == Wake::Agent::"support/refunder#toolu_1", action, resource == Wake::Tool::"wire");"#,
            vec![Limit {
                tools: vec![],
                calls: 1,
            }],
        );
        c.check().unwrap();
        let rt = Runtime::default();
        let unknown = Caller {
            name: "unknown",
            under: vec![],
            unknown: true,
        };
        // It could be the agent the policy names.
        let d = rt
            .decide(&[&c], &unknown, same, Some(Resource::Tool("wire")), NOW)
            .unwrap();
        assert_eq!(d.why, "never");
        // But its calls don't use the run's budget up.
        for _ in 0..3 {
            assert_eq!(
                rt.decide(&[&c], &unknown, same, Some(Resource::Tool("lookup")), NOW),
                None
            );
        }
        assert_eq!(
            rt.used().get("refund-4711").cloned().unwrap_or_default(),
            Vec::<u64>::new()
        );
        assert_eq!(
            rt.decide(
                &[&c],
                &by("support/refunder#toolu_1"),
                same,
                Some(Resource::Tool("lookup")),
                NOW
            ),
            None
        );
        // And it's held to the expiry.
        assert_eq!(
            rt.decide(&[&c], &unknown, same, None, c.expires_at).unwrap().why,
            "expired"
        );
    }

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
            .decide(
                &[&c],
                &by("support"),
                same,
                Some(Resource::Tool("delete_customer")),
                NOW,
            )
            .unwrap();
        assert_eq!((d.halt.id.as_str(), d.why), ("charter:refund-4711", "never"));
        assert_eq!(d.detail, "forbidden by no-deletes");
        let d = rt
            .decide(&[&c], &by("support"), same, Some(Resource::Agent("payments")), NOW)
            .unwrap();
        assert_eq!(d.detail, "forbidden by policy1");
        // A tool and an agent of the same name are different things.
        assert_eq!(
            rt.decide(
                &[&c],
                &by("support"),
                same,
                Some(Resource::Agent("delete_customer")),
                NOW
            ),
            None
        );
        assert_eq!(
            rt.decide(&[&c], &by("support"), same, Some(Resource::Tool("refund")), NOW),
            None
        );
        // With nothing to check never against, only expiry decides.
        assert_eq!(rt.decide(&[&c], &by("support"), same, None, NOW), None);
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
            rt.decide(
                &[&c],
                &by("support/refunder#toolu_1"),
                same,
                Some(Resource::Tool("refund")),
                NOW
            )
            .is_some()
        );
        assert!(
            rt.decide(&[&c], &by("support"), same, Some(Resource::Tool("refund")), NOW)
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
        // In milliseconds by mistake, it would never come.
        c.expires_at = 1_800_000_000_000;
        assert!(c.check().unwrap_err().contains("not milliseconds"));
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
        let call = |tool| rt.decide(&[&c], &by("support"), same, Some(Resource::Tool(tool)), NOW);
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
        assert_eq!(
            rt.decide(&[&c], &by("support"), same, Some(Resource::Agent("billing")), NOW),
            None
        );

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
            rt.decide(&[&other, &c], &by("support"), same, Some(Resource::Tool("refund")), NOW),
            None
        );
        assert_eq!(
            rt.decide(&[&other, &c], &by("support"), same, Some(Resource::Tool("refund")), NOW)
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
        assert_eq!(rt.decide(&[&c], &by("support"), same, None, c.expires_at - 1), None);
        for resource in [None, Some(Resource::Tool("lookup")), Some(Resource::Agent("billing"))] {
            let d = rt.decide(&[&c], &by("support"), same, resource, c.expires_at).unwrap();
            assert_eq!(d.why, "expired");
        }
    }

    #[test]
    fn a_never_that_cant_be_read_denies_everything_it_covers() {
        // Only a state file edited by hand can hold one: PUT refuses it.
        let c = charter("forbid(", vec![]);
        let d = Runtime::default()
            .decide(&[&c], &by("support"), same, None, NOW)
            .unwrap();
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
            rt.decide(
                &[&c],
                &by("sp\"iffe://x"),
                same,
                Some(Resource::Tool("file:///etc/passwd")),
                NOW
            )
            .is_some()
        );
        assert!(
            rt.decide(
                &[&c],
                &by("x"),
                same,
                Some(Resource::Tool(r#"weird "quoted" tool"#)),
                NOW
            )
            .is_some()
        );
        assert!(
            rt.decide(&[&c], &by("x"), same, Some(Resource::Tool("file:///etc/hosts")), NOW)
                .is_none()
        );
    }
}
