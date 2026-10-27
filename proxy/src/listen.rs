//! Where the processor listens, and how much each listener takes on.
//!
//! Anything that can call the hooks (ExtMCP, ext_proc, ext_authz) can get events signed in
//! the proxy's name, so only agentgateway may reach them: over a Unix socket shared with it
//! alone, or over loopback when it shares the host or pod. The control API and the memory
//! rules each cap their connections and how long one may stay open, so a client that
//! connects and stalls can't hold them.

use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::time::Duration;

use axum::Router;
use hyper_util::rt::{TokioExecutor, TokioIo};
use hyper_util::server::conn::auto;
use hyper_util::service::TowerToHyperService;
use tokio::io::{AsyncRead, AsyncWrite};

/// Where agentgateway reaches the hooks.
#[derive(Clone, Debug, PartialEq)]
pub enum Hooks {
    /// A Unix socket, `unix:/path`, in a directory only agentgateway shares.
    Unix(PathBuf),
    /// A loopback address, for agentgateway on the same host or in the same pod.
    Tcp(SocketAddr),
}

impl Hooks {
    pub fn parse(listen: &str) -> Result<Self, String> {
        if let Some(path) = listen.strip_prefix("unix:") {
            let path = PathBuf::from(path);
            if !path.is_absolute() {
                return Err(format!("--listen unix:{} must be an absolute path", path.display()));
            }
            return Ok(Hooks::Unix(path));
        }
        let addr: SocketAddr = listen
            .parse()
            .map_err(|e| format!("--listen {listen}: {e}; give unix:/path or a loopback address and port"))?;
        if !addr.ip().is_loopback() {
            return Err(format!(
                "--listen {listen} isn't loopback: anything that can reach the hooks can get events signed, so serve them on a Unix socket only agentgateway shares (unix:/path), or on loopback"
            ));
        }
        Ok(Hooks::Tcp(addr))
    }
}

/// Binds a Unix socket only its owner and group can connect to, replacing one a previous
/// run left behind. Anything else at the path is left alone, and binding fails.
#[cfg(unix)]
pub fn bind_unix(path: &Path) -> std::io::Result<tokio::net::UnixListener> {
    use std::os::unix::fs::{FileTypeExt, PermissionsExt};
    match std::fs::symlink_metadata(path) {
        Ok(m) if m.file_type().is_socket() => std::fs::remove_file(path)?,
        Ok(_) => {
            return Err(std::io::Error::new(
                std::io::ErrorKind::AlreadyExists,
                format!("{} exists and isn't a socket", path.display()),
            ));
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    let listener = tokio::net::UnixListener::bind(path)?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o660))?;
    Ok(listener)
}

/// How much one HTTP listener takes on.
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    /// Connections open at once. Past it, a new one is closed at once.
    pub connections: usize,
    /// How long a TLS handshake may take.
    pub handshake: Duration,
    /// How long a connection may stay open, busy or idle. Then it's closed gracefully: a
    /// request under way gets GRACE more to finish.
    pub lifetime: Duration,
}

/// The brake's control API: one brake, a few connections.
pub const CONTROL: Limits = Limits {
    connections: 32,
    handshake: Duration::from_secs(5),
    lifetime: Duration::from_secs(60),
};

/// The memory rules: every agent's memory adapter, polling.
pub const MEMORY: Limits = Limits {
    connections: 512,
    handshake: Duration::from_secs(5),
    lifetime: Duration::from_secs(30),
};

pub const GRACE: Duration = Duration::from_secs(5);

/// Serves one HTTP connection until it ends or limits.lifetime is up.
pub async fn serve_for<I>(io: I, app: Router, limits: Limits)
where
    I: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let mut builder = auto::Builder::new(TokioExecutor::new());
    builder.http2().max_concurrent_streams(16);
    let conn = builder.serve_connection(TokioIo::new(io), TowerToHyperService::new(app));
    tokio::pin!(conn);
    tokio::select! {
        _ = &mut conn => return,
        _ = tokio::time::sleep(limits.lifetime) => {}
    }
    conn.as_mut().graceful_shutdown();
    let _ = tokio::time::timeout(GRACE, conn).await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hooks_are_reachable_only_through_a_socket_or_loopback() {
        assert_eq!(
            Hooks::parse("unix:/run/wake/proxy.sock"),
            Ok(Hooks::Unix("/run/wake/proxy.sock".into()))
        );
        assert!(Hooks::parse("unix:proxy.sock").is_err());
        assert_eq!(
            Hooks::parse("127.0.0.1:9001"),
            Ok(Hooks::Tcp("127.0.0.1:9001".parse().unwrap()))
        );
        assert!(Hooks::parse("[::1]:9001").is_ok());
        for open in ["0.0.0.0:9001", "[::]:9001", "10.0.0.5:9001"] {
            let err = Hooks::parse(open).unwrap_err();
            assert!(err.contains("isn't loopback"), "{open}: {err}");
        }
        assert!(Hooks::parse("wake-proxy:9001").is_err());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn a_socket_is_for_its_owner_and_group_and_replaces_a_stale_one() {
        use std::os::unix::fs::PermissionsExt;
        // Short: a socket's path has to fit in about 100 bytes.
        let dir = PathBuf::from("/tmp").join(format!("wake-sock-{}", &uuid::Uuid::new_v4().to_string()[..8]));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("proxy.sock");
        let first = bind_unix(&path).unwrap();
        let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o660);
        drop(first);
        // A restart finds the old socket and replaces it.
        let _second = bind_unix(&path).unwrap();
        // Anything else at the path is left alone.
        let file = dir.join("not-a-socket");
        std::fs::write(&file, b"keep").unwrap();
        assert!(bind_unix(&file).is_err());
        assert_eq!(std::fs::read(&file).unwrap(), b"keep");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
