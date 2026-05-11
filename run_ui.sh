#!/bin/bash

# Run script for A2UI Development Server
# Usage: ./run_ui.sh

set -e

# Configuration
IMAGE_NAME="declarative-agent"
TAG="${1:-latest}"
CONTAINER_NAME="a2ui-dev-server"
PORT="${PORT:-5173}"  # Default Vite dev server port

# Get the SDK directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
A2UI_DIR="${SCRIPT_DIR}/a2ui"

echo "========================================"
echo "Starting A2UI Development Server"
echo "Image: ${IMAGE_NAME}:${TAG}"
echo "Container: ${CONTAINER_NAME}"
echo "Port: ${PORT}"
echo "========================================"

# Check if a2ui directory exists
if [ ! -d "${A2UI_DIR}" ]; then
    echo "Error: a2ui directory not found at ${A2UI_DIR}"
    exit 1
fi

# Stop and remove existing container if it exists
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "Stopping and removing existing container..."
    docker stop "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    docker rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true
fi

# Run the container
echo "Starting container..."
docker run -d \
  --rm \
  --name "${CONTAINER_NAME}" \
  -p "${PORT}:5173" \
  -e VITE_AGENT_URL="https://declarative-agent-382266101555.us-west2.run.app/" \
  -w /app/a2ui \
  "${IMAGE_NAME}:${TAG}" \
  npx vite dev --host 0.0.0.0 --port ${PORT}

echo ""
echo "========================================"
echo "A2UI Development Server started!"
echo "========================================"
echo ""
echo "  Local:    http://localhost:${PORT}"
echo "  Container: ${CONTAINER_NAME}"
echo ""
echo "Commands:"
echo "  View logs:    docker logs -f ${CONTAINER_NAME}"
echo "  Stop:         docker stop ${CONTAINER_NAME}"
echo "  Restart:      docker restart ${CONTAINER_NAME}"
echo ""
echo "The a2ui directory is mounted - changes will hot-reload automatically."
echo ""
