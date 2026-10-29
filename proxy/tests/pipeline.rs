//! The processor end to end: a gRPC client standing in for agentgateway, the real
//! pipeline, and a stand-in collector that decodes the OTLP it receives.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
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
use wake_proxy::budget::MAX_CAPTURE;
use wake_proxy::canonical::{Event, Value, canonical};
use wake_proxy::export::{Counters, MAX_BATCH_BYTES};
use wake_proxy::extmcp::pb::{
    McpRequest, McpResponse, ext_mcp_client::ExtMcpClient, mcp_request_result, mcp_response_result,
};
use wake_proxy::pipeline::{Options, serve};
use wake_proxy::signer::{Signer, dev_identity};

/// A stand-in collector: accepts OTLP/HTTP posts and hands each span to the test.
async fn collector() -> (String, mpsc::UnboundedReceiver<Span>) {
    let (url, spans, _) = measuring_collector().await;
    (url, spans)
}

/// A stand-in collector that also keeps the largest request body it was sent.
async fn measuring_collector() -> (String, mpsc::UnboundedReceiver<Span>, Arc<AtomicUsize>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let (tx, rx) = mpsc::unbounded_channel();
    let largest = Arc::new(AtomicUsize::new(0));
    let seen = largest.clone();
    tokio::spawn(async move {
        loop {
            let Ok((mut conn, _)) = listener.accept().await else {
                return;
            };
            let tx = tx.clone();
            let seen = seen.clone();
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut chunk = [0u8; 65536];
                loop {
                    let n = conn.read(&mut chunk).await.unwrap_or(0);
                    if n == 0 {
                        return;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                    let Some(end) = buf.windows(4).position(|w| w == b"\r\n\r\n") else {
                        continue;
                    };
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
                    seen.fetch_max(body.len(), Ordering::Relaxed);
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
    (url, rx, largest)
}

struct Proxy {
    addr: String,
    client: ExtMcpClient<tonic::transport::Channel>,
    counters: Arc<Counters>,
    stop: Option<oneshot::Sender<()>>,
    done: tokio::task::JoinHandle<()>,
}

async fn proxy(collector: &str, response_timeout: Duration) -> Proxy {
    proxy_with(Options {
        collector: collector.into(),
        // For against_a_real_collector over TLS: the CA the collector's certificate chains to.
        collector_ca: std::env::var("WAKE_TEST_COLLECTOR_CA")
            .ok()
            .map(|p| std::fs::read(p).unwrap()),
        response_timeout,
        ..Default::default()
    })
    .await
}

async fn proxy_with(options: Options) -> Proxy {
    let id = dev_identity("spiffe://wake.local/proxy").unwrap();
    let signer = Signer::from_pem(&id.key_pem, &id.cert_pem).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let counters = Arc::new(Counters::default());
    let (stop, stopped) = oneshot::channel::<()>();
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
    Proxy {
        addr: format!("http://{addr}"),
        client,
        counters,
        stop: Some(stop),
        done,
    }
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
        fields: pairs
            .iter()
            .map(|(k, v)| {
                (
                    k.to_string(),
                    PbValue {
                        kind: Some(Kind::StringValue(v.to_string())),
                    },
                )
            })
            .collect(),
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
                    a.values
                        .into_iter()
                        .filter_map(|v| match v.value {
                            Some(Any::StringValue(s)) => Some(s),
                            _ => None,
                        })
                        .collect(),
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

    let span = tokio::time::timeout(Duration::from_secs(5), spans.recv())
        .await
        .unwrap()
        .unwrap();
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
        .check_request(McpRequest {
            method: "tools/list".into(),
            ..Default::default()
        })
        .await
        .unwrap()
        .into_inner();
    assert!(matches!(list.result, Some(mcp_request_result::Result::Pass(_))));
    assert!(list.metadata.is_none());

    let counters = p.counters.clone();
    p.shutdown().await;
    assert_eq!(Counters::get(&counters.sent), 1);
    assert_eq!(Counters::get(&counters.paired_by_id), 1);
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
    assert!(
        started.elapsed() < Duration::from_secs(1),
        "20 calls took {:?}",
        started.elapsed()
    );

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

#[tokio::test]
async fn records_an_a2a_message_and_reply_through_ext_proc() {
    use wake_proxy::extproc::pb::common::HeaderValue;
    use wake_proxy::extproc::pb::ext_proc::{
        HeaderMap, HttpBody, HttpHeaders, ProcessingRequest, ProtocolConfiguration,
        external_processor_client::ExternalProcessorClient, processing_mode::BodySendMode,
        processing_request::Request as Phase,
    };

    let (url, mut spans) = collector().await;
    let p = proxy(&url, Duration::from_secs(60)).await;
    let mut client = ExternalProcessorClient::connect(p.addr.clone()).await.unwrap();

    let headers = |pairs: &[(&str, &str)]| HttpHeaders {
        headers: Some(HeaderMap {
            headers: pairs
                .iter()
                .map(|(k, v)| HeaderValue {
                    key: k.to_string(),
                    raw_value: v.as_bytes().to_vec(),
                    ..Default::default()
                })
                .collect(),
        }),
        ..Default::default()
    };
    let route: Struct = Struct {
        fields: [
            ("sender", "spiffe://wake.local/agent/planner"),
            ("receiver", "research"),
        ]
        .into_iter()
        .map(|(k, v)| {
            (
                k.to_string(),
                PbValue {
                    kind: Some(Kind::StringValue(v.into())),
                },
            )
        })
        .collect(),
    };
    let messages = vec![
        ProcessingRequest {
            request: Some(Phase::RequestHeaders(headers(&[(":method", "POST"), ("content-type", "application/json")]))),
            attributes: [("envoy.filters.http.ext_proc".to_string(), route)].into(),
            protocol_config: Some(ProtocolConfiguration {
                request_body_mode: BodySendMode::BufferedPartial as i32,
                response_body_mode: BodySendMode::BufferedPartial as i32,
                ..Default::default()
            }),
            ..Default::default()
        },
        ProcessingRequest {
            request: Some(Phase::RequestBody(HttpBody {
                body: br#"{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{"role":"user","messageId":"m1","parts":[{"kind":"text","text":"Is Acme legit?"}]}}}"#.to_vec(),
                end_of_stream: true,
            })),
            ..Default::default()
        },
        ProcessingRequest { request: Some(Phase::ResponseHeaders(headers(&[(":status", "200"), ("content-type", "application/json")]))), ..Default::default() },
        ProcessingRequest {
            request: Some(Phase::ResponseBody(HttpBody {
                body: br#"{"jsonrpc":"2.0","id":1,"result":{"kind":"message","messageId":"r1","parts":[{"kind":"text","text":"Acme is unverified."}]}}"#.to_vec(),
                end_of_stream: true,
            })),
            ..Default::default()
        },
    ];
    let mut answers = client.process(tokio_stream::iter(messages)).await.unwrap().into_inner();
    let mut n = 0;
    while let Some(answer) = answers.message().await.unwrap() {
        assert!(answer.response.is_some());
        n += 1;
    }
    assert_eq!(n, 4, "every message is answered");

    let mut got = vec![];
    for _ in 0..2 {
        got.push(
            tokio::time::timeout(Duration::from_secs(5), spans.recv())
                .await
                .unwrap()
                .unwrap(),
        );
    }
    got.sort_by_key(|s| s.start_time_unix_nano + if s.parent_span_id.is_empty() { 0 } else { 1 });
    let (message, reply) = (&got[0], &got[1]);
    let (m, r) = (attrs(message), attrs(reply));
    assert_eq!(s(&m, "wake.event.kind"), "agent_message");
    assert_eq!(s(&m, "wake.agent.id"), "research");
    assert_eq!(s(&r, "wake.agent.id"), "spiffe://wake.local/agent/planner");
    assert_eq!(r["wake.parents"], Value::Strs(vec![s(&m, "wake.event.id")]));
    verify(message);
    verify(reply);
    p.shutdown().await;
}

/// One MCP call through the hooks: a request, then its response with this result.
async fn call(p: &mut Proxy, tool: &str, result: Vec<u8>) {
    let req = p
        .client
        .check_request(McpRequest {
            service_names: vec!["files".into()],
            method: "tools/call".into(),
            metadata_context: metadata(&[("session", "s1")]),
            mcp_request: Some(format!(r#"{{"name":"{tool}"}}"#).into_bytes()),
            headers: vec![],
        })
        .await
        .unwrap()
        .into_inner();
    let event_id = match req.metadata.unwrap().fields["wake_event_id"].kind.clone() {
        Some(Kind::StringValue(id)) => id,
        other => panic!("{other:?}"),
    };
    let resp = p
        .client
        .check_response(McpResponse {
            service_names: vec!["files".into()],
            method: "tools/call".into(),
            metadata_context: metadata(&[("wake_event_id", &event_id), ("session", "s1")]),
            mcp_response: result,
        })
        .await
        .unwrap()
        .into_inner();
    assert!(matches!(resp.result, Some(mcp_response_result::Result::Pass(_))));
}

#[tokio::test]
async fn large_results_are_capped_and_batches_stay_under_the_collectors_limit() {
    let (url, mut spans, largest) = measuring_collector().await;
    let mut p = proxy(&url, Duration::from_secs(60)).await;
    // 30 results of 3 MiB: 30 MiB of events at 1 MiB each, over the collector's 20 MiB
    // request limit if they went out in one batch.
    let big = format!(r#"{{"content":[{{"type":"text","text":"{}"}}]}}"#, "x".repeat(3 << 20)).into_bytes();
    for i in 0..30 {
        call(&mut p, &format!("read_{i}"), big.clone()).await;
    }
    for _ in 0..30 {
        let span = tokio::time::timeout(Duration::from_secs(10), spans.recv())
            .await
            .unwrap()
            .unwrap();
        let a = attrs(&span);
        let payload: serde_json::Value = serde_json::from_str(&s(&a, "wake.payload")).unwrap();
        assert_eq!(payload["truncated"], true);
        assert_eq!(payload["output"].as_str().unwrap().len(), MAX_CAPTURE);
        assert!(s(&a, "gen_ai.tool.name").starts_with("read_"));
        verify(&span);
    }
    let largest = largest.load(Ordering::Relaxed);
    assert!(largest <= MAX_BATCH_BYTES + (1 << 20), "a request of {largest} bytes");
    let counters = p.counters.clone();
    p.shutdown().await;
    assert_eq!(Counters::get(&counters.sent), 30);
    assert_eq!(
        Counters::get(&counters.hooks_dropped) + Counters::get(&counters.export_dropped),
        0
    );
}

#[tokio::test]
async fn a_flood_of_large_calls_is_dropped_and_counted_not_held() {
    // Nothing listens here, so every event waits on retries.
    let dead = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", dead.local_addr().unwrap());
    drop(dead);
    let mut p = proxy_with(Options {
        collector: url,
        response_timeout: Duration::from_secs(60),
        queue_bytes: 4 << 20,
        ..Default::default()
    })
    .await;
    let big = vec![b'x'; 2 << 20];
    let started = Instant::now();
    for i in 0..20 {
        call(&mut p, &format!("t{i}"), big.clone()).await;
    }
    assert!(started.elapsed() < Duration::from_secs(5), "{:?}", started.elapsed());
    let counters = p.counters.clone();
    p.shutdown().await;
    // At most 4 MiB of events waited at once; the rest were counted, not kept.
    assert!(
        Counters::get(&counters.export_dropped) >= 10,
        "{}",
        Counters::get(&counters.export_dropped)
    );
    assert_eq!(Counters::get(&counters.sent), 0);
}

#[cfg(unix)]
#[tokio::test]
async fn serves_the_hooks_on_a_unix_socket() {
    use hyper_util::rt::TokioIo;
    use tokio::net::UnixStream;
    use wake_proxy::pipeline::serve_unix;

    let (url, mut spans) = collector().await;
    // Short: a socket's path has to fit in about 100 bytes.
    let dir = std::path::PathBuf::from("/tmp").join(format!("wake-hooks-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("proxy.sock");
    let listener = wake_proxy::listen::bind_unix(&path).unwrap();
    let id = dev_identity("spiffe://wake.local/proxy").unwrap();
    let signer = Signer::from_pem(&id.key_pem, &id.cert_pem).unwrap();
    let (stop, stopped) = oneshot::channel::<()>();
    let options = Options {
        collector: url,
        ..Default::default()
    };
    let done = tokio::spawn(serve_unix(
        listener,
        signer,
        options,
        Arc::new(Counters::default()),
        async {
            let _ = stopped.await;
        },
    ));

    let socket = path.clone();
    let channel = tonic::transport::Endpoint::try_from("http://wake-proxy")
        .unwrap()
        .connect_with_connector(tower::service_fn(move |_| {
            let socket = socket.clone();
            async move { Ok::<_, std::io::Error>(TokioIo::new(UnixStream::connect(socket).await?)) }
        }))
        .await
        .unwrap();
    let mut client = ExtMcpClient::new(channel);
    let req = client
        .check_request(McpRequest {
            service_names: vec!["inbox".into()],
            method: "tools/call".into(),
            metadata_context: metadata(&[("session", "s1")]),
            mcp_request: Some(br#"{"name":"read_email"}"#.to_vec()),
            headers: vec![],
        })
        .await
        .unwrap()
        .into_inner();
    let event_id = match req.metadata.unwrap().fields["wake_event_id"].kind.clone() {
        Some(Kind::StringValue(id)) => id,
        other => panic!("{other:?}"),
    };
    client
        .check_response(McpResponse {
            service_names: vec!["inbox".into()],
            method: "tools/call".into(),
            metadata_context: metadata(&[("wake_event_id", &event_id), ("session", "s1")]),
            mcp_response: b"{}".to_vec(),
        })
        .await
        .unwrap();
    let span = tokio::time::timeout(Duration::from_secs(5), spans.recv())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(s(&attrs(&span), "wake.event.id"), event_id);
    verify(&span);

    drop(client);
    let _ = stop.send(());
    let _ = done.await;
    std::fs::remove_dir_all(&dir).unwrap();
}

/// An ext_proc message: request headers on a route from sender to research.
fn a2a_start(sender: &str) -> wake_proxy::extproc::pb::ext_proc::ProcessingRequest {
    use wake_proxy::extproc::pb::ext_proc::{
        ProcessingRequest, ProtocolConfiguration, processing_mode::BodySendMode, processing_request::Request as Phase,
    };
    let route = metadata(&[("sender", sender), ("receiver", "research")]).unwrap();
    ProcessingRequest {
        request: Some(Phase::RequestHeaders(a2a_headers(&[
            (":method", "POST"),
            (":path", "/agents/research"),
            ("content-type", "application/json"),
        ]))),
        attributes: [("envoy.filters.http.ext_proc".to_string(), route)].into(),
        protocol_config: Some(ProtocolConfiguration {
            request_body_mode: BodySendMode::BufferedPartial as i32,
            response_body_mode: BodySendMode::BufferedPartial as i32,
            ..Default::default()
        }),
        ..Default::default()
    }
}

fn a2a_headers(pairs: &[(&str, &str)]) -> wake_proxy::extproc::pb::ext_proc::HttpHeaders {
    use wake_proxy::extproc::pb::common::HeaderValue;
    use wake_proxy::extproc::pb::ext_proc::{HeaderMap, HttpHeaders};
    HttpHeaders {
        headers: Some(HeaderMap {
            headers: pairs
                .iter()
                .map(|(k, v)| HeaderValue {
                    key: k.to_string(),
                    raw_value: v.as_bytes().to_vec(),
                    ..Default::default()
                })
                .collect(),
        }),
        ..Default::default()
    }
}

fn a2a_body(body: &[u8], response: bool, end: bool) -> wake_proxy::extproc::pb::ext_proc::ProcessingRequest {
    use wake_proxy::extproc::pb::ext_proc::{HttpBody, ProcessingRequest, processing_request::Request as Phase};
    let body = HttpBody {
        body: body.to_vec(),
        end_of_stream: end,
    };
    ProcessingRequest {
        request: Some(if response {
            Phase::ResponseBody(body)
        } else {
            Phase::RequestBody(body)
        }),
        ..Default::default()
    }
}

/// The rest of a streamed exchange, up to its first chunk: the message, the reply's
/// headers, and one event of the stream, which doesn't end.
fn a2a_streaming() -> Vec<wake_proxy::extproc::pb::ext_proc::ProcessingRequest> {
    use wake_proxy::extproc::pb::ext_proc::{ProcessingRequest, processing_request::Request as Phase};
    vec![
        a2a_body(
            br#"{"jsonrpc":"2.0","id":1,"method":"message/stream","params":{"message":{"role":"user","messageId":"m1","parts":[{"kind":"text","text":"Pay account 999-1234"}]}}}"#,
            false,
            true,
        ),
        ProcessingRequest {
            request: Some(Phase::ResponseHeaders(a2a_headers(&[
                (":status", "200"),
                ("content-type", "text/event-stream"),
            ]))),
            ..Default::default()
        },
        a2a_body(b"data: {\"kind\":\"status-update\"}\n\n", true, false),
    ]
}

/// Spans until none arrives for a while.
async fn drain(spans: &mut mpsc::UnboundedReceiver<Span>) -> Vec<Span> {
    let mut got = vec![];
    while let Ok(Some(span)) = tokio::time::timeout(Duration::from_millis(1500), spans.recv()).await {
        got.push(span);
    }
    got
}

#[tokio::test]
async fn a_halt_cuts_a_streamed_a2a_reply_under_way_and_only_that() {
    use wake_proxy::extproc::pb::ext_proc::external_processor_client::ExternalProcessorClient;
    use wake_proxy::halts::{Halts, Scope};

    let (url, mut spans) = collector().await;
    let halts = Arc::new(Halts::in_memory());
    let p = proxy_with(Options {
        collector: url,
        halts: halts.clone(),
        ..Default::default()
    })
    .await;
    let mut client = ExternalProcessorClient::connect(p.addr.clone()).await.unwrap();

    // Two exchanges under way, each mid-stream: billing's, and support's.
    let mut open = vec![];
    for sender in ["spiffe://wake.local/agent/billing", "spiffe://wake.local/agent/support"] {
        let (tx, rx) = mpsc::channel(8);
        tx.send(a2a_start(sender)).await.unwrap();
        for m in a2a_streaming() {
            tx.send(m).await.unwrap();
        }
        let mut answers = client
            .process(tokio_stream::wrappers::ReceiverStream::new(rx))
            .await
            .unwrap()
            .into_inner();
        for _ in 0..4 {
            assert!(answers.message().await.unwrap().is_some(), "answered while not halted");
        }
        open.push((tx, answers));
    }

    halts
        .put(
            "halt-billing",
            Scope {
                fleet: false,
                agents: vec!["spiffe://wake.local/agent/billing".into()],
                tokens: vec![],
            },
            1,
        )
        .unwrap();

    // Billing's stream ends at once, with the halt named; support's carries on.
    let (billing_tx, mut billing) = open.remove(0);
    let ended = tokio::time::timeout(Duration::from_secs(5), billing.message())
        .await
        .expect("cut, not left open");
    let status = ended.expect_err("the stream ends with an error");
    assert_eq!(status.code(), tonic::Code::PermissionDenied);
    assert!(status.message().contains("halt-billing"), "{}", status.message());
    let (support_tx, mut support) = open.remove(0);
    support_tx
        .send(a2a_body(b"data: {\"kind\":\"message\"}\n\n", true, true))
        .await
        .unwrap();
    assert!(
        support.message().await.unwrap().is_some(),
        "another agent's exchange isn't cut"
    );
    assert_eq!(Counters::get(&p.counters.cut), 1);
    drop((billing_tx, support_tx));

    let got: Vec<_> = drain(&mut spans).await.iter().map(attrs).collect();
    let cut: Vec<_> = got.iter().filter(|a| s(a, "wake.event.kind") == "denied").collect();
    assert_eq!(cut.len(), 1, "one cut");
    let payload: serde_json::Value = serde_json::from_str(&s(cut[0], "wake.payload")).unwrap();
    assert_eq!(payload["cut"], true);
    assert_eq!(payload["halt"], "halt-billing");
    assert_eq!(payload["receiver"], "research");
    assert_eq!(s(cut[0], "wake.agent.id"), "spiffe://wake.local/agent/billing");
    // It names the message billing's exchange recorded, and what the reply carried before
    // the cut is recorded too.
    let message = got
        .iter()
        .find(|a| s(a, "wake.event.id") == payload["recorded_as"].as_str().unwrap_or_default())
        .expect("the message the cut names was recorded");
    assert_eq!(s(message, "wake.event.kind"), "agent_message");
    let replies = got
        .iter()
        .filter(|a| s(a, "wake.event.kind") == "agent_message" && s(a, "wake.payload").contains("status-update"))
        .count();
    assert_eq!(replies, 2, "both replies so far are recorded, the cut one included");
    p.shutdown().await;
}

#[tokio::test]
async fn a_halt_keeps_an_mcp_result_from_its_caller_and_refuses_a_late_request() {
    use wake_proxy::extmcp::pb::authorization_error::Code;
    use wake_proxy::halts::{Halts, Scope};

    let (url, mut spans) = collector().await;
    let halts = Arc::new(Halts::in_memory());
    let mut p = proxy_with(Options {
        collector: url,
        halts: halts.clone(),
        ..Default::default()
    })
    .await;
    let caller = "spiffe://wake.local/agent/billing";
    let request = || McpRequest {
        service_names: vec!["payments".into()],
        method: "tools/call".into(),
        metadata_context: metadata(&[("agent", caller), ("session", "s1")]),
        mcp_request: Some(br#"{"name":"refund","arguments":{"amount":500}}"#.to_vec()),
        headers: vec![],
    };
    let req = p.client.check_request(request()).await.unwrap().into_inner();
    let event_id = match req.metadata.unwrap().fields["wake_event_id"].kind.clone() {
        Some(Kind::StringValue(id)) => id,
        other => panic!("{other:?}"),
    };

    // The fleet is halted while the payment runs.
    halts
        .put(
            "fleet-halt",
            Scope {
                fleet: true,
                agents: vec![],
                tokens: vec![],
            },
            1,
        )
        .unwrap();
    let resp = p
        .client
        .check_response(McpResponse {
            service_names: vec!["payments".into()],
            method: "tools/call".into(),
            metadata_context: metadata(&[("agent", caller), ("wake_event_id", &event_id), ("session", "s1")]),
            mcp_response: br#"{"content":[{"type":"text","text":"paid $500"}]}"#.to_vec(),
        })
        .await
        .unwrap()
        .into_inner();
    match resp.result {
        Some(mcp_response_result::Result::Error(e)) => {
            assert_eq!(e.code, Code::PermissionDenied as i32);
            assert!(e.reason.contains("fleet-halt"), "{}", e.reason);
        }
        other => panic!("the result went back to the caller: {other:?}"),
    }
    // A call that reaches the hook after the halt isn't forwarded.
    let late = p.client.check_request(request()).await.unwrap().into_inner();
    assert!(
        matches!(late.result, Some(mcp_request_result::Result::Error(_))),
        "{late:?}"
    );
    assert_eq!(
        (Counters::get(&p.counters.cut), Counters::get(&p.counters.denied)),
        (1, 1)
    );

    let got: Vec<_> = drain(&mut spans).await.iter().map(attrs).collect();
    let call = got
        .iter()
        .find(|a| s(a, "wake.event.id") == event_id)
        .expect("the call is recorded");
    assert!(
        s(call, "wake.payload").contains("paid $500"),
        "with what the tool returned"
    );
    let denied: Vec<(&BTreeMap<String, Value>, serde_json::Value)> = got
        .iter()
        .filter(|a| s(a, "wake.event.kind") == "denied")
        .map(|a| (a, serde_json::from_str(&s(a, "wake.payload")).unwrap()))
        .collect();
    let (cut_event, cut) = denied
        .iter()
        .find(|(_, d)| d["cut"] == true)
        .expect("the cut is recorded");
    assert_eq!(cut["recorded_as"], event_id.as_str());
    assert_eq!(cut["method"], "tools/call");
    assert_eq!(
        cut_event["wake.parents"],
        Value::Strs(vec![event_id.clone()]),
        "it follows the call it cut"
    );
    assert!(
        denied
            .iter()
            .any(|(_, d)| d["cut"].is_null() && d["halt"] == "fleet-halt"),
        "and the late call's denial"
    );
    p.shutdown().await;
}

/// A stand-in collector that answers every request with this status, and counts them.
async fn refusing_collector(status: u16) -> (String, Arc<AtomicUsize>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let requests = Arc::new(AtomicUsize::new(0));
    let seen = requests.clone();
    tokio::spawn(async move {
        loop {
            let Ok((mut conn, _)) = listener.accept().await else {
                return;
            };
            let seen = seen.clone();
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut chunk = [0u8; 65536];
                loop {
                    let n = conn.read(&mut chunk).await.unwrap_or(0);
                    if n == 0 {
                        return;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                    let Some(end) = buf.windows(4).position(|w| w == b"\r\n\r\n") else {
                        continue;
                    };
                    let head = String::from_utf8_lossy(&buf[..end]).to_lowercase();
                    let len: usize = head
                        .lines()
                        .find_map(|l| l.strip_prefix("content-length:"))
                        .map(|v| v.trim().parse().unwrap())
                        .unwrap_or(0);
                    if buf.len() < end + 4 + len {
                        continue;
                    }
                    buf.drain(..end + 4 + len);
                    seen.fetch_add(1, Ordering::Relaxed);
                    let answer = format!("HTTP/1.1 {status} Refused\r\ncontent-length: 0\r\n\r\n");
                    let _ = conn.write_all(answer.as_bytes()).await;
                }
            });
        }
    });
    (url, requests)
}

#[tokio::test]
async fn a_request_the_collector_refuses_as_bad_isnt_sent_again() {
    for (status, attempts) in [(400, 1), (413, 1), (429, 3), (503, 3)] {
        let (url, requests) = refusing_collector(status).await;
        let mut p = proxy(&url, Duration::from_millis(50)).await;
        call(&mut p, "read_email", br#"{"content":[]}"#.to_vec()).await;
        let deadline = Instant::now() + Duration::from_secs(10);
        while Counters::get(&p.counters.export_dropped) == 0 && Instant::now() < deadline {
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        assert_eq!(
            Counters::get(&p.counters.export_dropped),
            1,
            "HTTP {status}: dropped and counted"
        );
        assert_eq!(requests.load(Ordering::Relaxed), attempts, "HTTP {status}: requests");
        p.shutdown().await;
    }
}
