//! esRAGPro MCP Gateway
//!
//! Thin protocol adapter calling esRAGPro domain APIs.
//! No embedded storage - all data comes from backend services.
//!
//! WP7: Added Streamable HTTP and OAuth support.
//! WP7A: Added management tools.
//! WP8: Added evaluation framework and tracing.
//! Phase 1-U: Added unpermissioned handlers for Agent Memory Plane.

// Public API functions are designed for external use
#![allow(dead_code)]

pub mod audit;
pub mod backend;
pub mod config;
pub mod error;
pub mod handlers;
pub mod handlers_unpermissioned;
pub mod oauth;
pub mod rate_limit;
pub mod schema;
pub mod server;
pub mod session_store;
pub mod size_limits;
pub mod whitelist;

#[cfg(feature = "http")]
pub mod http;

// WP8: Evaluation and Tracing
pub mod eval;
pub mod tracing_config;

pub use config::Config;
pub use server::McpServer;

#[cfg(test)]
mod server_test;
