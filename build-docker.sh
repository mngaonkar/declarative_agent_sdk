#!/bin/bash

# Build script for Generic Declarative Agent SDK Docker Image
# Usage: ./build-docker.sh [tag]

set -e

# Default tag
TAG="${1:-latest}"
IMAGE_NAME="declarative-agent"

echo "========================================"
echo "Building Declarative Agent SDK Docker Image"
echo "Image: ${IMAGE_NAME}:${TAG}"
echo "========================================"

docker buildx build \
    --platform linux/amd64,linux/arm64 \
    --no-cache \
    -t "${IMAGE_NAME}:${TAG}" \
    -f Dockerfile \
    .

echo ""
echo "========================================"
echo "Build completed successfully!"
echo "Image: ${IMAGE_NAME}:${TAG}"
echo "========================================"
echo ""
echo "This image can be used for any agent by mounting the agent directory."
echo ""
echo "Note: SDK and A2UI are installed from GitHub - no local files needed for build."
echo ""
echo "Quick start:"
echo "  docker run -d -p 10004:10004 \\"
echo "    -v /path/to/agent:/app/agent \\"
echo "    -v /path/to/skills:/app/skills \\"
echo "    --env-file /path/to/agent/.env \\"
echo "    ${IMAGE_NAME}:${TAG}"
echo ""
echo "See DOCKER.md for full documentation."
echo ""
