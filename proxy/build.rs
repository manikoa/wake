// Generates the gRPC services agentgateway calls: ExtMCP for MCP, Envoy's ext_proc for A2A,
// and Envoy's ext_authz for the deny lever. The protos are vendored from agentgateway v1.6.0.
fn main() -> Result<(), Box<dyn std::error::Error>> {
    tonic_prost_build::configure().compile_protos(
        &["proto/ext_mcp.proto", "proto/ext_proc.proto", "proto/ext_authz.proto"],
        &["proto"],
    )?;
    Ok(())
}
