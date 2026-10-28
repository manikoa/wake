//! What the gateway says about the token a caller presented, for the record: whom it acts
//! for, the agents acting under it, its id, when it expires and whether it's bound to its
//! holder. Never the token: the gateway config computes these from the claims of the JWT it
//! verified, the same way for every hook (docs/proxy.md), and with no JWT there are none.
//!
//! A token from OAuth token exchange (RFC 8693) names whoever it acts for in `sub` and the
//! agent acting in `act.sub`, with earlier actors nested deeper in `act`. The gateway names
//! the caller by the agent acting; these keep the rest of the chain, so the record says who
//! a sub-agent was acting for, and which agents the delegation passed through.
//!
//! The claims come from a token the identity provider signed, but they're still bounded:
//! a value too long to be an identifier is left out rather than cut, and a chain longer
//! than MAX_ACTORS is cut there. A chain that goes on past what's kept, past MAX_ACTORS or
//! an actor that can't be read, is marked cut: its earliest actors are unknown, and a halt
//! decides on it as on a caller it can't name (halts.rs).

use prost_types::value::Kind;
use prost_types::{Struct, Value as PbValue};

use crate::canonical::{Event, Value};

/// The key the gateway config passes a caller's token under: in ext_authz's `wake`
/// metadata, in ExtMCP's metadata, and, as `sender_token`, in ext_proc's attributes.
pub const META_TOKEN: &str = "token";
/// The longest subject, actor or token id kept.
pub const MAX_LEN: usize = 1024;
/// The most actors kept from a delegation chain.
pub const MAX_ACTORS: usize = 8;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Token {
    /// Whom the token acts for: its `sub`.
    pub subject: String,
    /// The agents acting under it, the one acting now first, then each before it.
    pub actors: Vec<String>,
    /// Its `jti`.
    pub id: Option<String>,
    /// Its `exp`, in Unix seconds.
    pub expires: Option<i64>,
    /// Whether it's bound to its holder's key (it has a `cnf` claim), rather than a bearer token.
    pub bound: bool,
    /// Whether the chain goes on past the actors kept.
    pub cut: bool,
}

impl Token {
    /// The token the gateway config described in this value, if the caller presented one.
    pub fn from_value(v: &PbValue) -> Option<Token> {
        match &v.kind {
            Some(Kind::StructValue(s)) => Token::from_struct(s),
            _ => None,
        }
    }

    /// The token the gateway config described in these fields: `sub`, `jti`, `exp`, `act`
    /// and `cnf`. None when there's no subject, as when the caller presented no JWT.
    pub fn from_struct(s: &Struct) -> Option<Token> {
        let subject = identifier(s.fields.get("sub"))?;
        let mut actors = vec![];
        let mut act = s.fields.get("act");
        while actors.len() < MAX_ACTORS {
            let Some(Kind::StructValue(a)) = act.and_then(|a| a.kind.as_ref()) else {
                break;
            };
            let Some(actor) = identifier(a.fields.get("sub")) else {
                break;
            };
            actors.push(actor);
            act = a.fields.get("act");
        }
        // Where the walk stopped, an actor with any claims at all is one it didn't keep. The
        // gateway sends an empty object for a token without a chain.
        let cut = matches!(act.and_then(|a| a.kind.as_ref()), Some(Kind::StructValue(a)) if !a.fields.is_empty());
        let expires = match s.fields.get("exp").and_then(|e| e.kind.as_ref()) {
            Some(Kind::NumberValue(n)) if n.is_finite() && *n > 0.0 && n.fract() == 0.0 && *n < i64::MAX as f64 => {
                Some(*n as i64)
            }
            _ => None,
        };
        let bound = matches!(
            s.fields.get("cnf").and_then(|c| c.kind.as_ref()),
            Some(Kind::BoolValue(true))
        );
        Some(Token {
            subject,
            actors,
            id: identifier(s.fields.get("jti")),
            expires,
            bound,
            cut,
        })
    }

    /// Whom a call made with it is made under, besides its caller: each actor in its chain,
    /// the one acting now first, then whom they act for, its subject. Without a chain the
    /// subject is the agent the token was issued to.
    pub fn under(&self) -> impl Iterator<Item = &str> {
        self.actors.iter().chain([&self.subject]).map(String::as_str)
    }

    /// Records the token on an event: `wake.token.subject`, `wake.token.actors`,
    /// `wake.token.actors_cut`, `wake.token.id`, `wake.token.expires_unix` and
    /// `wake.token.bound` (docs/events.md).
    pub fn record(&self, e: &mut Event) {
        e.set("wake.token.subject", Value::Str(self.subject.clone()));
        if !self.actors.is_empty() {
            e.set("wake.token.actors", Value::Strs(self.actors.clone()));
        }
        if self.cut {
            e.set("wake.token.actors_cut", Value::Bool(true));
        }
        if let Some(id) = &self.id {
            e.set("wake.token.id", Value::Str(id.clone()));
        }
        if let Some(expires) = self.expires {
            e.set("wake.token.expires_unix", Value::Int(expires));
        }
        e.set("wake.token.bound", Value::Bool(self.bound));
    }
}

/// A non-empty string no longer than MAX_LEN.
fn identifier(v: Option<&PbValue>) -> Option<String> {
    match v.and_then(|v| v.kind.as_ref()) {
        Some(Kind::StringValue(s)) if !s.is_empty() && s.len() <= MAX_LEN => Some(s.clone()),
        _ => None,
    }
}

#[cfg(test)]
pub(crate) fn facts(fields: &[(&str, PbValue)]) -> PbValue {
    PbValue {
        kind: Some(Kind::StructValue(Struct {
            fields: fields.iter().map(|(k, v)| (k.to_string(), v.clone())).collect(),
        })),
    }
}

#[cfg(test)]
pub(crate) fn text(s: &str) -> PbValue {
    PbValue {
        kind: Some(Kind::StringValue(s.into())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn number(n: f64) -> PbValue {
        PbValue {
            kind: Some(Kind::NumberValue(n)),
        }
    }

    fn boolean(b: bool) -> PbValue {
        PbValue {
            kind: Some(Kind::BoolValue(b)),
        }
    }

    /// What the gateway config computes with no JWT: every claim at its default.
    fn no_jwt() -> PbValue {
        facts(&[
            ("sub", text("")),
            ("jti", text("")),
            ("exp", number(0.0)),
            ("act", facts(&[])),
            ("cnf", boolean(false)),
        ])
    }

    #[test]
    fn no_jwt_no_token() {
        assert_eq!(Token::from_value(&no_jwt()), None);
        assert_eq!(Token::from_value(&text("not a struct")), None);
    }

    #[test]
    fn a_plain_token() {
        let t = Token::from_value(&facts(&[
            ("sub", text("support")),
            ("jti", text("tok-1")),
            ("exp", number(1_793_226_604.0)),
            ("act", facts(&[])),
            ("cnf", boolean(false)),
        ]))
        .unwrap();
        assert_eq!(
            t,
            Token {
                subject: "support".into(),
                actors: vec![],
                id: Some("tok-1".into()),
                expires: Some(1_793_226_604),
                bound: false,
                cut: false,
            }
        );
    }

    #[test]
    fn a_delegation_chain_keeps_every_actor_in_order() {
        let chain = facts(&[
            ("sub", text("support/researcher#toolu_1")),
            ("act", facts(&[("sub", text("support"))])),
        ]);
        let t = Token::from_value(&facts(&[
            ("sub", text("user-42")),
            ("act", chain),
            ("cnf", boolean(true)),
        ]))
        .unwrap();
        assert_eq!(t.subject, "user-42");
        assert_eq!(t.actors, ["support/researcher#toolu_1", "support"]);
        assert!(!t.cut, "the chain ends where it says");
        assert!(t.bound);
        assert_eq!(t.id, None);
        assert_eq!(t.expires, None);
    }

    #[test]
    fn a_chain_stops_at_a_gap_and_at_its_bound() {
        // An actor with no sub ends the chain; nothing past it is guessed at.
        let gap = facts(&[("iss", text("idp")), ("act", facts(&[("sub", text("hidden"))]))]);
        let t = Token::from_value(&facts(&[("sub", text("u")), ("act", gap)])).unwrap();
        assert!(t.actors.is_empty());
        assert!(t.cut, "an actor it couldn't read is one it didn't keep");
        let long = facts(&[("sub", text(&"x".repeat(MAX_LEN + 1)))]);
        let t = Token::from_value(&facts(&[
            ("sub", text("u")),
            ("act", facts(&[("sub", text("child")), ("act", long)])),
        ]))
        .unwrap();
        assert_eq!((t.actors, t.cut), (vec!["child".to_string()], true));
        // An act that isn't an object is no chain.
        let t = Token::from_value(&facts(&[("sub", text("u")), ("act", text("support"))])).unwrap();
        assert!(t.actors.is_empty());
        assert!(!t.cut);
        // A long chain is cut at MAX_ACTORS.
        let mut act = facts(&[("sub", text("a0"))]);
        for i in 1..20 {
            act = facts(&[("sub", text(&format!("a{i}"))), ("act", act)]);
        }
        let t = Token::from_value(&facts(&[("sub", text("u")), ("act", act)])).unwrap();
        assert_eq!(t.actors.len(), MAX_ACTORS);
        assert_eq!(t.actors[0], "a19");
        assert!(t.cut);
        // A chain exactly MAX_ACTORS long is whole.
        let mut act = facts(&[("sub", text("a0"))]);
        for i in 1..MAX_ACTORS {
            act = facts(&[("sub", text(&format!("a{i}"))), ("act", act)]);
        }
        let t = Token::from_value(&facts(&[("sub", text("u")), ("act", act)])).unwrap();
        assert_eq!((t.actors.len(), t.cut), (MAX_ACTORS, false));
    }

    #[test]
    fn odd_claims_are_left_out_not_cut() {
        let long = "x".repeat(MAX_LEN + 1);
        assert_eq!(Token::from_value(&facts(&[("sub", text(&long))])), None);
        let t = Token::from_value(&facts(&[
            ("sub", text("u")),
            ("jti", text(&long)),
            ("exp", number(1.5)),
            ("cnf", text("yes")),
        ]))
        .unwrap();
        assert_eq!((t.id, t.expires, t.bound), (None, None, false));
        for exp in [f64::NAN, f64::INFINITY, -5.0, 1e30] {
            let t = Token::from_value(&facts(&[("sub", text("u")), ("exp", number(exp))])).unwrap();
            assert_eq!(t.expires, None, "{exp}");
        }
    }

    #[test]
    fn recorded_on_an_event() {
        let mut e = Event::default();
        Token {
            subject: "user-42".into(),
            actors: vec!["support/researcher#toolu_1".into(), "support".into()],
            id: Some("tok-1".into()),
            expires: Some(1_793_226_604),
            bound: false,
            cut: true,
        }
        .record(&mut e);
        assert_eq!(e.attributes["wake.token.subject"], Value::Str("user-42".into()));
        assert_eq!(
            e.attributes["wake.token.actors"],
            Value::Strs(vec!["support/researcher#toolu_1".into(), "support".into()])
        );
        assert_eq!(e.attributes["wake.token.id"], Value::Str("tok-1".into()));
        assert_eq!(e.attributes["wake.token.expires_unix"], Value::Int(1_793_226_604));
        assert_eq!(e.attributes["wake.token.bound"], Value::Bool(false));
        assert_eq!(e.attributes["wake.token.actors_cut"], Value::Bool(true));
        let mut e = Event::default();
        Token {
            subject: "support".into(),
            ..Default::default()
        }
        .record(&mut e);
        assert!(!e.attributes.contains_key("wake.token.actors"));
        assert!(!e.attributes.contains_key("wake.token.actors_cut"));
        assert!(!e.attributes.contains_key("wake.token.id"));
        assert!(!e.attributes.contains_key("wake.token.expires_unix"));
    }
}
