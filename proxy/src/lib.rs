//! Wake's MCP and A2A proxy: a processor that agentgateway calls to capture each call,
//! sign it as a Wake event and send it to the collector, and to deny calls the brake has
//! halted. See docs/proxy.md.

pub mod a2a;
pub mod agents;
pub mod authz;
pub mod budget;
pub mod canonical;
pub mod capture;
pub mod charters;
pub mod control;
pub mod export;
pub mod extmcp;
pub mod extproc;
pub mod halts;
pub mod listen;
pub mod mcp;
pub mod memory;
pub mod metrics;
pub mod models;
pub mod origins;
pub mod pipeline;
pub mod policy;
pub mod redact;
pub mod signer;
pub mod svid;
pub mod token;
