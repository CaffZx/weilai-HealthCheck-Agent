# 日志规范

## 文件命名

```
logs/cron-<task>-<YYYY-MM-DD>.log   # 每天一个文件，按 task 分开
logs/health-report-<YYYY-MM-DD>.json # check_coverage 结构化报告
logs/health-report-latest.json       # 最新报告的软链
```

## 行格式

```
<ISO8601-tz> [<LEVEL>] [<task>.<event>] <k1>=<v1> <k2>=<v2>
```

- **时间戳**：ISO 8601 with timezone，例 `2026-07-13T02:00:01+08:00`
- **级别**（右对齐 5 字符）：`INFO ` `WARN ` `ERROR` `DEBUG`
- **task.event**：短横线/点分模块.动作，例 `sync.begin` `inspect.done`
- **kv**：空格分隔，值有空格用双引号

## 标准 event 列表

| event | 含义 | 必带字段 |
|---|---|---|
| `<task>.begin` | 任务开始 | `cmd`, `pid` |
| `<task>.end` | 任务结束 | `status=(ok/fail)`, `exit`, `duration_s` |
| `<task>.progress` | 进度更新 | `done`, `total` |
| `<task>.fail_asin` | 单产品失败 | `asin`, `shop`, `reason` |
| `<task>.warn_coverage` | 覆盖率告警 | `table`, `coverage`, `missing` |

## 查询示例

```bash
# 昨天所有错误
grep '\[ERROR\]' logs/cron-*-2026-07-13.log

# 查看某任务开始结束
grep -E 'sync.(begin|end)' logs/cron-sync-*.log

# 提取失败的 ASIN
grep 'fail_asin' logs/cron-*.log | awk -F'asin=' '{print $2}' | awk '{print $1}' | sort -u

# 平均耗时
grep 'sync.end' logs/cron-sync-*.log | grep -oE 'duration_s=[0-9]+' | awk -F= '{sum+=$2; n++} END {print sum/n}'
```

## Rotation

30 天以上自动清理：
```
find logs/ -name 'cron-*.log' -mtime +30 -delete
find logs/ -name 'health-report-*.json' -mtime +90 -delete
```

放到 `0 3 * * 0` crontab 里每周日跑一次。
