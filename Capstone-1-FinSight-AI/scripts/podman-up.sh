#!/bin/sh
set -eu

POD_NAME="finsight-pod"
API_CONTAINER="finsight-api"
UI_CONTAINER="finsight-ui"
STORAGE_VOLUME="finsight-storage"

podman image exists finsight-api:local || {
    echo "Missing finsight-api:local. Build it with: podman build -f docker/Dockerfile.api -t finsight-api:local ." >&2
    exit 1
}
podman image exists finsight-ui:local || {
    echo "Missing finsight-ui:local. Build it with: podman build -f docker/Dockerfile.ui -t finsight-ui:local ." >&2
    exit 1
}

if podman pod exists "$POD_NAME"; then
    echo "$POD_NAME already exists. Run scripts/podman-down.sh first, or inspect it with: podman pod ps" >&2
    exit 1
fi

podman volume inspect "$STORAGE_VOLUME" >/dev/null 2>&1 || podman volume create "$STORAGE_VOLUME" >/dev/null
podman pod create --name "$POD_NAME" -p 8000:8000 -p 8501:8501

podman run -d --name "$API_CONTAINER" --pod "$POD_NAME" \
    -v "$STORAGE_VOLUME:/app/storage" \
    -e FINSIGHT_API_KEY="${FINSIGHT_API_KEY:-change-me}" \
    -e FINSIGHT_OLLAMA_URL="${FINSIGHT_OLLAMA_URL:-http://host.containers.internal:11434}" \
    -e FINSIGHT_SMALL_MODEL="${FINSIGHT_SMALL_MODEL:-llama3:latest}" \
    -e FINSIGHT_STRONG_MODEL="${FINSIGHT_STRONG_MODEL:-llama3.1:8b}" \
    -e FINSIGHT_EMBED_MODEL="${FINSIGHT_EMBED_MODEL:-nomic-embed-text:latest}" \
    -e FINSIGHT_AUTO_INGEST_DIR="/app/data/sample" \
    -e FINSIGHT_RERANKER_ENABLED=false \
    finsight-api:local

podman run -d --name "$UI_CONTAINER" --pod "$POD_NAME" \
    -e FINSIGHT_API_URL="http://127.0.0.1:8000" \
    -e FINSIGHT_API_KEY="${FINSIGHT_API_KEY:-change-me}" \
    finsight-ui:local

echo "FinSight started:"
echo "  UI:  http://localhost:8501"
echo "  API: http://localhost:8000/health"
echo "  Logs: podman logs -f $API_CONTAINER"