#!/usr/bin/env bash
# run.sh — start the video-compression-agent. Run install.sh first if you
# haven't already.
#
# Usage:
#   ./run.sh                              # 127.0.0.1:8000
#   ./run.sh --host 0.0.0.0 --port 8765
#   ./run.sh --reload                     # auto-reload on code change

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ ! -d ".venv" ]]; then
    echo "No .venv/ found — run ./install.sh first." >&2
    exit 1
fi

# shellcheck disable=SC1091
source ".venv/bin/activate"

HOST="127.0.0.1"
PORT="8000"
RELOAD=""
EXTRA=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)   HOST="$2"; shift 2 ;;
        --port)   PORT="$2"; shift 2 ;;
        --reload) RELOAD="--reload"; shift ;;
        *)        EXTRA+=("$1"); shift ;;
    esac
done

exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT" $RELOAD "${EXTRA[@]}"
