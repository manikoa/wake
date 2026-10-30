//! The Envoy ext_authz service agentgateway calls before each request on a route with an
//! `extAuthz` policy: the proxy deny lever's enforcement (Brake). See halts.rs for what's in
//! force and docs/proxy.md for the gateway config.
//!
//! A call is identified by what the gateway config computes: the caller from the `wake`
//! metadata's `agent` (the same CEL as capture: SPIFFE ID, else a delegated JWT's actor,
//! act.sub, else its subject), falling back
//! to the mTLS peer's principal, and the agent a route reaches from its static `receiver`
//! context. The identity map (agents.rs) names both. A denied call gets a 403 whose
//! `wake-halts` header names every halt that covers it, and `wake-halt` the first, and is
//! recorded as a signed `denied` event, linked to the step that made it.

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::Ordering;

use tonic::{Request, Response, Status};

use crate::agents::Agents;
use crate::budget::Events;
use crate::canonical::{Event, Value};
use crate::charters::{self, Resource};
use crate::export::Counters;
use crate::halts::{Halt, Halts};
use crate::mcp;
use crate::origins::{self, Observed, Seen, Who};
use crate::policy::{At, Denial, FlowPolicy, Policy, Target};
use crate::token::{self, Token};

// Generated from Envoy's protos, doc comments and all.
#[allow(clippy::all)]
pub mod pb {
    pub mod envoy {
        pub mod service {
            pub mod common {
                pub mod v3 {
                    tonic::include_proto!("envoy.service.common.v3");
                }
            }
            pub mod auth {
                pub mod v3 {
                    tonic::include_proto!("envoy.service.auth.v3");
                }
            }
        }
    }
    pub use envoy::service::auth::v3 as auth;
    pub use envoy::service::common::v3 as common;
}

use pb::auth::authorization_server::{Authorization, AuthorizationServer};
use pb::auth::{CheckRequest, CheckResponse, DeniedHttpResponse, OkHttpResponse, check_response};
use pb::common::{HeaderValue, HeaderValueOption, HttpStatus};

/// The metadata namespace the gateway config fills for Wake, and its key for the caller.
pub const METADATA: &str = "wake";
pub const METADATA_AGENT: &str = "agent";
/// The static context key naming the agent a route reaches.
pub const CONTEXT_RECEIVER: &str = "receiver";
/// The response header naming the first halt that denied a call.
pub const HEADER_HALT: &str = "wake-halt";
/// The response header naming every halt that denied a call, comma separated, in the order
/// they were put. A probe for any one of them finds it here.
pub const HEADER_HALTS: &str = "wake-halts";

const PERMISSION_DENIED: i32 = 7;

/// What a check is about.
#[derive(Debug, Default, PartialEq)]
pub struct Call {
    pub caller: Option<String>,
    pub receiver: Option<String>,
    pub method: String,
    pub host: String,
    pub path: String,
    pub headers: HashMap<String, String>,
    /// What the gateway says about the token the caller presented, if it presented a JWT.
    pub token: Option<Token>,
}

pub fn read_call(request: &CheckRequest) -> Call {
    let Some(attributes) = &request.attributes else {
        return Call::default();
    };
    let wake = attributes
        .metadata_context
        .as_ref()
        .and_then(|m| m.filter_metadata.get(METADATA));
    let from_metadata = wake
        .and_then(|s| s.fields.get(METADATA_AGENT))
        .and_then(|v| match &v.kind {
            Some(prost_types::value::Kind::StringValue(s)) => Some(s.clone()),
            _ => None,
        });
    let token = wake
        .and_then(|s| s.fields.get(token::META_TOKEN))
        .and_then(Token::from_value);
    let principal = attributes.source.as_ref().map(|s| s.principal.clone());
    let caller = from_metadata
        .filter(|s| !s.is_empty())
        .or(principal)
        .filter(|s| !s.is_empty());
    let receiver = attributes
        .context_extensions
        .get(CONTEXT_RECEIVER)
        .filter(|s| !s.is_empty())
        .cloned();
    let http = attributes.request.as_ref().and_then(|r| r.http.as_ref());
    Call {
        caller,
        receiver,
        method: http.map(|h| h.method.clone()).unwrap_or_default(),
        host: http.map(|h| h.host.clone()).unwrap_or_default(),
        path: http.map(|h| h.path.clone()).unwrap_or_default(),
        headers: http.map(|h| h.headers.clone()).unwrap_or_default(),
        token,
    }
}

pub fn allow() -> CheckResponse {
    CheckResponse {
        status: Some(pb::common::Status {
            code: 0,
            ..Default::default()
        }),
        http_response: Some(check_response::HttpResponse::OkResponse(OkHttpResponse::default())),
        ..Default::default()
    }
}

/// The answer for a call these halts deny. halts isn't empty.
pub fn deny(halts: &[Halt]) -> CheckResponse {
    let first = halts.first().map(|h| h.id.as_str()).unwrap_or_default();
    let ids: Vec<&str> = halts.iter().map(|h| h.id.as_str()).collect();
    let header = |key: &str, value: String| HeaderValueOption {
        header: Some(HeaderValue {
            key: key.into(),
            raw_value: value.into_bytes(),
            ..Default::default()
        }),
        ..Default::default()
    };
    CheckResponse {
        status: Some(pb::common::Status {
            code: PERMISSION_DENIED,
            message: format!("halted by Wake ({})", ids.join(", ")),
            ..Default::default()
        }),
        http_response: Some(check_response::HttpResponse::DeniedResponse(DeniedHttpResponse {
            status: Some(HttpStatus { code: 403 }),
            headers: vec![header(HEADER_HALT, first.into()), header(HEADER_HALTS, ids.join(","))],
            body: serde_json::json!({"error": "halted", "halt": first, "halts": ids}).to_string(),
        })),
        ..Default::default()
    }
}

/// The signed record of a denied call: who tried, what, under which halts, and which of the
/// caller's steps it came from. halts isn't empty; the first is the one named. The caller is
/// recorded under its agent's name, with the identity it authenticated as when that differs.
pub fn denied_event(call: &Call, halts: &[Halt], agents: &Agents, at: u64) -> Event {
    let header = |name: &str| call.headers.get(name).map(String::as_str);
    let trace = header(mcp::HEADER_TRACEPARENT).and_then(mcp::parse_traceparent);
    let parents = header(mcp::HEADER_PARENTS)
        .map(|p| mcp::event_ids(p.split(',')))
        .unwrap_or_default();
    let (trace_id, span_id) = ids();
    let mut e = Event {
        trace_id: trace.map(|t| t.trace_id).unwrap_or(trace_id),
        span_id,
        parent_span_id: trace.map(|t| t.span_id),
        start_time_unix_nano: at,
        end_time_unix_nano: at,
        ..Default::default()
    };
    let caller = call
        .caller
        .as_deref()
        .map(|c| agents.record(c).to_string())
        .unwrap_or_else(|| "unknown".into());
    let identity = call.caller.as_deref().filter(|c| *c != caller);
    e.name = format!("denied {} {}", call.method, call.path).trim().to_string();
    e.set("wake.event.id", Value::Str(uuid::Uuid::new_v4().to_string()));
    e.set("wake.event.kind", Value::Str("denied".into()));
    e.set("wake.capture", Value::Str("proxy".into()));
    e.set("wake.agent.id", Value::Str(caller.clone()));
    e.set("wake.parents", Value::Strs(parents));
    let first = halts.first().map(|h| h.id.clone()).unwrap_or_default();
    e.set("wake.brake.halt_id", Value::Str(first.clone()));
    if let Some(receiver) = &call.receiver {
        e.set("wake.brake.receiver", Value::Str(receiver.clone()));
    }
    if let Some(token) = &call.token {
        token.record(&mut e);
    }
    e.set(
        "wake.payload",
        Value::Str(
            serde_json::json!({
                "caller": caller,
                "identity": identity,
                "receiver": call.receiver,
                "method": call.method,
                "host": call.host,
                "path": call.path,
                "halt": first,
                "halts": halts.iter().map(|h| h.id.as_str()).collect::<Vec<_>>(),
            })
            .to_string(),
        ),
    );
    e
}

/// The signed record of a call that was under way when a halt landed, and was cut: a
/// denied event, as for a call refused at the start, whose payload says it was cut, and
/// names the event recording what the call carried, if there is one.
pub fn cut_event(call: &Call, halts: &[Halt], agents: &Agents, at: u64, recorded_as: Option<&str>) -> Event {
    let mut e = denied_event(call, halts, agents, at);
    e.name = format!("cut {} {}", call.method, call.path).trim().to_string();
    if let Some(Value::Str(payload)) = e.attributes.get("wake.payload")
        && let Ok(serde_json::Value::Object(mut p)) = serde_json::from_str::<serde_json::Value>(payload)
    {
        p.insert("cut".into(), true.into());
        if let Some(id) = recorded_as {
            p.insert("recorded_as".into(), id.into());
        }
        e.set("wake.payload", Value::Str(serde_json::Value::Object(p).to_string()));
    }
    e
}

/// The signed record of a call a flow policy denied: a denied event whose payload also says
/// which origins, of those that entered the caller, the rule kept from the call. They go in
/// the payload, not in wake.origins: no content entered at this event.
pub fn policy_denied_event(call: &Call, denial: &Denial, agents: &Agents, at: u64) -> Event {
    let mut e = denied_event(call, std::slice::from_ref(&denial.halt), agents, at);
    if let Some(Value::Str(payload)) = e.attributes.get("wake.payload")
        && let Ok(serde_json::Value::Object(mut p)) = serde_json::from_str::<serde_json::Value>(payload)
    {
        p.insert("origins".into(), denial.origins.clone().into());
        if let Some(why) = denial.every {
            p.insert("every_origin".into(), why.into());
        }
        e.set("wake.payload", Value::Str(serde_json::Value::Object(p).to_string()));
    }
    e
}

/// The signed record of a call a run charter denied: a denied event whose payload also says
/// which charter, why, and in words what it found.
pub fn charter_denied_event(call: &Call, denial: &charters::Denial, agents: &Agents, at: u64) -> Event {
    let mut e = denied_event(call, std::slice::from_ref(&denial.halt), agents, at);
    if let Some(Value::Str(payload)) = e.attributes.get("wake.payload")
        && let Ok(serde_json::Value::Object(mut p)) = serde_json::from_str::<serde_json::Value>(payload)
    {
        p.insert("charter".into(), denial.halt.id.trim_start_matches("charter:").into());
        p.insert("why".into(), denial.why.into());
        p.insert("detail".into(), denial.detail.clone().into());
        e.set("wake.payload", Value::Str(serde_json::Value::Object(p).to_string()));
    }
    e
}

/// What a policy decides on for a call from caller: what entered it, or any agent the call
/// is made under, within the window. Every origin when the caller could be any agent
/// (origins::behind).
pub fn check_policy(
    policy: &FlowPolicy,
    observed: &Observed,
    agents: &Agents,
    at_hook: At,
    target: &Target<'_>,
    now: u64,
) -> Option<Denial> {
    let seen = match origins::behind(agents, target.caller, target.token) {
        Ok(whos) => observed.seen_by(&whos, now, policy.window_seconds),
        Err(why) => Seen::Every(why),
    };
    policy.deciding(at_hook, target, &seen, agents)
}

fn ids() -> ([u8; 16], [u8; 8]) {
    let mut span = [0u8; 8];
    span.copy_from_slice(&uuid::Uuid::new_v4().as_bytes()[..8]);
    (*uuid::Uuid::new_v4().as_bytes(), span)
}

/// The gRPC service.
pub struct Authorizer {
    halts: Arc<Halts>,
    policy: Arc<Policy>,
    observed: Arc<Observed>,
    events: Events,
    counters: Arc<Counters>,
}

impl Authorizer {
    pub fn new(halts: Arc<Halts>, events: Events, counters: Arc<Counters>) -> Self {
        Self::with_policy(
            halts,
            Arc::new(Policy::fixed(FlowPolicy::empty())),
            Arc::new(Observed::starting(0, crate::policy::DEFAULT_WINDOW_SECS)),
            events,
            counters,
        )
    }

    /// Also denies the calls policy denies, given what observed saw enter each caller.
    pub fn with_policy(
        halts: Arc<Halts>,
        policy: Arc<Policy>,
        observed: Arc<Observed>,
        events: Events,
        counters: Arc<Counters>,
    ) -> Self {
        Authorizer {
            halts,
            policy,
            observed,
            events,
            counters,
        }
    }

    pub fn server(self) -> AuthorizationServer<Self> {
        AuthorizationServer::new(self)
    }
}

#[tonic::async_trait]
impl Authorization for Authorizer {
    async fn check(&self, request: Request<CheckRequest>) -> Result<Response<CheckResponse>, Status> {
        let call = read_call(request.get_ref());
        let now = crate::halts::now();
        let agents = self.halts.agents();
        self.halts.note_caller(call.caller.as_deref(), call.token.as_ref(), now);
        let halts = self
            .halts
            .deciding_with(call.caller.as_deref(), call.receiver.as_deref(), call.token.as_ref());
        if !halts.is_empty() {
            self.counters.denied.fetch_add(1, Ordering::Relaxed);
            self.events.send(denied_event(&call, &halts, agents, now));
            return Ok(Response::new(deny(&halts)));
        }
        let target = Target {
            caller: Who::of(agents, call.caller.as_deref()),
            token: call.token.as_ref(),
            receiver: call.receiver.as_deref(),
            path: Some(&call.path),
            tool: None,
        };
        let policy = self.policy.current();
        if let Some(denial) = check_policy(&policy, &self.observed, agents, At::Authz, &target, now) {
            self.counters.denied.fetch_add(1, Ordering::Relaxed);
            self.events.send(policy_denied_event(&call, &denial, agents, now));
            return Ok(Response::new(deny(std::slice::from_ref(&denial.halt))));
        }
        // A run's charter: past its expiry every call is denied here, and an A2A request is
        // checked against never by the agent its route reaches. MCP calls are checked, and
        // counted, at the MCP hook, which sees the tool.
        let resource = call.receiver.as_deref().map(Resource::Agent);
        if let Some(denial) = self
            .halts
            .charter_deciding(call.caller.as_deref(), call.token.as_ref(), resource, now)
        {
            self.counters.denied.fetch_add(1, Ordering::Relaxed);
            self.events.send(charter_denied_event(&call, &denial, agents, now));
            return Ok(Response::new(deny(std::slice::from_ref(&denial.halt))));
        }
        Ok(Response::new(allow()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::budget::Budget;
    use crate::halts::Scope;
    use pb::auth::attribute_context::{HttpRequest, Peer, Request as AttrRequest};
    use pb::auth::{AttributeContext, CheckRequest};

    fn check(agent: Option<&str>, principal: &str, receiver: Option<&str>, headers: &[(&str, &str)]) -> CheckRequest {
        let metadata = agent.map(|a| pb::common::Metadata {
            filter_metadata: [(
                METADATA.to_string(),
                prost_types::Struct {
                    fields: [(
                        METADATA_AGENT.to_string(),
                        prost_types::Value {
                            kind: Some(prost_types::value::Kind::StringValue(a.into())),
                        },
                    )]
                    .into(),
                },
            )]
            .into(),
        });
        CheckRequest {
            attributes: Some(AttributeContext {
                source: Some(Peer {
                    principal: principal.into(),
                    ..Default::default()
                }),
                request: Some(AttrRequest {
                    http: Some(HttpRequest {
                        method: "POST".into(),
                        host: "agentgateway:3000".into(),
                        path: "/agents/billing".into(),
                        headers: headers.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect(),
                        ..Default::default()
                    }),
                    ..Default::default()
                }),
                context_extensions: receiver
                    .map(|r| [(CONTEXT_RECEIVER.to_string(), r.to_string())].into())
                    .unwrap_or_default(),
                metadata_context: metadata,
                ..Default::default()
            }),
        }
    }

    #[test]
    fn reads_the_caller_from_metadata_then_the_peer() {
        let call = read_call(&check(Some("support"), "spiffe://x/peer", Some("billing"), &[]));
        assert_eq!(call.caller.as_deref(), Some("support"));
        assert_eq!(call.receiver.as_deref(), Some("billing"));
        assert_eq!(call.path, "/agents/billing");
        let call = read_call(&check(Some(""), "spiffe://x/peer", None, &[]));
        assert_eq!(call.caller.as_deref(), Some("spiffe://x/peer"));
        assert_eq!(read_call(&check(None, "", None, &[])).caller, None);
        assert_eq!(read_call(&CheckRequest::default()), Call::default());
    }

    /// A sub-agent calling with a token from token exchange: the gateway names it by the
    /// agent acting, and passes what the token says beside it.
    fn delegated(agent: &str) -> CheckRequest {
        use crate::token::{facts, text};
        let mut request = check(Some(agent), "", Some("billing"), &[]);
        let wake = request
            .attributes
            .as_mut()
            .and_then(|a| a.metadata_context.as_mut())
            .and_then(|m| m.filter_metadata.get_mut(METADATA))
            .unwrap();
        let chain = facts(&[("sub", text(agent)), ("act", facts(&[("sub", text("support"))]))]);
        wake.fields.insert(
            token::META_TOKEN.into(),
            facts(&[("sub", text("user-42")), ("jti", text("tok-7")), ("act", chain)]),
        );
        request
    }

    #[test]
    fn reads_the_token_the_caller_presented() {
        let call = read_call(&delegated("support/researcher#toolu_1"));
        assert_eq!(call.caller.as_deref(), Some("support/researcher#toolu_1"));
        let token = call.token.unwrap();
        assert_eq!(token.subject, "user-42");
        assert_eq!(token.actors, ["support/researcher#toolu_1", "support"]);
        assert_eq!(token.id.as_deref(), Some("tok-7"));
        // An SVID caller has no token.
        assert_eq!(read_call(&check(Some("support"), "", None, &[])).token, None);
    }

    #[tokio::test]
    async fn a_denied_call_records_the_token_it_came_with() {
        let halts = Arc::new(Halts::in_memory());
        let counters = Arc::new(Counters::default());
        let (tx, mut rx) = Events::channel(8, Budget::new(1 << 20), counters.clone());
        let authz = Authorizer::new(halts.clone(), tx, counters);
        halts
            .put(
                "halt-sub",
                Scope {
                    fleet: false,
                    agents: vec!["support/researcher#toolu_1".into()],
                    tokens: vec![],
                },
                1,
            )
            .unwrap();
        let denied = authz
            .check(Request::new(delegated("support/researcher#toolu_1")))
            .await
            .unwrap();
        assert_eq!(denied.get_ref().status.as_ref().unwrap().code, PERMISSION_DENIED);
        let event = rx.try_recv().unwrap().event;
        assert_eq!(event.str("wake.agent.id"), Some("support/researcher#toolu_1"));
        assert_eq!(event.str("wake.token.subject"), Some("user-42"));
        assert_eq!(event.str("wake.token.id"), Some("tok-7"));
        assert_eq!(
            event.attributes["wake.token.actors"],
            Value::Strs(vec!["support/researcher#toolu_1".into(), "support".into()])
        );
        assert_eq!(event.attributes["wake.token.bound"], Value::Bool(false));
        // Its parent isn't halted: the same delegation, acting as support, goes on.
        let allowed = authz.check(Request::new(delegated("support"))).await.unwrap();
        assert_eq!(allowed.get_ref().status.as_ref().unwrap().code, 0);
    }

    #[tokio::test]
    async fn a_halt_on_the_parent_denies_its_sub_agents_and_each_caller_is_noted() {
        let halts = Arc::new(Halts::in_memory());
        let counters = Arc::new(Counters::default());
        let (tx, mut rx) = Events::channel(8, Budget::new(1 << 20), counters.clone());
        let authz = Authorizer::new(halts.clone(), tx, counters);
        halts.put("halt-support", halted(&["support"]), 1).unwrap();
        let denied = authz
            .check(Request::new(delegated("support/researcher#toolu_1")))
            .await
            .unwrap();
        assert_eq!(denied_by(denied.get_ref()).unwrap().0, "halt-support");
        let event = rx.try_recv().unwrap().event;
        assert_eq!(event.str("wake.agent.id"), Some("support/researcher#toolu_1"));
        assert_eq!(event.str("wake.brake.halt_id"), Some("halt-support"));
        let allowed = authz
            .check(Request::new(check(Some("research"), "", None, &[])))
            .await
            .unwrap();
        assert_eq!(allowed.get_ref().status.as_ref().unwrap().code, 0);

        // Allowed or not, each caller is noted for the brake, with what its token said.
        let callers = halts.callers();
        let child = &callers["support/researcher#toolu_1"];
        assert_eq!(child.subject.as_deref(), Some("user-42"));
        assert_eq!(child.actors, ["support/researcher#toolu_1", "support"]);
        assert_eq!(child.tokens, ["tok-7"]);
        assert_eq!(child.calls, 1);
        assert!(callers["research"].tokens.is_empty());
        assert_eq!(callers.len(), 2);
    }

    #[tokio::test]
    async fn a_halt_by_token_denies_only_calls_with_it() {
        let halts = Arc::new(Halts::in_memory());
        let counters = Arc::new(Counters::default());
        let (tx, _rx) = Events::channel(8, Budget::new(1 << 20), counters.clone());
        let authz = Authorizer::new(halts.clone(), tx, counters);
        let scope = Scope {
            tokens: vec!["tok-7".into()],
            ..Default::default()
        };
        halts.put("halt-token", scope, 1).unwrap();
        let denied = authz
            .check(Request::new(delegated("support/researcher#toolu_1")))
            .await
            .unwrap();
        assert_eq!(denied_by(denied.get_ref()).unwrap().0, "halt-token");
        // The same caller with no token, and a caller nobody can name, go on.
        for request in [
            check(Some("support/researcher#toolu_1"), "", None, &[]),
            check(None, "", None, &[]),
        ] {
            let allowed = authz.check(Request::new(request)).await.unwrap();
            assert_eq!(allowed.get_ref().status.as_ref().unwrap().code, 0);
        }
    }

    #[tokio::test]
    async fn denies_in_scope_records_it_and_allows_the_rest() {
        let halts = Arc::new(Halts::in_memory());
        let counters = Arc::new(Counters::default());
        let (tx, mut rx) = Events::channel(8, Budget::new(1 << 20), counters.clone());
        let authz = Authorizer::new(halts.clone(), tx, counters.clone());

        let allowed = authz
            .check(Request::new(check(Some("billing"), "", None, &[])))
            .await
            .unwrap();
        assert_eq!(allowed.get_ref().status.as_ref().unwrap().code, 0);
        assert!(rx.try_recv().is_err(), "allowed calls aren't recorded here");

        halts
            .put(
                "halt-1",
                Scope {
                    fleet: false,
                    agents: vec!["billing".into()],
                    tokens: vec![],
                },
                1,
            )
            .unwrap();
        let traceparent = "00-0102030405060708090a0b0c0d0e0f10-1112131415161718-01";
        let denied = authz
            .check(Request::new(check(
                Some("support"),
                "",
                Some("billing"),
                &[("traceparent", traceparent), ("wake-parents", "support-step-4")],
            )))
            .await
            .unwrap();
        let r = denied.get_ref();
        assert_eq!(r.status.as_ref().unwrap().code, PERMISSION_DENIED);
        let Some(check_response::HttpResponse::DeniedResponse(d)) = &r.http_response else {
            panic!("{r:?}")
        };
        assert_eq!(d.status.as_ref().unwrap().code, 403);
        assert_eq!(d.headers[0].header.as_ref().unwrap().raw_value, b"halt-1");

        let event = rx.try_recv().unwrap().event;
        assert_eq!(event.str("wake.event.kind"), Some("denied"));
        assert_eq!(event.str("wake.agent.id"), Some("support"));
        assert_eq!(event.str("wake.brake.halt_id"), Some("halt-1"));
        assert_eq!(event.str("wake.brake.receiver"), Some("billing"));
        assert_eq!(
            event.attributes["wake.parents"],
            Value::Strs(vec!["support-step-4".into()])
        );
        assert_eq!(hex::encode(event.trace_id), "0102030405060708090a0b0c0d0e0f10");
        assert!(serde_json::from_str::<serde_json::Value>(event.str("wake.payload").unwrap()).is_ok());
        assert_eq!(counters.denied.load(Ordering::Relaxed), 1);

        let other = authz
            .check(Request::new(check(Some("support"), "", Some("research"), &[])))
            .await
            .unwrap();
        assert_eq!(other.get_ref().status.as_ref().unwrap().code, 0);
    }

    fn denied_by(response: &CheckResponse) -> Option<(String, String)> {
        let Some(check_response::HttpResponse::DeniedResponse(d)) = &response.http_response else {
            return None;
        };
        let header = |name: &str| {
            d.headers
                .iter()
                .filter_map(|h| h.header.as_ref())
                .find(|h| h.key == name)
                .map(|h| String::from_utf8(h.raw_value.clone()).unwrap())
                .unwrap_or_default()
        };
        Some((header(HEADER_HALT), header(HEADER_HALTS)))
    }

    fn halted(agents: &[&str]) -> Scope {
        Scope {
            fleet: agents.is_empty(),
            agents: agents.iter().map(|a| a.to_string()).collect(),
            tokens: vec![],
        }
    }

    #[tokio::test]
    async fn an_agent_halt_denies_the_agents_own_calls_and_unknown_callers() {
        let map =
            br#"{"billing": ["spiffe://wake.local/agent/billing"], "support": ["spiffe://wake.local/agent/support"]}"#;
        let halts = Arc::new(Halts::in_memory().with_agents(Arc::new(Agents::from_json(map).unwrap())));
        let counters = Arc::new(Counters::default());
        let (tx, mut rx) = Events::channel(8, Budget::new(1 << 20), counters.clone());
        let authz = Authorizer::new(halts.clone(), tx, counters);
        // The brake halts billing by its short name.
        halts.put("halt-billing", halted(&["billing"]), 1).unwrap();

        // Billing calling an MCP tool: an mTLS peer, no receiver.
        let r = authz
            .check(Request::new(check(
                None,
                "spiffe://wake.local/agent/billing",
                None,
                &[],
            )))
            .await
            .unwrap();
        assert_eq!(
            denied_by(r.get_ref()),
            Some(("halt-billing".into(), "halt-billing".into()))
        );
        let event = rx.try_recv().unwrap().event;
        assert_eq!(event.str("wake.agent.id"), Some("billing"));
        let payload: serde_json::Value = serde_json::from_str(event.str("wake.payload").unwrap()).unwrap();
        assert_eq!(payload["identity"], "spiffe://wake.local/agent/billing");

        // A caller with no identity, or one the map doesn't list, could be billing.
        for (agent, principal) in [(None, ""), (Some(""), ""), (None, "spiffe://wake.local/agent/new")] {
            let r = authz
                .check(Request::new(check(agent, principal, None, &[])))
                .await
                .unwrap();
            assert!(denied_by(r.get_ref()).is_some(), "{agent:?} {principal:?}");
        }
        // Support may still call its tools.
        let r = authz
            .check(Request::new(check(
                None,
                "spiffe://wake.local/agent/support",
                None,
                &[],
            )))
            .await
            .unwrap();
        assert_eq!(r.get_ref().status.as_ref().unwrap().code, 0);
    }

    #[tokio::test]
    async fn a_policy_decides_on_what_entered_the_caller_not_on_what_it_sends() {
        let halts = Arc::new(Halts::in_memory());
        let counters = Arc::new(Counters::default());
        let (tx, mut rx) = Events::channel(8, Budget::new(1 << 20), counters.clone());
        let policy = FlowPolicy::parse(
            br#"{"rules": [{"id": "billing-hears-support-only", "receivers": ["billing"], "allow_origins": ["agent:support"]}]}"#,
        )
        .unwrap();
        let observed = Arc::new(Observed::starting(0, crate::policy::DEFAULT_WINDOW_SECS));
        let authz = Authorizer::with_policy(halts, Arc::new(Policy::fixed(policy)), observed.clone(), tx, counters);
        let to_billing =
            |origins: &'static str| check(Some("planner"), "", Some("billing"), &[("wake-origins", origins)]);

        // Nothing has entered planner: its message to billing goes through, whatever it claims.
        let r = authz.check(Request::new(to_billing("tool:fetch_page"))).await.unwrap();
        assert_eq!(r.get_ref().status.as_ref().unwrap().code, 0);
        assert!(rx.try_recv().is_err());

        // A page planner fetched entered it: denied, and claiming otherwise changes nothing.
        observed.note(Who::Agent("planner"), "tool:fetch_page", crate::halts::now());
        let r = authz.check(Request::new(to_billing("agent:support"))).await.unwrap();
        let id = "policy:billing-hears-support-only";
        assert_eq!(denied_by(r.get_ref()), Some((id.into(), id.into())));
        let event = rx.try_recv().unwrap().event;
        assert_eq!(event.str("wake.brake.halt_id"), Some(id));
        assert_eq!(event.str("wake.agent.id"), Some("planner"));
        assert!(
            !event.attributes.contains_key("wake.origins"),
            "nothing entered at a denial"
        );
        let payload: serde_json::Value = serde_json::from_str(event.str("wake.payload").unwrap()).unwrap();
        assert_eq!(payload["origins"], serde_json::json!(["tool:fetch_page"]));

        // Planner's calls to other agents aren't covered.
        let r = authz
            .check(Request::new(check(Some("planner"), "", Some("research"), &[])))
            .await
            .unwrap();
        assert_eq!(r.get_ref().status.as_ref().unwrap().code, 0);
    }

    #[test]
    fn a_caller_the_agents_file_doesnt_list_or_place_has_every_origin() {
        let agents =
            Agents::from_json(br#"{"support": ["spiffe://wake.local/agent/support", "0oa1support"]}"#).unwrap();
        let observed = Observed::starting(0, crate::policy::DEFAULT_WINDOW_SECS);
        let now = crate::halts::now();
        let pay = |policy: &str, agents: &Agents, caller: &str, token: Option<&Token>| {
            let target = Target {
                caller: Who::of(agents, Some(caller)),
                token,
                receiver: None,
                path: None,
                tool: Some("pay"),
            };
            let policy = FlowPolicy::parse(policy.as_bytes()).unwrap();
            check_policy(&policy, &observed, agents, At::Mcp, &target, now)
        };
        let for_support = r#"{"rules": [{"id": "no-pay-after-web", "callers": ["support"], "tools": ["pay"], "forbid_origins": ["tool:fetch_page"]}]}"#;
        // Support fetched a page, and starts a sub-agent under an SVID of its own, which the
        // file doesn't list, with no token: nothing places it under support, so it could be
        // support, page and all.
        observed.note(
            Who::of(&agents, Some("spiffe://wake.local/agent/support")),
            "tool:fetch_page",
            now,
        );
        let child = "spiffe://wake.local/agent/support/researcher";
        let denial = pay(for_support, &agents, child, None).unwrap();
        assert_eq!(
            (denial.origins, denial.every),
            (vec!["*".to_string()], Some(origins::UNPLACED))
        );
        // Its own token places it under support, and it's held to what entered support.
        let own = Token {
            subject: "0oa1support".into(),
            actors: vec![child.into()],
            ..Default::default()
        };
        let denial = pay(for_support, &agents, child, Some(&own)).unwrap();
        assert_eq!(
            (denial.origins, denial.every),
            (vec!["tool:fetch_page".to_string()], None)
        );
        // Without an agents file, an identity is its own agent, and nothing entered this one.
        let any_caller =
            r#"{"rules": [{"id": "no-pay-after-web", "tools": ["pay"], "forbid_origins": ["tool:fetch_page"]}]}"#;
        assert!(pay(any_caller, &agents, child, None).is_some());
        assert_eq!(pay(any_caller, &Agents::none(), child, None), None);
    }

    #[tokio::test]
    async fn a_runs_charter_is_enforced_at_the_gateway() {
        use crate::charters::Charter;
        let halts = Arc::new(Halts::in_memory());
        let counters = Arc::new(Counters::default());
        let (tx, mut rx) = Events::channel(8, Budget::new(1 << 20), counters.clone());
        let authz = Authorizer::new(halts.clone(), tx, counters);
        let mut charter = Charter {
            id: "run-1".into(),
            scope: halted(&["planner"]),
            expires_at: 4_000_000_000,
            never: r#"@id("not-payments") forbid(principal, action, resource == Wake::Agent::"payments");"#.into(),
            issued_at: 1,
            ..Default::default()
        };
        halts.put_charter(charter.clone()).unwrap();
        let from_planner = |receiver| check(Some("planner"), "", receiver, &[]);

        // An A2A request to an agent the run must never reach is denied, and recorded.
        let r = authz.check(Request::new(from_planner(Some("payments")))).await.unwrap();
        let id = "charter:run-1";
        assert_eq!(denied_by(r.get_ref()), Some((id.into(), id.into())));
        let event = rx.try_recv().unwrap().event;
        assert_eq!(event.str("wake.brake.halt_id"), Some(id));
        let payload: serde_json::Value = serde_json::from_str(event.str("wake.payload").unwrap()).unwrap();
        assert_eq!(
            (&payload["charter"], &payload["why"], &payload["detail"]),
            (
                &serde_json::json!("run-1"),
                &serde_json::json!("never"),
                &serde_json::json!("forbidden by not-payments")
            )
        );
        // Another agent, and an MCP request, whose tool the MCP hook checks, go ahead.
        for receiver in [Some("research"), None] {
            let r = authz.check(Request::new(from_planner(receiver))).await.unwrap();
            assert_eq!(r.get_ref().status.as_ref().unwrap().code, 0);
        }

        // Expired, the charter denies every call the run makes, and only the run's.
        charter.expires_at = 1;
        halts.put_charter(charter).unwrap();
        let r = authz.check(Request::new(from_planner(None))).await.unwrap();
        assert_eq!(denied_by(r.get_ref()), Some((id.into(), id.into())));
        let payload: serde_json::Value =
            serde_json::from_str(rx.try_recv().unwrap().event.str("wake.payload").unwrap()).unwrap();
        assert_eq!(payload["why"], "expired");
        let r = authz
            .check(Request::new(check(Some("billing"), "", Some("payments"), &[])))
            .await
            .unwrap();
        assert_eq!(r.get_ref().status.as_ref().unwrap().code, 0);
    }

    #[tokio::test]
    async fn names_every_halt_that_covers_the_call() {
        let halts = Arc::new(Halts::in_memory());
        let counters = Arc::new(Counters::default());
        let (tx, _rx) = Events::channel(8, Budget::new(1 << 20), counters.clone());
        let authz = Authorizer::new(halts.clone(), tx, counters);
        halts.put("fleet-1", halted(&[]), 1).unwrap();
        halts.put("fleet-2", halted(&[]), 2).unwrap();
        let r = authz
            .check(Request::new(check(Some("support"), "", None, &[])))
            .await
            .unwrap();
        // A probe for the second fleet halt finds it too.
        assert_eq!(
            denied_by(r.get_ref()),
            Some(("fleet-1".into(), "fleet-1,fleet-2".into()))
        );
        let Some(check_response::HttpResponse::DeniedResponse(d)) = &r.get_ref().http_response else {
            unreachable!()
        };
        let body: serde_json::Value = serde_json::from_str(&d.body).unwrap();
        assert_eq!(body["halts"], serde_json::json!(["fleet-1", "fleet-2"]));
    }
}
