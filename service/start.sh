#!/bin/bash
set -euo pipefail

shutdown() {
    echo "Shutting down..."
    kill "${GUNICORN_PID:-}" "${NGINX_PID:-}" "${VALKEY_PID:-}" 2>/dev/null || true
    wait 2>/dev/null || true
    echo "Shutdown complete"
    exit 0
}

trap shutdown SIGTERM SIGINT

# Inject CloudFront protection and static-file denial when STAGE=prod.
# In dev/local, nginx.conf is used as-is (no marker replacement).
configure_nginx() {
    local stage="${STAGE:-prod}"
    echo "Configuring Nginx for STAGE=${stage}..."

    # Copy template to the config location for runtime modification
    cp /app/nginx.conf /etc/nginx/nginx.conf

    if [ "$stage" = "prod" ]; then
        # Require CloudFront secret header in production
        if [ -z "${X_STATEMENT_CF:-}" ]; then
            echo "ERROR: X_STATEMENT_CF environment variable is required when STAGE=prod"
            exit 1
        fi

        # Verify the protection marker exists before replacing
        if ! grep -q "CLOUDFRONT_PROTECTION_MARKER" /etc/nginx/nginx.conf; then
            echo "ERROR: CLOUDFRONT_PROTECTION_MARKER not found in nginx.conf"
            exit 1
        fi

        # Inject CloudFront protection check
        sed -i '/# CLOUDFRONT_PROTECTION_MARKER/c\
        # CloudFront protection - reject requests without valid secret\
        if ($http_x_statement_cf != "'"$X_STATEMENT_CF"'") {\
            return 403;\
        }' /etc/nginx/nginx.conf

        if grep -q "http_x_statement_cf" /etc/nginx/nginx.conf; then
            echo "CloudFront protection added"
        else
            echo "ERROR: CloudFront protection injection failed"
            exit 1
        fi

        # Replace static file serving with 403 (CloudFront/S3 handles it)
        sed -i '/# CLOUDFRONT_SERVE_STATIC_START/,/# CLOUDFRONT_SERVE_STATIC_END/c\
        # CLOUDFRONT_SERVE_STATIC_START\
        # Static content served by CloudFront/S3 in production\
        location /static/ {\
            return 403;\
        }\
        # CLOUDFRONT_SERVE_STATIC_END' /etc/nginx/nginx.conf

        if grep -A2 "location /static/" /etc/nginx/nginx.conf | grep -q "return 403"; then
            echo "Static content configured for production (denied)"
        else
            echo "ERROR: Static content configuration failed"
            exit 1
        fi
    fi

    # Validate the final config
    nginx -t
    echo "Nginx configuration complete for STAGE=${stage}"
}

start_valkey() {
    echo "Starting Valkey..."
    if id valkey >/dev/null 2>&1; then
        su -s /bin/bash valkey -c "valkey-server /etc/valkey/valkey.conf" &
    else
        valkey-server /etc/valkey/valkey.conf &
    fi
    VALKEY_PID=$!

    # Wait for Valkey to accept connections, not just exist as a process.
    local retries=0
    local max_retries=30
    until valkey-cli ping 2>/dev/null | grep -q PONG; do
        retries=$((retries + 1))
        if [ "$retries" -ge "$max_retries" ]; then
            echo "ERROR: Valkey failed to accept connections after ${max_retries} attempts"
            exit 1
        fi
        sleep 0.2
    done
    echo "Valkey ready (PID ${VALKEY_PID}, ${retries} retries)"
}

start_gunicorn() {
    echo "Starting Gunicorn on unix:/tmp/flask.sock..."
    python3.13 -m gunicorn \
        --bind "unix:/tmp/flask.sock" \
        --workers 2 \
        --threads 8 \
        --worker-class gthread \
        --timeout 60 \
        --graceful-timeout 30 \
        --keep-alive 2 \
        --max-requests 50000 \
        --max-requests-jitter 5000 \
        --access-logfile - \
        --error-logfile - \
        app:app &
    GUNICORN_PID=$!
    echo "Gunicorn started (PID ${GUNICORN_PID})"

    # Wait for Gunicorn to create the Unix socket before Nginx tries to proxy
    local retries=0
    until [ -S /tmp/flask.sock ]; do
        retries=$((retries + 1))
        if [ "$retries" -ge 30 ]; then
            echo "ERROR: Gunicorn socket not created after 30 attempts"
            exit 1
        fi
        sleep 0.2
    done
    echo "Gunicorn socket ready (${retries} retries)"
}

start_nginx() {
    echo "Starting Nginx..."
    nginx -g "daemon off;" &
    NGINX_PID=$!
    echo "Nginx started (PID ${NGINX_PID})"
}

wait_for_http() {
    local retries=0
    local max_retries=30
    local stage="${STAGE:-prod}"

    # In prod, Nginx rejects requests without the CloudFront header,
    # so check Gunicorn directly via the Unix socket instead.
    if [ "$stage" = "prod" ]; then
        echo "Waiting for Gunicorn readiness via socket..."
        until curl -sf --unix-socket /tmp/flask.sock http://localhost/healthz -o /dev/null; do
            retries=$((retries + 1))
            if [ "$retries" -ge "$max_retries" ]; then
                echo "WARNING: Gunicorn did not pass readiness after ${max_retries}s"
                return 0
            fi
            sleep 1
        done
        echo "Gunicorn ready via socket (${retries}s)"
        return 0
    fi

    local port=8080
    echo "Waiting for HTTP readiness on port ${port}..."
    until curl -sf -o /dev/null "http://127.0.0.1:${port}/"; do
        retries=$((retries + 1))
        if [ "$retries" -ge "$max_retries" ]; then
            echo "WARNING: App did not pass HTTP readiness after ${max_retries}s"
            return 0
        fi
        sleep 1
    done
    local status
    status=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${port}/")
    echo "App ready: HTTP ${status} (${retries}s)"
}

echo "=== Container startup ==="
configure_nginx
start_valkey
start_gunicorn
start_nginx
wait_for_http
echo "=== All services running ==="

wait -n "${VALKEY_PID}" "${GUNICORN_PID}" "${NGINX_PID}"
shutdown
