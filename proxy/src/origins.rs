//! What entered each agent through the proxy, for flow policies (policy.rs): the tools it
//! called, whose results come back to it, and the agents whose messages and replies reached
//! it, with whatever had entered them. A flow policy decides on these, never on anything the
//! caller says about itself, so an agent can't talk its way past one: leaving a header out,
//! or sending one, changes nothing.
//!
//! It's coarse on purpose. The proxy sees what reached an agent, not which of it feeds a
//! given call, so a policy treats everything that entered an agent within its window as
//! behind each call it makes. A message or a reply is treated the same way: it carries
//! everything that had entered the agent it comes from, so one agent can't fetch a page and
//! hand it to another to act on. What it carries keeps the time it entered, so passing
//! content on never makes it newer, and agents talking back and forth don't keep it alive.
//! It's coarse across delegation too: content crosses one both ways where the proxy can't
//! see it, a parent's task into the sub-agent it delegates to, and the sub-agent's result
//! back. So what enters a caller is noted under every agent its token says the call is made
//! under, and a call is checked against what entered any of them: a sub-agent can't take an
//! action its parent is kept from, with the parent's content, or hand its parent content to
//! take one with. Where it doesn't know, it assumes the worst: for one window after the
//! proxy starts or the window grows, for an agent past its bound of origins, once too many
//! agents are tracked to remember another, and for a caller the agents file doesn't list
//! and its token doesn't place, an agent counts as having every origin, and so does an
//! agent its messages or replies reach.

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

/// Whether a caller the identity map doesn't list is placed by token: the token is its own
/// (the agent it names as acting now is the caller), its chain is whole, and the chain or
/// whom it acts for is an agent the map lists. It acts under that agent then, by the
/// identity provider's word, as halts.rs decides too.
pub fn placed(agents: &Agents, identity: &str, token: Option<&Token>) -> bool {
    token.is_some_and(|t| {
        !t.cut && t.actors.first().is_some_and(|a| a == identity) && t.under().any(|a| agents.knows(a))
    })
}

/// The agents whose origins are behind a call, or a message, from caller with token: the
/// caller and the agents its token places it under (under). Err, with why, when it could be
/// any agent, so what entered it isn't known: its token's chain was cut, or the identity map
/// doesn't list it and its own token doesn't place it, as the halts treat a caller nobody
/// can name. Without a map every identity is an agent's name, so no caller is unlisted.
pub fn behind<'a>(agents: &'a Agents, caller: Who<'a>, token: Option<&'a Token>) -> Result<Vec<Who<'a>>, &'static str> {
    if token.is_some_and(|t| t.cut) {
        return Err(CUT_CHAIN);
    }
    if let Who::Unlisted(identity) = caller
        && !agents.is_empty()
        && !placed(agents, identity, token)
    {
        return Err(UNPLACED);
    }
    let mut whos = under(agents, token);
    whos.insert(0, caller);
    Ok(whos)
}

/// Why a call whose token's chain was cut counts as having every origin: what entered
/// the agents it didn't keep isn't known.
pub const CUT_CHAIN: &str = "the caller's token acts under more agents than wake-proxy keeps";
/// Why a caller the agents file doesn't list, and its own token doesn't place, counts as
/// having every origin: it could be any agent.
pub const UNPLACED: &str = "the agents file doesn't list the caller, and its token doesn't place it";
/// Why an agent counts as having every origin once a message or reply from one that does
/// reached it.
pub const CARRIED: &str = "a message or reply reached the agent from one with every origin";
/// Why an agent counts as having every origin once a model's response that wake-proxy
/// couldn't read reached it: it can't tell which tools the provider ran (models.rs).
pub const UNREAD: &str = "a model's response reached the agent that wake-proxy couldn't read";
/// Why an agent counts as having every origin past MAX_ORIGINS.
const SATURATED: &str = "more origins entered the agent than wake-proxy keeps";

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
    /// When the agent last came to have every origin, and why: more origins than
    /// MAX_ORIGINS within the window, or content from an agent with every origin.
    saturated: Option<(u64, &'static str)>,
}

impl Agent {
    fn newest(&self) -> u64 {
        self.origins
            .values()
            .copied()
            .chain(self.saturated.map(|(at, _)| at))
            .max()
            .unwrap_or(0)
    }

    /// Notes that origin entered at, forgetting origins older than oldest. Past
    /// MAX_ORIGINS, the agent has every origin from then.
    fn note(&mut self, origin: &str, at: u64, oldest: u64) {
        if let Some(seen) = self.origins.get_mut(origin) {
            *seen = (*seen).max(at);
            return;
        }
        self.origins.retain(|_, seen| *seen >= oldest);
        if self.origins.len() >= MAX_ORIGINS {
            self.saturate(at, SATURATED);
            return;
        }
        self.origins.insert(origin.to_string(), at);
    }

    /// Notes that the agent has every origin from at, for why, unless it does from later.
    fn saturate(&mut self, at: u64, why: &'static str) {
        if self.saturated.is_none_or(|(since, _)| since <= at) {
            self.saturated = Some((at, why));
        }
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

impl Inner {
    /// The agent who is remembered under, made room for if need be. None for nobody, and
    /// when it can't be remembered: MAX_AGENTS others had something enter them within the
    /// window.
    fn agent(&mut self, who: Who<'_>, at: u64) -> Option<&mut Agent> {
        let key = who.key()?;
        if !self.agents.contains_key(key) && self.agents.len() >= MAX_AGENTS {
            // Agents nothing has entered for a window can be forgotten.
            let oldest = at.saturating_sub(self.window);
            self.agents.retain(|_, a| a.newest() >= oldest);
            if self.agents.len() >= MAX_AGENTS {
                self.overflowed_at = Some(at);
                return None;
            }
        }
        Some(self.agents.entry(key.to_string()).or_default())
    }

    /// Why each of whos that has every origin since since has it, and since when: at, now,
    /// for a caller with no identity. First, while wake-proxy hasn't watched for a whole
    /// window, since it started.
    fn every(&self, whos: &[Who<'_>], since: u64, at: u64) -> impl Iterator<Item = (&'static str, u64)> {
        let started = (self.known_since > since)
            .then_some(("wake-proxy hasn't watched for a whole window yet", self.known_since));
        let each = whos.iter().filter_map(move |who| {
            let Some(key) = who.key() else {
                return Some(("the caller has no identity", at));
            };
            match self.agents.get(key) {
                Some(agent) => agent.saturated.filter(|(t, _)| *t >= since).map(|(t, why)| (why, t)),
                None => self
                    .overflowed_at
                    .filter(|t| *t >= since)
                    .map(|t| ("too many agents to remember this one", t)),
            }
        });
        started.into_iter().chain(each)
    }

    /// Each origin noted for any of whos since since, with when it last entered any of them,
    /// whether or not any of them has every origin.
    fn recorded(&self, whos: &[Who<'_>], since: u64) -> HashMap<&str, u64> {
        let mut newest: HashMap<&str, u64> = HashMap::new();
        for agent in whos.iter().filter_map(|who| self.agents.get(who.key()?)) {
            for (origin, seen) in agent.origins.iter().filter(|(_, seen)| **seen >= since) {
                let at = newest.entry(origin).or_default();
                *at = (*at).max(*seen);
            }
        }
        newest
    }

    /// What entered any of whos since since, each origin with when it last entered any of
    /// them. Err when that's every origin for any of them, with why and since when.
    fn entered(&self, whos: &[Who<'_>], since: u64, at: u64) -> Result<HashMap<&str, u64>, (&'static str, u64)> {
        match self.every(whos, since, at).next() {
            Some(every) => Err(every),
            None => Ok(self.recorded(whos, since)),
        }
    }
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
        let mut inner = self.lock();
        let oldest = at.saturating_sub(inner.window);
        if let Some(agent) = inner.agent(who, at) {
            agent.note(origin, at, oldest);
        }
    }

    /// Notes that content from from, a message or a reply, reached each of to at: whatever
    /// had entered any of from within the window entered each of to too. Each origin keeps
    /// when it last entered them, so passing content on never makes it newer. When that's
    /// every origin, each of to has every origin too, for as long as any of from does, and
    /// what entered from after that time enters each of to as well, so it still counts once
    /// every origin no longer does. What came from one of to isn't carried back to them: an
    /// agent's own content, or its chain's, isn't an origin of theirs.
    pub fn carry(&self, from: &[Who<'_>], to: &[Who<'_>], at: u64) {
        let mut inner = self.lock();
        let since = at.saturating_sub(inner.window);
        let every = inner.every(from, since, at).map(|(_, t)| t).max();
        let own: Vec<String> = to.iter().filter_map(Who::key).map(|k| format!("agent:{k}")).collect();
        // What entered by the time every origin did counts no longer than every origin does.
        let carried: Vec<(String, u64)> = inner
            .recorded(from, since)
            .into_iter()
            .filter(|(origin, seen)| every.is_none_or(|t| *seen > t) && !own.iter().any(|o| o == origin))
            .map(|(origin, seen)| (origin.to_string(), seen))
            .collect();
        if every.is_none() && carried.is_empty() {
            return;
        }
        for who in to {
            let Some(agent) = inner.agent(*who, at) else { continue };
            if let Some(t) = every {
                agent.saturate(t, CARRIED);
            }
            for (origin, seen) in &carried {
                agent.note(origin, *seen, since);
            }
        }
    }

    /// Notes that content nothing is known of reached each of to at, for why, such as a
    /// message from a caller nobody can name or place (CARRIED), or a model's response that
    /// couldn't be read (UNREAD): each has every origin from then, for a window.
    pub fn saturate(&self, to: &[Who<'_>], at: u64, why: &'static str) {
        let mut inner = self.lock();
        for who in to {
            if let Some(agent) = inner.agent(*who, at) {
                agent.saturate(at, why);
            }
        }
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
        match inner.entered(whos, since, at) {
            Err((why, _)) => Seen::Every(why),
            Ok(newest) => {
                let mut origins: Vec<(&str, u64)> = newest.into_iter().collect();
                origins.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
                Seen::These(origins.into_iter().map(|(o, _)| o.to_string()).collect())
            }
        }
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
    fn content_carries_what_entered_the_agent_it_comes_from() {
        let o = Observed::starting(START, WINDOW);
        let (research, billing, support) = (Who::Agent("research"), Who::Agent("billing"), Who::Agent("support"));
        o.note(research, "tool:fetch_page", after_start(0));
        o.note(research, "agent:billing", after_start(5));
        // Research replies to billing: the page comes with it, as of when research fetched
        // it, and billing's own message isn't carried back to it.
        o.note(billing, "agent:research", after_start(100));
        o.carry(&[research], &[billing], after_start(100));
        assert_eq!(
            o.seen(billing, after_start(101), WINDOW),
            Seen::These(vec!["agent:research".into(), "tool:fetch_page".into()])
        );
        // Passing it on never makes it newer: it leaves each of them when it leaves research,
        // however they pass it around.
        o.carry(&[billing], &[support], after_start(200));
        assert_eq!(
            o.seen(support, after_start(201), WINDOW),
            Seen::These(vec!["agent:research".into(), "tool:fetch_page".into()])
        );
        o.carry(&[support], &[billing], after_start(800));
        o.carry(&[billing], &[research], after_start(850));
        for who in [research, billing, support] {
            assert!(
                matches!(o.seen(who, after_start(901), WINDOW), Seen::These(seen) if !seen.contains(&"tool:fetch_page".to_string())),
                "{who:?}"
            );
        }
        // From several agents, it's what entered any of them, into each it reaches.
        let child = Who::Unlisted("support/researcher#toolu_1");
        o.note(child, "tool:search", after_start(1_000));
        o.carry(&[child, support], &[research, billing], after_start(1_001));
        for who in [research, billing] {
            assert!(
                matches!(o.seen(who, after_start(1_002), WINDOW), Seen::These(seen) if seen.contains(&"tool:search".to_string()))
            );
        }
        // Nothing to carry makes no agent.
        o.carry(&[Who::Agent("idle")], &[Who::Agent("listener")], after_start(1_003));
        assert!(!o.lock().agents.contains_key("listener"));
    }

    #[test]
    fn content_from_an_agent_with_every_origin_gives_every_origin() {
        let o = Observed::starting(START, WINDOW);
        let (a, b, c) = (Who::Agent("a"), Who::Agent("b"), Who::Agent("c"));
        // Just started, nothing is known; what a passes on then isn't known past that window.
        o.carry(&[a], &[b], START + 10 * S);
        assert_eq!(o.seen(b, START + 901 * S, WINDOW), Seen::These(vec![]));
        // An agent past its bound of origins passes that on, until its own window ends.
        for i in 0..=MAX_ORIGINS {
            o.note(a, &format!("tool:t{i}"), after_start(0));
        }
        o.carry(&[a], &[b], after_start(500));
        o.carry(&[b], &[c], after_start(600));
        assert_eq!(o.seen(c, after_start(601), WINDOW), Seen::Every(CARRIED));
        o.carry(&[c], &[a], after_start(700));
        assert_eq!(
            o.seen(a, after_start(901), WINDOW),
            Seen::These(vec![]),
            "nor kept alive"
        );
        assert_eq!(o.seen(c, after_start(901), WINDOW), Seen::These(vec![]));
        // Content from nobody, or that nothing is known of, is every origin from when it
        // arrives.
        o.carry(&[Who::Nobody], &[b], after_start(1_000));
        assert_eq!(o.seen(b, after_start(1_899), WINDOW), Seen::Every(CARRIED));
        assert_eq!(o.seen(b, after_start(1_901), WINDOW), Seen::These(vec![]));
        o.saturate(&[c, Who::Nobody], after_start(2_000), CARRIED);
        assert_eq!(o.seen(c, after_start(2_899), WINDOW), Seen::Every(CARRIED));
        assert_eq!(o.seen(c, after_start(2_901), WINDOW), Seen::These(vec![]));
        // So is a model's response that couldn't be read, with its own reason.
        o.saturate(&[a], after_start(3_000), UNREAD);
        assert_eq!(o.seen(a, after_start(3_001), WINDOW), Seen::Every(UNREAD));
        assert_eq!(o.seen(a, after_start(3_901), WINDOW), Seen::These(vec![]));
    }

    #[test]
    fn content_from_an_agent_with_every_origin_carries_what_entered_it_since() {
        let o = Observed::starting(START, WINDOW);
        let (research, billing) = (Who::Agent("research"), Who::Agent("billing"));
        // Research has every origin, carried at 0, then fetches the page at 300 and replies
        // to billing at 400.
        o.saturate(&[research], after_start(0), CARRIED);
        o.note(research, "tool:fetch_page", after_start(300));
        o.note(research, "agent:billing", after_start(300));
        o.carry(&[research], &[billing], after_start(400));
        assert_eq!(o.seen(billing, after_start(900), WINDOW), Seen::Every(CARRIED));
        // Once research's every origin is past, the page counts for billing until its own
        // window ends, and billing's own message doesn't come back to it.
        for at in [901, 1_200] {
            assert_eq!(
                o.seen(billing, after_start(at), WINDOW),
                Seen::These(vec!["tool:fetch_page".into()]),
                "at {at}"
            );
        }
        assert_eq!(o.seen(billing, after_start(1_201), WINDOW), Seen::These(vec![]));

        // From several agents, it's every origin for as long as any of them has it.
        let (child, support, notify) = (
            Who::Unlisted("support/researcher#toolu_1"),
            Who::Agent("support"),
            Who::Agent("notify"),
        );
        o.saturate(&[child], after_start(2_000), CARRIED);
        o.saturate(&[support], after_start(2_500), CARRIED);
        o.carry(&[child, support], &[notify], after_start(2_600));
        assert_eq!(o.seen(notify, after_start(3_400), WINDOW), Seen::Every(CARRIED));
        assert_eq!(o.seen(notify, after_start(3_401), WINDOW), Seen::These(vec![]));

        // What's carried counts toward the receiver's bound, as any origin does: past it, the
        // receiver has every origin from then.
        o.saturate(&[research], after_start(4_000), CARRIED);
        for i in 0..MAX_ORIGINS {
            o.note(research, &format!("tool:t{i}"), after_start(4_001));
        }
        o.note(billing, "tool:search", after_start(4_001));
        o.carry(&[research], &[billing], after_start(4_001));
        assert_eq!(
            o.seen(billing, after_start(4_901), WINDOW),
            Seen::Every("more origins entered the agent than wake-proxy keeps")
        );
        assert_eq!(o.seen(billing, after_start(4_902), WINDOW), Seen::These(vec![]));
    }

    #[test]
    fn a_caller_the_map_doesnt_list_or_place_could_be_any_agent() {
        let agents = Agents::from_json(br#"{"support": ["0oa1support"]}"#).unwrap();
        let child = "spiffe://wake.local/agent/support/researcher";
        assert_eq!(behind(&agents, Who::of(&agents, Some(child)), None), Err(UNPLACED));
        let own = Token {
            subject: "0oa1support".into(),
            actors: vec![child.into()],
            ..Default::default()
        };
        assert_eq!(
            behind(&agents, Who::of(&agents, Some(child)), Some(&own)),
            Ok(vec![Who::Unlisted(child), Who::Unlisted(child), Who::Agent("support")]),
            "its own token places it under support"
        );
        let anothers = Token {
            actors: vec!["support/other#toolu_2".into()],
            ..own.clone()
        };
        assert_eq!(
            behind(&agents, Who::of(&agents, Some(child)), Some(&anothers)),
            Err(UNPLACED)
        );
        let cut = Token {
            cut: true,
            ..own.clone()
        };
        assert_eq!(behind(&agents, Who::Agent("support"), Some(&cut)), Err(CUT_CHAIN));
        // A caller the map lists is itself, and without a map, every identity is an agent.
        assert_eq!(
            behind(&agents, Who::of(&agents, Some("0oa1support")), None),
            Ok(vec![Who::Agent("support")])
        );
        let none = Agents::none();
        assert_eq!(
            behind(&none, Who::of(&none, Some(child)), None),
            Ok(vec![Who::Agent(child)])
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
