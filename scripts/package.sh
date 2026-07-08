#!/usr/bin/env bash
# 打包给别人测试用的 zip（用 Python zipfile，UTF-8 中文名兼容）
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DATE=$(date +%Y%m%d)
OUT="$ROOT/../weilai-HealthCheck-Agent_$DATE.zip"

# 只保留 DeepSeek 相关的 env
TMPENV=$(mktemp)
grep -E '^(DEEPSEEK_|# )' "$ROOT/.env" > "$TMPENV" 2>/dev/null || true

python3 - "$ROOT" "$OUT" "$TMPENV" <<'PY'
import os, sys, zipfile
root, out, tmpenv = sys.argv[1], sys.argv[2], sys.argv[3]
parent = os.path.dirname(root)
prefix = "weilai-HealthCheck-Agent"

EXCLUDE_DIRS = {'.git', '__pycache__', '.pytest_cache', 'logs', '.venv', 'venv', 'node_modules'}
EXCLUDE_FILES = {'.DS_Store', '.env'}  # .env 用 tmpenv 替换
# 预同步好的本地库(healthcheck.db)一起打包，别人解压即用；但不带 WAL/SHM 临时文件
def skip(f):
    return f in EXCLUDE_FILES or f.endswith('.db-wal') or f.endswith('.db-shm') or f.endswith('.pyc')

if os.path.exists(out):
    os.remove(out)

with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if skip(f):
                continue
            full = os.path.join(base, f)
            rel = os.path.relpath(full, parent)
            # 显式 UTF-8 flag
            info = zipfile.ZipInfo.from_file(full, rel)
            info.flag_bits |= 0x800  # 强制 UTF-8
            with open(full, 'rb') as fh:
                z.writestr(info, fh.read(), zipfile.ZIP_DEFLATED)
    # 塞入精简 .env
    info = zipfile.ZipInfo(f"{prefix}/.env")
    info.flag_bits |= 0x800
    z.writestr(info, open(tmpenv).read(), zipfile.ZIP_DEFLATED)

size = os.path.getsize(out)
print(f"打包完成 → {out} ({size/1024:.0f}K)")
PY

rm -f "$TMPENV"

echo
echo "对方解压后："
echo "  1. cd weilai-HealthCheck-Agent"
echo "  2. pip3 install -r requirements.txt"
echo "  3. python3 -m uvicorn web.backend.main:app --port 8000"
echo "  4. 浏览器打开 http://127.0.0.1:8000"
