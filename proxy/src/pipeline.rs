//! Wires the processor together: the gRPC services agentgateway calls (ExtMCP for MCP,
//! ext_proc for A2A), the capture task that pairs MCP requests with responses, signing,
//! and the exporter. Each stage hands work on
//! through a bounded queue, so a slow stage drops and counts rather than slow the call.

use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::{Duration, SystemTime};

use tokio::net::TcpListener;
use tokio::sync::mpsc;

use crate::canonical::Event;
use crate::capture::Capture;
use crate::export::{Counters, Exporter};
use crate::extmcp::Processor;
use crate::extproc;
use crate::signer::Signer;

pub struct Options {
    /// The collector's OTLP/HTTP base URL.
    pub collector: String,
    /// How long a request waits for its response before it's recorded without one.
    pub response_timeout: Duration,
    /// How many hooks and events may wait in each queue.
    pub queue: usize,
}

impl Default for Options {
    fn default() -> Self {
        Options { collector: "http://localhost:4318".into(), response_timeout: Duration::from_secs(60), queue: 10_000 }
    }
}

/// Serves the processor's gRPC services, ExtMCP for MCP and ext_proc for A2A, on listener
/// until shutdown resolves.
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

    let exporter = Exporter::new(&options.collector, counters.clone());
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

    let incoming = tokio_stream::wrappers::TcpListenerStream::new(listener);
    let result = tonic::transport::Server::builder()
        .add_service(Processor::new(hooks_tx, counters.clone()).server())
        .add_service(extproc::Processor::new(unsigned_tx, counters).server())
        .serve_with_incoming_shutdown(incoming, shutdown)
        .await;
    // The server dropped its senders, so each stage drains and ends in turn.
    let _ = captured.await;
    let _ = signed.await;
    let _ = exported.await;
    result
}
