//! What the processor has done, for Prometheus to scrape, on its own port. Recording fails
//! open, so a call the proxy saw and didn't record must not go unnoticed: alert on
//! wake_proxy_hooks_dropped_total and wake_proxy_events_dropped_total, and on
//! wake_proxy_state_ok going to 0, which means every call is being denied.
//!
//! ```text
//! GET /metrics   the Prometheus text format
//! ```
//!
//! Only counts, and the halts' version: nothing an agent could use. It still caps its
//! connections, like the other ports.

use std::fmt::Write;
use std::sync::Arc;
use std::sync::atomic::AtomicU64;

use axum::Router;
use axum::extract::State;
use axum::http::header;
use axum::response::IntoResponse;
use axum::routing::get;
use tokio::net::TcpListener;
use tokio::sync::Semaphore;

use crate::export::Counters;
use crate::halts::Halts;
use crate::listen::{self, Limits};

/// The text Prometheus scrapes.
pub fn render(counters: &Counters, halts: &Halts) -> String {
    let mut out = String::new();
    let mut metric = |name: &str, kind: &str, help: &str, value: u64| {
        let _ = writeln!(out, "# HELP {name} {help}\n# TYPE {name} {kind}\n{name} {value}");
    };
    let counter = |c: &AtomicU64| Counters::get(c);
    let c = counters;
    metric(
        "wake_proxy_mcp_calls_total",
        "counter",
        "MCP calls the processor saw: tools/call, resources/read and prompts/get.",
        counter(&c.mcp_calls),
    );
    metric(
        "wake_proxy_a2a_exchanges_total",
        "counter",
        "A2A exchanges the processor saw.",
        counter(&c.a2a_exchanges),
    );
    metric(
        "wake_proxy_hooks_dropped_total",
        "counter",
        "MCP calls seen and not recorded, because the capture queue was full.",
        counter(&c.hooks_dropped),
    );
    metric(
        "wake_proxy_events_sent_total",
        "counter",
        "Signed events the collector took.",
        counter(&c.sent),
    );
    metric(
        "wake_proxy_events_dropped_total",
        "counter",
        "Events dropped on their way to the collector: a full queue, or the collector couldn't take them.",
        counter(&c.export_dropped),
    );
    metric(
        "wake_proxy_events_truncated_total",
        "counter",
        "Events whose payload was cut to a prefix, past 1 MiB of a body or the bytes recording may hold.",
        counter(&c.truncated),
    );
    metric(
        "wake_proxy_paired_by_id_total",
        "counter",
        "MCP responses paired with their request by the event id the gateway passed back.",
        counter(&c.paired_by_id),
    );
    metric(
        "wake_proxy_paired_in_order_total",
        "counter",
        "MCP responses paired by order within their session, because the id didn't come back.",
        counter(&c.paired_in_order),
    );
    metric(
        "wake_proxy_denied_total",
        "counter",
        "Calls the deny lever refused under a halt.",
        counter(&c.denied),
    );
    metric(
        "wake_proxy_cut_total",
        "counter",
        "Calls under way when a halt landed, and cut: A2A exchanges, and MCP results kept from the caller.",
        counter(&c.cut),
    );
    let state = halts.state();
    metric(
        "wake_proxy_halts_in_force",
        "gauge",
        "Halts the brake has in force.",
        state.halts.len() as u64,
    );
    metric(
        "wake_proxy_halts_version",
        "gauge",
        "The halts state's version, which moves on with each change.",
        state.version,
    );
    metric(
        "wake_proxy_state_ok",
        "gauge",
        "1 while the halts state is initialized and readable; 0 means every call is denied.",
        u64::from(!state.uninitialized && state.unreadable.is_none()),
    );
    out
}

/// How many scrapers may connect at once, and for how long.
pub const LIMITS: Limits = Limits {
    connections: 16,
    handshake: std::time::Duration::from_secs(5),
    lifetime: std::time::Duration::from_secs(30),
};

pub fn router(counters: Arc<Counters>, halts: Arc<Halts>) -> Router {
    Router::new()
        .route(
            "/metrics",
            get(
                |State((counters, halts)): State<(Arc<Counters>, Arc<Halts>)>| async move {
                    (
                        [(header::CONTENT_TYPE, "text/plain; version=0.0.4")],
                        render(&counters, &halts),
                    )
                        .into_response()
                },
            ),
        )
        .with_state((counters, halts))
}

/// Serves the metrics on listener until shutdown resolves.
pub async fn serve(
    listener: TcpListener,
    counters: Arc<Counters>,
    halts: Arc<Halts>,
    shutdown: impl std::future::Future<Output = ()> + Send,
) {
    let app = router(counters, halts);
    let open = Arc::new(Semaphore::new(LIMITS.connections));
    tokio::pin!(shutdown);
    loop {
        let stream = tokio::select! {
            accepted = listener.accept() => match accepted {
                Ok((stream, _)) => stream,
                Err(e) => {
                    tracing::warn!(error = %e, "metrics: accept failed");
                    continue;
                }
            },
            _ = &mut shutdown => return,
        };
        let Ok(slot) = open.clone().try_acquire_owned() else {
            continue;
        };
        let app = app.clone();
        tokio::spawn(async move {
            let _slot = slot;
            listen::serve_for(stream, app, LIMITS).await;
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::halts::Scope;
    use std::sync::atomic::Ordering;

    #[test]
    fn renders_every_counter_and_the_halts() {
        let counters = Counters::default();
        counters.hooks_dropped.fetch_add(3, Ordering::Relaxed);
        counters.cut.fetch_add(1, Ordering::Relaxed);
        let halts = Halts::in_memory();
        halts
            .put(
                "h1",
                Scope {
                    fleet: true,
                    agents: vec![],
                    tokens: vec![],
                },
                1,
            )
            .unwrap();
        let text = render(&counters, &halts);
        for line in [
            "wake_proxy_hooks_dropped_total 3",
            "wake_proxy_cut_total 1",
            "wake_proxy_events_sent_total 0",
            "wake_proxy_halts_in_force 1",
            "wake_proxy_halts_version 1",
            "wake_proxy_state_ok 1",
            "# TYPE wake_proxy_mcp_calls_total counter",
            "# TYPE wake_proxy_state_ok gauge",
        ] {
            assert!(text.lines().any(|l| l == line), "{line} in:\n{text}");
        }
    }

    #[test]
    fn a_lost_state_shows() {
        let dir = std::env::temp_dir().join(format!("wake-metrics-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let text = render(&Counters::default(), &Halts::load(&dir));
        assert!(text.lines().any(|l| l == "wake_proxy_state_ok 0"), "{text}");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
