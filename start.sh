#!/bin/bash
set -e

echo "[start.sh] Starting Redis..."
redis-server --bind 0.0.0.0 --port 6379 &

echo "[start.sh] Waiting for Redis..."
until redis-cli ping | grep -q PONG; do
  sleep 0.5
done
echo "[start.sh] Redis is up."


echo "[start.sh] Starting FastAPI on port 8000..."
exec uvicorn vllm_deployment:app --host 0.0.0.0 --port 8000
