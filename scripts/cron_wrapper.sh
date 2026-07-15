#!/bin/bash
# cron 任务统一包装：加时间戳 + begin/end 标记 + 状态码 + 按天滚动日志
#
# 用法：
#   cron_wrapper.sh <task_name> <cmd> [args...]
#
# 日志格式（每行）：
#   <ISO8601-tz> [<LEVEL>] [<task>.<event>] <k1>=<v1> ...
#
# 例：
#   2026-07-13T02:00:01+08:00 [INFO ] [sync.begin] cmd="python -m data.sync_all"
#   2026-07-13T02:15:30+08:00 [INFO ] [sync.end  ] status=ok exit=0 duration_s=930
#
# 输出：
#   /opt/weilai-HealthCheck-Agent/logs/cron-<task>-<YYYY-MM-DD>.log
#
# 保留策略：脚本本身不清理，配 logrotate 或每周清一次

set -o pipefail

TASK="${1:-unknown}"; shift
if [ $# -eq 0 ]; then
  echo "usage: $0 <task_name> <cmd> [args...]" >&2
  exit 2
fi

# 项目根路径
PROJECT_ROOT="/opt/weilai-HealthCheck-Agent"
LOG_DIR="${PROJECT_ROOT}/logs"
mkdir -p "${LOG_DIR}"

# 加载 .env 中的简单环境变量（避免 password 中的特殊字符导致 bash 语法错误）
# 完整变量（含密码）由 Python 里 load_dotenv() 加载
# 这里只 export 无特殊字符的 K=V（USE_ERP_CONFIGS / ERP_HOST / ERP_PORT / ERP_DB / ERP_USER 等）
if [ -f "${PROJECT_ROOT}/.env" ]; then
  while IFS= read -r line; do
    # 跳过注释和空行
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line// }" ]] && continue
    # 只处理 KEY=simple_value（值不含 shell 特殊字符）
    if [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=([A-Za-z0-9_./-]+)$ ]]; then
      export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
    fi
  done < "${PROJECT_ROOT}/.env"
fi

DATE=$(date +%Y-%m-%d)
LOG_FILE="${LOG_DIR}/cron-${TASK}-${DATE}.log"

# 时间戳前缀函数（每行）
_ts() {
  awk '{
    cmd = "date -Iseconds"; cmd | getline ts; close(cmd);
    printf "%s %s\n", ts, $0; fflush();
  }'
}

START_TS=$(date +%s)
CMD_STR="$*"

{
  # 开始标记
  echo "$(date -Iseconds) [INFO ] [${TASK}.begin] cmd=\"${CMD_STR}\" pid=$$"

  # 切到项目根，执行命令；把 stdout+stderr 都过时间戳
  cd "${PROJECT_ROOT}" || exit 3
  "$@" 2>&1 | _ts
  RC=${PIPESTATUS[0]}

  END_TS=$(date +%s)
  DUR=$(( END_TS - START_TS ))

  # 结束标记
  if [ $RC -eq 0 ]; then
    LEVEL="INFO "
    STATUS="ok"
  else
    LEVEL="ERROR"
    STATUS="fail"
  fi
  echo "$(date -Iseconds) [${LEVEL}] [${TASK}.end  ] status=${STATUS} exit=${RC} duration_s=${DUR}"
  exit $RC
} >> "${LOG_FILE}" 2>&1
