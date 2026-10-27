//! wake-proxy: the processor agentgateway calls to record MCP calls as signed Wake events.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use clap::{Parser, Subcommand};
use wake_proxy::export::Counters;
use wake_proxy::pipeline::{self, Options};
use wake_proxy::signer::{Signer, dev_identity};

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
    },
    /// Write a development identity: a stand-in trust bundle and a key and certificate
    /// signed by it. For local development only; SPIRE issues them in production.
    DevIdentity {
        #[arg(long, default_value = "spiffe://wake.local/proxy")]
        spiffe_id: String,
        #[arg(long, default_value = "keys")]
        out: PathBuf,
    },
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt().with_env_filter(tracing_subscriber::EnvFilter::from_default_env()).init();
    match Cli::parse().command {
        Command::DevIdentity { spiffe_id, out } => {
            let id = dev_identity(&spiffe_id)?;
            std::fs::create_dir_all(&out)?;
            std::fs::write(out.join("bundle.pem"), id.ca_pem)?;
            std::fs::write(out.join("proxy.pem"), id.cert_pem)?;
            write_private(&out.join("proxy.key"), &id.key_pem)?;
            println!("wrote {}/bundle.pem, proxy.pem and proxy.key for {spiffe_id}", out.display());
        }
        Command::Serve { listen, collector, key, cert, response_timeout } => {
            let signer = Signer::from_pem(&std::fs::read_to_string(key)?, &std::fs::read_to_string(cert)?)?;
            tracing::info!(listen, collector, signer = signer.id(), "serving the ExtMCP processor");
            let listener = tokio::net::TcpListener::bind(&listen).await?;
            let counters = Arc::new(Counters::default());
            let options = Options { collector, response_timeout: Duration::from_secs(response_timeout), ..Default::default() };
            let shutdown = async {
                let _ = tokio::signal::ctrl_c().await;
            };
            pipeline::serve(listener, signer, options, counters.clone(), shutdown).await?;
            tracing::info!(
                sent = Counters::get(&counters.sent),
                hooks_dropped = Counters::get(&counters.hooks_dropped),
                export_dropped = Counters::get(&counters.export_dropped),
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
