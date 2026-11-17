# Memory Compose Stack — Backend-only Runbook

> Scope document for
> `.agent-team/control-plane/plans/run-20260730-p4-memory-compose-boundary.md`.
> This runbook describes the **backend-only** memory compose profile
> (`docker-compose.memory.yml`). It does **not** replace the full
> `docker-compose.yml` stack, which remains available for production
> deployments that include frontend, MinIO, and Neo4j.

## Purpose

The memory compose profile provides a minimal, reproducible runtime for:

- TUI clients (e.g. Claude Code / MCP gateway) that only need the FastAPI
  backend and its backing stores;
- local development and CI smoke tests that should not pull in the full
  frontend/MinIO/Neo4j surface;
- validating the P2 memory-plane wiring (`MEMORY_CANDIDATE_SOURCES_ENABLED`)
  in isolation.

## Services

| Service           | Image              | Internal DNS  | Host port (default) |
|-------------------|--------------------|---------------|----------------------|
| elasticsearch     | elasticsearch 8.12 | `elasticsearch` | `19200` (`MEM_ES_PORT`) |
| postgres          | postgres 15        | `postgres`    | `15432` (`MEM_PG_PORT`) |
| redis             | redis 7-alpine     | `redis`       | `16379` (`MEM_REDIS_PORT`) |
| backend           | built from `./backend` | —          | `18000` (`MEM_BACKEND_PORT`) |

All host ports bind to `127.0.0.1` only and are overridable via environment
variables, so the memory stack can run alongside an existing local stack
without port conflicts.

All four services share an isolated `mem-internal` bridge network and use
`mem_*` prefixed named volumes so their state does not collide with the
full stack's `es_data` / `postgres_data` / `redis_data` volumes.

## What is NOT in this profile

The following services are **outside** the memory compose profile:

- **`frontend`** — remains in `docker-compose.yml`. The memory profile is
  backend-only; the Vue UI is optional and served separately when needed.
- **`minio`** / **`minio-worker`** — blob/evidence storage is not part of
  the memory profile. Set `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` in
  `.env` and use `docker-compose.yml` when MinIO is required.
- **`neo4j`** — knowledge-graph projector is outside this profile. The
  backend runs with `NEO4J_ENABLED=false` explicitly set here; flip to
  `true` only when using the full compose stack with a Neo4j service.

## Quick start

```bash
# From the repo root:
cp .env.example .env            # if you haven't already
# edit .env: set JWT_SECRET_KEY, LLM_API_KEY, DB_PASSWORD

# Bring up the memory stack (detached):
docker compose -f docker-compose.memory.yml up -d

# Tail backend logs:
docker compose -f docker-compose.memory.yml logs -f backend

# Health check the backend:
curl http://127.0.0.1:18000/api/v1/health

# Tear down (preserves volumes — pass -v to wipe state too):
docker compose -f docker-compose.memory.yml down
```

## Overriding host ports

If the default ports conflict with an existing stack:

```bash
MEM_ES_PORT=29200 \
MEM_PG_PORT=25432 \
MEM_REDIS_PORT=26379 \
MEM_BACKEND_PORT=28000 \
  docker compose -f docker-compose.memory.yml up -d
```

## TUI / MCP client usage

The backend is published on `MEM_BACKEND_PORT` (default `18000`). TUI and
MCP clients should point at:

```
http://127.0.0.1:18000/api/v1/...
```

The MCP gateway's unpermissioned agent path is served from the same
backend, so configuring the gateway's `ESRAGPRO_BACKEND_URL` to the above
address is sufficient.

## Memory candidate sources

The memory profile is the **only** compose entry that sets
`MEMORY_CANDIDATE_SOURCES_ENABLED=true`. The global default in
`.env.example` remains `false` so running the backend outside this
profile (e.g. `uvicorn app.main:app` directly) preserves the previous
opt-in behavior.

To disable candidate sources even within the memory profile, override on
the command line:

```bash
MEMORY_CANDIDATE_SOURCES_ENABLED=false \
  docker compose -f docker-compose.memory.yml up -d backend
```

## Config validation (no stack start)

```bash
# Validate the full stack interpolates cleanly with the example env:
docker compose --env-file .env.example -f docker-compose.yml config > /dev/null

# List services in the memory profile (expect exactly 4):
docker compose -f docker-compose.memory.yml config --services
# → backend  elasticsearch  postgres  redis
```

## Running the P2 regression suite

The P2 candidate-source tests are the relevant regression gate. They do
not require Docker:

```bash
cd backend
source venv/bin/activate           # or .venv/bin/activate
pytest tests/unit/test_candidate_sources_runtime.py -v
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `required variable MINIO_ACCESS_KEY is missing` when running the full stack | `.env` lacks MinIO placeholders | Uncomment `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` in `.env` |
| Backend unhealthy | LLM_API_KEY not set | Set a valid `LLM_API_KEY` in `.env` |
| Port conflict on 19200/15432/16379/18000 | Another stack is running | Override via `MEM_*_PORT` env vars |
| `network esragpro-internal not found` | Full stack was never started | This profile uses `mem-internal`, not `esragpro-internal` |
