#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-${PROJECT_ROOT}/dist}"
STAMP="$(date +%Y%m%d-%H%M%S)"
RELEASE_DIR="${OUTPUT_DIR}/weilai-HealthCheck-Agent-source-${STAMP}"
ARCHIVE="${OUTPUT_DIR}/weilai-HealthCheck-Agent-source-${STAMP}.tar.gz"

mkdir -p "${OUTPUT_DIR}"
rm -rf "${RELEASE_DIR}"
mkdir -p "${RELEASE_DIR}"

rsync -a \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='venv/' \
  --exclude='node_modules/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='.DS_Store' \
  --exclude='.claude/' \
  --exclude='data/local/' \
  --exclude='logs/' \
  --exclude='dist/' \
  --exclude='*.db' \
  --exclude='*.db-wal' \
  --exclude='*.db-shm' \
  --exclude='batch_cache.json' \
  --exclude='docker-compose*.yml' \
  --exclude='docker-compose*.yaml' \
  --exclude='Dockerfile*' \
  --exclude='*.tar.gz' \
  --exclude='amazon-agent-obvious-issue-scan-report.md' \
  --exclude='docs/' \
  --exclude='README.md' \
  --exclude='使用说明.md' \
  --exclude='logs/health-report-*.json' \
  "${PROJECT_ROOT}/" "${RELEASE_DIR}/"

cat > "${RELEASE_DIR}/PACKAGE_INFO.txt" <<EOF
Package: weilai-HealthCheck-Agent source-only
Built: ${STAMP}
Included: application source, frontend, rules, knowledge, config templates, requirements
Excluded: Docker files, Git metadata, runtime database/cache, logs, reports, environment secrets
Start: ./start.sh
Setup: copy .env.example to .env and fill in deployment values
EOF

tar -czf "${ARCHIVE}" -C "${OUTPUT_DIR}" "$(basename "${RELEASE_DIR}")"
rm -rf "${RELEASE_DIR}"
printf '%s\n' "${ARCHIVE}"
