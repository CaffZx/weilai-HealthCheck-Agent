#!/bin/bash
set -a
source /opt/weilai-HealthCheck-Agent-v2.0/.env
set +a
cd /opt/weilai-HealthCheck-Agent-v2.0
for f in missing_chunks/chunk-*.json; do
  n=$(/opt/weilai-HealthCheck-Agent-v2.0/.venv/bin/python -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$f")
  echo "START $f count=$n" >> logs/missing-patrol-batch.log
  /opt/weilai-HealthCheck-Agent-v2.0/.venv/bin/python -m scripts.run_real_patrol_batch --units-file "$f" --count "$n" --concurrency 4 --skip-live-precheck >> logs/missing-patrol-batch.log 2>&1 || echo "FAILED $f" >> logs/missing-patrol-batch.log
  echo "END $f" >> logs/missing-patrol-batch.log
done
echo ALL_MISSING_PATROL_DONE >> logs/missing-patrol-batch.log
