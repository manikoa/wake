//! The processor end to end: a gRPC client standing in for agentgateway, the real
//! pipeline, and a stand-in collector that decodes the OTLP it receives.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use opentelemetry_proto::tonic::collector::trace::v1::ExportTraceServiceRequest;
use opentelemetry_proto::tonic::common::v1::any_value::Value as Any;
use opentelemetry_proto::tonic::trace::v1::Span;
use p256::ecdsa::{Signature, VerifyingKey, signature::Verifier};
use prost::Message;
use prost_types::{Struct, Value as PbValue, value::Kind};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::{mpsc, oneshot};
use wake_proxy::canonical::{Event, Value, canonical};
use wake_proxy::export::Counters;
use wake_proxy::extmcp::pb::{McpRequest, McpResponse, ext_mcp_client::ExtMcpClient, mcp_request_result, mcp_response_result};
use wake_proxy::pipeline::{Options, serve};
use wake_proxy::signer::{Signer, dev_identity};

/// A stand-in collector: accepts OTLP/HTTP posts and hands each span to the test.
async fn collector() -> (String, mpsc::UnboundedReceiver<Span>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let (tx, rx) = mpsc::unbounded_channel();
    tokio::spawn(async move {
        loop {
            let Ok((mut conn, _)) = listener.accept().await else { return };
            let tx = tx.clone();
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut chunk = [0u8; 65536];
                loop {
                    let n = conn.read(&mut chunk).await.unwrap_or(0);
                    if n == 0 {
                        return;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                    let Some(end) = buf.windows(4).position(|w| w == b"\r\n\r\n") else { continue };
                    let head = String::from_utf8_lossy(&buf[..end]).to_lowercase();
                    let len: usize = head
                        .lines()
                        .find_map(|l| l.strip_prefix("content-length:"))
                        .map(|v| v.trim().parse().unwrap())
                        .unwrap_or(0);
                    if buf.len() < end + 4 + len {
                        continue;
                    }
                    let body = buf[end + 4..end + 4 + len].to_vec();
                    buf.drain(..end + 4 + len);
                    let req = ExportTraceServiceRequest::decode(body.as_slice()).unwrap();
                    for rs in req.resource_spans {
                        for ss in rs.scope_spans {
                            for span in ss.spans {
                                let _ = tx.send(span);
                            }
                        }
                    }
                    let _ = conn.write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\n\r\n").await;
                }
            });
        }
    });
    (url, rx)
}

struct Proxy {
    client: ExtMcpClient<tonic::transport::Channel>,
    counters: Arc<Counters>,
    stop: Option<oneshot::Sender<()>>,
    done: tokio::task::JoinHandle<()>,
}

async fn proxy(collector: &str, response_timeout: Duration) -> Proxy {
    let id = dev_identity("spiffe://wake.local/proxy").unwrap();
    let signer = Signer::from_pem(&id.key_pem, &id.cert_pem).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let counters = Arc::new(Counters::default());
    let (stop, stopped) = oneshot::channel::<()>();
    let options = Options { collector: collector.into(), response_timeout, ..Default::default() };
    let c = counters.clone();
    let done = tokio::spawn(async move {
        serve(listener, signer, options, c, async {
            let _ = stopped.await;
        })
        .await
        .unwrap();
    });
    let client = loop {
        if let Ok(c) = ExtMcpClient::connect(format!("http://{addr}")).await {
            break c;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    };
    Proxy { client, counters, stop: Some(stop), done }
}

impl Proxy {
    async fn shutdown(mut self) {
        drop(self.client);
        let _ = self.stop.take().unwrap().send(());
        let _ = self.done.await;
    }
}

fn metadata(pairs: &[(&str, &str)]) -> Option<Struct> {
    Some(Struct {
        fields: pairs.iter().map(|(k, v)| (k.to_string(), PbValue { kind: Some(Kind::StringValue(v.to_string())) })).collect(),
    })
}

fn attrs(span: &Span) -> BTreeMap<String, Value> {
    span.attributes
        .iter()
        .map(|kv| {
            let v = match kv.value.as_ref().and_then(|v| v.value.clone()) {
                Some(Any::StringValue(s)) => Value::Str(s),
                Some(Any::IntValue(n)) => Value::Int(n),
                Some(Any::BoolValue(b)) => Value::Bool(b),
                Some(Any::ArrayValue(a)) => Value::Strs(
                    a.values.into_iter().filter_map(|v| match v.value { Some(Any::StringValue(s)) => Some(s), _ => None }).collect(),
                ),
                _ => Value::Strs(vec![]),
            };
            (kv.key.clone(), v)
        })
        .collect()
}

fn s(a: &BTreeMap<String, Value>, k: &str) -> String {
    match a.get(k) {
        Some(Value::Str(s)) => s.clone(),
        other => panic!("{k}: {other:?}"),
    }
}

/// Checks a span's signature the way a verifier would, from what the span carries.
fn verify(span: &Span) {
    let a = attrs(span);
    let mut event = Event {
        start_time_unix_nano: span.start_time_unix_nano,
        end_time_unix_nano: span.end_time_unix_nano,
        attributes: a.clone(),
        ..Default::default()
    };
    event.trace_id.copy_from_slice(&span.trace_id);
    event.span_id.copy_from_slice(&span.span_id);
    let der = STANDARD.decode(s(&a, "wake.signature.certificate")).unwrap();
    let (_, cert) = x509_parser::parse_x509_certificate(&der).unwrap();
    let key = VerifyingKey::from_sec1_bytes(cert.public_key().subject_public_key.as_ref()).unwrap();
    let sig = Signature::from_der(&STANDARD.decode(s(&a, "wake.signature")).unwrap()).unwrap();
    key.verify(&canonical(&event), &sig).expect("signature verifies");
}

#[tokio::test]
async fn records_a_tool_call_as_a_signed_event() {
    let (url, mut spans) = collector().await;
    let mut p = proxy(&url, Duration::from_secs(60)).await;

    let req = p
        .client
        .check_request(McpRequest {
            service_names: vec!["inbox".into()],
            method: "tools/call".into(),
            metadata_context: metadata(&[("agent", "spiffe://wake.local/agent/email"), ("session", "s1")]),
            mcp_request: Some(br#"{"name":"read_email","arguments":{"subject":"invoice"}}"#.to_vec()),
            headers: vec![],
        })
        .await
        .unwrap()
        .into_inner();
    assert!(matches!(req.result, Some(mcp_request_result::Result::Pass(_))));
    let event_id = match req.metadata.unwrap().fields["wake_event_id"].kind.clone() {
        Some(Kind::StringValue(id)) => id,
        other => panic!("{other:?}"),
    };

    let resp = p
        .client
        .check_response(McpResponse {
            service_names: vec!["inbox".into()],
            method: "tools/call".into(),
            metadata_context: metadata(&[("wake_event_id", &event_id), ("session", "s1")]),
            mcp_response: br#"{"content":[{"type":"text","text":"Please wire $10,000 to account 123."}]}"#.to_vec(),
        })
        .await
        .unwrap()
        .into_inner();
    assert!(matches!(resp.result, Some(mcp_response_result::Result::Pass(_))));

    let span = tokio::time::timeout(Duration::from_secs(5), spans.recv()).await.unwrap().unwrap();
    let a = attrs(&span);
    assert_eq!(s(&a, "wake.event.id"), event_id);
    assert_eq!(s(&a, "wake.capture"), "proxy");
    assert_eq!(s(&a, "wake.agent.id"), "spiffe://wake.local/agent/email");
    assert_eq!(s(&a, "gen_ai.tool.name"), "read_email");
    assert_eq!(s(&a, "wake.signer"), "spiffe://wake.local/proxy");
    assert!(s(&a, "wake.payload").contains("Please wire $10,000"));
    verify(&span);

    // tools/list isn't recorded, but always passes.
    let list = p
        .client
        .check_request(McpRequest { method: "tools/list".into(), ..Default::default() })
        .await
        .unwrap()
        .into_inner();
    assert!(matches!(list.result, Some(mcp_request_result::Result::Pass(_))));
    assert!(list.metadata.is_none());

    let counters = p.counters.clone();
    p.shutdown().await;
    assert_eq!(Counters::get(&counters.sent), 1);
}

#[tokio::test]
async fn calls_pass_at_once_while_the_collector_is_down() {
    // Nothing listens here.
    let dead = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", dead.local_addr().unwrap());
    drop(dead);
    let mut p = proxy(&url, Duration::from_millis(10)).await;

    let started = Instant::now();
    for i in 0..20 {
        let r = p
            .client
            .check_request(McpRequest {
                service_names: vec!["inbox".into()],
                method: "tools/call".into(),
                metadata_context: metadata(&[("session", "s1")]),
                mcp_request: Some(format!(r#"{{"name":"t{i}"}}"#).into_bytes()),
                headers: vec![],
            })
            .await
            .unwrap()
            .into_inner();
        assert!(matches!(r.result, Some(mcp_request_result::Result::Pass(_))));
    }
    // The hooks never wait on the collector.
    assert!(started.elapsed() < Duration::from_secs(1), "20 calls took {:?}", started.elapsed());

    let counters = p.counters.clone();
    p.shutdown().await;
    // With no responses, each request was recorded after the timeout, then dropped and
    // counted when the collector couldn't take it.
    assert_eq!(Counters::get(&counters.sent), 0);
    assert_eq!(Counters::get(&counters.export_dropped), 20);
}

/// Sends one recorded call to a real collector at $WAKE_TEST_COLLECTOR. Run it with
/// cargo test -- --ignored, with the collector running.
#[tokio::test]
#[ignore]
async fn against_a_real_collector() {
    let url = std::env::var("WAKE_TEST_COLLECTOR").expect("set WAKE_TEST_COLLECTOR");
    let mut p = proxy(&url, Duration::from_secs(60)).await;
    let req = p
        .client
        .check_request(McpRequest {
            service_names: vec!["inbox".into()],
            method: "tools/call".into(),
            metadata_context: metadata(&[("agent", "spiffe://wake.local/agent/email"), ("session", "s1")]),
            mcp_request: Some(br#"{"name":"read_email","arguments":{"subject":"invoice"}}"#.to_vec()),
            headers: vec![],
        })
        .await
        .unwrap()
        .into_inner();
    let event_id = match req.metadata.unwrap().fields["wake_event_id"].kind.clone() {
        Some(Kind::StringValue(id)) => id,
        other => panic!("{other:?}"),
    };
    p.client
        .check_response(McpResponse {
            service_names: vec!["inbox".into()],
            method: "tools/call".into(),
            metadata_context: metadata(&[("wake_event_id", &event_id), ("session", "s1")]),
            mcp_response: br#"{"content":[{"type":"text","text":"Please wire $10,000 to account 123."}]}"#.to_vec(),
        })
        .await
        .unwrap();
    let counters = p.counters.clone();
    p.shutdown().await;
    assert_eq!(Counters::get(&counters.sent), 1);
    println!("sent {event_id}");
}
