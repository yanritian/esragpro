//! MCP Server Implementation
//!
//! Implements the MCP protocol using rmcp SDK.
//! Provides 6 read-only tools, Resources, and Prompts for WP6.
//! WP7A: Adds 4 management tools for operator/admin profiles.

use anyhow::Result;
use rmcp::{
    handler::server::wrapper::Parameters,
    model::{
        AnnotateAble, GetPromptRequestParams, GetPromptResult, ListPromptsResult,
        ListResourceTemplatesResult, ListResourcesResult, PaginatedRequestParams, Prompt,
        PromptArgument, PromptMessage, PromptMessageRole, RawResource, RawResourceTemplate,
        ReadResourceResult, ResourceContents,
    },
    serve_server,
    service::{RequestContext, RoleServer},
    transport::io::stdio,
    Json, ServerHandler,
};
use tracing::info;

use crate::backend::http::EnterpriseHttpBackend;
use crate::config::Config;
use crate::handlers;
use crate::schema::admin::{
    CancelIngestJobInput, CreateIngestJobInput, ManagementJobOutput, RequestDocumentDeletionInput,
    RequestProjectionRebuildInput,
};
use crate::schema::graph::{
    ExpandInput, ExpandOutput, FindPathsInput, FindPathsOutput, LookupEntityInput,
    LookupEntityOutput, VerifyClaimInput, VerifyClaimOutput,
};
use crate::schema::kb::{
    AnswerWithEvidenceInput, AnswerWithEvidenceOutput, SearchEvidenceInput, SearchEvidenceOutput,
};
use crate::schema::prompt::{
    AnswerWithCitationsInput, CompareEvidenceInput, TraceRequirementInput, VerifyClaimPromptInput,
};
use crate::schema::resource::{
    ServerCapabilitiesResource, ToolCapability, RESOURCE_TEMPLATES, SERVER_CAPABILITIES_URI,
};
use crate::schema::unpermissioned::{
    MemoryContextInput, MemoryContextPack, MemoryQueryResult, MemoryRememberInput,
    UnpermissionedMemoryQuery,
};
use crate::schema::world_memory::{
    WorldMemoryQueryInput, WorldMemoryQueryResponse, WorldMemoryRememberInput,
    WorldMemoryRememberResponse, WorldMemoryScenarioContextInput,
    WorldMemoryScenarioContextResponse, WorldMemoryStateAtInput,
};
use crate::schema::ToolEnvelope;

use crate::schema::PrincipalContext;

// Task-local storage for PrincipalContext in async context
// P0-Profile-02: Allows tool methods to access the principal from HTTP request
tokio::task_local! {
    pub(crate) static CURRENT_PRINCIPAL: Option<PrincipalContext>;
}

/// Helper to get current principal from task-local storage
pub fn get_current_principal() -> Option<PrincipalContext> {
    CURRENT_PRINCIPAL.try_with(|p| p.clone()).ok().flatten()
}

/// MCP Server wrapper
pub struct McpServer {
    config: Config,
    /// Backend client (pub(crate) for server_management.rs access)
    pub(crate) backend: EnterpriseHttpBackend,
}

impl McpServer {
    /// Create new MCP server
    /// P0-Auth-01: Use with_auth() when auth_token is configured
    pub fn new(config: Config) -> Result<Self> {
        let backend = if let Some(token) = &config.auth_token {
            EnterpriseHttpBackend::with_auth(
                config.backend_url.clone(),
                config.timeout_secs,
                token.clone(),
            )?
        } else {
            EnterpriseHttpBackend::new(config.backend_url.clone(), config.timeout_secs)?
        };

        Ok(Self { config, backend })
    }

    /// Run the MCP server using stdio transport
    pub async fn run_stdio(&self) -> Result<()> {
        info!("Starting MCP Gateway on stdio");
        info!("Backend URL: {}", self.config.backend_url);

        // Create stdio transport
        let (stdin, stdout) = stdio();

        // Create service instance
        let service = Self::new(self.config.clone())?;

        // Serve the server
        let running = serve_server(service, (stdin, stdout)).await?;

        // Wait for the server to complete
        running.waiting().await?;

        info!("MCP Gateway shutdown");
        Ok(())
    }
}

/// Tool definitions using rmcp attribute macros
/// All outputs use ToolEnvelope<T> for MCP V1 contract compliance (P0-3)
/// P0-Profile-02: Uses get_current_principal() to access PrincipalContext from task-local
#[rmcp::tool_router]
impl McpServer {
    /// Search for evidence chunks in specified knowledge bases
    #[rmcp::tool(
        name = "kb.search_evidence",
        description = "Search for evidence chunks in specified knowledge bases using hybrid retrieval (BM25 + vector)"
    )]
    async fn search_evidence(
        &self,
        Parameters(input): Parameters<SearchEvidenceInput>,
    ) -> Result<Json<ToolEnvelope<SearchEvidenceOutput>>, rmcp::model::ErrorData> {
        let principal = get_current_principal();
        let result = handlers::handle_search_evidence_with_principal(
            &self.backend,
            input,
            principal.as_ref(),
        )
        .await
        .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(result))
    }

    /// Generate an answer with evidence citations
    #[rmcp::tool(
        name = "kb.answer_with_evidence",
        description = "Generate an answer with evidence citations from specified knowledge bases"
    )]
    async fn answer_with_evidence(
        &self,
        Parameters(input): Parameters<AnswerWithEvidenceInput>,
    ) -> Result<Json<ToolEnvelope<AnswerWithEvidenceOutput>>, rmcp::model::ErrorData> {
        let principal = get_current_principal();
        let result = handlers::handle_answer_with_evidence_with_principal(
            &self.backend,
            input,
            principal.as_ref(),
        )
        .await
        .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(result))
    }

    /// Look up a canonical entity by name or identifier
    #[rmcp::tool(
        name = "graph.lookup_entity",
        description = "Look up a canonical entity by name or identifier in the knowledge graph"
    )]
    async fn lookup_entity(
        &self,
        Parameters(input): Parameters<LookupEntityInput>,
    ) -> Result<Json<ToolEnvelope<LookupEntityOutput>>, rmcp::model::ErrorData> {
        let principal = get_current_principal();
        let result =
            handlers::handle_lookup_entity_with_principal(&self.backend, input, principal.as_ref())
                .await
                .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(result))
    }

    /// Expand from an entity to find connected entities
    #[rmcp::tool(
        name = "graph.expand",
        description = "Expand from an entity to find connected entities via claims in the knowledge graph"
    )]
    async fn expand(
        &self,
        Parameters(input): Parameters<ExpandInput>,
    ) -> Result<Json<ToolEnvelope<ExpandOutput>>, rmcp::model::ErrorData> {
        let principal = get_current_principal();
        let result =
            handlers::handle_expand_with_principal(&self.backend, input, principal.as_ref())
                .await
                .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(result))
    }

    /// Find paths between two entities
    #[rmcp::tool(
        name = "graph.find_paths",
        description = "Find paths between two entities in the knowledge graph"
    )]
    async fn find_paths(
        &self,
        Parameters(input): Parameters<FindPathsInput>,
    ) -> Result<Json<ToolEnvelope<FindPathsOutput>>, rmcp::model::ErrorData> {
        let principal = get_current_principal();
        let result =
            handlers::handle_find_paths_with_principal(&self.backend, input, principal.as_ref())
                .await
                .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(result))
    }

    /// Verify a claim against evidence
    #[rmcp::tool(
        name = "graph.verify_claim",
        description = "Verify a claim against supporting and conflicting evidence in the knowledge graph"
    )]
    async fn verify_claim(
        &self,
        Parameters(input): Parameters<VerifyClaimInput>,
    ) -> Result<Json<ToolEnvelope<VerifyClaimOutput>>, rmcp::model::ErrorData> {
        let principal = get_current_principal();
        let result =
            handlers::handle_verify_claim_with_principal(&self.backend, input, principal.as_ref())
                .await
                .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(result))
    }

    // ========================================================================
    // WP7A: Management Tools
    // ========================================================================

    /// Create an ingest job from a finalized upload session
    /// Requires: ingest:create scope
    #[rmcp::tool(
        name = "admin.create_ingest_job",
        description = "Create an ingest job from a finalized upload session. Requires ingest:create scope."
    )]
    async fn create_ingest_job(
        &self,
        Parameters(input): Parameters<CreateIngestJobInput>,
    ) -> Result<Json<ToolEnvelope<ManagementJobOutput>>, rmcp::model::ErrorData> {
        let principal = get_current_principal();
        let result = handlers::handle_create_ingest_job_with_principal(
            &self.backend,
            input,
            principal.as_ref(),
        )
        .await
        .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(result))
    }

    /// Cancel a running ingest job
    /// Requires: ingest:cancel scope
    #[rmcp::tool(
        name = "admin.cancel_ingest_job",
        description = "Cancel a running ingest job. Requires ingest:cancel scope."
    )]
    async fn cancel_ingest_job(
        &self,
        Parameters(input): Parameters<CancelIngestJobInput>,
    ) -> Result<Json<ToolEnvelope<ManagementJobOutput>>, rmcp::model::ErrorData> {
        let principal = get_current_principal();
        let result = handlers::handle_cancel_ingest_job_with_principal(
            &self.backend,
            input,
            principal.as_ref(),
        )
        .await
        .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(result))
    }

    /// Request a projection rebuild (requires approval)
    /// Requires: projection:rebuild scope
    #[rmcp::tool(
        name = "admin.request_projection_rebuild",
        description = "Request a projection rebuild. Requires approval. Requires projection:rebuild scope."
    )]
    async fn request_projection_rebuild(
        &self,
        Parameters(input): Parameters<RequestProjectionRebuildInput>,
    ) -> Result<Json<ToolEnvelope<ManagementJobOutput>>, rmcp::model::ErrorData> {
        let principal = get_current_principal();
        let result = handlers::handle_request_projection_rebuild_with_principal(
            &self.backend,
            input,
            principal.as_ref(),
        )
        .await
        .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(result))
    }

    /// Request document deletion (requires approval)
    /// Requires: document:delete:request scope
    #[rmcp::tool(
        name = "admin.request_document_deletion",
        description = "Request document deletion. Requires approval. Requires document:delete:request scope."
    )]
    async fn request_document_deletion(
        &self,
        Parameters(input): Parameters<RequestDocumentDeletionInput>,
    ) -> Result<Json<ToolEnvelope<ManagementJobOutput>>, rmcp::model::ErrorData> {
        let principal = get_current_principal();
        let result = handlers::handle_request_document_deletion_with_principal(
            &self.backend,
            input,
            principal.as_ref(),
        )
        .await
        .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(result))
    }

    // ========================================================================
    // Phase 1-U: Unpermissioned Memory Tools (Agent Memory Plane)
    // ========================================================================
    // These tools do NOT require PrincipalContext, scope, ACL, or OAuth.
    // Data dimensions (project_id, session_id, agent_id, knowledge_base_id) are
    // for query scoping - NOT for permission checking.
    //
    // Zero-hallucination guarantee: no evidence → return refused status.
    // Never fabricates or guesses results.

    /// Query memory items without authorization requirements
    ///
    /// This is the core memory retrieval tool for the Agent Memory Plane.
    /// It does NOT require PrincipalContext - data dimensions are for scoping only.
    ///
    /// Zero-hallucination guarantee:
    /// - If no evidence is found, returns status: Refused
    /// - Never fabricates or guesses results
    ///
    /// P1-09: Production path calls FastAPI POST /memory-plane/query via HTTP.
    #[rmcp::tool(
        name = "memory.query",
        description = "Query memory items. No authorization required - data dimensions (project/session/agent/kb) are for organization, NOT permissions. Returns refused status if insufficient evidence."
    )]
    async fn memory_query(
        &self,
        Parameters(input): Parameters<UnpermissionedMemoryQuery>,
    ) -> Result<Json<ToolEnvelope<MemoryQueryResult>>, rmcp::model::ErrorData> {
        let request_id = crate::schema::new_request_id();
        let result = self
            .backend
            .memory_query(input)
            .await
            .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        // Wrap the FastAPI response in a ToolEnvelope at this layer so the
        // HTTP adapter remains envelope-agnostic.
        Ok(Json(ToolEnvelope::ok(request_id, result)))
    }

    /// Store a memory item with evidence
    ///
    /// Stores a new memory item with evidence linkage for zero-hallucination.
    /// No authorization required - dimensions are for organization.
    ///
    /// P1-09: Production path calls FastAPI POST /memory-plane/remember via
    /// HTTP. The FastAPI route commits memory + evidence + outbox atomically.
    #[rmcp::tool(
        name = "memory.remember",
        description = "Store a memory item with evidence linkage. No authorization required - data dimensions are for organization, NOT permissions. Evidence refs provide zero-hallucination traceability."
    )]
    async fn memory_remember(
        &self,
        Parameters(input): Parameters<MemoryRememberInput>,
    ) -> Result<
        Json<ToolEnvelope<crate::schema::unpermissioned::MemoryRememberResponse>>,
        rmcp::model::ErrorData,
    > {
        let request_id = crate::schema::new_request_id();
        let result = self
            .backend
            .memory_remember(input)
            .await
            .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(ToolEnvelope::ok(request_id, result)))
    }

    /// Create a memory context pack for LLM
    ///
    /// Creates an LLM-ready context pack from memory items.
    /// No authorization required - dimensions are for organization.
    ///
    /// P1-09: Production path calls FastAPI POST /memory-plane/context via
    /// HTTP. Context creation is a pure packaging operation — no LLM call.
    #[rmcp::tool(
        name = "memory.create_context",
        description = "Create a memory context pack for LLM consumption. No authorization required - data dimensions are for organization, NOT permissions."
    )]
    async fn memory_create_context(
        &self,
        Parameters(input): Parameters<MemoryContextInput>,
    ) -> Result<Json<ToolEnvelope<MemoryContextPack>>, rmcp::model::ErrorData> {
        let request_id = crate::schema::new_request_id();
        let result = self
            .backend
            .memory_create_context(input)
            .await
            .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(ToolEnvelope::ok(request_id, result)))
    }

    // ========================================================================
    // P4-W: World-Memory Tools (cross-process typed contract)
    // ========================================================================
    // These tools accept P7-B envelopes (validated by the backend's
    // validate_world_memory_envelope) and return typed WorldContextPack items.
    // No authorization required - data dimensions are for organization only.
    //
    // Evidence constraint: when require_evidence is true and no evidence-backed
    // items are found, the response status is "refused".

    /// Store a P7-B world-memory envelope
    ///
    /// Accepts a typed world-memory envelope (P7-B format) and stores it
    /// for cross-process retrieval. The backend validates the envelope using
    /// the P7-B serializer/replay invariants.
    ///
    /// No authorization required.
    #[rmcp::tool(
        name = "world_memory.remember",
        description = "Store a P7-B world-memory envelope. Accepts typed world-memory objects (observation, state, hypothesis, fact) in the P7-B envelope format. No authorization required. Validates via P7-B serializer invariants."
    )]
    async fn world_memory_remember(
        &self,
        Parameters(input): Parameters<WorldMemoryRememberInput>,
    ) -> Result<Json<ToolEnvelope<WorldMemoryRememberResponse>>, rmcp::model::ErrorData> {
        let request_id = crate::schema::new_request_id();
        let result = self
            .backend
            .world_memory_remember(input)
            .await
            .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(ToolEnvelope::ok(request_id, result)))
    }

    /// Query world-memory items
    ///
    /// Queries stored world-memory envelopes and returns typed context items
    /// plus raw P7-B envelopes. Fail-closed: when `require_evidence` is true
    /// and no evidence-backed items are found, returns status "refused".
    ///
    /// No authorization required.
    #[rmcp::tool(
        name = "world_memory.query",
        description = "Query world-memory items with PostgreSQL-authoritative rehydration and observable candidate-source traces. A nonblank query plus event_time_lte uses immutable historical event candidates only when the authoritative corpus is complete; otherwise FastAPI returns a refused response with no items or envelopes. Returns typed context items with evidence IDs and raw P7-B envelopes. Set require_evidence=true for evidence fail-closed behavior. No authorization required."
    )]
    async fn world_memory_query(
        &self,
        Parameters(input): Parameters<WorldMemoryQueryInput>,
    ) -> Result<Json<ToolEnvelope<WorldMemoryQueryResponse>>, rmcp::model::ErrorData> {
        let request_id = crate::schema::new_request_id();
        let result = self
            .backend
            .world_memory_query(input)
            .await
            .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(ToolEnvelope::ok(request_id, result)))
    }

    #[rmcp::tool(
        name = "world_memory.state_at",
        description = "Resolve one aggregate's authoritative WorldState at an explicit valid-time instant using half-open validity intervals. Returns typed context, the exact authority envelope, or a fail-closed no_match/refused response for no state, ambiguity, unavailable authority, or insufficient evidence. This is read-only and never plans or issues actions. No authorization required."
    )]
    async fn world_memory_state_at(
        &self,
        Parameters(input): Parameters<WorldMemoryStateAtInput>,
    ) -> Result<Json<ToolEnvelope<WorldMemoryQueryResponse>>, rmcp::model::ErrorData> {
        let request_id = crate::schema::new_request_id();
        let result = self
            .backend
            .world_memory_state_at(input)
            .await
            .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(ToolEnvelope::ok(request_id, result)))
    }

    /// Create a passive, evidence-constrained scenario context.
    ///
    /// The scenario label identifies only a downstream consumer class. This
    /// tool delegates to the FastAPI world-memory plane and never predicts,
    /// plans, controls, or emits actions/safety decisions.
    ///
    /// No authorization required.
    #[rmcp::tool(
        name = "world_memory.scenario_context",
        description = "Return read-only, evidence-constrained world-memory context for rescue, embodied, or autonomous-driving consumers. Preserves typed items, authority envelopes, refusal status, candidate traces, and latency. It does not plan, predict, control, or issue actions. No authorization required."
    )]
    async fn world_memory_scenario_context(
        &self,
        Parameters(input): Parameters<WorldMemoryScenarioContextInput>,
    ) -> Result<Json<ToolEnvelope<WorldMemoryScenarioContextResponse>>, rmcp::model::ErrorData>
    {
        let request_id = crate::schema::new_request_id();
        let result = self
            .backend
            .world_memory_scenario_context(input)
            .await
            .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;
        Ok(Json(ToolEnvelope::ok(request_id, result)))
    }
}

/// Prompt definitions using rmcp attribute macros
#[rmcp::prompt_router]
impl McpServer {
    /// Generate an answer with citations
    #[rmcp::prompt(
        name = "answer-with-citations",
        description = "Generate a comprehensive answer with evidence citations"
    )]
    async fn answer_with_citations(
        &self,
        Parameters(input): Parameters<AnswerWithCitationsInput>,
    ) -> Result<Vec<PromptMessage>, rmcp::model::ErrorData> {
        let kb_list: String = input
            .knowledge_base_ids
            .iter()
            .map(|id| id.to_string())
            .collect::<Vec<_>>()
            .join(", ");

        let lang = input.language.as_deref().unwrap_or("the user's language");
        let style = input
            .citation_style
            .as_ref()
            .map(|s| format!("{:?}", s).to_lowercase())
            .unwrap_or_else(|| "inline".to_string());

        Ok(vec![
            PromptMessage::new_text(
                PromptMessageRole::User,
                format!(
                    "Answer the following question based on the knowledge bases [{}]:\n\n{}\n\n\
                     Provide citations using {} style. Respond in {}.\n\
                     Use kb.search_evidence to find relevant evidence, then synthesize an answer with proper citations.",
                    kb_list, input.question, style, lang
                )
            )
        ])
    }

    /// Compare evidence from two sources
    #[rmcp::prompt(
        name = "compare-evidence",
        description = "Compare evidence from two references"
    )]
    async fn compare_evidence(
        &self,
        Parameters(input): Parameters<CompareEvidenceInput>,
    ) -> Result<Vec<PromptMessage>, rmcp::model::ErrorData> {
        let left_desc = match (&input.left_ref.document_id, &input.left_ref.entity_id) {
            (Some(doc_id), None) => format!("document {}", doc_id),
            (None, Some(entity_id)) => format!("entity {}", entity_id),
            _ => "unknown reference".to_string(),
        };

        let right_desc = match (&input.right_ref.document_id, &input.right_ref.entity_id) {
            (Some(doc_id), None) => format!("document {}", doc_id),
            (None, Some(entity_id)) => format!("entity {}", entity_id),
            _ => "unknown reference".to_string(),
        };

        Ok(vec![PromptMessage::new_text(
            PromptMessageRole::User,
            format!(
                "Compare evidence about '{}' from:\n\
                     - {}: {}\n\
                     - {}: {}\n\n\
                     Analyze agreements, conflicts, and complementary information.",
                input.topic, input.left_ref.label, left_desc, input.right_ref.label, right_desc
            ),
        )])
    }

    /// Trace a requirement through evidence
    #[rmcp::prompt(
        name = "trace-requirement",
        description = "Trace a requirement through evidence and claims"
    )]
    async fn trace_requirement(
        &self,
        Parameters(input): Parameters<TraceRequirementInput>,
    ) -> Result<Vec<PromptMessage>, rmcp::model::ErrorData> {
        Ok(vec![PromptMessage::new_text(
            PromptMessageRole::User,
            format!(
                "Trace the requirement: '{}'\n\n\
                     Find:\n\
                     1. Original evidence documents\n\
                     2. Related claims in the knowledge graph\n\
                     3. Implementation artifacts (if any)\n\n\
                     Use graph.find_paths to explore relationships.",
                input.requirement
            ),
        )])
    }

    /// Verify a claim
    #[rmcp::prompt(name = "verify-claim", description = "Verify a claim against evidence")]
    async fn verify_claim_prompt(
        &self,
        Parameters(input): Parameters<VerifyClaimPromptInput>,
    ) -> Result<Vec<PromptMessage>, rmcp::model::ErrorData> {
        let polarity = input
            .claim
            .polarity
            .as_ref()
            .map(|p| format!("{:?}", p).to_lowercase())
            .unwrap_or_else(|| "positive".to_string());

        Ok(vec![PromptMessage::new_text(
            PromptMessageRole::User,
            format!(
                "Verify the following claim ({} polarity):\n\n\
                     Subject: {}\n\
                     Predicate: {}\n\
                     Object: {}\n\n\
                     Find both supporting and conflicting evidence.\n\
                     Use graph.verify_claim after gathering evidence.",
                polarity, input.claim.subject, input.claim.predicate, input.claim.object
            ),
        )])
    }
}

/// Implement ServerHandler trait with Resources support
#[rmcp::tool_handler]
impl ServerHandler for McpServer {
    /// List static resources
    async fn list_resources(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListResourcesResult, rmcp::model::ErrorData> {
        let resource = RawResource::new(SERVER_CAPABILITIES_URI, "Server Capabilities")
            .with_description("esRAGPro MCP Gateway capabilities")
            .with_mime_type("application/json")
            .no_annotation();

        Ok(ListResourcesResult::with_all_items(vec![resource]))
    }

    /// List resource templates
    async fn list_resource_templates(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListResourceTemplatesResult, rmcp::model::ErrorData> {
        let templates: Vec<_> = RESOURCE_TEMPLATES
            .iter()
            .map(|uri| {
                RawResourceTemplate::new(*uri, extract_template_name(uri))
                    .with_description(extract_template_description(uri))
                    .with_mime_type("application/json")
                    .no_annotation()
            })
            .collect();

        Ok(ListResourceTemplatesResult::with_all_items(templates))
    }

    /// Read a resource
    async fn read_resource(
        &self,
        request: rmcp::model::ReadResourceRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> Result<ReadResourceResult, rmcp::model::ErrorData> {
        let uri = &request.uri;

        if uri == SERVER_CAPABILITIES_URI {
            let caps = ServerCapabilitiesResource {
                schema_version: "1.0.0".to_string(),
                server_name: env!("CARGO_PKG_NAME").to_string(),
                server_version: env!("CARGO_PKG_VERSION").to_string(),
                protocol_version: "2025-11-25".to_string(),
                tools: vec![
                    // Phase 1-U: Unpermissioned Memory Tools (Agent Memory Plane)
                    // NO scopes - these tools do not require authorization
                    ToolCapability {
                        name: "memory.query".to_string(),
                        scope: String::new(), // No scope required
                        description: "Query memory items without authorization".to_string(),
                    },
                    ToolCapability {
                        name: "memory.remember".to_string(),
                        scope: String::new(), // No scope required
                        description: "Store memory item with evidence linkage".to_string(),
                    },
                    ToolCapability {
                        name: "memory.create_context".to_string(),
                        scope: String::new(), // No scope required
                        description: "Create LLM-ready memory context pack".to_string(),
                    },
                    // P4-W: World-Memory Tools (cross-process typed contract)
                    ToolCapability {
                        name: "world_memory.remember".to_string(),
                        scope: String::new(),
                        description: "Store P7-B world-memory envelope".to_string(),
                    },
                    ToolCapability {
                        name: "world_memory.query".to_string(),
                        scope: String::new(),
                        description: "Query world-memory items with an optional textual query, PostgreSQL-authoritative rehydration, and candidate-source traces".to_string(),
                    },
                    ToolCapability {
                        name: "world_memory.scenario_context".to_string(),
                        scope: String::new(),
                        description: "Create passive, read-only, evidence-constrained world-memory context for a named scenario consumer".to_string(),
                    },
                    // Legacy authenticated tools (retain existing scopes)
                    ToolCapability {
                        name: "kb.search_evidence".to_string(),
                        scope: "knowledge:read".to_string(),
                        description: "Search evidence in knowledge bases".to_string(),
                    },
                    ToolCapability {
                        name: "kb.answer_with_evidence".to_string(),
                        scope: "knowledge:answer".to_string(),
                        description: "Answer with evidence citations".to_string(),
                    },
                    ToolCapability {
                        name: "graph.lookup_entity".to_string(),
                        scope: "graph:read".to_string(),
                        description: "Look up entity in knowledge graph".to_string(),
                    },
                    ToolCapability {
                        name: "graph.expand".to_string(),
                        scope: "graph:read".to_string(),
                        description: "Expand entity neighbors".to_string(),
                    },
                    ToolCapability {
                        name: "graph.find_paths".to_string(),
                        scope: "graph:read".to_string(),
                        description: "Find paths between entities".to_string(),
                    },
                    ToolCapability {
                        name: "graph.verify_claim".to_string(),
                        scope: "graph:verify".to_string(),
                        description: "Verify claim against evidence".to_string(),
                    },
                ],
                scopes: vec![
                    "knowledge:read".to_string(),
                    "knowledge:answer".to_string(),
                    "graph:read".to_string(),
                    "graph:verify".to_string(),
                ],
            };

            let json_text = serde_json::to_string(&caps)
                .map_err(|e| rmcp::model::ErrorData::internal_error(e.to_string(), None))?;

            return Ok(ReadResourceResult::new(vec![ResourceContents::text(
                json_text,
                uri.clone(),
            )
            .with_mime_type("application/json")]));
        }

        // Return not found for unimplemented resources
        Err(rmcp::model::ErrorData::resource_not_found(
            format!("Resource not found: {}", uri),
            None,
        ))
    }

    /// Get prompt by name
    async fn get_prompt(
        &self,
        request: GetPromptRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> Result<GetPromptResult, rmcp::model::ErrorData> {
        match request.name.as_str() {
            "answer-with-citations" => Ok(GetPromptResult::new(vec![PromptMessage::new_text(
                PromptMessageRole::User,
                "Answer the question using kb.search_evidence to find relevant evidence, \
                         then synthesize an answer with proper citations. \
                         Cite sources using [Source: document title] format.",
            )])
            .with_description("Generate a comprehensive answer with evidence citations")),
            "compare-evidence" => Ok(GetPromptResult::new(vec![PromptMessage::new_text(
                PromptMessageRole::User,
                "Compare the evidence from the two provided references. \
                         Analyze agreements, conflicts, and complementary information. \
                         Use graph.find_paths to explore relationships.",
            )])
            .with_description("Compare evidence from two references")),
            "trace-requirement" => Ok(GetPromptResult::new(vec![PromptMessage::new_text(
                PromptMessageRole::User,
                "Trace the requirement through:\n\
                         1. Original evidence documents (use kb.search_evidence)\n\
                         2. Related claims in the knowledge graph (use graph.find_paths)\n\
                         3. Implementation artifacts (if any)",
            )])
            .with_description("Trace a requirement through evidence and claims")),
            "verify-claim" => Ok(GetPromptResult::new(vec![PromptMessage::new_text(
                PromptMessageRole::User,
                "Verify the claim by:\n\
                         1. Finding supporting evidence using kb.search_evidence\n\
                         2. Finding potentially conflicting evidence\n\
                         3. Use graph.verify_claim for structured verification\n\
                         Note: Absence of refuting evidence does not constitute proof.",
            )])
            .with_description("Verify a claim against evidence")),
            _ => Err(rmcp::model::ErrorData::invalid_params(
                format!("Unknown prompt: {}", request.name),
                None,
            )),
        }
    }

    /// List available prompts
    async fn list_prompts(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListPromptsResult, rmcp::model::ErrorData> {
        Ok(ListPromptsResult::with_all_items(vec![
            Prompt::new(
                "answer-with-citations",
                Some("Generate a comprehensive answer with evidence citations"),
                Some(vec![
                    PromptArgument::new("question")
                        .with_description("Question to answer")
                        .with_required(true),
                    PromptArgument::new("knowledge_base_ids")
                        .with_description("Target knowledge base IDs")
                        .with_required(true),
                ]),
            ),
            Prompt::new(
                "compare-evidence",
                Some("Compare evidence from two references"),
                Some(vec![
                    PromptArgument::new("topic")
                        .with_description("Topic to compare")
                        .with_required(true),
                    PromptArgument::new("knowledge_base_ids")
                        .with_description("Target knowledge base IDs")
                        .with_required(true),
                ]),
            ),
            Prompt::new(
                "trace-requirement",
                Some("Trace a requirement through evidence and claims"),
                Some(vec![
                    PromptArgument::new("requirement")
                        .with_description("Requirement to trace")
                        .with_required(true),
                    PromptArgument::new("knowledge_base_ids")
                        .with_description("Target knowledge base IDs")
                        .with_required(true),
                ]),
            ),
            Prompt::new(
                "verify-claim",
                Some("Verify a claim against evidence"),
                Some(vec![
                    PromptArgument::new("knowledge_base_id")
                        .with_description("Target knowledge base ID")
                        .with_required(true),
                    PromptArgument::new("claim")
                        .with_description("Claim to verify (subject, predicate, object)")
                        .with_required(true),
                ]),
            ),
        ]))
    }
}

/// Extract template name from URI
fn extract_template_name(uri: &str) -> String {
    if uri.contains("/documents/") {
        "Document Version"
    } else if uri.contains("/evidence/") {
        "Evidence"
    } else if uri.contains("/entities/") {
        "Entity"
    } else if uri.contains("/claims/") {
        "Claim"
    } else if uri.contains("/ontology/") {
        "Ontology"
    } else if uri.contains("/jobs/") {
        "Job"
    } else if uri.contains("/catalog") {
        "Catalog"
    } else if uri.contains("/stats") {
        "Stats"
    } else {
        "Resource"
    }
    .to_string()
}

/// Extract template description from URI
fn extract_template_description(uri: &str) -> String {
    if uri.contains("/documents/") {
        "Access a specific document version"
    } else if uri.contains("/evidence/") {
        "Access evidence by ID"
    } else if uri.contains("/entities/") {
        "Access entity information"
    } else if uri.contains("/claims/") {
        "Access claim details"
    } else if uri.contains("/ontology/") {
        "Access ontology schema"
    } else if uri.contains("/jobs/") {
        "Access job status"
    } else if uri.contains("/catalog") {
        "Access knowledge base catalog"
    } else if uri.contains("/stats") {
        "Access knowledge base statistics"
    } else {
        "Access resource"
    }
    .to_string()
}
