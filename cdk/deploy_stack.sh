#!/bin/bash

# Suppress typeguard warnings from AWS CDK when using Python 3.13.
# TODO: Remove this suppression once AWS CDK/typeguard fully support Python 3.13
export PYTHONWARNINGS="ignore:Typeguard cannot check:UserWarning"

set -euo pipefail

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

    # Refresh binfmt/qemu handlers so an x86 host can execute arm64 container steps.
    if ! docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null 2>&1; then
        echo "Error: failed to install/refresh arm64 binfmt support."
        echo "Try running manually: docker run --privileged --rm tonistiigi/binfmt --install arm64"
        exit 1
    fi

    # Reuse a persistent buildx builder when available, otherwise create one.
    if docker buildx inspect "$BUILDX_BUILDER_NAME" >/dev/null 2>&1; then
        docker buildx use "$BUILDX_BUILDER_NAME" >/dev/null
    else
        if ! docker buildx create --name "$BUILDX_BUILDER_NAME" --use --driver docker-container >/dev/null 2>&1; then
            echo "Error: failed to create docker buildx builder '$BUILDX_BUILDER_NAME'."
            exit 1
        fi
    fi

    if ! docker buildx inspect --bootstrap >/dev/null 2>&1; then
        echo "Error: failed to bootstrap docker buildx builder."
        exit 1
    fi

    # Final runtime smoke test: if this fails, asset image builds will fail later anyway.
    if ! docker run --rm --platform linux/arm64 alpine uname -m | grep -q "aarch64"; then
        echo "Error: linux/arm64 runtime validation failed (expected 'aarch64')."
        echo "Your host cannot currently execute arm64 containers required by this deploy."
        exit 1
    fi

    echo "Multi-arch preflight OK."
}

fetch_secure_parameter() {
    # Fetch one decrypted SecureString value from SSM at deploy time.
    # We pass these into CDK as process env vars because Lambda env vars
    # cannot use ssm-secure dynamic references directly in CloudFormation.
    local parameter_name="$1"
    local value

    if ! value="$(aws ssm get-parameter --name "$parameter_name" --with-decryption --query "Parameter.Value" --output text --region "$AWS_REGION" --profile "$PROFILE")"; then
        echo "Error: failed to read secure SSM parameter '$parameter_name' for profile '$PROFILE' in region '$AWS_REGION'."
        exit 1
    fi

    if [ -z "$value" ]; then
        echo "Error: SSM parameter '$parameter_name' is empty."
        exit 1
    fi

    printf '%s' "$value"
}

deploy() {
    # Fail fast on local build capability before interacting with CloudFormation.
    preflight_multiarch

    # Resolve runtime secrets once at deploy time (not at Lambda cold start).
    echo "Resolving deploy-time secrets from SSM Parameter Store..."
    XERO_CLIENT_ID="$(fetch_secure_parameter "/StatementProcessor/XERO_CLIENT_ID")"
    XERO_CLIENT_SECRET="$(fetch_secure_parameter "/StatementProcessor/XERO_CLIENT_SECRET")"
    SESSION_FERNET_KEY="$(fetch_secure_parameter "/StatementProcessor/SESSION_FERNET_KEY")"
    FLASK_SECRET_KEY="$(fetch_secure_parameter "/StatementProcessor/FLASK_SECRET_KEY")"
    echo "Deploy-time secrets resolved."

    # Scope secrets to this single cdk process by using inline env assignment.
    # This avoids persisting them in shell startup files or global environment.
    echo "Deploying stack ${STACK_NAME} with profile ${PROFILE}..."
    XERO_CLIENT_ID="$XERO_CLIENT_ID" \
        XERO_CLIENT_SECRET="$XERO_CLIENT_SECRET" \
        SESSION_FERNET_KEY="$SESSION_FERNET_KEY" \
        FLASK_SECRET_KEY="$FLASK_SECRET_KEY" \
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
