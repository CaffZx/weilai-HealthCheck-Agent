#!/bin/bash
# 全量子 ASIN 补抓：分两批顺序执行并写日志
set -euo pipefail
cd /opt/weilai-HealthCheck-Agent-v2.0
LOG_DIR=logs
mkdir -p "$LOG_DIR"
TOTAL=4892
HALF=$((TOTAL / 2))
PY=.venv/bin/python
COMMON="--all-units --force --concurrency 12 --poll-interval 30 --wait-seconds 7200"

echo "=== batch1 start $(date -Is) ===" | tee -a "$LOG_DIR/child-fact-full-2batch.log"
$PY -m scripts.requeue_child_fact_batch --count "$HALF" --offset 0 \
  --batch-name batch1 $COMMON \
  2>&1 | tee -a "$LOG_DIR/child-fact-batch1.log"
B1=$?
echo "=== batch1 exit=$B1 $(date -Is) ===" | tee -a "$LOG_DIR/child-fact-full-2batch.log"

echo "=== batch2 start $(date -Is) ===" | tee -a "$LOG_DIR/child-fact-full-2batch.log"
$PY -m scripts.requeue_child_fact_batch --count "$HALF" --offset "$HALF" \
  --batch-name batch2 $COMMON \
  2>&1 | tee -a "$LOG_DIR/child-fact-batch2.log"
B2=$?
echo "=== batch2 exit=$B2 $(date -Is) ===" | tee -a "$LOG_DIR/child-fact-full-2batch.log"

echo "ALL_DONE batch1=$B1 batch2=$B2" | tee -a "$LOG_DIR/child-fact-full-2batch.log"
