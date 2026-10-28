//! wake-proxy: the processor agentgateway calls to record MCP and A2A calls as signed Wake
//! events, and to deny calls the brake has halted.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use clap::{Parser, Subcommand};
use wake_proxy::agents::Agents;
use wake_proxy::control;
use wake_proxy::export::Counters;
use wake_proxy::halts::Halts;
use wake_proxy::listen::Hooks;
use wake_proxy::origins::Observed;
use wake_proxy::pipeline::{self, Options};
use wake_proxy::policy::{FlowPolicy, Policy};
use wake_proxy::signer::{DevExtras, Signer, dev_identity_with};

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
        /// Where agentgateway reaches the processor's hooks: a Unix socket in a directory
        /// only agentgateway shares (unix:/run/wake/proxy.sock), or a loopback address.
        /// Anything that can reach them can get events signed, so nothing else is accepted.
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
        /// Where the halts in force are kept, so a restart doesn't lift one: an absolute path,
        /// initialized once with `wake-proxy init`. Until it is, every call is denied.
        #[arg(long)]
        state_dir: PathBuf,
        /// A JSON file naming each agent and the identities it authenticates with, its SPIFFE
        /// IDs and JWT subjects: {"billing": ["spiffe://example.org/agent/billing"]}. With it,
        /// the brake halts agents by name, proxy events name agents the same way, and a caller
        /// it doesn't list is denied while any agent is halted. Without it, identities are
        /// names.
        #[arg(long)]
        agents: Option<PathBuf>,
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
        /// Where Prometheus scrapes the processor's metrics, at /metrics: calls seen,
        /// dropped and cut, events sent, and the halts in force.
        #[arg(long)]
        metrics_listen: Option<String>,
        /// A JSON flow policy: rules that deny a call when content from origins it names
        /// entered the caller through the proxy within its window (docs/proxy.md). Read
        /// again when it changes; one that can't be read or isn't valid denies every call.
        #[arg(long)]
        policy_file: Option<PathBuf>,
    },
    /// Initialize the halts state: a new, empty state with a new epoch. The deny lever
    /// denies every call until this has been done once, so a proxy that loses its state
    /// fails closed instead of starting over with no halts.
    Init {
        /// The directory `serve --state-dir` names.
        #[arg(long)]
        state_dir: PathBuf,
        /// Succeed without writing if the state already exists, as on a restart.
        #[arg(long)]
        keep_existing: bool,
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
        /// Also issue agentgateway a TLS server certificate for these host names (repeatable),
        /// to serve agents mTLS with. Written to --gateway-out as gateway.pem and .key, with
        /// the trust bundle, apart from the keys agentgateway mustn't hold.
        #[arg(long = "gateway-host")]
        gateway_hosts: Vec<String>,
        #[arg(long)]
        gateway_out: Option<PathBuf>,
        /// Also issue each of these agents an SVID, spiffe://<trust domain>/agent/<name>
        /// (repeatable, or comma separated), to call through agentgateway with. Written as
        /// agents/<name>.pem and .key.
        #[arg(long = "agent", value_delimiter = ',')]
        agents: Vec<String>,
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
        Command::Init {
            state_dir,
            keep_existing,
        } => {
            absolute(&state_dir)?;
            match Halts::init(&state_dir) {
                Ok(state) => println!(
                    "initialized the halts state in {}, epoch {}",
                    state_dir.display(),
                    state.epoch
                ),
                Err(e) if keep_existing && e.kind() == std::io::ErrorKind::AlreadyExists => {
                    println!("the halts state in {} already exists", state_dir.display())
                }
                Err(e) => return Err(format!("{}: {e}", state_dir.display()).into()),
            }
        }
        Command::DevIdentity {
            spiffe_id,
            out,
            tls_hosts,
            brake_id,
            gateway_hosts,
            gateway_out,
            agents,
            keep_existing,
        } => {
            let gateway_out = gateway_out.unwrap_or_else(|| out.join("gateway"));
            let agents: Vec<String> = agents.into_iter().filter(|a| !a.is_empty()).collect();
            let mut files: Vec<PathBuf> = ["bundle.pem", "proxy.pem", "proxy.key"].map(|f| out.join(f)).into();
            if !tls_hosts.is_empty() {
                files.extend(["collector-tls.pem", "collector-tls.key"].map(|f| out.join(f)));
            }
            if brake_id.is_some() {
                files.extend(["brake.pem", "brake.key"].map(|f| out.join(f)));
            }
            if !gateway_hosts.is_empty() {
                files.extend(["bundle.pem", "gateway.pem", "gateway.key"].map(|f| gateway_out.join(f)));
            }
            for agent in &agents {
                files.extend([".pem", ".key"].map(|ext| out.join("agents").join(format!("{agent}{ext}"))));
            }
            if keep_existing && files.iter().all(|f| f.exists()) {
                return Ok(());
            }
            let id = dev_identity_with(
                &spiffe_id,
                &DevExtras {
                    tls_hosts: &tls_hosts,
                    brake_id: brake_id.as_deref(),
                    gateway_hosts: &gateway_hosts,
                    agents: &agents,
                },
            )?;
            std::fs::create_dir_all(&out)?;
            std::fs::write(out.join("bundle.pem"), &id.ca_pem)?;
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
            if let Some((cert, key)) = id.gateway {
                std::fs::create_dir_all(&gateway_out)?;
                std::fs::write(gateway_out.join("bundle.pem"), &id.ca_pem)?;
                std::fs::write(gateway_out.join("gateway.pem"), cert)?;
                write_private(&gateway_out.join("gateway.key"), &key)?;
            }
            if !id.agents.is_empty() {
                std::fs::create_dir_all(out.join("agents"))?;
            }
            for (name, cert, key) in id.agents {
                std::fs::write(out.join("agents").join(format!("{name}.pem")), cert)?;
                write_private(&out.join("agents").join(format!("{name}.key")), &key)?;
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
            agents,
            control_listen,
            trust_bundle,
            brake_id,
            memory_listen,
            metrics_listen,
            policy_file,
        } => {
            let key_pem = std::fs::read_to_string(key)?;
            let cert_pem = std::fs::read_to_string(cert)?;
            let signer = Signer::from_pem(&key_pem, &cert_pem)?;
            absolute(&state_dir)?;
            let agents = match agents {
                Some(path) => {
                    Agents::from_json(&std::fs::read(&path)?).map_err(|e| format!("{}: {e}", path.display()))?
                }
                None => Agents::none(),
            };
            let halts = Arc::new(Halts::load(&state_dir).with_agents(Arc::new(agents)));
            let policy = Arc::new(match policy_file.as_deref() {
                Some(path) => Policy::from_file(path),
                None => Policy::fixed(FlowPolicy::empty()),
            });
            // What entered each agent before now is unknown: for one window, every agent
            // counts as having every origin.
            let observed = Arc::new(Observed::starting(
                wake_proxy::halts::now(),
                policy.current().window_seconds,
            ));
            tokio::spawn(policy.clone().watch(observed.clone(), stopping()));
            let in_force = halts.state();
            tracing::info!(
                version = in_force.version,
                epoch = in_force.epoch,
                halts = in_force.halts.len(),
                uninitialized = in_force.uninitialized,
                unreadable = in_force.unreadable.is_some(),
                "halts in force"
            );
            tracing::info!(
                listen,
                collector,
                signer = signer.id(),
                "serving the ExtMCP and ext_proc processor"
            );
            let hooks = Hooks::parse(&listen)?;
            let counters = Arc::new(Counters::default());
            let collector_ca = collector_ca.map(std::fs::read).transpose()?;
            let options = Options {
                collector,
                collector_ca,
                response_timeout: Duration::from_secs(response_timeout),
                halts: halts.clone(),
                policy,
                observed,
                ..Default::default()
            };
            if let Some(memory_listen) = memory_listen {
                let listener = tokio::net::TcpListener::bind(&memory_listen).await?;
                tracing::info!(memory_listen, "serving memory rules to memory adapters");
                tokio::spawn(wake_proxy::memory::serve(listener, halts.clone(), stopping()));
            }
            if let Some(metrics_listen) = metrics_listen {
                let listener = tokio::net::TcpListener::bind(&metrics_listen).await?;
                tracing::info!(metrics_listen, "serving metrics at /metrics");
                tokio::spawn(wake_proxy::metrics::serve(
                    listener,
                    counters.clone(),
                    halts.clone(),
                    stopping(),
                ));
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
                        truncated = Counters::get(&stats.truncated),
                        paired_by_id = Counters::get(&stats.paired_by_id),
                        paired_in_order = Counters::get(&stats.paired_in_order),
                        denied = Counters::get(&stats.denied),
                        cut = Counters::get(&stats.cut),
                        "recording"
                    );
                }
            });
            match hooks {
                Hooks::Tcp(addr) => {
                    let listener = tokio::net::TcpListener::bind(addr).await?;
                    pipeline::serve(listener, signer, options, counters.clone(), shutdown).await?;
                }
                Hooks::Unix(path) => {
                    let listener = wake_proxy::listen::bind_unix(&path)?;
                    pipeline::serve_unix(listener, signer, options, counters.clone(), shutdown).await?;
                }
            }
            tracing::info!(
                sent = Counters::get(&counters.sent),
                hooks_dropped = Counters::get(&counters.hooks_dropped),
                export_dropped = Counters::get(&counters.export_dropped),
                paired_by_id = Counters::get(&counters.paired_by_id),
                paired_in_order = Counters::get(&counters.paired_in_order),
                denied = Counters::get(&counters.denied),
                cut = Counters::get(&counters.cut),
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

/// A relative state directory would depend on where the proxy happens to start, and a
/// different one is a state that's lost.
fn absolute(dir: &std::path::Path) -> Result<(), String> {
    if dir.is_absolute() {
        Ok(())
    } else {
        Err(format!("--state-dir must be an absolute path, not {}", dir.display()))
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
