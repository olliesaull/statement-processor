#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_FILE="$SCRIPT_DIR/sample_statement.png"
TARGET_DIR="$SCRIPT_DIR/../../service/static/assets/images"

mkdir -p "$TARGET_DIR"
cp "$SOURCE_FILE" "$TARGET_DIR/"
