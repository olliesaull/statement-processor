#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "-------------------- Running MyPy --------------------"
mypy_status=0
python -m mypy main.py config.py exceptions.py core || mypy_status=$?

echo "-------------------- Running Pylint --------------------"
pylint_status=0
python -m pylint main.py config.py exceptions.py core || pylint_status=$?

if [ "$mypy_status" -ne 0 ] || [ "$pylint_status" -ne 0 ]; then
  exit 1
fi
