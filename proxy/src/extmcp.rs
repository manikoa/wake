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
        }
    }

    fn agents(&self) -> &Arc<Agents> {
        self.halts.agents()
    }

    /// The string values in the metadata, with the caller named through the identity map,
    /// and the identity the caller authenticated as.
    fn metadata(&self, s: Option<Struct>) -> (HashMap<String, String>, Option<String>) {
        let mut metadata = strings(s);
        let identity = metadata.get(META_AGENT).filter(|a| !a.is_empty()).cloned();
        if let Some(agent) = &identity {
            let named = self.agents().record(agent).to_string();
            metadata.insert(META_AGENT.into(), named);
        }
        (metadata, identity)
    }

    /// The halts that cover a call from this caller now.
    fn covering(&self, caller: Option<&str>) -> Vec<Halt> {
        self.halts.deciding(caller, None)
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
        let (metadata, caller) = self.metadata(r.metadata_context);
        let server = r.service_names.into_iter().next().unwrap_or_default();
        let headers: Vec<(String, Vec<u8>)> = r.headers.into_iter().map(|h| (h.key, h.value)).collect();
        // A halt that landed after ext_authz let the call in: it isn't forwarded.
        let covering = self.covering(caller.as_deref());
        if !covering.is_empty() {
            let call = Call {
                caller,
                receiver: None,
                method: r.method,
                host: server,
                path: String::new(),
                headers: headers
                    .iter()
                    .filter(|(k, _)| k == mcp::HEADER_TRACEPARENT || k == mcp::HEADER_PARENTS)
                    .map(|(k, v)| (k.clone(), String::from_utf8_lossy(v).into_owned()))
                    .collect(),
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
        self.counters.mcp_calls.fetch_add(1, Ordering::Relaxed);
        let event_id = uuid::Uuid::new_v4().to_string();
        self.queue(Hook::Request {
            event_id: event_id.clone(),
            method: r.method,
            server,
            metadata,
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
        let (metadata, caller) = self.metadata(r.metadata_context);
        let server = r.service_names.into_iter().next().unwrap_or_default();
        let event_id = metadata.get(META_EVENT_ID).filter(|id| !id.is_empty()).cloned();
        let covering = self.covering(caller.as_deref());
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
