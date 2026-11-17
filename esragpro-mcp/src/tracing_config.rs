//! OpenTelemetry Tracing Configuration (WP8)
//!
//! Provides tracing setup for MCP Gateway.
//! Traces MCP requests through backend services.

use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt, EnvFilter};

// Note: In production, these would be actual OpenTelemetry types
// For now, we provide a simplified interface

/// Tracing configuration
#[derive(Debug, Clone)]
pub struct TracingConfig {
    /// Service name for traces
    pub service_name: String,

    /// OTLP endpoint (e.g., "http://localhost:4317")
    pub otlp_endpoint: Option<String>,

    /// Enable console output
    pub console_output: bool,

    /// Sampling ratio (0.0 to 1.0)
    pub sampling_ratio: f64,
}

impl Default for TracingConfig {
    fn default() -> Self {
        Self {
            service_name: "esragpro-mcp".to_string(),
            otlp_endpoint: None,
            console_output: true,
            sampling_ratio: 1.0,
        }
    }
}

/// Initialize tracing for the service
pub fn init_tracing(config: &TracingConfig) -> anyhow::Result<()> {
    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));

    // Console output layer
    let fmt_layer = tracing_subscriber::fmt::layer()
        .with_target(true)
        .with_thread_ids(true);

    // Initialize subscriber
    tracing_subscriber::registry()
        .with(env_filter)
        .with(fmt_layer)
        .init();

    // OpenTelemetry configuration (when endpoint is provided)
    // In production, this would initialize OTLP exporter:
    // if let Some(endpoint) = &config.otlp_endpoint {
    //     let tracer = opentelemetry_otlp::new_pipeline()
    //         .tracing()
    //         .with_exporter(opentelemetry_otlp::new_exporter().tonic())
    //         .install_batch(opentelemetry::runtime::Tokio)?;
    // }

    tracing::info!(
        service = %config.service_name,
        otlp_endpoint = ?config.otlp_endpoint,
        sampling_ratio = config.sampling_ratio,
        "Tracing initialized"
    );

    Ok(())
}

/// Span attributes for MCP requests
pub mod span_attributes {
    pub const MCP_REQUEST_ID: &str = "mcp.request_id";
    pub const MCP_TOOL_NAME: &str = "mcp.tool_name";
    pub const MCP_TENANT_ID: &str = "mcp.tenant_id";
    pub const MCP_KB_ID: &str = "mcp.knowledge_base_id";
    pub const MCP_STATUS: &str = "mcp.status";
    pub const MCP_ERROR: &str = "mcp.error";

    pub const BACKEND_SERVICE: &str = "backend.service";
    pub const BACKEND_OPERATION: &str = "backend.operation";
    pub const BACKEND_LATENCY_MS: &str = "backend.latency_ms";

    pub const ES_INDEX: &str = "elasticsearch.index";
    pub const ES_QUERY_TYPE: &str = "elasticsearch.query_type";

    pub const NEO4J_QUERY: &str = "neo4j.query_type";
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_tracing_config_default() {
        let config = TracingConfig::default();
        assert_eq!(config.service_name, "esragpro-mcp");
        assert!(config.console_output);
        assert!(config.otlp_endpoint.is_none());
    }

    #[test]
    fn test_span_attributes_constants() {
        assert_eq!(span_attributes::MCP_REQUEST_ID, "mcp.request_id");
        assert_eq!(span_attributes::MCP_TOOL_NAME, "mcp.tool_name");
    }
}
