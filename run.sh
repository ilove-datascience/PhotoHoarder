#!/usr/bin/env bash
set -euo pipefail

# Move to the folder where this script is located
cd "$(dirname "$0")"

PYTHON_EXE="./.venv/bin/python"

if [ ! -x "$PYTHON_EXE" ]; then
  echo "[WARN] Virtual environment Python not found at:"
  echo "        $PYTHON_EXE"
  echo
  echo "Attempting to run 'uv sync' to create the virtualenv and install deps..."
  if command -v uv >/dev/null 2>&1; then
    uv sync
  else
    echo "[ERROR] 'uv' command not found. Please install uv or run 'uv sync' manually."
    exit 1
  fi

  # Re-check after attempting setup
  if [ ! -x "$PYTHON_EXE" ]; then
    echo "[ERROR] Virtual environment still missing after 'uv sync'. Please run 'uv sync' manually."
    exit 1
  fi
fi

exec "$PYTHON_EXE" main.py "$@"
