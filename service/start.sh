#!/bin/bash
set -euo pipefail

shutdown() {
    echo "Shutting down..."
    kill "${GUNICORN_PID:-}" "${VALKEY_PID:-}" 2>/dev/null || true
    wait 2>/dev/null || true
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
    sleep 1
    if ! kill -0 "${VALKEY_PID}" 2>/dev/null; then
        echo "Valkey failed to start"
        exit 1
    fi
}

start_gunicorn() {
    local bind_port="${PORT:-8080}"
    echo "Starting Gunicorn on port ${bind_port}..."
    python3.13 -m gunicorn --bind "0.0.0.0:${bind_port}" app:app &
    GUNICORN_PID=$!
}

start_valkey
start_gunicorn

wait -n "${VALKEY_PID}" "${GUNICORN_PID}"
shutdown
