//! Streamable HTTP Server (WP7)
//!
//! Implements MCP Streamable HTTP transport using rmcp SDK.
//! Provides OAuth protected resource endpoint and /mcp endpoint.
//!
//! CRITICAL FIX: OAuth middleware protects /mcp endpoint.

use std::sync::Arc;
use std::time::Duration;

use axum::{
    body::Body,
    extract::State,
    http::Request,
    http::{header, Method, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Json},
    routing::get,
    Router,
};
use tower_http::cors::CorsLayer;
use tower_http::limit::RequestBodyLimitLayer;
use tracing::{info, warn};

use rmcp::transport::streamable_http_server::{
    session::local::LocalSessionManager,
    tower::{StreamableHttpServerConfig, StreamableHttpService},
};

use crate::audit::{hash_for_audit, AuditEvent, AuditLogger, PolicyDecision, ResultCode};
use crate::config::Config;
use crate::oauth::{OAuthValidator, ProtectedResourceMetadata};
use crate::rate_limit::RateLimiter;
use crate::server::{McpServer, CURRENT_PRINCIPAL};

/// HTTP server state
pub struct HttpServerState {
    pub config: Config,
    pub oauth: Arc<OAuthValidator>,
    pub rate_limiter: Arc<RateLimiter>,
    pub audit: Arc<AuditLogger>,
}

/// Create Streamable HTTP service
pub fn create_http_service(
    config: Config,
) -> anyhow::Result<StreamableHttpService<McpServer, LocalSessionManager>> {
    let session_manager = Arc::new(LocalSessionManager::default());

    let server_config = StreamableHttpServerConfig::default()
        .with_stateful_mode(true)
        .with_sse_keep_alive(Some(Duration::from_secs(30)))
        .with_sse_retry(Some(Duration::from_secs(3)));

    // Configure allowed origins for DNS rebinding protection
    let server_config = if config.allowed_origins.is_empty() {
        server_config.disable_allowed_origins()
    } else {
        server_config.with_allowed_origins(config.allowed_origins.clone())
    };

    let service = StreamableHttpService::new(
        move || McpServer::new(config.clone()).map_err(|e| std::io::Error::other(e.to_string())),
        session_manager,
        server_config,
    );

    Ok(service)
}

/// Build axum router for MCP HTTP server
pub fn build_router(config: Config) -> anyhow::Result<Router> {
    // Create OAuth validator (required for HTTP mode)
    let oauth = OAuthValidator::new(crate::oauth::OAuthConfig {
        issuer: config.oauth_issuer.clone().unwrap_or_default(),
        audience: config.oauth_audience.clone().unwrap_or_default(),
        jwks_url: config.jwks_url.clone(),
        static_token: config.auth_token.clone(),
    });

    // Create rate limiter
    let rate_limiter = Arc::new(RateLimiter::new(crate::rate_limit::RateLimitConfig {
        rpm: config.rate_limit_rpm,
        concurrent: config.concurrent_limit,
    }));

    // Create audit logger
    let audit = Arc::new(AuditLogger::new());

    let state = Arc::new(HttpServerState {
        config: config.clone(),
        oauth: Arc::new(oauth),
        rate_limiter,
        audit,
    });

    // Create Streamable HTTP service (implements tower::Service)
    let http_service = create_http_service(config.clone())?;

    // Build /mcp sub-router with OAuth middleware
    let mcp_router = Router::new()
        .fallback_service(http_service)
        // CRITICAL FIX: Add OAuth middleware
        .layer(middleware::from_fn_with_state(
            state.clone(),
            oauth_middleware,
        ))
        .layer(RequestBodyLimitLayer::new(1024 * 1024));

    // Build main router
    let app = Router::new()
        // OAuth protected resource metadata (unauthenticated)
        .route("/.well-known/oauth-protected-resource", get(get_metadata))
        // MCP endpoint - protected by OAuth middleware
        .nest("/mcp", mcp_router)
        .with_state(state.clone());

    // CRITICAL FIX: Proper CORS configuration
    let app = if !config.allowed_origins.is_empty() {
        let origins: Vec<_> = config
            .allowed_origins
            .iter()
            .filter_map(|o| o.parse().ok())
            .collect();

        if !origins.is_empty() {
            app.layer(
                CorsLayer::new()
                    .allow_origin(origins)
                    .allow_methods([Method::GET, Method::POST, Method::DELETE])
                    .allow_headers([header::CONTENT_TYPE, header::AUTHORIZATION])
                    .allow_credentials(false),
            )
        } else {
            app
        }
    } else {
        app
    };

    Ok(app)
}

/// OAuth middleware for MCP endpoints
///
/// CRITICAL: Validates bearer token before allowing access
async fn oauth_middleware(
    State(state): State<Arc<HttpServerState>>,
    mut request: Request<Body>,
    next: Next,
) -> axum::response::Response {
    let start = std::time::Instant::now();

    // Extract Authorization header
    let auth_header = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|h| h.to_str().ok());

    let token = match auth_header {
        Some(h) if h.starts_with("Bearer ") => &h[7..],
        _ => {
            warn!("Missing or invalid Authorization header");
            return (
                StatusCode::UNAUTHORIZED,
                [("WWW-Authenticate", "Bearer error=\"invalid_request\"")],
            )
                .into_response();
        }
    };

    // Validate token
    let claims = match state.oauth.validate_token(token).await {
        Ok(c) => c,
        Err(e) => {
            warn!(error = %e, "Token validation failed");
            return (
                StatusCode::UNAUTHORIZED,
                [("WWW-Authenticate", "Bearer error=\"invalid_token\"")],
            )
                .into_response();
        }
    };

    // Rate limiting
    if let Err(retry_after) = state.rate_limiter.check(&claims.sub).await {
        warn!(
            subject = %claims.sub,
            retry_after = retry_after,
            "Rate limit exceeded"
        );
        return (
            StatusCode::TOO_MANY_REQUESTS,
            [("Retry-After", retry_after.to_string())],
        )
            .into_response();
    }

    // Record request start
    state.rate_limiter.start_request(&claims.sub).await;

    // Extract principal for downstream
    let principal = state.oauth.extract_principal(&claims);

    // P0-Profile-02: Use task-local to pass principal to rmcp tool handlers
    // The principal is stored in request extensions AND task-local for rmcp handlers
    request.extensions_mut().insert(principal.clone());

    // Process request within CURRENT_PRINCIPAL scope
    let response = CURRENT_PRINCIPAL
        .scope(Some(principal.clone()), async { next.run(request).await })
        .await;

    // Record completion
    state.rate_limiter.complete_request(&claims.sub).await;

    // Audit log
    let event = AuditEvent {
        timestamp: chrono::Utc::now(),
        request_id: uuid::Uuid::new_v4().to_string(),
        trace_id: claims.jti.clone().unwrap_or_default(),
        subject_hash: hash_for_audit(&claims.sub),
        tenant_hash: hash_for_audit(claims.tenant_id.as_deref().unwrap_or("unknown")),
        tool: "mcp".to_string(), // Would be extracted from request body
        kb_hashes: vec![],
        decision: if response.status().is_success() {
            PolicyDecision::Allowed
        } else {
            PolicyDecision::Denied
        },
        latency_ms: start.elapsed().as_secs_f64() * 1000.0,
        result: if response.status().is_success() {
            ResultCode::Success
        } else {
            ResultCode::Error
        },
        evidence_count: 0,
    };

    state.audit.log(event).await;

    response
}

/// Handle GET /.well-known/oauth-protected-resource
async fn get_metadata(State(state): State<Arc<HttpServerState>>) -> impl IntoResponse {
    let public_origin = state
        .config
        .public_origin
        .clone()
        .unwrap_or_else(|| format!("http://{}", state.config.http_addr));

    let issuer = state
        .config
        .oauth_issuer
        .clone()
        .unwrap_or_else(|| "https://auth.example.com".to_string());

    let metadata = ProtectedResourceMetadata::new(&public_origin, &issuer);

    (StatusCode::OK, Json(metadata))
}

/// Run HTTP server
pub async fn run_http_server(config: Config) -> anyhow::Result<()> {
    let addr = config.http_addr.clone();
    info!(
        addr = %addr,
        backend = %config.backend_url,
        oauth = config.oauth_configured(),
        "Starting MCP HTTP server"
    );

    let app = build_router(config)?;

    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!(addr = %addr, "Listening for HTTP connections");

    axum::serve(listener, app).await?;

    Ok(())
}
