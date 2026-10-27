// Generates the ExtMCP gRPC service from agentgateway's proto, vendored from v1.6.0.
fn main() -> Result<(), Box<dyn std::error::Error>> {
    tonic_prost_build::configure().compile_protos(&["proto/ext_mcp.proto"], &["proto"])?;
    Ok(())
}
