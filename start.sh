#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_ROOT}"

if [[ -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${PROJECT_ROOT}/.venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

exec "${PYTHON}" -m uvicorn web.backend.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
