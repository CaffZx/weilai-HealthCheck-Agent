#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-${PROJECT_ROOT}/dist}"
STAMP="$(date +%Y%m%d-%H%M%S)"
RELEASE_DIR="${OUTPUT_DIR}/weilai-HealthCheck-Agent-${STAMP}"
ARCHIVE="${OUTPUT_DIR}/weilai-HealthCheck-Agent-${STAMP}.tar.gz"
PYTHON="${PYTHON:-python3}"

mkdir -p "${OUTPUT_DIR}"
rm -rf "${RELEASE_DIR}"
mkdir -p "${RELEASE_DIR}/data/local"

rsync -a \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='venv/' \
  --exclude='node_modules/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='data/local/' \
  --exclude='logs/*.log' \
  --exclude='dist/' \
  --exclude='.DS_Store' \
  --exclude='.pytest_cache/' \
  --exclude='.claude/' \
  --exclude='.ruff_cache/' \
  "${PROJECT_ROOT}/" "${RELEASE_DIR}/"

# 数据默认不打包（代码包）。需要连数据一起打时：INCLUDE_DATA=1 bash scripts/build_release.sh
if [[ "${INCLUDE_DATA:-0}" == "1" ]]; then
  if [[ -f "${PROJECT_ROOT}/data/local/healthcheck.db" ]]; then
    "${PYTHON}" - "${PROJECT_ROOT}/data/local/healthcheck.db" "${RELEASE_DIR}/data/local/healthcheck.db" <<'PY'
import sqlite3
import sys

source, target = sys.argv[1:]
source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
target_conn = sqlite3.connect(target)
try:
    source_conn.backup(target_conn)
finally:
    target_conn.close()
    source_conn.close()
PY
  fi
  if [[ -f "${PROJECT_ROOT}/data/local/batch_cache.json" ]]; then
    cp "${PROJECT_ROOT}/data/local/batch_cache.json" "${RELEASE_DIR}/data/local/batch_cache.json"
  fi
  DB_NOTE="included as a consistent SQLite backup"
else
  DB_NOTE="NOT included (code-only); server runs init_db on startup"
fi

cat > "${RELEASE_DIR}/PACKAGE_INFO.txt" <<EOF
Package: weilai-HealthCheck-Agent
Built: ${STAMP}
Runtime database: ${DB_NOTE}
Secrets: excluded; copy .env.example to .env and fill in deployment values
Start: ./start.sh
EOF

tar -czf "${ARCHIVE}" -C "${OUTPUT_DIR}" "$(basename "${RELEASE_DIR}")"
rm -rf "${RELEASE_DIR}"
printf '%s\n' "${ARCHIVE}"
