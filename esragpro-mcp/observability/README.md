# OpenTelemetry Configuration (WP8)

## Overview

esRAGPro MCP Gateway uses OpenTelemetry for distributed tracing and metrics collection.

## Configuration

Environment variables:
- `OTEL_SERVICE_NAME`: Service name (default: esragpro-mcp)
- `OTEL_EXPORTER_OTLP_ENDPOINT`: OTLP endpoint URL
- `OTEL_TRACES_SAMPLER`: Sampling strategy (always_on, traceidratio)
- `OTEL_TRACES_SAMPLER_ARG`: Sampling ratio (0.0-1.0)

## Traced Operations

### MCP Layer
- `mcp.request`: Incoming MCP request
  - `mcp.request_id`: Unique request ID
  - `mcp.tool_name`: Tool being called
  - `mcp.tenant_id`: Tenant context
  - `mcp.knowledge_base_id`: Knowledge base ID

### Backend Layer
- `backend.request`: Backend API call
  - `backend.service`: Service name (elasticsearch, neo4j, core)
  - `backend.operation`: Operation type
  - `backend.latency_ms`: Response latency

### Elasticsearch Layer
- `es.query`: Elasticsearch query
  - `es.index`: Target index
  - `es.query_type`: Query type (bm25, knn, hybrid)

### Neo4j Layer
- `neo4j.query`: Cypher query
  - `neo4j.query_type`: Query type

## Metrics

### Request Metrics
- `mcp_requests_total`: Total MCP requests by tool
- `mcp_request_duration_ms`: Request latency histogram
- `mcp_request_errors_total`: Error count by tool

### Backend Metrics
- `backend_requests_total`: Backend requests by service
- `backend_request_duration_ms`: Backend latency histogram
- `backend_request_errors_total`: Backend error count

### Custom Metrics
- `evidence_chunks_retrieved`: Evidence chunks retrieved per query
- `claims_verified`: Claims verified per request
- `acl_check_duration_ms`: ACL check latency

## Dashboards

### Grafana Dashboard Panels
1. Request Rate (req/s)
2. Request Latency (p50, p95, p99)
3. Error Rate (%)
4. Tool Usage Distribution
5. Backend Service Latency
6. Elasticsearch Query Latency
7. Neo4j Query Latency

## Alerting Rules

### Critical
- `acl_leakage_detected`: ACL boundary violation (immediate action)
- `error_rate_high`: Error rate > 1% for 5 minutes

### Warning
- `latency_p99_high`: p99 latency > 5s for 5 minutes
- `rate_limit_rejected`: Rate limit rejections increasing