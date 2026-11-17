# Backend Dockerfile
#
# DEFAULT BUILD = slim image WITHOUT the local embedding stack (torch).
#   - Runtime: set EMBEDDING_PROVIDER=qwen_api (or zhipu_api) + EMBEDDING_API_KEY
#   - Result: ~500-700MB image, ~150-250MB RSS instead of ~1.2GB+/~600MB+
#
# PRIVATE/OFFLINE deployments that need in-process local embeddings build with:
#   docker build --build-arg LOCAL_EMBEDDING=1 -t esragpro-backend:local .
# and run with EMBEDDING_PROVIDER=local. That variant installs the stable
# CPU-only PyTorch wheel from the official CPU index so the unconstrained
# `torch>=2.0.0` resolution cannot pull the multi-GB CUDA stack into this
# CPU-only image. The embedding service selects CPU automatically when
# torch.cuda.is_available() is false, so runtime semantics are unchanged.
FROM python:3.12-slim

WORKDIR /app

ARG LOCAL_EMBEDDING=0

# curl is used by container healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# README.md must ship alongside pyproject.toml: hatchling reads
# `readme = "README.md"` during metadata generation for the editable install.
COPY pyproject.toml README.md ./

RUN if [ "$LOCAL_EMBEDDING" = "1" ]; then \
        pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.12.1 && \
        pip install --no-cache-dir --default-timeout=180 -e ".[local-embedding]" ; \
    else \
        pip install --no-cache-dir --default-timeout=180 -e . ; \
    fi

# Copy application code
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts/provision_embedding_model.py ./scripts/provision_embedding_model.py
COPY model-locks ./model-locks

# Provision local model files only in local-embedding builds
RUN if [ "$LOCAL_EMBEDDING" = "1" ]; then \
        python scripts/provision_embedding_model.py || echo "model provisioning skipped (offline?)" ; \
    fi

# Create uploads directory
RUN mkdir -p /app/uploads

# Expose port
EXPOSE 8000

# Run application
# UVICORN_WORKERS is the deployment capacity seam. The default stays 1
# (the long-standing single-worker behavior); override it to scale the API
# process. `exec` keeps the uvicorn master as PID 1 so shutdown signals are
# delivered correctly.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers \"${UVICORN_WORKERS:-1}\""]
