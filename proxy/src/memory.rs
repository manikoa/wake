//! The memory rules the SDKs' memory adapters fetch: whether the agent's writes are refused,
//! and which entries it mustn't serve (see halts.rs). Read only, on its own port, so agents
//! can reach it without reaching the control API.
//!
//! ```text
//! GET /v1/memory?agent=<id>   {"version", "locked_by", "quarantines": [...]}
//! ```
//!
//! Each fetch is noted, and the brake reads the notes from the control API to confirm an
//! agent's adapter has the quarantine. The agent names itself, so that confirmation trusts
//! the agent's process, as the adapter's enforcement does; the proxy deny doesn't. Agents
//! can reach this port, so it caps its connections and how long each stays open
//! (listen::MEMORY), and the notes are bounded (halts.rs).

use std::collections::HashMap;
use std::sync::Arc;

use axum::extract::{Query, State};
use axum::routing::get;
use axum::{Json, Router};
use tokio::net::TcpListener;
use tokio::sync::Semaphore;

use crate::halts::{Halts, MemoryRules};
use crate::listen::{self, Limits};

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
    serve_with(listener, halts, listen::MEMORY, shutdown).await
}

/// Serves the memory rules with these limits.
pub async fn serve_with(
    listener: TcpListener,
    halts: Arc<Halts>,
    limits: Limits,
    shutdown: impl std::future::Future<Output = ()> + Send,
) {
    let app = router(halts);
    let open = Arc::new(Semaphore::new(limits.connections));
    tokio::pin!(shutdown);
    loop {
        let stream = tokio::select! {
            accepted = listener.accept() => match accepted {
                Ok((stream, _)) => stream,
                Err(e) => {
                    tracing::warn!(error = %e, "memory rules: accept failed");
                    continue;
                }
            },
            _ = &mut shutdown => return,
        };
        let Ok(slot) = open.clone().try_acquire_owned() else {
            tracing::warn!("memory rules: too many connections; closed one");
            continue;
        };
        let app = app.clone();
        tokio::spawn(async move {
            let _slot = slot;
            listen::serve_for(stream, app, limits).await;
        });
    }
}
