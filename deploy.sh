#!/bin/bash
# esRAGPro Production Deployment Script

set -e

echo "========================================"
echo "  esRAGPro Production Deployment"
echo "========================================"
echo

# Check if .env exists
if [ ! -f .env ]; then
    echo "Error: .env file not found!"
    echo "Please copy .env.example to .env and configure it."
    exit 1
fi

# Load environment variables
export $(cat .env | grep -v '^#' | xargs)

echo "Building and starting services..."
docker compose up -d --build

echo
echo "Waiting for services to be healthy..."
sleep 10

# Check Elasticsearch
echo "Checking Elasticsearch..."
until curl -s http://localhost:9200/_cluster/health | grep -q "green\|yellow"; do
    echo "Waiting for Elasticsearch..."
    sleep 5
done
echo "Elasticsearch is healthy!"

# Check Backend
echo "Checking Backend..."
until curl -s http://localhost:8000/health > /dev/null 2>&1; do
    echo "Waiting for Backend..."
    sleep 5
done
echo "Backend is healthy!"

echo
echo "========================================"
echo "  Deployment Complete!"
echo "  Frontend: http://localhost"
echo "  Backend:  http://localhost:8000"
echo "  ES:       http://localhost:9200"
echo "========================================"