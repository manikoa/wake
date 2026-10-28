//! The ExtMCP gRPC service agentgateway calls for each MCP call (its `mcpGuardrails`
//! remote processor). For capture it lets the call through at once: the hook is queued for
//! the capture task and never waits on it, and a full queue, by count or by bytes, drops the
//! hook and counts it rather than slow the call.
//!
//! The deny lever is ext_authz (authz.rs), before the call starts. This hook backs it up
//! for a call a halt catches under way: a request that reaches the hook after a halt that
//! covers its caller is refused, and a result coming back to that caller after one is kept
//! from it, as the gateway's error, and recorded as cut. The tool already ran; the agent
//! never gets what it returned. The hook fails open, as capture does, so this holds only
//! while wake-proxy answers; ext_authz fails closed for every call after.
//!
//! It also checks the flow policy's rules that name tools, since only this hook sees which
//! tool a call is for, and notes the tool as an origin entering its caller (origins.rs)
//! before the call goes on, so the result never reaches the caller ahead of the note.

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::SystemTime;

use prost_types::{Struct, Value as PbValue, value::Kind};
use tokio::sync::mpsc;
use tonic::{Request, Response, Status};

use crate::agents::Agents;
use crate::authz::{self, Call};
use crate::budget::{Budget, Events, Permit};
use crate::capture::{Hook, META_AGENT, META_EVENT_ID};
use crate::export::Counters;
use crate::halts::{Halt, Halts, now};
use crate::mcp;
use crate::origins::{Observed, Who};
use crate::policy::{At, FlowPolicy, Policy, Target};
use crate::token::{self, Token};

pub mod pb {
    tonic::include_proto!("agentgateway.dev.ext_mcp");
}

use pb::ext_mcp_server::ExtMcp;
use pb::{
    AuthorizationError, McpRequest, McpRequestResult, McpResponse, McpResponseResult, Pass, authorization_error,
    mcp_request_result, mcp_response_result,
};

/// Largest hook message accepted. A tool result larger than this is passed through by
/// agentgateway under failOpen and isn't recorded.
pub const MAX_MESSAGE: usize = 32 << 20;

pub struct Processor {
    hooks: mpsc::Sender<(Hook, Permit)>,
    budget: Arc<Budget>,
    counters: Arc<Counters>,
    halts: Arc<Halts>,
    events: Events,
    policy: Arc<Policy>,
    observed: Arc<Observed>,
}

impl Processor {
    /// Queues each hook on hooks with the bytes it holds, taken from budget. Calls a halt
    /// covers are refused or cut, recorded on events, and callers are named through the
    /// halts' identity map.
    pub fn new(
        hooks: mpsc::Sender<(Hook, Permit)>,
        budget: Arc<Budget>,
        counters: Arc<Counters>,
        halts: Arc<Halts>,
        events: Events,
    ) -> Self {
        Processor {
            hooks,
            budget,
            counters,
            halts,
            events,
            policy: Arc::new(Policy::fixed(FlowPolicy::empty())),
            observed: Arc::new(Observed::starting(0, crate::policy::DEFAULT_WINDOW_SECS)),
        }
    }

    /// Checks policy's rules that name tools, and notes each tool called in observed.
    pub fn with_policy(mut self, policy: Arc<Policy>, observed: Arc<Observed>) -> Self {
        self.policy = policy;
        self.observed = observed;
        self
    }

    fn agents(&self) -> &Arc<Agents> {
        self.halts.agents()
    }

    /// The string values in the metadata, with the caller named through the identity map,
    /// the identity the caller authenticated as, and the token it presented, if a JWT.
    fn metadata(&self, s: Option<Struct>) -> (HashMap<String, String>, Option<String>, Option<Token>) {
        let token = s
            .as_ref()
            .and_then(|s| s.fields.get(token::META_TOKEN))
            .and_then(Token::from_value);
        let mut metadata = strings(s);
        let identity = metadata.get(META_AGENT).filter(|a| !a.is_empty()).cloned();
        if let Some(agent) = &identity {
            let named = self.agents().record(agent).to_string();
            metadata.insert(META_AGENT.into(), named);
        }
        (metadata, identity, token)
    }

    /// The halts that cover a call from this caller now.
    fn covering(&self, caller: Option<&str>, token: Option<&Token>) -> Vec<Halt> {
        self.halts.deciding_with(caller, None, token)
    }

    pub fn server(self) -> pb::ext_mcp_server::ExtMcpServer<Self> {
        pb::ext_mcp_server::ExtMcpServer::new(self).max_decoding_message_size(MAX_MESSAGE)
    }

    fn queue(&self, hook: Hook) {
        let queued = self
            .budget
            .take(hook.size())
            .map(|permit| self.hooks.try_send((hook, permit)).is_ok())
            .unwrap_or(false);
        if !queued {
            self.counters.hooks_dropped.fetch_add(1, Ordering::Relaxed);
        }
    }
}

fn pass_request(metadata: Option<Struct>) -> Response<McpRequestResult> {
    Response::new(McpRequestResult {
        result: Some(mcp_request_result::Result::Pass(Pass {})),
        header_mutation: None,
        metadata,
    })
}

fn pass_response() -> Response<McpResponseResult> {
    Response::new(McpResponseResult {
        result: Some(mcp_response_result::Result::Pass(Pass {})),
    })
}

/// The gateway's error for a call these halts cover: a tools/call gets an error result,
/// other methods a JSON-RPC error.
fn halted(halts: &[Halt]) -> AuthorizationError {
    let ids: Vec<&str> = halts.iter().map(|h| h.id.as_str()).collect();
    AuthorizationError {
        code: authorization_error::Code::PermissionDenied as i32,
        reason: format!("halted by Wake ({})", ids.join(", ")),
        mcp_error: None,
    }
}

/// The call's link headers, for the record of refusing it.
fn link_headers(headers: &[(String, Vec<u8>)]) -> HashMap<String, String> {
    headers
        .iter()
        .filter(|(k, _)| k == mcp::HEADER_TRACEPARENT || k == mcp::HEADER_PARENTS)
        .map(|(k, v)| (k.clone(), String::from_utf8_lossy(v).into_owned()))
        .collect()
}

/// The string values in the CEL metadata the gateway config sends. Others are ignored.
fn strings(s: Option<Struct>) -> HashMap<String, String> {
    s.map(|s| {
        s.fields
            .into_iter()
            .filter_map(|(k, v)| match v.kind {
                Some(Kind::StringValue(s)) => Some((k, s)),
                _ => None,
            })
            .collect()
    })
    .unwrap_or_default()
}

#[tonic::async_trait]
impl ExtMcp for Processor {
    async fn check_request(&self, request: Request<McpRequest>) -> Result<Response<McpRequestResult>, Status> {
        let r = request.into_inner();
        let (metadata, caller, token) = self.metadata(r.metadata_context);
        let server = r.service_names.into_iter().next().unwrap_or_default();
        let headers: Vec<(String, Vec<u8>)> = r.headers.into_iter().map(|h| (h.key, h.value)).collect();
        // A halt that landed after ext_authz let the call in: it isn't forwarded.
        let covering = self.covering(caller.as_deref(), token.as_ref());
        if !covering.is_empty() {
            let call = Call {
                caller,
                receiver: None,
                method: r.method,
                host: server,
                path: String::new(),
                headers: link_headers(&headers),
                token: token.clone(),
            };
            self.counters.denied.fetch_add(1, Ordering::Relaxed);
            self.events
                .send(authz::denied_event(&call, &covering, self.agents(), now()));
            return Ok(Response::new(McpRequestResult {
                result: Some(mcp_request_result::Result::Error(halted(&covering))),
                header_mutation: None,
                metadata: None,
            }));
        }
        if !mcp::captured(&r.method) {
            return Ok(pass_request(None));
        }
        if self.policy.enforcing() {
            // What the tool returns enters the caller, so the call is checked against what
            // entered it already, and then noted, before it goes on.
            let at = now();
            let target = mcp::parse_request(&r.method, r.mcp_request.as_deref(), &[]).target;
            let tool = target.filter(|t| !t.is_empty()).unwrap_or_else(|| server.clone());
            let who = Who::of(self.agents(), caller.as_deref());
            let policy = self.policy.current();
            let checked = Target {
                caller: who,
                receiver: None,
                path: None,
                tool: Some(&tool),
            };
            if let Some(denial) = authz::check_policy(&policy, &self.observed, self.agents(), At::Mcp, &checked, at) {
                let call = Call {
                    caller,
                    receiver: None,
                    method: r.method,
                    host: server,
                    path: String::new(),
                    headers: link_headers(&headers),
                    token: token.clone(),
                };
                self.counters.denied.fetch_add(1, Ordering::Relaxed);
                self.events
                    .send(authz::policy_denied_event(&call, &denial, self.agents(), at));
                return Ok(Response::new(McpRequestResult {
                    result: Some(mcp_request_result::Result::Error(halted(std::slice::from_ref(
                        &denial.halt,
                    )))),
                    header_mutation: None,
                    metadata: None,
                }));
            }
            self.observed.note(who, &format!("tool:{tool}"), at);
        }
        self.counters.mcp_calls.fetch_add(1, Ordering::Relaxed);
        let event_id = uuid::Uuid::new_v4().to_string();
        self.queue(Hook::Request {
            event_id: event_id.clone(),
            method: r.method,
            server,
            metadata,
            token,
            params: r.mcp_request,
            headers,
            at: SystemTime::now(),
        });
        // Handed back so the response hook can say which call it belongs to.
        let metadata = Struct {
            fields: [(
                META_EVENT_ID.to_string(),
                PbValue {
                    kind: Some(Kind::StringValue(event_id)),
                },
            )]
            .into(),
        };
        Ok(pass_request(Some(metadata)))
    }

    async fn check_response(&self, request: Request<McpResponse>) -> Result<Response<McpResponseResult>, Status> {
        let r = request.into_inner();
        let (metadata, caller, token) = self.metadata(r.metadata_context);
        let server = r.service_names.into_iter().next().unwrap_or_default();
        let event_id = metadata.get(META_EVENT_ID).filter(|id| !id.is_empty()).cloned();
        let covering = self.covering(caller.as_deref(), token.as_ref());
        let method = r.method.clone();
        // What came back is recorded either way: it's what the tool did.
        if mcp::captured(&r.method) {
            self.queue(Hook::Response {
                method: r.method,
                server: server.clone(),
                metadata,
                result: r.mcp_response,
                at: SystemTime::now(),
            });
        }
        if covering.is_empty() {
            return Ok(pass_response());
        }
        // A halt landed while the call was under way: the caller doesn't get the result.
        let call = Call {
            caller,
            receiver: None,
            method,
            host: server,
            path: String::new(),
            headers: event_id
                .iter()
                .map(|id| (mcp::HEADER_PARENTS.to_string(), id.clone()))
                .collect(),
            token,
        };
        self.counters.cut.fetch_add(1, Ordering::Relaxed);
        self.events.send(authz::cut_event(
            &call,
            &covering,
            self.agents(),
            now(),
            event_id.as_deref(),
        ));
        Ok(Response::new(McpResponseResult {
            result: Some(mcp_response_result::Result::Error(halted(&covering))),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::origins::Seen;

    fn call(agent: &str, tool: &str) -> Request<McpRequest> {
        Request::new(McpRequest {
            service_names: vec!["tools".into()],
            method: "tools/call".into(),
            metadata_context: Some(Struct {
                fields: [(
                    META_AGENT.to_string(),
                    PbValue {
                        kind: Some(Kind::StringValue(agent.into())),
                    },
                )]
                .into(),
            }),
            mcp_request: Some(format!(r#"{{"name":"{tool}","arguments":{{}}}}"#).into_bytes()),
            headers: vec![],
        })
    }

    fn refused(r: &McpRequestResult) -> Option<&str> {
        match &r.result {
            Some(mcp_request_result::Result::Error(e)) => Some(&e.reason),
            _ => None,
        }
    }

    #[tokio::test]
    async fn a_tool_rule_is_checked_against_the_tools_the_caller_called() {
        let counters = Arc::new(Counters::default());
        let (hooks, _hooks_rx) = mpsc::channel(16);
        let (events, mut events_rx) = Events::channel(16, Budget::new(1 << 20), counters.clone());
        let policy = FlowPolicy::parse(
            br#"{"rules": [{"id": "no-pay-after-web", "callers": ["billing"], "tools": ["pay"], "forbid_origins": ["tool:fetch_page"]}]}"#,
        )
        .unwrap();
        let observed = Arc::new(Observed::starting(0, crate::policy::DEFAULT_WINDOW_SECS));
        let p = Processor::new(
            hooks,
            Budget::new(1 << 20),
            counters.clone(),
            Arc::new(Halts::in_memory()),
            events,
        )
        .with_policy(Arc::new(Policy::fixed(policy)), observed.clone());

        // Billing may pay while nothing it fetched has come back to it.
        let r = p.check_request(call("billing", "pay")).await.unwrap();
        assert_eq!(refused(r.get_ref()), None);
        // Fetching a page is a call like any other: it passes, and is noted first.
        let r = p.check_request(call("billing", "fetch_page")).await.unwrap();
        assert_eq!(refused(r.get_ref()), None);
        assert!(
            matches!(observed.seen(Who::Agent("billing"), now(), 900), Seen::These(o) if o.contains(&"tool:fetch_page".to_string()))
        );
        // Now paying is refused, recorded, and named.
        let r = p.check_request(call("billing", "pay")).await.unwrap();
        assert!(refused(r.get_ref()).unwrap().contains("policy:no-pay-after-web"));
        let event = events_rx.try_recv().unwrap().event;
        assert_eq!(event.str("wake.event.kind"), Some("denied"));
        assert_eq!(event.str("wake.brake.halt_id"), Some("policy:no-pay-after-web"));
        let payload: serde_json::Value = serde_json::from_str(event.str("wake.payload").unwrap()).unwrap();
        assert_eq!(payload["origins"], serde_json::json!(["tool:fetch_page"]));
        // Support fetched nothing, and the rule isn't about support anyway.
        let r = p.check_request(call("support", "pay")).await.unwrap();
        assert_eq!(refused(r.get_ref()), None);
    }

    #[tokio::test]
    async fn without_a_policy_nothing_is_checked_or_noted() {
        let counters = Arc::new(Counters::default());
        let (hooks, _hooks_rx) = mpsc::channel(16);
        let (events, _events_rx) = Events::channel(16, Budget::new(1 << 20), counters.clone());
        let observed = Arc::new(Observed::starting(0, crate::policy::DEFAULT_WINDOW_SECS));
        let p = Processor::new(
            hooks,
            Budget::new(1 << 20),
            counters,
            Arc::new(Halts::in_memory()),
            events,
        )
        .with_policy(Arc::new(Policy::fixed(FlowPolicy::empty())), observed.clone());
        let r = p.check_request(call("billing", "fetch_page")).await.unwrap();
        assert_eq!(refused(r.get_ref()), None);
        assert_eq!(observed.seen(Who::Agent("billing"), now(), 900), Seen::These(vec![]));
    }

    #[tokio::test]
    async fn a_captured_call_records_the_token_it_came_with() {
        use crate::canonical::Value;
        use crate::capture::Capture;
        use crate::token::{facts, text};
        use std::time::Duration;

        let counters = Arc::new(Counters::default());
        let (hooks, mut hooks_rx) = mpsc::channel(16);
        let (events, _events_rx) = Events::channel(16, Budget::new(1 << 20), counters.clone());
        let p = Processor::new(
            hooks,
            Budget::new(1 << 20),
            counters,
            Arc::new(Halts::in_memory()),
            events,
        );
        let mut request = call("support/researcher#toolu_1", "lookup_order");
        let chain = facts(&[("sub", text("support/researcher#toolu_1"))]);
        request.get_mut().metadata_context.as_mut().unwrap().fields.insert(
            token::META_TOKEN.into(),
            facts(&[("sub", text("user-42")), ("jti", text("tok-7")), ("act", chain)]),
        );
        assert_eq!(refused(p.check_request(request).await.unwrap().get_ref()), None);

        let (hook, _permit) = hooks_rx.try_recv().unwrap();
        let mut capture = Capture::new(Duration::from_secs(30), 16);
        assert!(capture.handle(hook).is_empty(), "it waits for its response");
        let events = capture.expire(SystemTime::now() + Duration::from_secs(60));
        let [event] = events.as_slice() else {
            panic!("{events:?}")
        };
        assert_eq!(event.str("wake.event.kind"), Some("tool_call"));
        assert_eq!(event.str("wake.agent.id"), Some("support/researcher#toolu_1"));
        assert_eq!(event.str("wake.token.subject"), Some("user-42"));
        assert_eq!(event.str("wake.token.id"), Some("tok-7"));
        assert_eq!(
            event.attributes["wake.token.actors"],
            Value::Strs(vec!["support/researcher#toolu_1".into()])
        );

        // A caller with an SVID presented no token, and none is recorded.
        p.check_request(call("support", "lookup_order")).await.unwrap();
        let (hook, _permit) = hooks_rx.try_recv().unwrap();
        capture.handle(hook);
        let events = capture.expire(SystemTime::now() + Duration::from_secs(60));
        assert!(events.iter().all(|e| !e.attributes.contains_key("wake.token.subject")));
    }
}
