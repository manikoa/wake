//! The brake's control API for the proxy deny lever: put, lift and list the halts in
//! force (halts.rs).
//!
//! ```text
//! PUT    /v1/halts/{id}         {"scope": {"fleet": true} or {"agents": [...]}, "issued_at": <Unix ns>}
//! DELETE /v1/halts/{id}
//! GET    /v1/halts              the halts and quarantines in force
//! PUT    /v1/quarantines/{id}   {"selection": {"lock": {...}, "agents": [...], "origins": [...], ...}}
//! DELETE /v1/quarantines/{id}
//! GET    /v1/memory/seen        each agent's memory adapter: the version it last fetched, and when
//! ```
//!
//! Each answers with the state now in force and its version, and a change answers only
//! once it's on disk, so the brake can count that answer as the lever's acknowledgement.
//!
//! It listens on its own port, apart from the hooks agentgateway calls, over TLS with the
//! proxy's SVID, and serves only clients that present an X.509-SVID chained to the trust
//! bundle with the brake's SPIFFE ID. An agent can neither reach it nor present that
//! identity, so no agent can lift a halt.

use std::sync::Arc;

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::routing::get;
use axum::{Json, Router};
use hyper_util::rt::TokioIo;
use hyper_util::service::TowerToHyperService;
use serde::Deserialize;
use tokio::net::TcpListener;
use tokio_rustls::TlsAcceptor;
use tokio_rustls::rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use tokio_rustls::rustls::server::WebPkiClientVerifier;
use tokio_rustls::rustls::{self, RootCertStore, ServerConfig};

use crate::halts::{self, Halts, Scope, Selection};
use crate::listen;
use crate::signer::spiffe_id;

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

fn stored(result: std::io::Result<halts::State>) -> Result<Json<halts::State>, Failure> {
    result.map(Json).map_err(|e| {
        tracing::error!(error = %e, "couldn't store the halts");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("couldn't store the halts: {e}"),
        )
    })
}

async fn put_halt(
    State(halts): State<Arc<Halts>>,
    Path(id): Path<String>,
    Json(body): Json<PutHalt>,
) -> Result<Json<halts::State>, Failure> {
    valid_id(&id)?;
    if body.scope.is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            "a halt needs a scope: the fleet, or agents".into(),
        ));
    }
    let issued_at = body.issued_at.unwrap_or_else(halts::now);
    let result = tokio::task::spawn_blocking(move || halts.put(&id, body.scope, issued_at))
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    stored(result)
}

async fn lift_halt(State(halts): State<Arc<Halts>>, Path(id): Path<String>) -> Result<Json<halts::State>, Failure> {
    valid_id(&id)?;
    let result = tokio::task::spawn_blocking(move || halts.lift(&id))
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    stored(result)
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
) -> Result<Json<halts::State>, Failure> {
    valid_id(&id)?;
    if body.selection.is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            "a quarantine needs something to lock or hide".into(),
        ));
    }
    let issued_at = body.issued_at.unwrap_or_else(halts::now);
    let result = tokio::task::spawn_blocking(move || halts.quarantine(&id, body.selection, issued_at))
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    stored(result)
}

async fn release_quarantine(
    State(halts): State<Arc<Halts>>,
    Path(id): Path<String>,
) -> Result<Json<halts::State>, Failure> {
    valid_id(&id)?;
    let result = tokio::task::spawn_blocking(move || halts.release(&id))
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    stored(result)
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
        .with_state(halts)
}

/// Serves the control API on listener until shutdown resolves.
pub async fn serve(
    listener: TcpListener,
    halts: Arc<Halts>,
    tls: Tls,
    shutdown: impl std::future::Future<Output = ()> + Send,
) -> Result<(), String> {
    let acceptor = tls_acceptor(&tls)?;
    let brake_id = Arc::new(tls.brake_id);
    let app = router(halts);
    let limit = listen::limit();
    tokio::pin!(shutdown);
    loop {
        let (stream, peer, permit) = tokio::select! {
            accepted = listen::accept(&listener, &limit, "control") => accepted,
            _ = &mut shutdown => return Ok(()),
        };
        let acceptor = acceptor.clone();
        let brake_id = brake_id.clone();
        let app = app.clone();
        tokio::spawn(async move {
            let _permit = permit;
            let Ok(Ok(stream)) = tokio::time::timeout(listen::HANDSHAKE_TIMEOUT, acceptor.accept(stream)).await else {
                tracing::warn!(%peer, "control API: TLS handshake failed or timed out");
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
            let service = TowerToHyperService::new(app);
            if let Err(e) = listen::http().serve_connection(TokioIo::new(stream), service).await {
                tracing::debug!(%peer, error = %e, "control API: connection ended");
            }
        });
    }
}
