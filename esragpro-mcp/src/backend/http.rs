//! Enterprise HTTP Backend
//!
//! Calls esRAGPro domain APIs over HTTP.
//! P0-1: Supports authentication via bearer token.
//! P0-2: Passes PrincipalContext for authorization.
//! P1-Security-06: URL encodes parameters to prevent injection.
//! P1-09: Adds memory-plane HTTP calls (unpermissioned).

use super::*;
use crate::schema::graph::ClaimInput;
use crate::schema::unpermissioned::{
    MemoryContextInput, MemoryContextPack, MemoryQueryResult, MemoryRememberInput,
    MemoryRememberResponse, UnpermissionedMemoryQuery,
};
use crate::schema::world_memory::{
    WorldMemoryQueryInput, WorldMemoryQueryResponse, WorldMemoryRememberInput,
    WorldMemoryRememberResponse, WorldMemoryScenarioContextInput,
    WorldMemoryScenarioContextResponse, WorldMemoryStateAtInput,
};
use crate::schema::PrincipalContext;
use async_trait::async_trait;
use reqwest::Client;
use std::time::Duration;
use tracing::{debug, instrument};

/// URL encode a string for safe inclusion in query parameters
fn url_encode(s: &str) -> String {
    urlencoding::encode(s).to_string()
}

/// HTTP backend calling esRAGPro domain APIs
pub struct EnterpriseHttpBackend {
    client: Client,
    base_url: String,
    auth_token: Option<String>,
}

impl EnterpriseHttpBackend {
    /// Create new HTTP backend without auth
    pub fn new(base_url: String, timeout_secs: u64) -> anyhow::Result<Self> {
        let client = Client::builder()
            .timeout(Duration::from_secs(timeout_secs))
            .build()?;

        Ok(Self {
            client,
            base_url,
            auth_token: None,
        })
    }

    /// Create new HTTP backend with authentication
    pub fn with_auth(
        base_url: String,
        timeout_secs: u64,
        auth_token: String,
    ) -> anyhow::Result<Self> {
        let client = Client::builder()
            .timeout(Duration::from_secs(timeout_secs))
            .build()?;

        Ok(Self {
            client,
            base_url,
            auth_token: Some(auth_token),
        })
    }

    fn kb_url(&self, kb_id: &Uuid, path: &str) -> String {
        format!("{}/kb/{}/{}", self.base_url, kb_id, path)
    }

    /// Add authentication header to request builder
    fn add_auth(&self, builder: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        if let Some(token) = &self.auth_token {
            builder.bearer_auth(token)
        } else {
            builder
        }
    }

    /// Add principal context headers to request
    fn add_principal_headers(
        &self,
        builder: reqwest::RequestBuilder,
        principal: &PrincipalContext,
    ) -> reqwest::RequestBuilder {
        builder
            .header("X-Tenant-ID", principal.tenant_id.to_string())
            .header("X-Subject-ID", &principal.subject_id)
            .header("X-Request-ID", &principal.request_id)
            .header("X-Trace-ID", &principal.trace_id)
    }

    // ========================================================================
    // P1-09: Memory Plane HTTP helpers (unpermissioned)
    // ========================================================================

    /// Memory-plane remember endpoint URL.
    fn memory_remember_url(&self) -> String {
        format!("{}/memory-plane/remember", self.base_url)
    }

    /// Memory-plane query endpoint URL.
    fn memory_query_url(&self) -> String {
        format!("{}/memory-plane/query", self.base_url)
    }

    /// Memory-plane context endpoint URL.
    fn memory_create_context_url(&self) -> String {
        format!("{}/memory-plane/context", self.base_url)
    }

    /// POST /memory-plane/remember
    ///
    /// Stores a memory item with evidence linkage through the FastAPI
    /// memory-plane endpoint. P1-09: the FastAPI route commits the
    /// memory row, evidence rows, and outbox rows atomically.
    #[instrument(skip(self, input))]
    pub async fn memory_remember(
        &self,
        input: MemoryRememberInput,
    ) -> anyhow::Result<MemoryRememberResponse> {
        let url = self.memory_remember_url();
        let builder = self.client.post(&url).json(&input);
        // No auth headers — memory plane is unpermissioned.
        let response = builder.send().await?;
        let response = response.error_for_status()?;
        let body: MemoryRememberResponse = response.json().await?;
        debug!(
            item_id = %body.item.id,
            "memory_remember completed"
        );
        Ok(body)
    }

    /// POST /memory-plane/query
    ///
    /// Queries memory items via the FastAPI endpoint. Fail-closed: the
    /// response status field communicates no_match/refused rather than
    /// fabricating results.
    #[instrument(skip(self, input))]
    pub async fn memory_query(
        &self,
        input: UnpermissionedMemoryQuery,
    ) -> anyhow::Result<MemoryQueryResult> {
        let url = self.memory_query_url();
        let builder = self.client.post(&url).json(&input);
        let response = builder.send().await?;
        let response = response.error_for_status()?;
        let body: MemoryQueryResult = response.json().await?;
        debug!(
            total = body.total,
            status = ?body.status,
            "memory_query completed"
        );
        Ok(body)
    }

    /// POST /memory-plane/context
    ///
    /// Creates an LLM-ready context pack through the FastAPI endpoint.
    #[instrument(skip(self, input))]
    pub async fn memory_create_context(
        &self,
        input: MemoryContextInput,
    ) -> anyhow::Result<MemoryContextPack> {
        let url = self.memory_create_context_url();
        let builder = self.client.post(&url).json(&input);
        let response = builder.send().await?;
        let response = response.error_for_status()?;
        let body: MemoryContextPack = response.json().await?;
        debug!(
            items = body.memory_items.len(),
            token_used = body.token_used,
            "memory_create_context completed"
        );
        Ok(body)
    }

    // ========================================================================
    // P4-W: World-Memory HTTP helpers
    // ========================================================================

    /// World-memory remember endpoint URL.
    fn world_memory_remember_url(&self) -> String {
        format!("{}/world-memory/remember", self.base_url)
    }

    /// World-memory query endpoint URL.
    fn world_memory_query_url(&self) -> String {
        format!("{}/world-memory/query", self.base_url)
    }

    fn world_memory_state_at_url(&self) -> String {
        format!("{}/world-memory/state-at", self.base_url)
    }

    /// World-memory scenario context endpoint URL.
    fn world_memory_scenario_context_url(&self) -> String {
        format!("{}/world-memory/scenario-context", self.base_url)
    }

    /// POST /world-memory/remember
    ///
    /// Stores a P7-B world-memory envelope through the FastAPI endpoint.
    /// The backend validates the envelope using `validate_world_memory_envelope`
    /// and stores it in the reference store.
    #[instrument(skip(self, input))]
    pub async fn world_memory_remember(
        &self,
        input: WorldMemoryRememberInput,
    ) -> anyhow::Result<WorldMemoryRememberResponse> {
        let url = self.world_memory_remember_url();
        let builder = self.client.post(&url).json(&input);
        let response = builder.send().await?;
        let response = response.error_for_status()?;
        let body: WorldMemoryRememberResponse = response.json().await?;
        debug!(
            kind = %body.kind,
            aggregate_id = %body.aggregate_id,
            "world_memory_remember completed"
        );
        Ok(body)
    }

    /// POST /world-memory/query
    ///
    /// Queries world-memory items through the FastAPI endpoint.
    /// Returns typed context items plus raw P7-B envelopes.
    /// Fail-closed: insufficient evidence returns status "refused".
    #[instrument(skip(self, input))]
    pub async fn world_memory_query(
        &self,
        input: WorldMemoryQueryInput,
    ) -> anyhow::Result<WorldMemoryQueryResponse> {
        let url = self.world_memory_query_url();
        let builder = self.client.post(&url).json(&input);
        let response = builder.send().await?;
        let response = response.error_for_status()?;
        let body: WorldMemoryQueryResponse = response.json().await?;
        debug!(
            items = body.items.len(),
            status = %body.status,
            "world_memory_query completed"
        );
        Ok(body)
    }

    #[instrument(skip(self, input))]
    pub async fn world_memory_state_at(
        &self,
        input: WorldMemoryStateAtInput,
    ) -> anyhow::Result<WorldMemoryQueryResponse> {
        let url = self.world_memory_state_at_url();
        let builder = self.client.post(&url).json(&input);
        let response = builder.send().await?;
        let response = response.error_for_status()?;
        let body: WorldMemoryQueryResponse = response.json().await?;
        debug!(
            aggregate_id = %input.aggregate_id,
            status = %body.status,
            "world_memory_state_at completed"
        );
        Ok(body)
    }

    /// POST /world-memory/scenario-context
    ///
    /// Delegates passive, evidence-constrained scenario context retrieval to
    /// the FastAPI world-memory plane. The backend, rather than MCP callers,
    /// enforces evidence requirements.
    #[instrument(skip(self, input))]
    pub async fn world_memory_scenario_context(
        &self,
        input: WorldMemoryScenarioContextInput,
    ) -> anyhow::Result<WorldMemoryScenarioContextResponse> {
        let url = self.world_memory_scenario_context_url();
        let builder = self.client.post(&url).json(&input);
        let response = builder.send().await?;
        let response = response.error_for_status()?;
        let body: WorldMemoryScenarioContextResponse = response.json().await?;
        debug!(
            scenario = ?body.scenario,
            status = %body.status,
            "world_memory_scenario_context completed"
        );
        Ok(body)
    }
}

#[async_trait]
impl KnowledgeBackend for EnterpriseHttpBackend {
    #[instrument(skip(self, principal), fields(query = %query))]
    async fn search_evidence(
        &self,
        principal: &PrincipalContext,
        kb_ids: &[Uuid],
        query: &str,
        top_k: usize,
    ) -> anyhow::Result<Vec<EvidenceResult>> {
        let url = format!("{}/evidence/search", self.base_url);

        let builder = self.client.post(&url).json(&serde_json::json!({
            "knowledge_base_ids": kb_ids,
            "query": query,
            "top_k": top_k
        }));

        let response = self
            .add_auth(self.add_principal_headers(builder, principal))
            .send()
            .await?;

        let results: Vec<EvidenceResult> = response.json().await?;
        debug!(count = results.len(), "search_evidence completed");
        Ok(results)
    }

    #[instrument(skip(self, principal), fields(question = %question))]
    async fn answer_with_evidence(
        &self,
        principal: &PrincipalContext,
        kb_ids: &[Uuid],
        question: &str,
    ) -> anyhow::Result<AnswerResult> {
        let url = format!("{}/answer", self.base_url);

        let builder = self.client.post(&url).json(&serde_json::json!({
            "knowledge_base_ids": kb_ids,
            "question": question
        }));

        let response = self
            .add_auth(self.add_principal_headers(builder, principal))
            .send()
            .await?;

        let answer: AnswerResult = response.json().await?;
        debug!(
            confidence = answer.confidence,
            "answer_with_evidence completed"
        );
        Ok(answer)
    }

    #[instrument(skip(self, principal))]
    async fn lookup_entity(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        name_or_id: &str,
    ) -> anyhow::Result<Option<EntityResult>> {
        // P1-Security-06: URL encode name_or_id to prevent injection
        let encoded_name = url_encode(name_or_id);
        let url = self.kb_url(&kb_id, &format!("entities/lookup?name={}", encoded_name));

        let builder = self.client.get(&url);
        let response = self
            .add_auth(self.add_principal_headers(builder, principal))
            .send()
            .await?;

        if response.status() == 404 {
            return Ok(None);
        }

        let entity: EntityResult = response.json().await?;
        Ok(Some(entity))
    }

    #[instrument(skip(self, principal))]
    async fn expand(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        entity_id: Uuid,
        direction: Option<&str>,
    ) -> anyhow::Result<ExpandResult> {
        let mut url = self.kb_url(&kb_id, &format!("entities/{}/expand", entity_id));
        if let Some(dir) = direction {
            // P1-Security-06: URL encode direction parameter
            url = format!("{}?direction={}", url, url_encode(dir));
        }

        let builder = self.client.get(&url);
        let response = self
            .add_auth(self.add_principal_headers(builder, principal))
            .send()
            .await?;

        let result: ExpandResult = response.json().await?;
        Ok(result)
    }

    #[instrument(skip(self, principal))]
    async fn find_paths(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        from_entity_id: Uuid,
        to_entity_id: Uuid,
        max_hops: Option<usize>,
    ) -> anyhow::Result<PathResult> {
        let hops = max_hops.unwrap_or(3);
        let url = self.kb_url(
            &kb_id,
            &format!(
                "paths?from={}&to={}&max_hops={}",
                from_entity_id, to_entity_id, hops
            ),
        );

        let builder = self.client.get(&url);
        let response = self
            .add_auth(self.add_principal_headers(builder, principal))
            .send()
            .await?;

        let result: PathResult = response.json().await?;
        Ok(result)
    }

    #[instrument(skip(self, principal))]
    async fn verify_claim(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        claim: &ClaimInput,
    ) -> anyhow::Result<VerifyResult> {
        let url = self.kb_url(&kb_id, "claims/verify");

        let builder = self.client.post(&url).json(&serde_json::json!({
            "knowledge_base_id": kb_id,
            "claim": {
                "subject": claim.subject,
                "predicate": claim.predicate,
                "object": claim.object,
                "qualifiers": claim.qualifiers,
                "polarity": claim.polarity
            }
        }));

        let response = self
            .add_auth(self.add_principal_headers(builder, principal))
            .send()
            .await?;

        let result: VerifyResult = response.json().await?;
        Ok(result)
    }

    async fn health_check(&self) -> anyhow::Result<bool> {
        let url = format!("{}/health", self.base_url);
        let response = self.client.get(&url).send().await?;
        Ok(response.status().is_success())
    }

    // ========================================================================
    // WP7A: Management Job Methods
    // ========================================================================

    #[instrument(skip(self, principal))]
    async fn create_ingest_job(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        source_ref: &str,
        ingest_profile: Option<&str>,
        idempotency_key: &str,
    ) -> anyhow::Result<IngestJobResult> {
        let url = self.kb_url(&kb_id, "jobs/ingest");

        let mut body = serde_json::json!({
            "source_ref": source_ref,
            "idempotency_key": idempotency_key
        });

        if let Some(profile) = ingest_profile {
            body["ingest_profile"] = serde_json::Value::String(profile.to_string());
        }

        let builder = self.client.post(&url).json(&body);
        let response = self
            .add_auth(self.add_principal_headers(builder, principal))
            .send()
            .await?;

        let result: IngestJobResult = response.json().await?;
        Ok(result)
    }

    #[instrument(skip(self, principal))]
    async fn cancel_ingest_job(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        job_id: Uuid,
        reason: &str,
        idempotency_key: &str,
    ) -> anyhow::Result<CancelJobResult> {
        let url = self.kb_url(&kb_id, &format!("jobs/{}/cancel", job_id));

        let builder = self.client.post(&url).json(&serde_json::json!({
            "reason": reason,
            "idempotency_key": idempotency_key
        }));

        let response = self
            .add_auth(self.add_principal_headers(builder, principal))
            .send()
            .await?;

        let result: CancelJobResult = response.json().await?;
        Ok(result)
    }

    #[instrument(skip(self, principal))]
    async fn request_projection_rebuild(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        projections: &[&str],
        reason: &str,
        idempotency_key: &str,
    ) -> anyhow::Result<RebuildRequestResult> {
        let url = self.kb_url(&kb_id, "jobs/rebuild");

        let builder = self.client.post(&url).json(&serde_json::json!({
            "projections": projections,
            "reason": reason,
            "idempotency_key": idempotency_key
        }));

        let response = self
            .add_auth(self.add_principal_headers(builder, principal))
            .send()
            .await?;

        let result: RebuildRequestResult = response.json().await?;
        Ok(result)
    }

    #[instrument(skip(self, principal))]
    async fn request_document_deletion(
        &self,
        principal: &PrincipalContext,
        kb_id: Uuid,
        document_id: Uuid,
        expected_revision: u64,
        reason: &str,
        idempotency_key: &str,
    ) -> anyhow::Result<DeletionRequestResult> {
        let url = self.kb_url(
            &kb_id,
            &format!("documents/{}/deletion-request", document_id),
        );

        let builder = self.client.post(&url).json(&serde_json::json!({
            "expected_revision": expected_revision,
            "reason": reason,
            "idempotency_key": idempotency_key
        }));

        let response = self
            .add_auth(self.add_principal_headers(builder, principal))
            .send()
            .await?;

        let result: DeletionRequestResult = response.json().await?;
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::unpermissioned::{
        EvidenceRef as SchemaEvidenceRef, MemoryContextInput, MemoryContextPack, MemoryItem,
        MemoryItemStatus, MemoryQueryResult, MemoryRememberInput, MemoryRememberResponse,
        QueryStatus, UnpermissionedMemoryQuery,
    };
    use crate::schema::world_memory::WorldMemoryCandidateSourceTrace;
    use crate::schema::world_memory::{
        WorldMemoryContextItem, WorldMemoryScenario, WorldMemoryTemporalTrace,
    };
    use wiremock::{
        matchers::{body_json, method, path},
        Mock, ResponseTemplate,
    };

    /// Build a backend pointed at the wiremock server.
    async fn backend_for(mock_server: &wiremock::MockServer) -> EnterpriseHttpBackend {
        EnterpriseHttpBackend::new(mock_server.uri(), 5).unwrap()
    }

    fn sample_remember_input(content: &str) -> MemoryRememberInput {
        MemoryRememberInput {
            content: content.to_string(),
            evidence_refs: vec![SchemaEvidenceRef {
                source_id: "doc-1".to_string(),
                source_type: "document".to_string(),
                timestamp: chrono::Utc::now(),
                chunk_id: None,
                confidence: 0.9,
                metadata: serde_json::json!({}),
            }],
            provenance: None,
            project_id: Some(uuid::Uuid::new_v4()),
            session_id: None,
            agent_id: None,
            knowledge_base_id: None,
            expires_at: None,
            metadata: serde_json::json!({}),
        }
    }

    fn sample_item_for(input: &MemoryRememberInput) -> MemoryItem {
        MemoryItem {
            id: uuid::Uuid::new_v4(),
            content: input.content.clone(),
            evidence_refs: input.evidence_refs.clone(),
            provenance: None,
            status: MemoryItemStatus::Active,
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
            expires_at: None,
            metadata: serde_json::json!({}),
            project_id: input.project_id,
            session_id: input.session_id,
            agent_id: input.agent_id,
            knowledge_base_id: input.knowledge_base_id,
        }
    }

    /// P1-09 wire test: `memory_remember` calls `POST /memory-plane/remember`,
    /// forwards the JSON body, uses `error_for_status`, and deserialises the
    /// FastAPI response.
    #[tokio::test]
    async fn memory_remember_calls_fastapi_endpoint() {
        let mock_server = wiremock::MockServer::start().await;
        let backend = backend_for(&mock_server).await;
        let input = sample_remember_input("wire-level remember test");

        let expected_response = MemoryRememberResponse {
            success: true,
            item: sample_item_for(&input),
            message: "Memory item stored successfully".to_string(),
        };

        Mock::given(method("POST"))
            .and(path("/memory-plane/remember"))
            .respond_with(ResponseTemplate::new(201).set_body_json(&expected_response))
            .expect(1)
            .mount(&mock_server)
            .await;

        let result = backend.memory_remember(input).await.unwrap();
        assert!(result.success);
        assert_eq!(result.item.content, "wire-level remember test");
        assert_eq!(result.item.id, expected_response.item.id);
    }

    /// P1-09 wire test: `memory_query` calls `POST /memory-plane/query` and
    /// surfaces the fail-closed status from the FastAPI response.
    #[tokio::test]
    async fn memory_query_calls_fastapi_endpoint() {
        let mock_server = wiremock::MockServer::start().await;
        let backend = backend_for(&mock_server).await;
        let input = UnpermissionedMemoryQuery {
            query: "nonexistent content xyz".to_string(),
            project_id: None,
            session_id: None,
            agent_id: None,
            knowledge_base_id: None,
            max_items: 10,
            token_budget: 4000,
            memory_types: vec![],
            require_evidence: true,
            min_confidence: 0.0,
            start_time: None,
            end_time: None,
        };

        let expected_response = MemoryQueryResult {
            items: vec![],
            total: 0,
            token_used: 0,
            token_budget: 4000,
            query_id: "q-wire".to_string(),
            latency_ms: 0.5,
            status: QueryStatus::NoMatch,
            refusal_reason: Some("no_matching_items".to_string()),
            warnings: vec![],
        };

        Mock::given(method("POST"))
            .and(path("/memory-plane/query"))
            .respond_with(ResponseTemplate::new(200).set_body_json(&expected_response))
            .expect(1)
            .mount(&mock_server)
            .await;

        let result = backend.memory_query(input).await.unwrap();
        assert_eq!(result.status, QueryStatus::NoMatch);
        assert_eq!(result.total, 0);
        assert_eq!(result.refusal_reason.as_deref(), Some("no_matching_items"));
    }

    /// P1-09 wire test: a non-2xx FastAPI response must surface as an error,
    /// not silently deserialise into a success. Proves `error_for_status` use.
    #[tokio::test]
    async fn memory_remember_surfaces_http_error() {
        let mock_server = wiremock::MockServer::start().await;
        let backend = backend_for(&mock_server).await;
        let input = sample_remember_input("will-fail");

        Mock::given(method("POST"))
            .and(path("/memory-plane/remember"))
            .respond_with(
                ResponseTemplate::new(500).set_body_json(serde_json::json!({"detail": "db down"})),
            )
            .expect(1)
            .mount(&mock_server)
            .await;

        let result = backend.memory_remember(input).await;
        assert!(
            result.is_err(),
            "non-2xx response must propagate as an error"
        );
    }

    /// P1-09 wire test: `memory_create_context` calls the context endpoint.
    #[tokio::test]
    async fn memory_create_context_calls_fastapi_endpoint() {
        let mock_server = wiremock::MockServer::start().await;
        let backend = backend_for(&mock_server).await;
        let input = MemoryContextInput {
            project_id: Some(uuid::Uuid::new_v4()),
            session_id: None,
            agent_id: None,
            memory_items: vec![],
            evidence_packet: None,
            token_budget: 4000,
        };

        let expected_response = MemoryContextPack {
            project_id: input.project_id,
            session_id: None,
            agent_id: None,
            memory_items: vec![],
            evidence_packet: None,
            token_budget: 4000,
            token_used: 0,
            created_at: chrono::Utc::now(),
        };

        Mock::given(method("POST"))
            .and(path("/memory-plane/context"))
            .respond_with(ResponseTemplate::new(200).set_body_json(&expected_response))
            .expect(1)
            .mount(&mock_server)
            .await;

        let result = backend.memory_create_context(input).await.unwrap();
        assert_eq!(result.project_id, expected_response.project_id);
        assert_eq!(result.token_budget, 4000);
    }

    // ========================================================================
    // P4-W: World-Memory HTTP wire tests
    // ========================================================================

    /// P4-W wire test: `world_memory_remember` calls `POST /world-memory/remember`,
    /// forwards the envelope, and deserialises the response.
    #[tokio::test]
    async fn world_memory_remember_calls_fastapi_endpoint() {
        let mock_server = wiremock::MockServer::start().await;
        let backend = backend_for(&mock_server).await;

        let input = WorldMemoryRememberInput {
            envelope: serde_json::json!({
                "event_type": "world.observation.ingested",
                "schema_version": 1,
                "aggregate_type": "world_memory",
                "aggregate_id": "00000000-0000-0000-0000-000000000001",
                "projection_target": "test",
                "idempotency_key": "world:00000000-0000-0000-0000-000000000001:world.observation.ingested:v1:test",
                "occurred_at": "2026-07-31T12:00:00+00:00",
                "world_memory_snapshot": {
                    "_kind": "observation",
                    "id": "00000000-0000-0000-0000-000000000001",
                    "content": "Wire-level test",
                    "observed_at": "2026-07-31T12:00:00+00:00",
                    "ingested_at": "2026-07-31T12:00:01+00:00",
                    "source": "test",
                    "version": 1,
                    "raw_evidence_ref": "doc-1",
                    "sensor_id": null,
                    "metadata": {}
                }
            }),
        };

        let expected_response = WorldMemoryRememberResponse {
            success: true,
            idempotency_key: input.envelope["idempotency_key"]
                .as_str()
                .unwrap()
                .to_string(),
            aggregate_id: "00000000-0000-0000-0000-000000000001".to_string(),
            kind: "observation".to_string(),
            event_type: "world.observation.ingested".to_string(),
            message: "World-memory envelope stored successfully".to_string(),
        };

        Mock::given(method("POST"))
            .and(path("/world-memory/remember"))
            .respond_with(ResponseTemplate::new(201).set_body_json(&expected_response))
            .expect(1)
            .mount(&mock_server)
            .await;

        let result = backend.world_memory_remember(input).await.unwrap();
        assert!(result.success);
        assert_eq!(result.kind, "observation");
    }

    /// P4-W wire test: `world_memory_query` calls `POST /world-memory/query`
    /// and surfaces the fail-closed status.
    #[tokio::test]
    async fn world_memory_query_calls_fastapi_endpoint() {
        let mock_server = wiremock::MockServer::start().await;
        let backend = backend_for(&mock_server).await;

        let input = WorldMemoryQueryInput {
            query: "persistent memory".to_string(),
            kind: Some("observation".to_string()),
            aggregate_id: None,
            event_time_lte: None,
            require_evidence: true,
            max_items: 10,
            token_budget: 4000,
        };

        let expected_response = WorldMemoryQueryResponse {
            query_id: "q-wm-1".to_string(),
            items: vec![],
            envelopes: vec![],
            token_budget: 4000,
            token_used: 0,
            status: "no_match".to_string(),
            refusal_reason: Some("no_matching_items".to_string()),
            candidate_sources: vec![WorldMemoryCandidateSourceTrace {
                source: "postgresql".to_string(),
                status: "ok".to_string(),
                candidate_count: 3,
                latency_ms: 5.75,
                error: None,
            }],
            latency_ms: 8.25,
        };

        Mock::given(method("POST"))
            .and(path("/world-memory/query"))
            .and(body_json(serde_json::json!({
                "query": "persistent memory",
                "kind": "observation",
                "aggregate_id": null,
                "require_evidence": true,
                "max_items": 10,
                "token_budget": 4000
            })))
            .respond_with(ResponseTemplate::new(200).set_body_json(&expected_response))
            .expect(1)
            .mount(&mock_server)
            .await;

        let result = backend.world_memory_query(input).await.unwrap();
        assert_eq!(result.status, "no_match");
        assert_eq!(result.items.len(), 0);
        assert_eq!(result.candidate_sources.len(), 1);
        assert_eq!(result.candidate_sources[0].source, "postgresql");
        assert_eq!(result.candidate_sources[0].candidate_count, 3);
        assert_eq!(result.candidate_sources[0].latency_ms, 5.75);
        assert_eq!(result.latency_ms, 8.25);
    }

    #[tokio::test]
    async fn world_memory_state_at_calls_fastapi_endpoint() {
        let mock_server = wiremock::MockServer::start().await;
        let backend = backend_for(&mock_server).await;
        let input = WorldMemoryStateAtInput {
            aggregate_id: "aggregate-1".to_string(),
            valid_at: "2026-08-02T04:00:00Z".parse().unwrap(),
            require_evidence: true,
            token_budget: 4000,
        };
        let expected_response = WorldMemoryQueryResponse {
            query_id: "q-state-at".to_string(),
            items: vec![],
            envelopes: vec![],
            token_budget: 4000,
            token_used: 0,
            status: "no_match".to_string(),
            refusal_reason: Some("no_matching_items".to_string()),
            candidate_sources: vec![],
            latency_ms: 1.25,
        };

        Mock::given(method("POST"))
            .and(path("/world-memory/state-at"))
            .and(body_json(serde_json::json!({
                "aggregate_id": "aggregate-1",
                "valid_at": "2026-08-02T04:00:00Z",
                "require_evidence": true,
                "token_budget": 4000
            })))
            .respond_with(ResponseTemplate::new(200).set_body_json(&expected_response))
            .expect(1)
            .mount(&mock_server)
            .await;

        let result = backend.world_memory_state_at(input).await.unwrap();
        assert_eq!(result.query_id, "q-state-at");
        assert_eq!(result.status, "no_match");
    }

    #[tokio::test]
    async fn world_memory_query_forwards_event_time_lte() {
        let mock_server = wiremock::MockServer::start().await;
        let backend = backend_for(&mock_server).await;
        let input = WorldMemoryQueryInput {
            query: String::new(),
            kind: None,
            aggregate_id: None,
            event_time_lte: Some("2026-08-02T04:00:00Z".parse().unwrap()),
            require_evidence: true,
            max_items: 10,
            token_budget: 4000,
        };
        let expected_response = WorldMemoryQueryResponse {
            query_id: "q-cutoff".to_string(),
            items: vec![],
            envelopes: vec![],
            token_budget: 4000,
            token_used: 0,
            status: "no_match".to_string(),
            refusal_reason: Some("no_matching_items".to_string()),
            candidate_sources: vec![],
            latency_ms: 0.0,
        };

        Mock::given(method("POST"))
            .and(path("/world-memory/query"))
            .and(body_json(serde_json::json!({
                "query": "",
                "kind": null,
                "aggregate_id": null,
                "event_time_lte": "2026-08-02T04:00:00Z",
                "require_evidence": true,
                "max_items": 10,
                "token_budget": 4000
            })))
            .respond_with(ResponseTemplate::new(200).set_body_json(&expected_response))
            .expect(1)
            .mount(&mock_server)
            .await;

        let result = backend.world_memory_query(input).await.unwrap();
        assert_eq!(result.query_id, "q-cutoff");
    }

    #[tokio::test]
    async fn world_memory_query_forwards_historical_text_refusal() {
        let mock_server = wiremock::MockServer::start().await;
        let backend = backend_for(&mock_server).await;
        let input = WorldMemoryQueryInput {
            query: "road closure".to_string(),
            kind: Some("observation".to_string()),
            aggregate_id: None,
            event_time_lte: Some("2026-08-02T04:00:00Z".parse().unwrap()),
            require_evidence: true,
            max_items: 10,
            token_budget: 4000,
        };
        let expected_response = WorldMemoryQueryResponse {
            query_id: "q-history-refused".to_string(),
            items: vec![],
            envelopes: vec![],
            token_budget: 4000,
            token_used: 0,
            status: "refused".to_string(),
            refusal_reason: Some("event_history_lagging".to_string()),
            candidate_sources: vec![WorldMemoryCandidateSourceTrace {
                source: "elasticsearch_event_history".to_string(),
                status: "refused".to_string(),
                candidate_count: 0,
                latency_ms: 2.0,
                error: Some("event_history_lagging".to_string()),
            }],
            latency_ms: 3.0,
        };

        Mock::given(method("POST"))
            .and(path("/world-memory/query"))
            .and(body_json(serde_json::json!({
                "query": "road closure",
                "kind": "observation",
                "aggregate_id": null,
                "event_time_lte": "2026-08-02T04:00:00Z",
                "require_evidence": true,
                "max_items": 10,
                "token_budget": 4000
            })))
            .respond_with(ResponseTemplate::new(200).set_body_json(&expected_response))
            .expect(1)
            .mount(&mock_server)
            .await;

        let result = backend.world_memory_query(input).await.unwrap();
        assert_eq!(result.status, "refused");
        assert_eq!(
            result.refusal_reason.as_deref(),
            Some("event_history_lagging")
        );
        assert!(result.items.is_empty());
        assert!(result.envelopes.is_empty());
        assert_eq!(
            result.candidate_sources[0].source,
            "elasticsearch_event_history"
        );
    }

    /// P9-A wire test: scenario context is forwarded unchanged, while the
    /// FastAPI endpoint owns evidence enforcement and refusal semantics.
    #[tokio::test]
    async fn world_memory_scenario_context_calls_fastapi_endpoint() {
        let mock_server = wiremock::MockServer::start().await;
        let backend = backend_for(&mock_server).await;

        let input = WorldMemoryScenarioContextInput {
            scenario: WorldMemoryScenario::Rescue,
            query: String::new(),
            aggregate_id: Some("aggregate-1".to_string()),
            event_time_lte: Some("2026-08-02T04:00:00Z".parse().unwrap()),
            max_items: 8,
            token_budget: 512,
        };
        let expected_response = WorldMemoryScenarioContextResponse {
            scenario: WorldMemoryScenario::Rescue,
            passive: true,
            read_only: true,
            items: vec![WorldMemoryContextItem {
                kind: "hypothesis".to_string(),
                id: "hypothesis-1".to_string(),
                content: "Bridge may be flooded".to_string(),
                evidence_ids: vec!["evidence-1".to_string()],
                temporal: WorldMemoryTemporalTrace {
                    event_occurred_at: "2026-08-02T04:00:00Z".parse().unwrap(),
                    observed_at: None,
                    ingested_at: None,
                    validity_start: Some("2026-08-02T04:00:00Z".parse().unwrap()),
                    validity_end: Some("2026-08-02T04:05:00Z".parse().unwrap()),
                    created_at: None,
                    verified_at: None,
                },
            }],
            envelopes: vec![serde_json::json!({"source": "authority"})],
            status: "ok".to_string(),
            refusal_reason: None,
            candidate_sources: vec![WorldMemoryCandidateSourceTrace {
                source: "elasticsearch".to_string(),
                status: "ok".to_string(),
                candidate_count: 1,
                latency_ms: 2.5,
                error: None,
            }],
            latency_ms: 4.0,
        };

        Mock::given(method("POST"))
            .and(path("/world-memory/scenario-context"))
            .and(body_json(serde_json::json!({
                "scenario": "rescue",
                "query": "",
                "aggregate_id": "aggregate-1",
                "event_time_lte": "2026-08-02T04:00:00Z",
                "max_items": 8,
                "token_budget": 512
            })))
            .respond_with(ResponseTemplate::new(200).set_body_json(&expected_response))
            .expect(1)
            .mount(&mock_server)
            .await;

        let result = backend.world_memory_scenario_context(input).await.unwrap();
        assert_eq!(result.scenario, WorldMemoryScenario::Rescue);
        assert!(result.passive);
        assert!(result.read_only);
        assert_eq!(result.items[0].kind, "hypothesis");
        assert_eq!(
            result.items[0]
                .temporal
                .validity_start
                .unwrap()
                .to_rfc3339(),
            "2026-08-02T04:00:00+00:00"
        );
        assert_eq!(
            result.items[0].temporal.validity_end.unwrap().to_rfc3339(),
            "2026-08-02T04:05:00+00:00"
        );
        assert!(result.items[0].temporal.observed_at.is_none());
        assert_eq!(result.envelopes, expected_response.envelopes);
        assert_eq!(result.candidate_sources[0].source, "elasticsearch");
        assert_eq!(result.latency_ms, 4.0);
    }

    /// P4-W wire test: non-2xx response surfaces as error.
    #[tokio::test]
    async fn world_memory_remember_surfaces_http_error() {
        let mock_server = wiremock::MockServer::start().await;
        let backend = backend_for(&mock_server).await;

        let input = WorldMemoryRememberInput {
            envelope: serde_json::json!({"event_type": "world.observation.ingested"}),
        };

        Mock::given(method("POST"))
            .and(path("/world-memory/remember"))
            .respond_with(
                ResponseTemplate::new(422)
                    .set_body_json(serde_json::json!({"detail": "validation error"})),
            )
            .expect(1)
            .mount(&mock_server)
            .await;

        let result = backend.world_memory_remember(input).await;
        assert!(result.is_err(), "non-2xx response must propagate as error");
    }
}
