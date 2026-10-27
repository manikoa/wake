//! wake-proxy: the processor agentgateway calls to record MCP and A2A calls as signed Wake
//! events, and to deny calls the brake has halted.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use clap::{Parser, Subcommand};
use wake_proxy::control;
use wake_proxy::export::Counters;
use wake_proxy::halts::Halts;
use wake_proxy::pipeline::{self, Options};
use wake_proxy::signer::{Signer, dev_identity_with_tls};

#[derive(Parser)]
#[command(version, about = "Records MCP calls through agentgateway as signed Wake events")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Serve the ExtMCP processor agentgateway calls.
    Serve {
        /// Where agentgateway reaches the processor. Only agentgateway may reach it.
        #[arg(long, default_value = "127.0.0.1:9001")]
        listen: String,
        /// The Wake collector's OTLP/HTTP endpoint.
        #[arg(long, env = "WAKE_ENDPOINT", default_value = "http://localhost:4318")]
        collector: String,
        /// The processor's P-256 private key, PKCS#8 PEM.
        #[arg(long)]
        key: PathBuf,
        /// The processor's X.509-SVID, PEM, with its SPIFFE ID as a URI SAN.
        #[arg(long)]
        cert: PathBuf,
        /// Seconds a request waits for its response before it's recorded without one.
        #[arg(long, default_value_t = 60)]
        response_timeout: u64,
        /// A CA certificate (PEM) to trust for the collector's TLS, besides the system's.
        #[arg(long)]
        collector_ca: Option<PathBuf>,
        /// Where the halts in force are kept, so a restart doesn't lift one.
        #[arg(long, default_value = "state")]
        state_dir: PathBuf,
        /// Where the brake reaches the control API (mTLS). Only the brake may reach it.
        /// Without it, halts can't be put or lifted.
        #[arg(long)]
        control_listen: Option<String>,
        /// The SPIRE trust bundle (PEM) the brake's SVID must chain to. Needed with
        /// --control-listen.
        #[arg(long)]
        trust_bundle: Option<PathBuf>,
        /// The one SPIFFE ID the control API serves.
        #[arg(long, default_value = "spiffe://wake.local/brake")]
        brake_id: String,
        /// Where the SDKs' memory adapters fetch the memory quarantines (read only).
        #[arg(long)]
        memory_listen: Option<String>,
    },
    /// Write a development identity: a stand-in trust bundle and a key and certificate
    /// signed by it. For local development only; SPIRE issues them in production.
    DevIdentity {
        #[arg(long, default_value = "spiffe://wake.local/proxy")]
        spiffe_id: String,
        #[arg(long, default_value = "keys")]
        out: PathBuf,
        /// Also issue a TLS server certificate for the collector, signed by the same CA,
        /// for these host names (repeatable). Written as collector-tls.pem and .key.
        #[arg(long = "tls-host")]
        tls_hosts: Vec<String>,
        /// Also issue the brake an SVID with this SPIFFE ID, for the control API. Written as
        /// brake.pem and brake.key.
        #[arg(long)]
        brake_id: Option<String>,
        /// Succeed without writing if the identity already exists, as on a restart.
        #[arg(long)]
        keep_existing: bool,
    },
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    match Cli::parse().command {
        Command::DevIdentity {
            spiffe_id,
            out,
            tls_hosts,
            brake_id,
            keep_existing,
        } => {
            let mut files = vec!["bundle.pem", "proxy.pem", "proxy.key"];
            if !tls_hosts.is_empty() {
                files.extend(["collector-tls.pem", "collector-tls.key"]);
            }
            if brake_id.is_some() {
                files.extend(["brake.pem", "brake.key"]);
            }
            if keep_existing && files.iter().all(|f| out.join(f).exists()) {
                return Ok(());
            }
            let id = dev_identity_with_tls(&spiffe_id, &tls_hosts, brake_id.as_deref())?;
            std::fs::create_dir_all(&out)?;
            std::fs::write(out.join("bundle.pem"), id.ca_pem)?;
            std::fs::write(out.join("proxy.pem"), id.cert_pem)?;
            write_private(&out.join("proxy.key"), &id.key_pem)?;
            if let Some((cert, key)) = id.tls {
                std::fs::write(out.join("collector-tls.pem"), cert)?;
                write_private(&out.join("collector-tls.key"), &key)?;
            }
            if let Some((cert, key)) = id.brake {
                std::fs::write(out.join("brake.pem"), cert)?;
                write_private(&out.join("brake.key"), &key)?;
            }
            println!("wrote a development identity for {spiffe_id} to {}", out.display());
        }
        Command::Serve {
            listen,
            collector,
            key,
            cert,
            response_timeout,
            collector_ca,
            state_dir,
            control_listen,
            trust_bundle,
            brake_id,
            memory_listen,
        } => {
            let key_pem = std::fs::read_to_string(key)?;
            let cert_pem = std::fs::read_to_string(cert)?;
            let signer = Signer::from_pem(&key_pem, &cert_pem)?;
            let halts = Arc::new(Halts::load(&state_dir));
            let in_force = halts.state();
            tracing::info!(
                version = in_force.version,
                halts = in_force.halts.len(),
                "halts in force"
            );
            tracing::info!(
                listen,
                collector,
                signer = signer.id(),
                "serving the ExtMCP and ext_proc processor"
            );
            let listener = tokio::net::TcpListener::bind(&listen).await?;
            let counters = Arc::new(Counters::default());
            let collector_ca = collector_ca.map(std::fs::read).transpose()?;
            let options = Options {
                collector,
                collector_ca,
                response_timeout: Duration::from_secs(response_timeout),
                halts: halts.clone(),
                ..Default::default()
            };
            if let Some(memory_listen) = memory_listen {
                let listener = tokio::net::TcpListener::bind(&memory_listen).await?;
                tracing::info!(memory_listen, "serving memory rules to memory adapters");
                tokio::spawn(wake_proxy::memory::serve(listener, halts.clone(), stopping()));
            }
            if let Some(control_listen) = control_listen {
                let bundle = trust_bundle.ok_or("--control-listen needs --trust-bundle")?;
                let tls = control::Tls {
                    cert_pem: cert_pem.clone(),
                    key_pem: key_pem.clone(),
                    bundle_pem: std::fs::read_to_string(bundle)?,
                    brake_id: brake_id.clone(),
                };
                control::tls_acceptor(&tls)?;
                let control_listener = tokio::net::TcpListener::bind(&control_listen).await?;
                tracing::info!(control_listen, brake_id, "serving the brake's control API");
                let halts = halts.clone();
                tokio::spawn(async move {
                    if let Err(e) = control::serve(control_listener, halts, tls, stopping()).await {
                        tracing::error!(error = %e, "control API stopped");
                    }
                });
            }
            let shutdown = stopping();
            let stats = counters.clone();
            tokio::spawn(async move {
                let mut tick = tokio::time::interval(Duration::from_secs(60));
                tick.tick().await;
                loop {
                    tick.tick().await;
                    tracing::info!(
                        sent = Counters::get(&stats.sent),
                        hooks_dropped = Counters::get(&stats.hooks_dropped),
                        export_dropped = Counters::get(&stats.export_dropped),
                        paired_by_id = Counters::get(&stats.paired_by_id),
                        paired_in_order = Counters::get(&stats.paired_in_order),
                        denied = Counters::get(&stats.denied),
                        "recording"
                    );
                }
            });
            pipeline::serve(listener, signer, options, counters.clone(), shutdown).await?;
            tracing::info!(
                sent = Counters::get(&counters.sent),
                hooks_dropped = Counters::get(&counters.hooks_dropped),
                export_dropped = Counters::get(&counters.export_dropped),
                paired_by_id = Counters::get(&counters.paired_by_id),
                paired_in_order = Counters::get(&counters.paired_in_order),
                denied = Counters::get(&counters.denied),
                "stopped"
            );
        }
    }
    Ok(())
}

/// Resolves on SIGINT or SIGTERM, which is how Docker and Kubernetes stop a container, so
/// events still queued go out before the process ends.
async fn stopping() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        let Ok(mut term) = signal(SignalKind::terminate()) else {
            let _ = tokio::signal::ctrl_c().await;
            return;
        };
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = term.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

fn write_private(path: &std::path::Path, contents: &str) -> std::io::Result<()> {
    use std::io::Write;
    #[cfg(unix)]
    use std::os::unix::fs::OpenOptionsExt;
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    options.mode(0o600);
    options.open(path)?.write_all(contents.as_bytes())
}
