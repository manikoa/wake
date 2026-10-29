//! The brake's control API for the proxy deny lever: put, lift and list the halts in
//! force (halts.rs).
//!
//! ```text
//! PUT    /v1/halts/{id}         {"scope": {"fleet": true} or {"agents": [...], "tokens": [...]}, "issued_at": <Unix ns>}
//! DELETE /v1/halts/{id}
//! GET    /v1/halts              the halts and quarantines in force
//! PUT    /v1/quarantines/{id}   {"selection": {"lock": {...}, "agents": [...], "origins": [...], ...}}
//! DELETE /v1/quarantines/{id}
//! GET    /v1/memory/seen        each agent's memory adapter: the version it last fetched, and when
//! GET    /v1/callers            each identity the deny lever has seen calling: its agent, the
//!                               delegation chain and token ids it came with, and when;
//!                               ?identity=<id> only that one, and the callers whose chain names it
//! ```
//!
//! Each answers with the state now in force and its version, and a change answers 200 only
//! once it's on disk, so the brake can count that answer as the lever's acknowledgement. A
//! change that couldn't be written is in force anyway: it answers 500 with the state, whose
//! `not_persisted` says why, and a restart would lose it. Until the state is initialized,
//! or while it can't be read, every call is denied; lifting `uninitialized-state` or
//! `unreadable-state` starts it over with a new `epoch`.
//!
//! It listens on its own port, apart from the hooks agentgateway calls, over TLS with the
//! proxy's SVID, and serves only clients that present an X.509-SVID chained to the trust
//! bundle with the brake's SPIFFE ID. An agent can neither reach it nor present that
//! identity, so no agent can lift a halt. It takes a few connections at once, each with a
//! handshake timeout and a lifetime (listen::CONTROL), so a client that connects and stalls
//! can't keep the brake out. As SPIRE rotates the SVID and the trust bundle, each new
//! connection gets the ones in use (svid.rs).

use std::sync::Arc;

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde::Deserialize;
use tokio::net::TcpListener;
use tokio::sync::Semaphore;
use tokio_rustls::TlsAcceptor;
use tokio_rustls::rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use tokio_rustls::rustls::server::WebPkiClientVerifier;
use tokio_rustls::rustls::{self, RootCertStore, ServerConfig};

use crate::halts::{self, Halts, Scope, Selection};
use crate::listen::{self, Limits};
use crate::signer::spiffe_id;
use crate::svid::Svid;

/// What the control API needs to serve mTLS.
pub struct Tls {
    /// The proxy's SVID and key, PEM: the server certificate.
    pub cert_pem: String,
    pub key_pem: String,
    /// The trust bundle the brake's SVID chains to, PEM.
    pub bundle_pem: String,
    /// The one SPIFFE ID allowed to call.
    pub brake_id: String,
}

/// Where the control API's TLS comes from: fixed, or the processor's SVID and the trust
/// bundle as SPIRE rotates them.
pub enum TlsSource {
    Fixed(Tls),
    Svid { svid: Arc<Svid>, brake_id: String },
}

impl From<Tls> for TlsSource {
    fn from(tls: Tls) -> Self {
        TlsSource::Fixed(tls)
    }
}

impl TlsSource {
    fn brake_id(&self) -> &str {
        match self {
            TlsSource::Fixed(tls) => &tls.brake_id,
            TlsSource::Svid { brake_id, .. } => brake_id,
        }
    }

    /// The acceptor for what's in use now, and what identifies it, unless that's what the
    /// acceptor in hand already serves.
    fn fresh(&self, serving: Option<[u8; 32]>) -> Result<Option<([u8; 32], TlsAcceptor)>, String> {
        match self {
            TlsSource::Fixed(tls) => match serving {
                Some(_) => Ok(None),
                None => Ok(Some(([0; 32], tls_acceptor(tls)?))),
            },
            TlsSource::Svid { svid, brake_id } => {
                let m = svid.current();
                if serving == Some(m.digest()) {
                    return Ok(None);
                }
                let tls = Tls {
                    cert_pem: m.cert_pem.clone(),
                    key_pem: m.key_pem.clone(),
                    bundle_pem: m.bundle_pem.clone().ok_or("the control API needs a trust bundle")?,
                    brake_id: brake_id.clone(),
                };
                Ok(Some((m.digest(), tls_acceptor(&tls)?)))
            }
        }
    }
}

pub fn tls_acceptor(tls: &Tls) -> Result<TlsAcceptor, String> {
    let provider = Arc::new(rustls::crypto::aws_lc_rs::default_provider());
    let mut roots = RootCertStore::empty();
    for cert in pem::parse_many(&tls.bundle_pem).map_err(|e| format!("trust bundle: {e}"))? {
        roots
            .add(CertificateDer::from(cert.contents().to_vec()))
            .map_err(|e| format!("trust bundle: {e}"))?;
    }
    if roots.is_empty() {
        return Err("trust bundle has no certificates".into());
    }
    let verifier = WebPkiClientVerifier::builder_with_provider(Arc::new(roots), provider.clone())
        .build()
        .map_err(|e| format!("client verifier: {e}"))?;
    let chain = pem::parse_many(&tls.cert_pem)
        .map_err(|e| format!("certificate: {e}"))?
        .into_iter()
        .map(|c| CertificateDer::from(c.contents().to_vec()))
        .collect();
    let key = pem::parse(&tls.key_pem).map_err(|e| format!("key: {e}"))?;
    let key = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(key.contents().to_vec()));
    let config = ServerConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|e| e.to_string())?
        .with_client_cert_verifier(verifier)
        .with_single_cert(chain, key)
        .map_err(|e| format!("server certificate: {e}"))?;
    Ok(TlsAcceptor::from(Arc::new(config)))
}

#[derive(Deserialize)]
struct PutHalt {
    scope: Scope,
    #[serde(default)]
    issued_at: Option<u64>,
}

type Failure = (StatusCode, String);

fn valid_id(id: &str) -> Result<(), Failure> {
    let ok = (1..=128).contains(&id.len())
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'-'));
    ok.then_some(()).ok_or((
        StatusCode::BAD_REQUEST,
        "a halt id is 1 to 128 of A-Z a-z 0-9 . _ : -".into(),
    ))
}

fn stored(result: Result<halts::Changed, tokio::task::JoinError>) -> Response {
    match result {
        Ok(Ok(state)) => Json(state).into_response(),
        Ok(Err(not_persisted)) => (StatusCode::INTERNAL_SERVER_ERROR, Json(*not_persisted.state)).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

async fn put_halt(State(halts): State<Arc<Halts>>, Path(id): Path<String>, Json(body): Json<PutHalt>) -> Response {
    if let Err(failure) = valid_id(&id) {
        return failure.into_response();
    }
    if body.scope.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            "a halt needs a scope: the fleet, agents or tokens",
        )
            .into_response();
    }
    // A blank name matches no call: the halt would be acknowledged and stop nothing.
    if body
        .scope
        .agents
        .iter()
        .chain(&body.scope.tokens)
        .any(|n| n.trim().is_empty())
    {
        return (StatusCode::BAD_REQUEST, "a halt's agents and tokens can't be blank").into_response();
    }
    let issued_at = body.issued_at.unwrap_or_else(halts::now);
    stored(tokio::task::spawn_blocking(move || halts.put(&id, body.scope, issued_at)).await)
}

async fn lift_halt(State(halts): State<Arc<Halts>>, Path(id): Path<String>) -> Response {
    if let Err(failure) = valid_id(&id) {
        return failure.into_response();
    }
    stored(tokio::task::spawn_blocking(move || halts.lift(&id)).await)
}

async fn list_halts(State(halts): State<Arc<Halts>>) -> Json<halts::State> {
    Json(halts.state())
}

#[derive(Deserialize)]
struct PutQuarantine {
    selection: Selection,
    #[serde(default)]
    issued_at: Option<u64>,
}

async fn put_quarantine(
    State(halts): State<Arc<Halts>>,
    Path(id): Path<String>,
    Json(body): Json<PutQuarantine>,
) -> Response {
    if let Err(failure) = valid_id(&id) {
        return failure.into_response();
    }
    if body.selection.is_empty() {
        return (StatusCode::BAD_REQUEST, "a quarantine needs something to lock or hide").into_response();
    }
    // An agent's memory adapter refuses its writes by the agent's name; it never sees a
    // token, so a lock by token, or by a blank name, would be acknowledged and lock nothing.
    let lock = &body.selection.lock;
    if !lock.tokens.is_empty() || lock.agents.iter().any(|n| n.trim().is_empty()) {
        return (
            StatusCode::BAD_REQUEST,
            "a quarantine locks the fleet or agents by name, not tokens",
        )
            .into_response();
    }
    let issued_at = body.issued_at.unwrap_or_else(halts::now);
    stored(tokio::task::spawn_blocking(move || halts.quarantine(&id, body.selection, issued_at)).await)
}

async fn release_quarantine(State(halts): State<Arc<Halts>>, Path(id): Path<String>) -> Response {
    if let Err(failure) = valid_id(&id) {
        return failure.into_response();
    }
    stored(tokio::task::spawn_blocking(move || halts.release(&id)).await)
}

#[derive(Deserialize)]
struct CallersQuery {
    identity: Option<String>,
}

async fn callers(
    State(halts): State<Arc<Halts>>,
    axum::extract::Query(q): axum::extract::Query<CallersQuery>,
) -> Json<std::collections::HashMap<String, halts::Caller>> {
    Json(match q.identity {
        Some(identity) => halts.callers_naming(&identity),
        None => halts.callers(),
    })
}

async fn memory_seen(State(halts): State<Arc<Halts>>) -> Json<std::collections::HashMap<String, halts::Seen>> {
    Json(halts.seen())
}

pub fn router(halts: Arc<Halts>) -> Router {
    Router::new()
        .route("/v1/halts", get(list_halts))
        .route("/v1/halts/{id}", axum::routing::put(put_halt).delete(lift_halt))
        .route(
            "/v1/quarantines/{id}",
            axum::routing::put(put_quarantine).delete(release_quarantine),
        )
        .route("/v1/memory/seen", get(memory_seen))
        .route("/v1/callers", get(callers))
        .with_state(halts)
}

/// Serves the control API on listener until shutdown resolves.
pub async fn serve(
    listener: TcpListener,
    halts: Arc<Halts>,
    tls: impl Into<TlsSource>,
    shutdown: impl std::future::Future<Output = ()> + Send,
) -> Result<(), String> {
    serve_with(listener, halts, tls, listen::CONTROL, shutdown).await
}

/// Serves the control API with these limits.
pub async fn serve_with(
    listener: TcpListener,
    halts: Arc<Halts>,
    tls: impl Into<TlsSource>,
    limits: Limits,
    shutdown: impl std::future::Future<Output = ()> + Send,
) -> Result<(), String> {
    let source = tls.into();
    let (mut serving, mut acceptor) = source.fresh(None)?.ok_or("no TLS")?;
    let brake_id = Arc::new(source.brake_id().to_string());
    let app = router(halts);
    let open = Arc::new(Semaphore::new(limits.connections));
    tokio::pin!(shutdown);
    loop {
        let (stream, peer) = tokio::select! {
            accepted = listener.accept() => match accepted {
                Ok(a) => a,
                Err(e) => {
                    tracing::warn!(error = %e, "control API: accept failed");
                    continue;
                }
            },
            _ = &mut shutdown => return Ok(()),
        };
        let Ok(slot) = open.clone().try_acquire_owned() else {
            tracing::warn!(%peer, "control API: too many connections; closed one");
            continue;
        };
        // A rotated SVID or bundle serves from this connection on; one that won't build
        // keeps the TLS in use.
        match source.fresh(Some(serving)) {
            Ok(Some((digest, fresh))) => (serving, acceptor) = (digest, fresh),
            Ok(None) => {}
            Err(error) => tracing::warn!(error, "control API: can't use the new SVID; keeping the one in use"),
        }
        let acceptor = acceptor.clone();
        let brake_id = brake_id.clone();
        let app = app.clone();
        tokio::spawn(async move {
            let _slot = slot;
            let Ok(Ok(stream)) = tokio::time::timeout(limits.handshake, acceptor.accept(stream)).await else {
                tracing::warn!(%peer, "control API: TLS handshake failed or took too long");
                return;
            };
            let caller = stream
                .get_ref()
                .1
                .peer_certificates()
                .and_then(|c| c.first())
                .and_then(|c| spiffe_id(c.as_ref()).ok());
            if caller.as_deref() != Some(brake_id.as_str()) {
                tracing::warn!(%peer, ?caller, "control API: refused a caller that isn't the brake");
                return;
            }
            listen::serve_for(stream, app, limits).await;
        });
    }
}
