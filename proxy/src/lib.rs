//! Wake's MCP and A2A proxy: a processor that agentgateway calls to capture each call,
//! sign it as a Wake event and send it to the collector. See docs/proxy.md.

pub mod a2a;
pub mod canonical;
pub mod capture;
pub mod export;
pub mod extmcp;
pub mod extproc;
pub mod mcp;
pub mod pipeline;
pub mod signer;
