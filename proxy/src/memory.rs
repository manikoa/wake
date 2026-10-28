//! The memory rules the SDKs' memory adapters fetch: whether the agent's writes are refused,
//! and which entries it mustn't serve (see halts.rs). Read only, on its own port, so agents
//! can reach it without reaching the control API.
//!
//! ```text
//! GET /v1/memory?agent=<id>   {"version", "locked_by", "hidden_by", "quarantines": [...]}
//! ```
//!
//! Each fetch is noted, and the brake reads the notes from the control API to confirm an
//! agent's adapter has the quarantine. The agent names itself, so that confirmation trusts
//! the agent's process, as the adapter's enforcement does; the proxy deny doesn't.

use std::collections::HashMap;
use std::sync::Arc;

use axum::extract::{Query, State};
use axum::routing::get;
use axum::{Json, Router};
use hyper_util::rt::TokioIo;
use hyper_util::service::TowerToHyperService;
use tokio::net::TcpListener;

use crate::halts::{Halts, MemoryRules};
use crate::listen;

async fn rules(State(halts): State<Arc<Halts>>, Query(query): Query<HashMap<String, String>>) -> Json<MemoryRules> {
    let agent = query.get("agent").map(String::as_str).unwrap_or("");
    // An agent name is short; a longer one isn't noted.
    let agent = if agent.len() <= 256 { agent } else { "" };
    Json(halts.memory_rules(agent))
}

pub fn router(halts: Arc<Halts>) -> Router {
    Router::new().route("/v1/memory", get(rules)).with_state(halts)
}

/// Serves the memory rules on listener until shutdown resolves.
pub async fn serve(listener: TcpListener, halts: Arc<Halts>, shutdown: impl std::future::Future<Output = ()> + Send) {
    let app = router(halts);
    let limit = listen::limit();
    tokio::pin!(shutdown);
    loop {
        let (stream, _, permit) = tokio::select! {
            accepted = listen::accept(&listener, &limit, "memory") => accepted,
            _ = &mut shutdown => return,
        };
        let service = TowerToHyperService::new(app.clone());
        tokio::spawn(async move {
            let _permit = permit;
            let _ = listen::http().serve_connection(TokioIo::new(stream), service).await;
        });
    }
}
