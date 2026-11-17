//! esRAGPro MCP Gateway
//!
//! Thin protocol adapter calling esRAGPro domain APIs via HTTP.
//! No embedded storage - all data comes from backend services.
//!
//! WP7: Added Streamable HTTP transport mode.

use clap::{Parser, Subcommand};
use tracing::info;
use tracing_subscriber::EnvFilter;

mod audit;
mod backend;
mod config;
mod error;
mod handlers;
mod handlers_unpermissioned;
mod oauth;
mod rate_limit;
mod schema;
mod server;
mod session_store;
mod size_limits;
mod whitelist;

#[cfg(feature = "http")]
mod http;

#[derive(Parser, Debug)]
#[command(author, version, about, long_about = None)]
struct Args {
    /// Log level
    #[arg(short, long, default_value = "info")]
    log_level: String,

    /// Transport mode
    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Debug, Subcommand)]
enum Commands {
    /// Run stdio transport (default)
    #[command(name = "stdio")]
    Stdio,

    /// Run HTTP transport (WP7)
    #[cfg(feature = "http")]
    #[command(name = "http")]
    Http {
        /// Listen address
        #[arg(short, long, default_value = "127.0.0.1:3000")]
        addr: String,
    },
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Parse arguments
    let args = Args::parse();

    // Initialize logging.
    //
    // P1-09 regression: tracing MUST write to stderr for stdio transport.
    // The stdio MCP transport uses stdout for JSON-RPC framing; writing log
    // lines to stdout corrupts the stream and triggers parse errors when
    // clients send `notifications/initialized` immediately after `initialize`
    // (the server's startup logs interleave with the initialize response).
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new(&args.log_level)),
        )
        .init();

    info!("esRAGPro MCP Gateway v{}", env!("CARGO_PKG_VERSION"));

    // Load configuration
    let config = config::Config::from_env();

    // Handle transport mode
    match args.command {
        None => {
            // Default: stdio or HTTP based on config
            if config.http_enabled {
                #[cfg(feature = "http")]
                {
                    run_http(config).await?
                }
                #[cfg(not(feature = "http"))]
                {
                    info!("HTTP mode requested but not compiled. Running stdio.");
                    run_stdio(config).await?
                }
            } else {
                run_stdio(config).await?
            }
        }
        Some(Commands::Stdio) => run_stdio(config).await?,
        #[cfg(feature = "http")]
        Some(Commands::Http { addr }) => {
            let mut config = config;
            config.http_enabled = true;
            config.http_addr = addr;
            run_http(config).await?
        }
    }

    info!("MCP Gateway shutdown");
    Ok(())
}

async fn run_stdio(config: config::Config) -> anyhow::Result<()> {
    info!("Running in stdio mode");
    let server = server::McpServer::new(config)?;
    server.run_stdio().await?;
    Ok(())
}

#[cfg(feature = "http")]
async fn run_http(config: config::Config) -> anyhow::Result<()> {
    info!(
        addr = %config.http_addr,
        oauth = config.oauth_configured(),
        "Running in HTTP mode"
    );
    http::run_http_server(config).await?;
    Ok(())
}
