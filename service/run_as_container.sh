#!/bin/bash
set -euo pipefail

IMAGE_NAME="statement-processor"
CONTAINER_NAME="statement-processor"
PORT="${PORT:-8080}"

INTERACTIVE=false
if [[ "${1:-}" == "-i" || "${1:-}" == "--interactive" ]]; then
    INTERACTIVE=true
fi

if docker ps -q -f "name=^${CONTAINER_NAME}$" >/dev/null && [[ -n "$(docker ps -q -f "name=^${CONTAINER_NAME}$")" ]]; then
    echo "Stopping running container: ${CONTAINER_NAME}"
    docker stop "${CONTAINER_NAME}" >/dev/null
fi

if docker ps -aq -f "name=^${CONTAINER_NAME}$" >/dev/null && [[ -n "$(docker ps -aq -f "name=^${CONTAINER_NAME}$")" ]]; then
    echo "Removing existing container: ${CONTAINER_NAME}"
    docker rm "${CONTAINER_NAME}" >/dev/null
fi

echo "Building Docker image: ${IMAGE_NAME}"
# Build context is the repo root so the shared common/ package is accessible.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
docker build -t "${IMAGE_NAME}" -f "${SCRIPT_DIR}/Dockerfile" "${REPO_ROOT}"

DOCKER_ARGS=(--name "${CONTAINER_NAME}" --env-file .env -p "${PORT}:8080")
if [[ -d "${HOME}/.aws" ]]; then
    DOCKER_ARGS+=(-v "${HOME}/.aws:/root/.aws:ro")
fi

if [[ "${INTERACTIVE}" == true ]]; then
    echo "Running container in interactive mode"
    docker run -it "${DOCKER_ARGS[@]}" "${IMAGE_NAME}" /bin/bash
else
    echo "Running container on http://localhost:${PORT}"
    docker run -d "${DOCKER_ARGS[@]}" "${IMAGE_NAME}" >/dev/null
    docker logs -f "${CONTAINER_NAME}"
fi
