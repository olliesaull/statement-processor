#!/bin/bash

# Suppress typeguard warnings from AWS CDK when using Python 3.13.
# TODO: Remove this suppression once AWS CDK/typeguard fully support Python 3.13
export PYTHONWARNINGS="ignore:Typeguard cannot check:UserWarning"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Use the repo-local CDK virtualenv so deploys do not depend on the caller's shell
# state. Prefer `venv/` because that is what this repo currently uses for CDK.
if [ -f "${SCRIPT_DIR}/venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/venv/bin/activate"
elif [ -f "${SCRIPT_DIR}/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/.venv/bin/activate"
fi

PROFILE=""

# Parse required CLI args once up front so deploy target is explicit.
while [ "$#" -gt 0 ]; do
    case "$1" in
        --profile)
            PROFILE="${2:-}"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: ./deploy_stack.sh --profile {dotelastic-dev|dotelastic-production}"
            exit 1
            ;;
    esac
done

# Enforce explicit profile selection to avoid accidental deploys to the wrong account.
if [ -z "$PROFILE" ]; then
    echo "Error: --profile option is required"
    echo "Usage: ./deploy_stack.sh --profile {dotelastic-dev|dotelastic-production}"
    exit 1
fi

# Restrict deploy profiles to known AWS named profiles used by this project.
if [ "$PROFILE" != "dotelastic-dev" ] && [ "$PROFILE" != "dotelastic-production" ]; then
    echo "Invalid profile: $PROFILE"
    echo "Expected one of: dotelastic-dev, dotelastic-production"
    exit 1
fi

# Derive stack and bucket names from profile so downstream commands stay deterministic.
if [ "$PROFILE" = "dotelastic-production" ]; then
    STAGE_SUFFIX="Prod"
    STAGE_BUCKET_SUFFIX="prod"
else
    STAGE_SUFFIX="Dev"
    STAGE_BUCKET_SUFFIX="dev"
fi

STACK_NAME="StatementProcessorStack${STAGE_SUFFIX}"
ASSETS_BUCKET_NAME="dexero-statement-processor-${STAGE_BUCKET_SUFFIX}-assets"
BUILDX_BUILDER_NAME="multiarch"
AWS_REGION="eu-west-1"
DOCKER_STEP_TIMEOUT_SECONDS=60
DOCKER_BOOTSTRAP_TIMEOUT_SECONDS=120
SELECTED_BUILDX_BUILDER=""

run_timed_step() {
    # Bound long-running docker/bootstrap commands so deploys fail loudly instead of appearing hung.
    local timeout_seconds="$1"
    local description="$2"
    shift 2

    echo " - ${description}"
    if timeout "${timeout_seconds}s" "$@"; then
        return 0
    else
        local exit_code=$?
        if [ "$exit_code" -eq 124 ]; then
            echo "Error: ${description} timed out after ${timeout_seconds}s."
        else
            echo "Error: ${description} failed (exit code ${exit_code})."
        fi
        return "$exit_code"
    fi
}

current_buildx_builder() {
    docker buildx ls | awk '$1 ~ /\*/ { gsub(/\*/, "", $1); print $1; exit }'
}

builder_supports_arm64() {
    # Prefer a builder that already advertises linux/arm64 so we avoid stale custom builders.
    local builder_name="$1"

    if [ -z "$builder_name" ]; then
        return 1
    fi

    docker buildx inspect "$builder_name" 2>/dev/null | grep -q "linux/arm64"
}

use_buildx_builder() {
    local builder_name="$1"

    if ! docker buildx use "$builder_name" >/dev/null; then
        echo "Error: failed to switch to docker buildx builder '$builder_name'."
        exit 1
    fi

    SELECTED_BUILDX_BUILDER="$builder_name"
    echo " - Using docker buildx builder '${SELECTED_BUILDX_BUILDER}'."
}

ensure_buildx_builder() {
    local current_builder=""
    current_builder="$(current_buildx_builder)"

    if builder_supports_arm64 "$current_builder"; then
        SELECTED_BUILDX_BUILDER="$current_builder"
        echo " - Reusing active docker buildx builder '${SELECTED_BUILDX_BUILDER}' (already supports linux/arm64)."
        return 0
    fi

    if builder_supports_arm64 "default"; then
        use_buildx_builder "default"
        return 0
    fi

    if docker buildx inspect "$BUILDX_BUILDER_NAME" >/dev/null 2>&1; then
        use_buildx_builder "$BUILDX_BUILDER_NAME"
        return 0
    fi

    if ! run_timed_step "$DOCKER_STEP_TIMEOUT_SECONDS" "Creating docker buildx builder '${BUILDX_BUILDER_NAME}'" \
        docker buildx create --name "$BUILDX_BUILDER_NAME" --use --driver docker-container; then
        echo "Error: failed to create docker buildx builder '$BUILDX_BUILDER_NAME'."
        exit 1
    fi

    SELECTED_BUILDX_BUILDER="$BUILDX_BUILDER_NAME"
}

arm64_runtime_available() {
    # Probe runtime support first so we can skip privileged binfmt work when emulation already works.
    local architecture=""
    echo " - Probing linux/arm64 container runtime (may pull alpine on first run)..."
    if architecture="$(timeout "${DOCKER_STEP_TIMEOUT_SECONDS}s" docker run --rm --platform linux/arm64 alpine uname -m 2>/dev/null)"; then
        [ "$architecture" = "aarch64" ]
    else
        return 1
    fi
}

validate_arm64_runtime() {
    local architecture=""

    echo " - Validating linux/arm64 container runtime..."
    if architecture="$(timeout "${DOCKER_STEP_TIMEOUT_SECONDS}s" docker run --rm --platform linux/arm64 alpine uname -m)"; then
        :
    else
        local exit_code=$?
        if [ "$exit_code" -eq 124 ]; then
            echo "Error: linux/arm64 runtime validation timed out after ${DOCKER_STEP_TIMEOUT_SECONDS}s."
        else
            echo "Error: linux/arm64 runtime validation failed (exit code ${exit_code})."
        fi
        return "$exit_code"
    fi

    if [ "$architecture" != "aarch64" ]; then
        echo "Error: linux/arm64 runtime validation returned '${architecture}' instead of 'aarch64'."
        return 1
    fi

    echo "   linux/arm64 runtime reported '${architecture}'."
}

preflight_multiarch() {
    # ARM Lambdas require linux/arm64 images. This preflight ensures the local Docker
    # host can build and run arm64 containers before we start a long CDK deploy.
    echo "Checking Docker multi-arch support (linux/arm64)..."

    if ! command -v docker >/dev/null 2>&1; then
        echo "Error: docker is required but was not found in PATH."
        exit 1
    fi

    if ! docker info >/dev/null 2>&1; then
        echo "Error: docker daemon is not reachable. Start Docker Desktop/daemon and retry."
        exit 1
    fi

    if ! docker buildx version >/dev/null 2>&1; then
        echo "Error: docker buildx is required for multi-arch builds but is not available."
        exit 1
    fi

    ensure_buildx_builder

    if ! run_timed_step "$DOCKER_BOOTSTRAP_TIMEOUT_SECONDS" "Bootstrapping docker buildx builder '${SELECTED_BUILDX_BUILDER}'" \
        docker buildx inspect "$SELECTED_BUILDX_BUILDER" --bootstrap; then
        echo "Error: failed to bootstrap docker buildx builder '${SELECTED_BUILDX_BUILDER}'."
        exit 1
    fi

    if arm64_runtime_available; then
        echo " - Existing linux/arm64 runtime already works; skipping binfmt refresh."
    else
        if ! run_timed_step "$DOCKER_STEP_TIMEOUT_SECONDS" "Installing/refreshing arm64 binfmt handlers" \
            docker run --privileged --rm tonistiigi/binfmt --install arm64; then
            echo "Error: failed to install/refresh arm64 binfmt support."
            echo "Try running manually: docker run --privileged --rm tonistiigi/binfmt --install arm64"
            exit 1
        fi
    fi

    # Final runtime smoke test: if this fails, asset image builds will fail later anyway.
    if ! validate_arm64_runtime; then
        echo "Error: linux/arm64 runtime validation failed."
        echo "Your host cannot currently execute arm64 containers required by this deploy."
        exit 1
    fi

    echo "Multi-arch preflight OK."
}

deploy() {
    # Fail fast on local build capability before interacting with CloudFormation.
    # Secrets are now fetched from SSM at container startup by the Flask app, so
    # no secret resolution is needed here at deploy time.
    preflight_multiarch

    echo "Deploying stack ${STACK_NAME} with profile ${PROFILE}..."
    cdk deploy "$STACK_NAME" --profile "$PROFILE"

    # Push static assets after infra/app deploy so CloudFront serves latest files.
    echo "Syncing static assets to s3://${ASSETS_BUCKET_NAME}/static/..."
    aws s3 sync ../service/static "s3://${ASSETS_BUCKET_NAME}/static/" --delete --profile "$PROFILE"
}

# Keep an explicit manual confirmation step for production deploys.
if [ "$PROFILE" = "dotelastic-production" ]; then
    echo "Are you sure you want to deploy to ${PROFILE}? (Y/n): "
    read -r response
    if [ "$response" = "Y" ]; then
        deploy
    else
        echo "Deployment aborted."
        exit 1
    fi
else
    deploy
fi
