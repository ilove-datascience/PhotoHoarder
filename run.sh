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

# Ensure persistent data directories exist (Railway uses /data)
DATA_DIR=${CREDS_DIR:-}
if [ -z "$DATA_DIR" ]; then
  if [ -n "${RAILWAY_ENVIRONMENT:-}" ]; then
    DATA_DIR="/data"
  else
    DATA_DIR="./config"
  fi
fi

mkdir -p "$DATA_DIR"
echo "Using data dir: $DATA_DIR"

# Ensure configured paths exist
GOOGLE_CLIENT_SECRET_PATH=${GOOGLE_CLIENT_SECRET_PATH:-"$DATA_DIR/client_secret.json"}
GOOGLE_TOKEN_PATH=${GOOGLE_TOKEN_PATH:-"$DATA_DIR/token.json"}
CREDS_DIR=${CREDS_DIR:-$DATA_DIR}

mkdir -p "$(dirname "$GOOGLE_CLIENT_SECRET_PATH")"
mkdir -p "$(dirname "$GOOGLE_TOKEN_PATH")"
mkdir -p "$CREDS_DIR"

# If client secret JSON provided via env, write it to the configured path
if [ -n "${GOOGLE_CLIENT_SECRET_JSON:-}" ]; then
  if [ ! -s "$GOOGLE_CLIENT_SECRET_PATH" ]; then
    printf '%s' "$GOOGLE_CLIENT_SECRET_JSON" > "$GOOGLE_CLIENT_SECRET_PATH"
    chmod 600 "$GOOGLE_CLIENT_SECRET_PATH" || true
    echo "Wrote GOOGLE_CLIENT_SECRET_JSON to $GOOGLE_CLIENT_SECRET_PATH"
  else
    echo "Client secret file already exists at $GOOGLE_CLIENT_SECRET_PATH; not overwriting"
  fi
fi

exec "$PYTHON_EXE" main.py "$@"
