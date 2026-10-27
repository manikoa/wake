//! The ExtMCP gRPC service agentgateway calls for each MCP call (its `mcpGuardrails`
//! remote processor). For capture it always lets the call through, at once: the hook is
//! queued for the capture task and never waits on it, and a full queue drops the hook and
//! counts it rather than slow the call. Deny comes with the Brake phase.

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::SystemTime;

use prost_types::{Struct, Value as PbValue, value::Kind};
use tokio::sync::mpsc;
use tonic::{Request, Response, Status};

use crate::capture::{Hook, META_EVENT_ID};
use crate::export::Counters;
use crate::mcp;

pub mod pb {
    tonic::include_proto!("agentgateway.dev.ext_mcp");
}

use pb::ext_mcp_server::ExtMcp;
use pb::{McpRequest, McpRequestResult, McpResponse, McpResponseResult, Pass, mcp_request_result, mcp_response_result};

/// Largest hook message accepted. A tool result larger than this is passed through by
/// agentgateway under failOpen and isn't recorded.
pub const MAX_MESSAGE: usize = 32 << 20;

pub struct Processor {
    hooks: mpsc::Sender<Hook>,
    counters: Arc<Counters>,
}

impl Processor {
    pub fn new(hooks: mpsc::Sender<Hook>, counters: Arc<Counters>) -> Self {
        Processor { hooks, counters }
    }

    pub fn server(self) -> pb::ext_mcp_server::ExtMcpServer<Self> {
        pb::ext_mcp_server::ExtMcpServer::new(self).max_decoding_message_size(MAX_MESSAGE)
    }

    fn queue(&self, hook: Hook) {
        if self.hooks.try_send(hook).is_err() {
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
    Response::new(McpResponseResult { result: Some(mcp_response_result::Result::Pass(Pass {})) })
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
        if !mcp::captured(&r.method) {
            return Ok(pass_request(None));
        }
        let event_id = uuid::Uuid::new_v4().to_string();
        self.queue(Hook::Request {
            event_id: event_id.clone(),
            method: r.method,
            server: r.service_names.into_iter().next().unwrap_or_default(),
            metadata: strings(r.metadata_context),
            params: r.mcp_request,
            headers: r.headers.into_iter().map(|h| (h.key, h.value)).collect(),
            at: SystemTime::now(),
        });
        // Handed back so the response hook can say which call it belongs to.
        let metadata = Struct {
            fields: [(META_EVENT_ID.to_string(), PbValue { kind: Some(Kind::StringValue(event_id)) })].into(),
        };
        Ok(pass_request(Some(metadata)))
    }

    async fn check_response(&self, request: Request<McpResponse>) -> Result<Response<McpResponseResult>, Status> {
        let r = request.into_inner();
        if mcp::captured(&r.method) {
            self.queue(Hook::Response {
                method: r.method,
                server: r.service_names.into_iter().next().unwrap_or_default(),
                metadata: strings(r.metadata_context),
                result: r.mcp_response,
                at: SystemTime::now(),
            });
        }
        Ok(pass_response())
    }
}
