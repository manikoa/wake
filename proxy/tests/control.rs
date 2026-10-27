//! The brake's control API over mTLS: only the brake's SVID can put, list and lift halts,
//! and what it puts is what ext_authz enforces.

use std::sync::Arc;

use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tonic::Request;
use wake_proxy::authz::Authorizer;
use wake_proxy::authz::pb::auth::authorization_server::Authorization;
use wake_proxy::authz::pb::auth::{AttributeContext, CheckRequest, attribute_context::Peer};
use wake_proxy::budget::{Budget, Events};
use wake_proxy::control;
use wake_proxy::export::Counters;
use wake_proxy::halts::Halts;
use wake_proxy::signer::dev_identity_with_tls;

const BRAKE: &str = "spiffe://wake.local/brake";

struct Control {
    url: String,
    ca_pem: String,
    brake: (String, String),
    proxy: (String, String),
    halts: Arc<Halts>,
    _stop: oneshot::Sender<()>,
}

async fn control() -> Control {
    control_with(Halts::in_memory()).await
}

async fn control_with(halts: Halts) -> Control {
    control_limited(halts, wake_proxy::listen::CONTROL).await
}

async fn control_limited(halts: Halts, limits: wake_proxy::listen::Limits) -> Control {
    let id = dev_identity_with_tls("spiffe://wake.local/proxy", &[], Some(BRAKE)).unwrap();
    let halts = Arc::new(halts);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("https://{}", listener.local_addr().unwrap());
    let tls = control::Tls {
        cert_pem: id.cert_pem.clone(),
        key_pem: id.key_pem.clone(),
        bundle_pem: id.ca_pem.clone(),
        brake_id: BRAKE.into(),
    };
    let (stop, stopped) = oneshot::channel::<()>();
    let served = halts.clone();
    tokio::spawn(async move {
        control::serve_with(listener, served, tls, limits, async {
            let _ = stopped.await;
        })
        .await
        .unwrap();
    });
    Control {
        url,
        ca_pem: id.ca_pem,
        brake: id.brake.unwrap(),
        proxy: (id.cert_pem, id.key_pem),
        halts,
        _stop: stop,
    }
}

/// A client presenting this SVID. The proxy's certificate names a SPIFFE ID, not a host,
/// so the host isn't checked; the chain is.
fn client(c: &Control, (cert, key): &(String, String)) -> reqwest::Client {
    reqwest::Client::builder()
        .tls_certs_only([reqwest::Certificate::from_pem(c.ca_pem.as_bytes()).unwrap()])
        .identity(reqwest::Identity::from_pem(format!("{cert}{key}").as_bytes()).unwrap())
        .danger_accept_invalid_hostnames(true)
        .build()
        .unwrap()
}

#[tokio::test]
async fn the_brake_puts_lists_and_lifts_halts() {
    let c = control().await;
    let brake = client(&c, &c.brake);

    let put = brake
        .put(format!("{}/v1/halts/halt-1", c.url))
        .json(&serde_json::json!({"scope": {"agents": ["billing"]}, "issued_at": 42}))
        .send()
        .await
        .unwrap();
    assert_eq!(put.status(), 200);
    let state: serde_json::Value = put.json().await.unwrap();
    assert_eq!(state["version"], 1);
    assert_eq!(state["halts"][0]["id"], "halt-1");
    assert_eq!(state["halts"][0]["issued_at"], 42);
    assert_eq!(c.halts.deciding(Some("billing"), None).len(), 1);

    let listed: serde_json::Value = brake
        .get(format!("{}/v1/halts", c.url))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(listed, state);

    let lifted = brake.delete(format!("{}/v1/halts/halt-1", c.url)).send().await.unwrap();
    assert_eq!(lifted.status(), 200);
    let state: serde_json::Value = lifted.json().await.unwrap();
    assert_eq!(
        (state["version"].as_u64(), state["halts"].as_array().unwrap().len()),
        (Some(2), 0)
    );
    assert!(c.halts.deciding(Some("billing"), None).is_empty());
}

#[tokio::test]
async fn bad_requests_change_nothing() {
    let c = control().await;
    let brake = client(&c, &c.brake);
    let empty = brake
        .put(format!("{}/v1/halts/halt-1", c.url))
        .json(&serde_json::json!({"scope": {}}))
        .send()
        .await
        .unwrap();
    assert_eq!(empty.status(), 400);
    let bad_id = brake
        .put(format!("{}/v1/halts/not%20an%20id", c.url))
        .json(&serde_json::json!({"scope": {"fleet": true}}))
        .send()
        .await
        .unwrap();
    assert_eq!(bad_id.status(), 400);
    assert_eq!(c.halts.state().version, 0);
}

#[tokio::test]
async fn any_other_identity_is_refused() {
    let c = control().await;
    // The proxy's own SVID chains to the same bundle, but isn't the brake.
    let impostor = client(&c, &c.proxy);
    let refused = impostor
        .put(format!("{}/v1/halts/halt-1", c.url))
        .json(&serde_json::json!({"scope": {"fleet": true}}))
        .send()
        .await;
    assert!(refused.is_err(), "{refused:?}");
    // And a client with no certificate at all doesn't get past the handshake.
    let anonymous = reqwest::Client::builder()
        .tls_certs_only([reqwest::Certificate::from_pem(c.ca_pem.as_bytes()).unwrap()])
        .danger_accept_invalid_hostnames(true)
        .build()
        .unwrap();
    assert!(anonymous.get(format!("{}/v1/halts", c.url)).send().await.is_err());
    assert_eq!(c.halts.state().version, 0, "nothing changed");
}

#[tokio::test]
async fn what_the_brake_puts_is_what_ext_authz_enforces() {
    let c = control().await;
    let counters = Arc::new(Counters::default());
    let (events, mut recorded) = Events::channel(8, Budget::new(1 << 20), counters.clone());
    let authz = Authorizer::new(c.halts.clone(), events, counters);
    let check = || {
        Request::new(CheckRequest {
            attributes: Some(AttributeContext {
                source: Some(Peer {
                    principal: "spiffe://wake.local/agent/billing".into(),
                    ..Default::default()
                }),
                ..Default::default()
            }),
        })
    };
    assert_eq!(
        authz
            .check(check())
            .await
            .unwrap()
            .get_ref()
            .status
            .as_ref()
            .unwrap()
            .code,
        0
    );
    client(&c, &c.brake)
        .put(format!("{}/v1/halts/fleet-halt", c.url))
        .json(&serde_json::json!({"scope": {"fleet": true}}))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap();
    assert_eq!(
        authz
            .check(check())
            .await
            .unwrap()
            .get_ref()
            .status
            .as_ref()
            .unwrap()
            .code,
        7
    );
    let denied = recorded.try_recv().unwrap().event;
    assert_eq!(denied.str("wake.agent.id"), Some("spiffe://wake.local/agent/billing"));
}

#[tokio::test]
async fn a_quarantine_reaches_the_memory_adapters() {
    let c = control().await;
    let brake = client(&c, &c.brake);
    let put = brake
        .put(format!("{}/v1/quarantines/q1", c.url))
        .json(&serde_json::json!({"selection": {"lock": {"agents": ["billing"]}, "origins": ["tool:fetch_page"]}}))
        .send()
        .await
        .unwrap();
    assert_eq!(put.status(), 200);
    let state: serde_json::Value = put.json().await.unwrap();
    assert_eq!(state["quarantines"][0]["id"], "q1");

    // An adapter fetches its rules from the read-only port, with no certificate.
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let memory = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(wake_proxy::memory::serve(
        listener,
        c.halts.clone(),
        std::future::pending(),
    ));
    let rules: serde_json::Value = reqwest::get(format!("{memory}/v1/memory?agent=billing"))
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(rules["locked_by"], "q1");
    assert_eq!(rules["quarantines"][0]["selection"]["origins"][0], "tool:fetch_page");

    // The brake sees that billing's adapter has it.
    let seen: serde_json::Value = brake
        .get(format!("{}/v1/memory/seen", c.url))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(seen["billing"]["version"], state["version"]);

    let empty = brake
        .put(format!("{}/v1/quarantines/q2", c.url))
        .json(&serde_json::json!({"selection": {}}))
        .send()
        .await
        .unwrap();
    assert_eq!(empty.status(), 400);
    let released = brake
        .delete(format!("{}/v1/quarantines/q1", c.url))
        .send()
        .await
        .unwrap();
    let state: serde_json::Value = released.json().await.unwrap();
    assert_eq!(state["quarantines"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn a_halt_that_cant_be_stored_is_in_force_but_not_acknowledged() {
    let dir = std::env::temp_dir().join(format!("wake-control-{}", uuid::Uuid::new_v4()));
    Halts::init(&dir).unwrap();
    let c = control_with(Halts::load(&dir)).await;
    // The disk goes away.
    std::fs::remove_dir_all(&dir).unwrap();
    std::fs::write(&dir, b"").unwrap();
    let put = client(&c, &c.brake)
        .put(format!("{}/v1/halts/halt-1", c.url))
        .json(&serde_json::json!({"scope": {"agents": ["billing"]}}))
        .send()
        .await
        .unwrap();
    assert_eq!(put.status(), 500, "not the lever's acknowledgement");
    let state: serde_json::Value = put.json().await.unwrap();
    assert_eq!(state["halts"][0]["id"], "halt-1");
    assert!(state["not_persisted"].is_string(), "{state}");
    assert_eq!(c.halts.deciding(Some("billing"), None).len(), 1, "but in force");
    let listed: serde_json::Value = client(&c, &c.brake)
        .get(format!("{}/v1/halts", c.url))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(listed["not_persisted"].is_string());
    std::fs::remove_file(&dir).unwrap();
}

#[tokio::test]
async fn the_brake_initializes_a_proxy_that_lost_its_state() {
    let dir = std::env::temp_dir().join(format!("wake-control-{}", uuid::Uuid::new_v4()));
    let c = control_with(Halts::load(&dir)).await;
    let brake = client(&c, &c.brake);
    let listed: serde_json::Value = brake
        .get(format!("{}/v1/halts", c.url))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(listed["uninitialized"], true);
    assert!(listed.get("epoch").is_none());
    assert_eq!(c.halts.deciding(Some("billing"), None)[0].id, "uninitialized-state");
    let lifted: serde_json::Value = brake
        .delete(format!("{}/v1/halts/uninitialized-state", c.url))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(lifted.get("uninitialized").is_none());
    assert!(lifted["epoch"].as_str().is_some_and(|e| !e.is_empty()));
    assert!(c.halts.deciding(Some("billing"), None).is_empty());
    std::fs::remove_dir_all(&dir).unwrap();
}

#[tokio::test]
async fn clients_that_connect_and_stall_cant_keep_the_brake_out() {
    use tokio::io::AsyncReadExt;
    let limits = wake_proxy::listen::Limits {
        connections: 2,
        handshake: std::time::Duration::from_millis(300),
        lifetime: std::time::Duration::from_secs(5),
    };
    let c = control_limited(Halts::in_memory(), limits).await;
    let addr = c.url.trim_start_matches("https://").to_string();
    // Two connections that never start TLS take every slot.
    let mut stalled = vec![];
    for _ in 0..2 {
        stalled.push(tokio::net::TcpStream::connect(&addr).await.unwrap());
    }
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    // A third is closed at once.
    let mut third = tokio::net::TcpStream::connect(&addr).await.unwrap();
    let mut buf = [0u8; 1];
    let read = tokio::time::timeout(std::time::Duration::from_millis(200), third.read(&mut buf)).await;
    assert!(matches!(read, Ok(Ok(0)) | Ok(Err(_))), "{read:?}");
    // The stalled ones are closed once the handshake times out, and the brake gets in.
    for mut s in stalled {
        let read = tokio::time::timeout(std::time::Duration::from_secs(2), s.read(&mut buf)).await;
        assert!(matches!(read, Ok(Ok(0)) | Ok(Err(_))), "{read:?}");
    }
    let listed = client(&c, &c.brake)
        .get(format!("{}/v1/halts", c.url))
        .send()
        .await
        .unwrap();
    assert_eq!(listed.status(), 200);
}
