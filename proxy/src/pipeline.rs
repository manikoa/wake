//! Wires the processor together: the gRPC service agentgateway calls, the capture task
//! that pairs requests with responses, signing, and the exporter. Each stage hands work on
//! through a bounded queue, so a slow stage drops and counts rather than slow the call.

use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::{Duration, SystemTime};

use tokio::net::TcpListener;
use tokio::sync::mpsc;

use crate::capture::Capture;
use crate::export::{Counters, Exporter};
use crate::extmcp::Processor;
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

/// Serves the ExtMCP processor on listener until shutdown resolves.
pub async fn serve(
    listener: TcpListener,
    signer: Signer,
    options: Options,
    counters: Arc<Counters>,
    shutdown: impl std::future::Future<Output = ()> + Send + 'static,
) -> Result<(), tonic::transport::Error> {
    let (hooks_tx, mut hooks_rx) = mpsc::channel(options.queue);
    let (events_tx, events_rx) = mpsc::channel(options.queue);

    let exporter = Exporter::new(&options.collector, counters.clone());
    let exported = tokio::spawn(exporter.run(events_rx));

    let capture_counters = counters.clone();
    let timeout = options.response_timeout;
    let limit = options.queue;
    let captured = tokio::spawn(async move {
        let mut capture = Capture::new(timeout, limit);
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        let emit = |events: Vec<crate::canonical::Event>| {
            for mut event in events {
                signer.sign(&mut event);
                if events_tx.try_send(event).is_err() {
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
        .add_service(Processor::new(hooks_tx, counters).server())
        .serve_with_incoming_shutdown(incoming, shutdown)
        .await;
    // The server dropped its sender, so the capture task drains and ends, then the exporter.
    let _ = captured.await;
    let _ = exported.await;
    result
}
