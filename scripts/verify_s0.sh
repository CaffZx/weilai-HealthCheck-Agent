#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PROJECT_ROOT}/.venv/bin/python"
LOCK_FILE="${PROJECT_ROOT}/requirements.lock"
EXPECTED_LOCK_SHA256="2a8103d7e9583c61ce12f9bf29135eb2b19399a513705dacafcf0b3c37358530"
UV="${PROJECT_ROOT}/runtime/bin/uv"

if [[ ! -x "${UV}" ]]; then
  UV="$(command -v uv || true)"
fi

if [[ ! -x "${PYTHON}" ]]; then
  echo "缺少 .venv；请按 README 使用 Python 3.12.11 和 requirements.lock 安装。" >&2
  exit 2
fi

"${PYTHON}" -c 'import sys; assert sys.version_info[:3] == (3, 12, 11), sys.version'
ACTUAL_LOCK_SHA256="$(openssl dgst -sha256 "${LOCK_FILE}" | awk '{print $NF}')"
if [[ "${ACTUAL_LOCK_SHA256}" != "${EXPECTED_LOCK_SHA256}" ]]; then
  echo "requirements.lock SHA-256 不匹配：${ACTUAL_LOCK_SHA256}" >&2
  exit 3
fi

EXPECTED_PACKAGES="$(mktemp /tmp/patrol-s0-expected.XXXXXX)"
ACTUAL_PACKAGES="$(mktemp /tmp/patrol-s0-actual.XXXXXX)"
trap 'rm -f "${EXPECTED_PACKAGES}" "${ACTUAL_PACKAGES}"' EXIT
sed -nE 's/^([A-Za-z0-9_.-]+)==([^[:space:]\\]+).*/\1==\2/p' "${LOCK_FILE}" \
  | LC_ALL=C sort -f > "${EXPECTED_PACKAGES}"
if [[ -z "${UV}" ]]; then
  echo "缺少 uv；无法校验依赖锁定状态。" >&2
  exit 2
fi
UV_CACHE_DIR="${PROJECT_ROOT}/.uv-cache" "${UV}" pip freeze --python "${PYTHON}" \
  | LC_ALL=C sort -f > "${ACTUAL_PACKAGES}"
if ! diff -u "${EXPECTED_PACKAGES}" "${ACTUAL_PACKAGES}"; then
  echo "当前 .venv 与 requirements.lock 不一致。" >&2
  exit 4
fi

cd "${PROJECT_ROOT}"
"${PYTHON}" -m scripts.export_contracts --check
"${PYTHON}" -m pytest tests/contract -q
"${PYTHON}" -m pytest tests/unit/test_legacy_regression.py -q
"${PYTHON}" -m pytest tests -q
