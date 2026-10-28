//! Wires the processor together: the gRPC services agentgateway calls (ExtMCP for MCP,
//! ext_proc for A2A, ext_authz for the deny lever), the capture task that pairs MCP
//! requests with responses, signing, and the exporter. Each stage hands work on through a
//! queue bounded by count and by bytes (budget.rs), so a slow stage drops and counts rather
//! than slow the call, and recording can't grow the process the deny lever runs in.

use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::{Duration, SystemTime};

use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tonic::transport::server::Connected;

use crate::authz::Authorizer;
use crate::budget::{Budget, Events, Permit};
use crate::canonical::Event;
use crate::capture::{Capture, Hook};
use crate::export::{Counters, Exporter};
use crate::extmcp::Processor;
use crate::extproc;
use crate::halts::Halts;
use crate::origins::Observed;
use crate::policy::{DEFAULT_WINDOW_SECS, FlowPolicy, Policy};
use crate::svid::Svid;

pub struct Options {
    /// The collector's OTLP/HTTP base URL.
    pub collector: String,
    /// A CA certificate (PEM) to trust for the collector's TLS, besides the system's.
    pub collector_ca: Option<Vec<u8>>,
    /// How long a request waits for its response before it's recorded without one.
    pub response_timeout: Duration,
    /// How many hooks and events may wait in each queue.
    pub queue: usize,
    /// How many bytes recording may hold in each of: hooks waiting for the capture task,
    /// requests waiting for their responses, A2A bodies being captured, and events on
    /// their way to the collector.
    pub queue_bytes: usize,
    /// The halts in force, which the ext_authz service enforces, and ext_proc and ExtMCP
    /// enforce on calls already under way.
    pub halts: Arc<Halts>,
    /// The origin flow policy in force: ext_authz checks its rules that name no tools, and
    /// ExtMCP those that do.
    pub policy: Arc<Policy>,
    /// What entered each agent through the proxy, which ExtMCP and ext_proc note and the
    /// policy decides on.
    pub observed: Arc<Observed>,
}

impl Default for Options {
    fn default() -> Self {
        Options {
            collector: "http://localhost:4318".into(),
            collector_ca: None,
            response_timeout: Duration::from_secs(60),
            queue: 10_000,
            queue_bytes: 64 << 20,
            halts: Arc::new(Halts::in_memory()),
            policy: Arc::new(Policy::fixed(FlowPolicy::empty())),
            observed: Arc::new(Observed::starting(crate::halts::now(), DEFAULT_WINDOW_SECS)),
        }
    }
}

/// Serves the processor's gRPC services, ExtMCP for MCP, ext_proc for A2A and ext_authz for
/// the deny lever, on listener until shutdown resolves. Only agentgateway may reach it; see
/// listen.rs.
pub async fn serve(
    listener: TcpListener,
    signer: impl Into<Arc<Svid>>,
    options: Options,
    counters: Arc<Counters>,
    shutdown: impl std::future::Future<Output = ()> + Send + 'static,
) -> Result<(), tonic::transport::Error> {
    let incoming = tokio_stream::wrappers::TcpListenerStream::new(listener);
    serve_incoming(incoming, signer.into(), options, counters, shutdown).await
}

/// The same, on a Unix socket shared only with agentgateway.
#[cfg(unix)]
pub async fn serve_unix(
    listener: tokio::net::UnixListener,
    signer: impl Into<Arc<Svid>>,
    options: Options,
    counters: Arc<Counters>,
    shutdown: impl std::future::Future<Output = ()> + Send + 'static,
) -> Result<(), tonic::transport::Error> {
    let incoming = tokio_stream::wrappers::UnixListenerStream::new(listener);
    serve_incoming(incoming, signer.into(), options, counters, shutdown).await
}

async fn serve_incoming<I, IO, IE>(
    incoming: I,
    signer: Arc<Svid>,
    options: Options,
    counters: Arc<Counters>,
    shutdown: impl std::future::Future<Output = ()> + Send + 'static,
) -> Result<(), tonic::transport::Error>
where
    I: tokio_stream::Stream<Item = Result<IO, IE>>,
    IO: tokio::io::AsyncRead + tokio::io::AsyncWrite + Connected + Unpin + Send + 'static,
    IO::ConnectInfo: Clone + Send + Sync + 'static,
    IE: Into<Box<dyn std::error::Error + Send + Sync>>,
{
    let (hooks_tx, mut hooks_rx) = mpsc::channel::<(Hook, Permit)>(options.queue);
    let hooks_budget = Budget::new(options.queue_bytes);
    let (events, mut unsigned_rx) = Events::channel(options.queue, Budget::new(options.queue_bytes), counters.clone());
    let (signed_tx, signed_rx) = mpsc::channel(options.queue);

    let exporter = Exporter::new(&options.collector, options.collector_ca.as_deref(), counters.clone());
    let exported = tokio::spawn(exporter.run(signed_rx));

    let sign_counters = counters.clone();
    let signed = tokio::spawn(async move {
        while let Some(mut queued) = unsigned_rx.recv().await {
            // The SVID in use now: SPIRE rotates it, and svid.rs reads the new one.
            signer.current().signer.sign(&mut queued.event);
            if signed_tx.try_send(queued).is_err() {
                sign_counters.export_dropped.fetch_add(1, Ordering::Relaxed);
            }
        }
    });

    let capture_counters = counters.clone();
    let capture_events = events.clone();
    let timeout = options.response_timeout;
    let limit = options.queue;
    let pending_bytes = options.queue_bytes;
    let captured = tokio::spawn(async move {
        let mut capture = Capture::new(timeout, limit).with_max_bytes(pending_bytes);
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        let emit = |events: Vec<Event>| {
            for event in events {
                capture_events.send(event);
            }
        };
        loop {
            tokio::select! {
                hook = hooks_rx.recv() => match hook {
                    // The hook's bytes are given back once it's handled: what a waiting
                    // request keeps is capped, and bounded by the capture itself.
                    Some((hook, _permit)) => {
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

    let bodies = Budget::new(options.queue_bytes);
    let result = tonic::transport::Server::builder()
        .add_service(
            Processor::new(
                hooks_tx,
                hooks_budget,
                counters.clone(),
                options.halts.clone(),
                events.clone(),
            )
            .with_policy(options.policy.clone(), options.observed.clone())
            .server(),
        )
        .add_service(
            extproc::Processor::new(events.clone(), options.halts.clone(), bodies, counters.clone())
                .with_policy(options.policy.clone(), options.observed.clone())
                .server(),
        )
        .add_service(
            Authorizer::with_policy(
                options.halts.clone(),
                options.policy.clone(),
                options.observed.clone(),
                events,
                counters,
            )
            .server(),
        )
        .serve_with_incoming_shutdown(incoming, shutdown)
        .await;
    // The server dropped its senders, so each stage drains and ends in turn.
    let _ = captured.await;
    let _ = signed.await;
    let _ = exported.await;
    result
}
