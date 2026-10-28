//! Wires the processor together: the gRPC services agentgateway calls (ExtMCP for MCP,
//! ext_proc for A2A, ext_authz for the deny lever), the capture task that pairs MCP requests with responses, signing,
//! and the exporter. Each stage hands work on
//! through a bounded queue, so a slow stage drops and counts rather than slow the call.

use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::{Duration, SystemTime};

use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio_stream::StreamExt;

use crate::authz::Authorizer;
use crate::canonical::Event;
use crate::capture::Capture;
use crate::export::{Counters, Exporter};
use crate::extmcp::Processor;
use crate::extproc;
use crate::halts::Halts;
use crate::signer::Signer;

pub struct Options {
    /// The collector's OTLP/HTTP base URL.
    pub collector: String,
    /// A CA certificate (PEM) to trust for the collector's TLS, besides the system's.
    pub collector_ca: Option<Vec<u8>>,
    /// How long a request waits for its response before it's recorded without one.
    pub response_timeout: Duration,
    /// How many hooks and events may wait in each queue.
    pub queue: usize,
    /// The halts in force, which the ext_authz service enforces.
    pub halts: Arc<Halts>,
}

impl Default for Options {
    fn default() -> Self {
        Options {
            collector: "http://localhost:4318".into(),
            collector_ca: None,
            response_timeout: Duration::from_secs(60),
            queue: 10_000,
            halts: Arc::new(Halts::in_memory()),
        }
    }
}

/// Serves the processor's gRPC services, ExtMCP for MCP, ext_proc for A2A and ext_authz for
/// the deny lever, on listener until shutdown resolves.
pub async fn serve(
    listener: TcpListener,
    signer: Signer,
    options: Options,
    counters: Arc<Counters>,
    shutdown: impl std::future::Future<Output = ()> + Send + 'static,
) -> Result<(), tonic::transport::Error> {
    let (hooks_tx, mut hooks_rx) = mpsc::channel(options.queue);
    let (unsigned_tx, mut unsigned_rx) = mpsc::channel::<Event>(options.queue);
    let (signed_tx, signed_rx) = mpsc::channel(options.queue);

    let exporter = Exporter::new(&options.collector, options.collector_ca.as_deref(), counters.clone());
    let exported = tokio::spawn(exporter.run(signed_rx));

    let sign_counters = counters.clone();
    let signed = tokio::spawn(async move {
        while let Some(mut event) = unsigned_rx.recv().await {
            signer.sign(&mut event);
            if signed_tx.try_send(event).is_err() {
                sign_counters.export_dropped.fetch_add(1, Ordering::Relaxed);
            }
        }
    });

    let capture_counters = counters.clone();
    let capture_tx = unsigned_tx.clone();
    let timeout = options.response_timeout;
    let limit = options.queue;
    let captured = tokio::spawn(async move {
        let mut capture = Capture::new(timeout, limit);
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        let emit = |events: Vec<Event>| {
            for event in events {
                if capture_tx.try_send(event).is_err() {
                    capture_counters.export_dropped.fetch_add(1, Ordering::Relaxed);
                }
            }
        };
        loop {
            tokio::select! {
                hook = hooks_rx.recv() => match hook {
                    Some(hook) => {
                        emit(capture.handle(hook));
                        capture_counters.paired_by_id.store(capture.paired_by_id, Ordering::Relaxed);
                        capture_counters.paired_in_order.store(capture.paired_in_order, Ordering::Relaxed);
                    }
                    None => break,
                },
                _ = tick.tick() => emit(capture.expire(SystemTime::now())),
            }
        }
        // Shutting down: what's still waiting goes out without its response.
        emit(capture.expire(SystemTime::now() + timeout));
    });

    // Only agentgateway may call the hooks, from beside the processor: the same pod or
    // network namespace, over loopback. Anything else could get events signed as the
    // proxy's, so it's turned away before a byte is read.
    let refused = counters.clone();
    let incoming = tokio_stream::wrappers::TcpListenerStream::new(listener).filter(move |conn| match conn {
        Ok(stream) => match stream.peer_addr() {
            Ok(peer) if from_beside(peer) => true,
            peer => {
                refused.hooks_refused.fetch_add(1, Ordering::Relaxed);
                tracing::warn!(?peer, "refused a hook connection from off the processor's host");
                false
            }
        },
        Err(_) => true,
    });
    let result = tonic::transport::Server::builder()
        .add_service(Processor::new(hooks_tx, counters.clone()).server())
        .add_service(extproc::Processor::new(unsigned_tx.clone(), counters.clone()).server())
        .add_service(Authorizer::new(options.halts.clone(), unsigned_tx, counters).server())
        .serve_with_incoming_shutdown(incoming, shutdown)
        .await;
    // The server dropped its senders, so each stage drains and ends in turn.
    let _ = captured.await;
    let _ = signed.await;
    let _ = exported.await;
    result
}

/// Whether a hook connection comes from beside the processor: a loopback address, IPv4
/// mapped into IPv6 included.
pub fn from_beside(peer: SocketAddr) -> bool {
    match peer.ip() {
        IpAddr::V4(ip) => ip.is_loopback(),
        IpAddr::V6(ip) => ip.is_loopback() || ip.to_ipv4_mapped().is_some_and(|ip| ip.is_loopback()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hooks_are_served_only_over_loopback() {
        for peer in [
            "127.0.0.1:5000",
            "127.8.9.10:5000",
            "[::1]:5000",
            "[::ffff:127.0.0.1]:5000",
        ] {
            assert!(from_beside(peer.parse().unwrap()), "{peer}");
        }
        for peer in [
            "172.18.0.5:5000",
            "10.0.0.1:5000",
            "0.0.0.0:5000",
            "[::ffff:10.0.0.1]:5000",
            "[fe80::1]:5000",
        ] {
            assert!(!from_beside(peer.parse().unwrap()), "{peer}");
        }
    }
}
