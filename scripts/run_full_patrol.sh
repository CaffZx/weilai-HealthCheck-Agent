#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PATROL_PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
LOG_DIR="${PATROL_LOG_DIR:-$ROOT_DIR/logs}"
LOCK_DIR="${PATROL_LOCK_DIR:-$ROOT_DIR/run/full-patrol.lock}"
CONCURRENCY="${PATROL_CONCURRENCY:-4}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "python runtime not found: $PYTHON_BIN" >&2
  exit 1
fi

mkdir -p "$LOG_DIR" "$(dirname "$LOCK_DIR")"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "full patrol is already running: $LOCK_DIR" >&2
  exit 2
fi
cleanup() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

cd "$ROOT_DIR"

run_id="$(date +%Y%m%d-%H%M%S)"
log_file="$LOG_DIR/full-patrol-$run_id.log"
exec > >(tee -a "$log_file") 2>&1

echo "full patrol started run_id=$run_id concurrency=$CONCURRENCY"
echo "refreshing the complete ERP operating-unit catalog"
"$PYTHON_BIN" -m scripts.refresh_operating_unit_catalog
echo "running every catalog operating unit in independent batches"
"$PYTHON_BIN" -m scripts.run_real_patrol_batch \
  --all-active \
  --concurrency "$CONCURRENCY" \
  --skip-live-precheck
echo "full patrol finished run_id=$run_id"
