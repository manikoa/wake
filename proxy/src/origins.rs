//! What entered each agent through the proxy, for flow policies (policy.rs): the tools it
//! called, whose results come back to it, and the agents whose messages and replies reached
//! it. A flow policy decides on these, never on anything the caller says about itself, so
//! an agent can't talk its way past one: leaving a header out, or sending one, changes
//! nothing.
//!
//! It's coarse on purpose. The proxy sees what reached an agent, not which of it feeds a
//! given call, so a policy treats everything that entered an agent within its window as
//! behind each call it makes. It's coarse across delegation too: content crosses one both
//! ways where the proxy can't see it, a parent's task into the sub-agent it delegates to,
//! and the sub-agent's result back. So what enters a caller is noted under every agent its
//! token says the call is made under, and a call is checked against what entered any of
//! them: a sub-agent can't take an action its parent is kept from, with the parent's
//! content, or hand its parent content to take one with. Where it doesn't know, it assumes the worst: for one window
//! after the proxy starts or the window grows, for an agent past its bound of origins, and
//! once too many agents are tracked to remember another, an agent counts as having every
//! origin.

use std::collections::HashMap;
use std::sync::Mutex;

use crate::agents::Agents;
use crate::token::Token;

/// The most distinct origins remembered for one agent within a window. Past it, the agent
/// counts as having every origin until the window passes.
pub const MAX_ORIGINS: usize = 256;
/// The most agents remembered at once. Past it, an agent not remembered counts as having
/// every origin.
pub const MAX_AGENTS: usize = 10_000;
/// The longest window a policy may set, in seconds.
pub const MAX_WINDOW_SECS: u64 = 86_400;

const NANOS: u64 = 1_000_000_000;

/// Who a call or a delivery is about, as the identity map names it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Who<'a> {
    /// An agent the identity map names, or, without a map, an identity.
    Agent(&'a str),
    /// An identity the map doesn't list: it could be any agent.
    Unlisted(&'a str),
    /// No identity at all.
    Nobody,
}

impl<'a> Who<'a> {
    /// The agent behind an identity the gateway authenticated.
    pub fn of(agents: &'a Agents, identity: Option<&'a str>) -> Who<'a> {
        match identity.filter(|i| !i.is_empty()) {
            None => Who::Nobody,
            Some(identity) => match agents.caller(Some(identity)) {
                Some(name) => Who::Agent(name),
                None => Who::Unlisted(identity),
            },
        }
    }

    /// What origins are remembered under: the agent's name, or an unlisted identity.
    pub(crate) fn key(&self) -> Option<&'a str> {
        match self {
            Who::Agent(name) => Some(name),
            Who::Unlisted(identity) => Some(identity),
            Who::Nobody => None,
        }
    }
}

/// The agents a call made with token is made under, besides its caller, as the identity
/// map names them (Token::under). Its subject counts only when the map lists it, or when
/// there's no map to say: a user a token acts for isn't an agent, and keying origins under
/// one would tie together every agent acting for that user.
pub fn under<'a>(agents: &'a Agents, token: Option<&'a Token>) -> Vec<Who<'a>> {
    token
        .map(|t| {
            let subject = Some(t.subject.as_str()).filter(|s| agents.is_empty() || agents.knows(s));
            t.actors
                .iter()
                .map(String::as_str)
                .chain(subject)
                .map(|id| Who::of(agents, Some(id)))
                .filter(|who| *who != Who::Nobody)
                .collect()
        })
        .unwrap_or_default()
}

/// Why a call whose token's chain was cut counts as having every origin: what entered
/// the agents it didn't keep isn't known.
pub const CUT_CHAIN: &str = "the caller's token acts under more agents than wake-proxy keeps";

/// What entered an agent within a window.
#[derive(Clone, Debug, PartialEq)]
pub enum Seen {
    /// These origins, newest first.
    These(Vec<String>),
    /// Every origin, as far as a policy knows, and why.
    Every(&'static str),
}

#[derive(Default)]
struct Agent {
    /// Each origin, and when it last entered, Unix nanoseconds.
    origins: HashMap<String, u64>,
    /// When the agent last had more origins than MAX_ORIGINS within the window.
    saturated_at: Option<u64>,
}

impl Agent {
    fn newest(&self) -> u64 {
        self.origins
            .values()
            .copied()
            .chain(self.saturated_at)
            .max()
            .unwrap_or(0)
    }
}

struct Inner {
    agents: HashMap<String, Agent>,
    /// When an agent last couldn't be remembered because MAX_AGENTS were.
    overflowed_at: Option<u64>,
    /// How long origins are kept, the policy's window.
    window: u64,
    /// Since when everything that entered is known: when the proxy started, or when the
    /// window last grew, since less than the new window was kept before.
    known_since: u64,
}

/// What entered each agent, shared by the hooks that see deliveries and the checks that
/// decide on them.
pub struct Observed {
    inner: Mutex<Inner>,
}

impl Observed {
    /// Nothing seen yet, at, Unix nanoseconds, keeping origins for window_secs. Until a
    /// window has passed, every agent counts as having every origin, since what entered
    /// before isn't known.
    pub fn starting(at: u64, window_secs: u64) -> Self {
        Observed {
            inner: Mutex::new(Inner {
                agents: HashMap::new(),
                overflowed_at: None,
                window: window_secs.clamp(1, MAX_WINDOW_SECS) * NANOS,
                known_since: at,
            }),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Keeps origins for window_secs from now on. A longer window than before isn't known
    /// to be complete until it has passed: less was kept.
    pub fn set_window(&self, at: u64, window_secs: u64) {
        let window = window_secs.clamp(1, MAX_WINDOW_SECS) * NANOS;
        let mut inner = self.lock();
        if window > inner.window {
            inner.known_since = inner.known_since.max(at);
        }
        inner.window = window;
    }

    /// Notes that origin entered who at, Unix nanoseconds. A delivery to nobody is noted
    /// nowhere: a caller with no identity counts as having every origin anyway.
    pub fn note(&self, who: Who<'_>, origin: &str, at: u64) {
        let Some(key) = who.key() else { return };
        let mut inner = self.lock();
        let oldest = at.saturating_sub(inner.window);
        if !inner.agents.contains_key(key) && inner.agents.len() >= MAX_AGENTS {
            // Agents nothing has entered for a window can be forgotten.
            inner.agents.retain(|_, a| a.newest() >= oldest);
            if inner.agents.len() >= MAX_AGENTS {
                inner.overflowed_at = Some(at);
                return;
            }
        }
        let agent = inner.agents.entry(key.to_string()).or_default();
        if let Some(seen) = agent.origins.get_mut(origin) {
            *seen = (*seen).max(at);
            return;
        }
        agent.origins.retain(|_, seen| *seen >= oldest);
        if agent.origins.len() >= MAX_ORIGINS {
            agent.saturated_at = Some(at);
            return;
        }
        agent.origins.insert(origin.to_string(), at);
    }

    /// What entered who in the window_secs up to at.
    pub fn seen(&self, who: Who<'_>, at: u64, window_secs: u64) -> Seen {
        self.seen_by(&[who], at, window_secs)
    }

    /// What entered any of whos in the window_secs up to at: a caller and the agents it
    /// acts under. Each origin once, newest first; every origin when that's so for any.
    pub fn seen_by(&self, whos: &[Who<'_>], at: u64, window_secs: u64) -> Seen {
        let since = at.saturating_sub(window_secs.clamp(1, MAX_WINDOW_SECS) * NANOS);
        let inner = self.lock();
        if inner.known_since > since {
            return Seen::Every("wake-proxy hasn't watched for a whole window yet");
        }
        let mut newest: HashMap<&str, u64> = HashMap::new();
        for who in whos {
            let Some(key) = who.key() else {
                return Seen::Every("the caller has no identity");
            };
            let Some(agent) = inner.agents.get(key) else {
                if inner.overflowed_at.is_some_and(|t| t >= since) {
                    return Seen::Every("too many agents to remember this one");
                }
                continue;
            };
            if agent.saturated_at.is_some_and(|t| t >= since) {
                return Seen::Every("more origins entered the agent than wake-proxy keeps");
            }
            for (origin, seen) in agent.origins.iter().filter(|(_, seen)| **seen >= since) {
                let at = newest.entry(origin).or_default();
                *at = (*at).max(*seen);
            }
        }
        let mut origins: Vec<(&str, u64)> = newest.into_iter().collect();
        origins.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
        Seen::These(origins.into_iter().map(|(o, _)| o.to_string()).collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const S: u64 = NANOS;
    const START: u64 = 1_000_000 * S;
    const WINDOW: u64 = 900;

    fn after_start(secs: u64) -> u64 {
        START + 3_600 * S + secs * S
    }

    #[test]
    fn remembers_each_origin_for_a_window() {
        let o = Observed::starting(START, WINDOW);
        let billing = Who::Agent("billing");
        o.note(billing, "tool:fetch_page", after_start(0));
        o.note(billing, "agent:research", after_start(10));
        assert_eq!(
            o.seen(billing, after_start(20), WINDOW),
            Seen::These(vec!["agent:research".into(), "tool:fetch_page".into()])
        );
        // Seen again later, it stays.
        o.note(billing, "tool:fetch_page", after_start(800));
        assert_eq!(
            o.seen(billing, after_start(905), WINDOW),
            Seen::These(vec!["tool:fetch_page".into(), "agent:research".into()])
        );
        assert_eq!(
            o.seen(billing, after_start(1_000), WINDOW),
            Seen::These(vec!["tool:fetch_page".into()])
        );
        assert_eq!(o.seen(billing, after_start(1_800), WINDOW), Seen::These(vec![]));
        // Nothing entered another agent.
        assert_eq!(
            o.seen(Who::Agent("support"), after_start(20), WINDOW),
            Seen::These(vec![])
        );
    }

    #[test]
    fn what_entered_several_agents_is_what_entered_any_of_them() {
        let o = Observed::starting(START, WINDOW);
        let child = Who::Unlisted("support/researcher#toolu_1");
        let support = Who::Agent("support");
        o.note(support, "tool:fetch_page", after_start(0));
        o.note(child, "tool:search", after_start(5));
        o.note(child, "tool:fetch_page", after_start(10));
        assert_eq!(
            o.seen_by(&[child, support], after_start(20), WINDOW),
            Seen::These(vec!["tool:fetch_page".into(), "tool:search".into()]),
            "each origin once, by when it last entered any of them"
        );
        assert_eq!(
            o.seen_by(&[support], after_start(20), WINDOW),
            Seen::These(vec!["tool:fetch_page".into()])
        );
        assert_eq!(
            o.seen_by(
                &[Who::Agent("billing"), Who::Agent("research")],
                after_start(20),
                WINDOW
            ),
            Seen::These(vec![])
        );
        // Every origin, when that's so for any of them.
        for i in 0..MAX_ORIGINS {
            o.note(support, &format!("tool:t{i}"), after_start(30));
        }
        assert_eq!(
            o.seen_by(&[child, support], after_start(31), WINDOW),
            Seen::Every("more origins entered the agent than wake-proxy keeps")
        );
        assert_eq!(
            o.seen_by(&[child, Who::Nobody], after_start(31), WINDOW),
            Seen::Every("the caller has no identity")
        );
        assert_eq!(
            o.seen(child, after_start(31), WINDOW),
            Seen::These(vec!["tool:fetch_page".into(), "tool:search".into()]),
            "on its own, the child has only what entered it"
        );
    }

    #[test]
    fn a_call_is_made_under_the_chain_of_its_token() {
        let agents = Agents::from_json(br#"{"support": ["0oa1support"]}"#).unwrap();
        let token = Token {
            subject: "0oa1support".into(),
            actors: vec!["support/researcher#toolu_1".into()],
            ..Default::default()
        };
        assert_eq!(
            under(&agents, Some(&token)),
            [Who::Unlisted("support/researcher#toolu_1"), Who::Agent("support")]
        );
        assert!(under(&agents, None).is_empty());
        // A user a token acts for isn't an agent: what entered one agent acting for that
        // user isn't kept under the user, for every other agent acting for them to inherit.
        let for_user = Token {
            subject: "user-42".into(),
            actors: vec!["support/researcher#toolu_1".into(), "0oa1support".into()],
            ..Default::default()
        };
        assert_eq!(
            under(&agents, Some(&for_user)),
            [Who::Unlisted("support/researcher#toolu_1"), Who::Agent("support")]
        );
        // Without a map there's no telling, so it counts.
        assert_eq!(under(&Agents::default(), Some(&for_user)).len(), 3);
    }

    #[test]
    fn assumes_every_origin_where_it_cant_know() {
        let o = Observed::starting(START, WINDOW);
        // Just started: what entered before is unknown.
        assert_eq!(
            o.seen(Who::Agent("billing"), START + 100 * S, WINDOW),
            Seen::Every("wake-proxy hasn't watched for a whole window yet")
        );
        assert_eq!(
            o.seen(Who::Agent("billing"), START + 901 * S, WINDOW),
            Seen::These(vec![])
        );
        // A caller with no identity could have been sent anything.
        o.note(Who::Nobody, "tool:fetch_page", after_start(0));
        assert_eq!(
            o.seen(Who::Nobody, after_start(1), WINDOW),
            Seen::Every("the caller has no identity")
        );
        // An unlisted identity is remembered under itself.
        o.note(Who::Unlisted("spiffe://x/intruder"), "tool:t", after_start(0));
        assert_eq!(
            o.seen(Who::Unlisted("spiffe://x/intruder"), after_start(1), WINDOW),
            Seen::These(vec!["tool:t".into()])
        );
    }

    #[test]
    fn a_longer_window_is_known_only_once_it_has_passed() {
        let o = Observed::starting(START, WINDOW);
        o.note(Who::Agent("a"), "tool:t", after_start(0));
        o.set_window(after_start(100), 3_600);
        // Less than an hour was kept, so the last hour isn't known yet.
        assert_eq!(
            o.seen(Who::Agent("a"), after_start(200), 3_600),
            Seen::Every("wake-proxy hasn't watched for a whole window yet")
        );
        assert_eq!(o.seen(Who::Agent("a"), after_start(3_701), 3_600), Seen::These(vec![]));
        // A shorter one is known at once.
        o.note(Who::Agent("a"), "tool:t", after_start(3_702));
        o.set_window(after_start(3_703), 60);
        assert_eq!(
            o.seen(Who::Agent("a"), after_start(3_704), 60),
            Seen::These(vec!["tool:t".into()])
        );
    }

    #[test]
    fn an_agent_past_its_bound_has_every_origin_for_a_window() {
        let o = Observed::starting(START, WINDOW);
        let a = Who::Agent("a");
        for i in 0..=MAX_ORIGINS {
            o.note(a, &format!("tool:t{i}"), after_start(0));
        }
        assert_eq!(
            o.seen(a, after_start(1), WINDOW),
            Seen::Every("more origins entered the agent than wake-proxy keeps")
        );
        // Once the window has passed with nothing new, it's known again.
        assert_eq!(o.seen(a, after_start(901), WINDOW), Seen::These(vec![]));
        // Origins older than the window don't count toward the bound.
        for i in 0..MAX_ORIGINS {
            o.note(a, &format!("tool:later{i}"), after_start(1_000));
        }
        assert!(matches!(o.seen(a, after_start(1_001), WINDOW), Seen::These(v) if v.len() == MAX_ORIGINS));
    }

    #[test]
    fn past_its_bound_of_agents_an_unremembered_one_has_every_origin() {
        let o = Observed::starting(START, WINDOW);
        for i in 0..MAX_AGENTS {
            o.note(Who::Agent(&format!("agent-{i}")), "tool:t", after_start(0));
        }
        o.note(Who::Agent("one-too-many"), "tool:t", after_start(1));
        assert_eq!(
            o.seen(Who::Agent("one-too-many"), after_start(2), WINDOW),
            Seen::Every("too many agents to remember this one")
        );
        // The ones remembered are still known.
        assert_eq!(
            o.seen(Who::Agent("agent-0"), after_start(2), WINDOW),
            Seen::These(vec!["tool:t".into()])
        );
        // Agents nothing entered for a window make room.
        o.note(Who::Agent("newcomer"), "tool:t", after_start(WINDOW + 10));
        assert_eq!(
            o.seen(Who::Agent("newcomer"), after_start(WINDOW + 11), WINDOW),
            Seen::These(vec!["tool:t".into()])
        );
    }

    #[test]
    fn names_callers_through_the_identity_map() {
        let agents = Agents::from_json(br#"{"billing": ["spiffe://wake.local/agent/billing"]}"#).unwrap();
        assert_eq!(
            Who::of(&agents, Some("spiffe://wake.local/agent/billing")),
            Who::Agent("billing")
        );
        assert_eq!(
            Who::of(&agents, Some("spiffe://x/other")),
            Who::Unlisted("spiffe://x/other")
        );
        assert_eq!(Who::of(&agents, Some("")), Who::Nobody);
        assert_eq!(Who::of(&agents, None), Who::Nobody);
        assert_eq!(Who::of(&Agents::none(), Some("billing")), Who::Agent("billing"));
    }
}
