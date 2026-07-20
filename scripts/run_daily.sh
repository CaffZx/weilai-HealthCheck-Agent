#!/bin/bash
# 每天 02:00 定时任务的编排脚本：
#   1. 前台跑 sync_all（后台进程）
#   2. sync 结束 → 立即触发批量巡检 → 立即触发覆盖率检查
#   3. 兜底：到 07:00 sync 还没跑完 → 强制触发巡检（sync 后台继续跑）
#      逻辑上运营 9 点上班前必须有结果，不能死等 sync
#
# 用法（crontab）：
#   0 2 * * * /opt/weilai-HealthCheck-Agent/scripts/run_daily.sh
#
# 日志：
#   logs/cron-orch-<date>.log —— 编排脚本自己的日志
#   logs/cron-sync-<date>.log —— sync 子任务日志（cron_wrapper.sh 产出）
#   logs/cron-inspect-<date>.log
#   logs/cron-health-<date>.log

set -o pipefail

PROJECT_ROOT="/opt/weilai-HealthCheck-Agent"
LOG_DIR="${PROJECT_ROOT}/logs"
DATE=$(date +%Y-%m-%d)
LOG_FILE="${LOG_DIR}/cron-orch-${DATE}.log"
LOCK_FILE="/tmp/weilai-healthcheck-orch.lock"
mkdir -p "${LOG_DIR}"

# 防重入：如果已经在跑，直接退出（避免手动测试/异常时重复触发）
exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
  echo "$(date -Iseconds) [WARN ] [orch.skip] 上一次 orch 仍在运行，本次跳过（避免重叠）" >> "${LOG_FILE}"
  exit 0
fi

# 兜底截止时间：07:00
# 用 date +%H%M 拿本地时间，如果起脚本时已经过 07:00（补跑场景），直接跳过兜底逻辑
DEADLINE_HHMM=700   # 07:00

_log() {
  # $1=LEVEL $2=EVENT $3+ 剩余
  local level="$1"; local event="$2"; shift 2
  echo "$(date -Iseconds) [${level}] [orch.${event}] $*" >> "${LOG_FILE}"
}

_log "INFO " "begin" "cmd=\"run_daily.sh\" pid=$$"

# ---- 后台跑 sync ----
"${PROJECT_ROOT}/scripts/cron_wrapper.sh" sync "${PROJECT_ROOT}/.venv/bin/python" -u -m data.sync_all &
SYNC_PID=$!
_log "INFO " "sync_started" "pid=${SYNC_PID}"

# ---- 循环等待 sync 结束或到点 ----
while kill -0 "${SYNC_PID}" 2>/dev/null; do
  NOW_HHMM=$(date +%H%M)
  # 去掉前导 0 转成 int 便于比较
  NOW_HHMM_INT=$((10#$NOW_HHMM))
  if [ ${NOW_HHMM_INT} -ge ${DEADLINE_HHMM} ]; then
    _log "WARN " "timeout" "到 07:00 sync 仍未结束，触发兜底巡检；sync 进程 ${SYNC_PID} 后台继续跑"
    break
  fi
  sleep 60
done

# 如果 sync 是自然结束（不是超时 break），记录状态
if ! kill -0 "${SYNC_PID}" 2>/dev/null; then
  wait "${SYNC_PID}"
  SYNC_RC=$?
  _log "INFO " "sync_finished" "exit=${SYNC_RC}"
fi

# ---- 触发批量巡检（POST）----
_log "INFO " "inspect_trigger" "POST /api/batch/inspect"
"${PROJECT_ROOT}/scripts/cron_wrapper.sh" inspect curl --fail-with-body -s -X POST http://127.0.0.1:8000/api/batch/inspect

# 批量巡检接口只负责启动后台任务；必须等待 ready 后再做覆盖率检查，
# 否则健康报告可能先于巡检完成，且前端仍看到上一批结果。
INSPECT_DEADLINE=$(( $(date +%s) + 7200 ))
while true; do
  STATUS=$(curl --fail-with-body -s http://127.0.0.1:8000/api/batch/status || true)
  if [[ "${STATUS}" == *'"ready":true'* ]]; then
    _log "INFO " "inspect_finished" "status=${STATUS}"
    break
  fi
  if [ "$(date +%s)" -ge "${INSPECT_DEADLINE}" ]; then
    _log "ERROR" "inspect_timeout" "status=${STATUS}"
    exit 1
  fi
  sleep 30
done

# ---- 触发覆盖率检查 ----
_log "INFO " "health_trigger" "python -m data.check_coverage --days 3"
"${PROJECT_ROOT}/scripts/cron_wrapper.sh" health "${PROJECT_ROOT}/.venv/bin/python" -u -m data.check_coverage --days 3

_log "INFO " "end" "done"
