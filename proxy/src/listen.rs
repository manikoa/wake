//! Accepting connections on the side ports: the brake's control API and the memory rules.
//! Agents can reach both, so a client that floods them or stalls mid-handshake mustn't use
//! up the file descriptors the hooks agentgateway calls need too.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use hyper_util::rt::{TokioExecutor, TokioTimer};
use hyper_util::server::conn::auto;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

/// How many connections each side port serves at once. Past it, new ones wait in the
/// listen backlog.
pub const MAX_CONNECTIONS: usize = 256;

/// How long a client may take to finish its TLS handshake, and to send a request's headers.
pub const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);

/// The connection limit for one port.
pub fn limit() -> Arc<Semaphore> {
    Arc::new(Semaphore::new(MAX_CONNECTIONS))
}

/// Accepts the next connection once the port is under its limit, with the permit it holds
/// while it lasts. A failed accept, as when the process is out of file descriptors, waits a
/// moment before the next rather than spinning.
pub async fn accept(
    listener: &TcpListener,
    limit: &Arc<Semaphore>,
    port: &str,
) -> (TcpStream, SocketAddr, OwnedSemaphorePermit) {
    loop {
        let Ok(permit) = limit.clone().acquire_owned().await else {
            // The semaphore is never closed; wait rather than spin if it ever is.
            tokio::time::sleep(Duration::from_secs(1)).await;
            continue;
        };
        match listener.accept().await {
            Ok((stream, peer)) => return (stream, peer, permit),
            Err(e) => {
                tracing::warn!(port, error = %e, "accept failed");
                drop(permit);
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        }
    }
}

/// An HTTP server for one connection that drops a client slow to send its headers.
pub fn http() -> auto::Builder<TokioExecutor> {
    let mut builder = auto::Builder::new(TokioExecutor::new());
    builder
        .http1()
        .timer(TokioTimer::new())
        .header_read_timeout(HANDSHAKE_TIMEOUT);
    builder
}
