//! wake-proxy: the processor agentgateway calls to record MCP calls as signed Wake events.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use clap::{Parser, Subcommand};
use wake_proxy::export::Counters;
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
            keep_existing,
        } => {
            let mut files = vec!["bundle.pem", "proxy.pem", "proxy.key"];
            if !tls_hosts.is_empty() {
                files.extend(["collector-tls.pem", "collector-tls.key"]);
            }
            if keep_existing && files.iter().all(|f| out.join(f).exists()) {
                return Ok(());
            }
            let id = dev_identity_with_tls(&spiffe_id, &tls_hosts)?;
            std::fs::create_dir_all(&out)?;
            std::fs::write(out.join("bundle.pem"), id.ca_pem)?;
            std::fs::write(out.join("proxy.pem"), id.cert_pem)?;
            write_private(&out.join("proxy.key"), &id.key_pem)?;
            if let Some((cert, key)) = id.tls {
                std::fs::write(out.join("collector-tls.pem"), cert)?;
                write_private(&out.join("collector-tls.key"), &key)?;
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
        } => {
            let signer = Signer::from_pem(&std::fs::read_to_string(key)?, &std::fs::read_to_string(cert)?)?;
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
                ..Default::default()
            };
            let shutdown = async {
                let _ = tokio::signal::ctrl_c().await;
            };
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
                "stopped"
            );
        }
    }
    Ok(())
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
