#!/bin/bash
# esRAGPro Development Startup Script for Linux/macOS

set -e

echo "========================================"
echo "  esRAGPro Development Environment"
echo "========================================"
echo

# Check if .env exists
if [ ! -f .env ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
    echo "Please edit .env with your configuration and run again."
    exit 1
fi

# Start Elasticsearch (requires Docker)
echo "Checking Elasticsearch..."
if ! curl -s http://localhost:9200/_cluster/health > /dev/null 2>&1; then
    echo "Starting Elasticsearch with Docker..."
    docker run -d --name esragpro-es \
        -p 9200:9200 -p 9300:9300 \
        -e "discovery.type=single-node" \
        -e "xpack.security.enabled=false" \
        -e "ES_JAVA_OPTS=-Xms1g -Xmx1g" \
        docker.elastic.co/elasticsearch/elasticsearch:8.12.0
    echo "Waiting for Elasticsearch to start..."
    sleep 30
fi

# Start Backend
echo
echo "Starting Backend..."
cd backend
if [ ! -d venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate
pip install -e . -q
uvicorn app.main:app --reload --port 8000 &
cd ..

# Start Frontend
echo
echo "Starting Frontend..."
cd frontend
if [ ! -d node_modules ]; then
    echo "Installing frontend dependencies..."
    npm install --registry=https://registry.npmmirror.com
fi
npm run dev &
cd ..

echo
echo "========================================"
echo "  Services starting..."
echo "  Backend:  http://localhost:8000"
echo "  Frontend: http://localhost:3000"
echo "  ES:       http://localhost:9200"
echo "========================================"

# Wait for all background jobs
wait