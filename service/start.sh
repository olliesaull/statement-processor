#!/bin/bash
set -euo pipefail

shutdown() {
    echo "Shutting down..."
    kill "${GUNICORN_PID:-}" "${VALKEY_PID:-}" 2>/dev/null || true
    wait 2>/dev/null || true
    echo "Shutdown complete"
    exit 0
}

trap shutdown SIGTERM SIGINT

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
    local bind_port="${PORT:-8080}"
    echo "Starting Gunicorn on port ${bind_port}..."
    python3.13 -m gunicorn \
        --bind "0.0.0.0:${bind_port}" \
        --workers 2 \
        --threads 4 \
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
}

wait_for_http() {
    local port="${PORT:-8080}"
    local retries=0
    local max_retries=30
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
start_valkey
start_gunicorn
wait_for_http
echo "=== All services running ==="

wait -n "${VALKEY_PID}" "${GUNICORN_PID}"
shutdown