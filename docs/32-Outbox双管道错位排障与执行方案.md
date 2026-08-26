# 巡检 Agent v2.0 — Outbox 双管道错位问题排障与执行方案

> 日期:2026-08-12
> 关联文档:[31-进程与巡检启动运维规范.md](31-进程与巡检启动运维规范.md)
> 状态:待执行(阶段 1 自愈观察中)

## 一、问题一句话

> 6 个 delivery worker 空转、outbox PENDING 只涨不消化 —— 因为 **worker 在等 `CONTROL_CENTER_PATROL_BATCH`,而 758 条 PENDING 全是历史遗留的 `INSPECTION_RUN`**,门牌号对不上。

## 二、架构现状(必读)

`t_patrol_delivery_outbox` 表按 `aggregate_type` 分成两条投递管道,互不相干:

### 管道 A:INSPECTION_RUN(老,即将退役)

| 项 | 位置 |
|---|---|
| 生产者 | `integrations/repositories/patrol.py:486,559` — 每个 patrol run 落库时写入 |
| 消费者 | `integrations/runtime_delivery_main.py` + `integrations/result_delivery.py` |
| 开关 | `feature_flags.result_delivery_enabled` = **false** |
| rollout.py 关键注释 | "result_delivery_enabled 关闭时也不写 PENDING —— 否则 outbox 会无限累积(历史遗留:曾积压 17271 条 INSPECTION_RUN PENDING)" |

### 管道 B:CONTROL_CENTER_PATROL_BATCH(新,当前主链路)

| 项 | 位置 |
|---|---|
| 生产者 | **仅** `scripts/run_real_patrol_batch.py:166` 的 `publisher.enqueue(request)` — 一批 50 unit 结束时打包成 1 条 outbox 行 |
| 消费者 | `integrations/runtime_control_center_delivery_main.py`(今天起了 6 个 systemd 实例) |
| 开关 | `feature_flags.control_center_delivery_enabled` = **true** |

## 三、outbox 表当前分布(2026-08-12 17:08)

```
INSPECTION_RUN         PENDING    758   ⚠️ 旁路漏进来的,应被 rollout 抑制成 SUPPRESSED
INSPECTION_RUN         DEAD       116   ⚠️ 历史失败
INSPECTION_RUN         DELIVERED  295   
INSPECTION_RUN         SUPPRESSED 18009 ✅ 关闭 flag 后被正确抑制的
CONTROL_CENTER_PATROL_BATCH  ?       0  ← 空,因为当前 batch 还没跑完 enqueue
```

## 四、这次改动与本问题的关系

**本问题非本次引入**,以下今日改动都正确、需要保留:

- 8 项 `config/settings.yaml` 并发/间隔调整 
- `scripts/run_real_patrol_batch.py` 并发上限 8 → 32 
- 4 个重复 systemd unit 删除、6 个 delivery worker template 化 
- MCP 端点确认走内网

今日改动**唯一的副作用**:6 个 CC delivery worker 一起空转,让"没在消费"变得更显眼。

## 五、四阶段执行方案

### 阶段 1:自愈观察(不动数据,仅等待与观察)

**目的**:验证 CC 管道能否端到端走通。

**前置条件**:
- 当前 patrol batch `weilai-v2-patrol-batch.service` 处于 active running
- 6 个 delivery worker `weilai-v2-delivery@{1..6}.service` 处于 active running

**动作**:

```bash
# 每 30 秒观察一次,直到 batch 收敛到 50/50
watch -n 30 'tail -3 /opt/weilai-HealthCheck-Agent-v2.0/logs/patrol-batch.log; echo ---; curl -s http://127.0.0.1:8020/api/v1/patrol/health | python3 -c "import sys,json;d=json.load(sys.stdin);print(d[chr(109)+chr(101)+chr(116)+chr(114)+chr(105)+chr(99)+chr(115)][chr(111)+chr(117)+chr(116)+chr(98)+chr(111)+chr(120)])"'
```

**核对 outbox 是否新增 CC batch 行**(替代表法直接查库):

```bash
cd /opt/weilai-HealthCheck-Agent-v2.0
.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv()
import os
from sqlalchemy import create_engine, text
eng = create_engine(os.environ['PATROL_DATABASE_URL'])
with eng.connect() as c:
    r = c.execute(text('SELECT aggregate_type, status, COUNT(*) FROM t_patrol_delivery_outbox GROUP BY aggregate_type, status'))
    for row in r: print(row)
"
```

**成功标准**:
- `CONTROL_CENTER_PATROL_BATCH` 出现新行,从 PENDING 快速走到 DELIVERED
- 6 个 worker 至少有 1 个的日志 `logs/delivery-worker-{1..6}.log` 有内容
- 健康检查 `patrol_health_status` 从 2(CRITICAL)向 1/0 下降

**失败预案**:
- 若 batch 结束后没有新 CC 行 → 生产端有问题,跳到阶段 3 修 `run_real_patrol_batch.py`
- 若 worker 日志仍空 → 消费端有问题,重启 `systemctl restart weilai-v2-delivery@{1..6}.service` 观察

**产出**:决定是否继续阶段 2。

### 阶段 2:清理历史 INSPECTION_RUN 积压(数据变更,需审慎)

**前置条件**:阶段 1 成功,CC 管道已走通。

**风险评估**:
- 只改老管道数据(INSPECTION_RUN),不动新管道
- SUPPRESSED 是终态,不再被任何 worker 触发
- 老管道 worker `runtime_delivery_main` 本来就没跑,不会有冲突
- 涉及 758 + 116 = 874 行 UPDATE
- 无需业务停机

**执行前先备份表**:

```bash
cd /opt/weilai-HealthCheck-Agent-v2.0
mysqldump -h 10.0.0.0 -u erp_example -p'REMOVED_PASSWORD' erp_example t_patrol_delivery_outbox \
  --where="status IN ('PENDING','DEAD') AND aggregate_type='INSPECTION_RUN'" \
  > /root/systemd-backup-20260812/outbox_inspection_run_cleanup_$(date +%H%M%S).sql
ls -la /root/systemd-backup-20260812/outbox_inspection_run_cleanup_*.sql
```

**执行清理**:

```bash
cd /opt/weilai-HealthCheck-Agent-v2.0
.venv/bin/python <<'PY'
from dotenv import load_dotenv; load_dotenv()
import os
from sqlalchemy import create_engine, text
eng = create_engine(os.environ['PATROL_DATABASE_URL'])
with eng.begin() as c:
    # 1) INSPECTION_RUN PENDING → SUPPRESSED
    r = c.execute(text(
        "UPDATE t_patrol_delivery_outbox "
        "SET status='SUPPRESSED', last_error='deprecated INSPECTION_RUN pipeline (2026-08-12 cleanup)' "
        "WHERE status='PENDING' AND aggregate_type='INSPECTION_RUN'"
    ))
    print('suppressed_pending=', r.rowcount)
    # 2) INSPECTION_RUN DEAD → ARCHIVED
    r = c.execute(text(
        "UPDATE t_patrol_delivery_outbox "
        "SET status='ARCHIVED' "
        "WHERE status='DEAD' AND aggregate_type='INSPECTION_RUN'"
    ))
    print('archived_dead=', r.rowcount)
    # 3) verify
    r = c.execute(text(
        "SELECT aggregate_type, status, COUNT(*) FROM t_patrol_delivery_outbox GROUP BY aggregate_type, status"
    ))
    for row in r: print(row)
PY
```

**验证**:

```bash
curl -s http://127.0.0.1:8020/api/v1/patrol/health | python3 -m json.tool | grep -E "status|alerts" | head -20
```

**成功标准**:
- `patrol_health_status` 降到 0(HEALTHY)或 1(DEGRADED)
- `alerts` 里不再有 `DEAD_OUTBOX(CRITICAL)`、`OUTBOX_BACKLOG`、`OUTBOX_WAIT_TOO_LONG`
- 若阶段 3 尚未做,`INSPECTION_RUN PENDING` 仍可能被下一次 patrol run 补进来 —— 那就要走阶段 3

**回滚**:

```bash
mysql -h 10.0.0.0 -u erp_example -p'REMOVED_PASSWORD' erp_example \
  < /root/systemd-backup-20260812/outbox_inspection_run_cleanup_*.sql
```

### 阶段 3:堵住 INSPECTION_RUN 旁路(代码变更)

**前置条件**:阶段 2 完成。

**根因**:`integrations/repositories/patrol.py:486, 559` 直接插入 `t_patrol_delivery_outbox` 且 status 写死 PENDING,没有走 `rollout_policy.outbox_delivery_status`,导致 `result_delivery_enabled=false` 时仍产入 PENDING。

**修复选项(二选一)**:

#### 选项 3A(保守,推荐):加 rollout 判断,不改架构

在 patrol.py:486 和 559 两处插入 outbox 前,检查 `result_delivery_enabled`,决定写 PENDING 还是 SUPPRESSED。

修改点位:

```python
# integrations/repositories/patrol.py:486 前后
from integrations.rollout import RolloutPolicy  # 若尚未 import
# 需要注入 rollout_policy 或调用方传入
status = rollout_policy.outbox_delivery_status  # 关闭时自动 SUPPRESSED
# 用 status 代替原来硬编码的 'PENDING'
```

**风险**:需要看 patrol.py 那两处能不能拿到 rollout_policy 实例。若拿不到,得从上游传参下来。

#### 选项 3B(彻底,推荐 v2.1):完全废弃 INSPECTION_RUN 管道

- 删除 `patrol.py:486, 559` 的 outbox 写入
- 删除 `runtime_delivery_main.py`、`result_delivery.py`(或标记 deprecated)
- 删除 `runtime_queries.py:46` 相关引用
- 清理 `config/settings.yaml` 的 `feature_flags.result_delivery_enabled`
- 清理 `feature_flags.control_center_delivery_enabled` 假设为 true(去掉这个二级 flag)

**风险**:大改动,需要 review。建议 v2.1 版本一并处理。

**执行(选 3A)**:

1. 先在本地或 dev 环境改代码 + 单测
2. `scp` 到服务器
3. `systemctl restart weilai-v2-api.service`(改动了 patrol.py,可能被 API 加载)
4. 触发一次 patrol batch,观察 outbox 是否还产入 INSPECTION_RUN PENDING

**成功标准**:
- 新一批 patrol 结束,`INSPECTION_RUN PENDING` 保持 0
- `CONTROL_CENTER_PATROL_BATCH DELIVERED` 有增长

### 阶段 4(可选,不阻塞跑数据):批次尾巴收敛优化

**问题**:当前批次 47/50 running=1 卡在外部 MCP timeout,单个卡住的 unit 拖住整个 50 unit 批次的推进,下一批要等它完全 terminal 才开始。

**优化方向**(三选一):

- **降 MCP timeout**:`config/settings.yaml → mcp.timeout_seconds: 60 → 15`(简单,不改代码)
- **降 max_attempts**:`mcp.max_attempts: 3 → 2`(总耗时从 3×60=180s 降到 2×15=30s)
- **改 batch 调度**:让新批次不等旧批次完全 terminal,允许 in-flight 交叠(需要看 `run_real_patrol_batch.py` 的 batch scheduler,改动量较大)

**建议**:先执行前两条(改配置),不动代码。

```bash
cd /opt/weilai-HealthCheck-Agent-v2.0
python3 -c "
import re, pathlib
p = pathlib.Path('config/settings.yaml')
s = p.read_text()
def r(text, block, key, val):
    return re.sub(rf'(^{block}:\n(?:[ \t]+.*\n)*?[ \t]+{key}:\s*)([^\n]+)', lambda m: m.group(1)+str(val), text, 1, re.M)
s = r(s, 'mcp', 'timeout_seconds', 15)
s = r(s, 'mcp', 'max_attempts', 2)
p.write_text(s)
print('OK')
"
systemctl restart weilai-v2-patrol-batch.service  # 让新参数生效
```

## 六、执行顺序 + 决策点

| 阶段 | 何时做 | 谁决策 | 数据/代码变更 |
|---|---|---|---|
| 1 自愈观察 | 立即,不需等待 | 无需决策 | 无 |
| 2 清历史积压 | 阶段 1 通过后 | 你确认后我执行 | UPDATE 874 行 |
| 3 堵旁路 | 阶段 2 完成后 | 需选 3A 或 3B | 改 py 文件 |
| 4 尾巴优化 | 可选 | 你决定要不要 | 改 yaml |

## 七、监控与验证命令速查

```bash
# 全景服务
systemctl list-units "weilai-v2-*" --no-pager
systemctl list-units "weilai-*" --state=failed --no-pager

# 采集进度
tail -f /opt/weilai-HealthCheck-Agent-v2.0/logs/patrol-batch.log
tail -f /opt/weilai-HealthCheck-Agent-v2.0/logs/delivery-worker-1.log

# 健康(JSON)
curl -s http://127.0.0.1:8020/api/v1/patrol/health | python3 -m json.tool

# Prometheus 指标
curl -s http://127.0.0.1:8020/api/v1/patrol/metrics | grep patrol_

# outbox 分布(直连 DB)
cd /opt/weilai-HealthCheck-Agent-v2.0 && .venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv()
import os
from sqlalchemy import create_engine, text
eng = create_engine(os.environ['PATROL_DATABASE_URL'])
with eng.connect() as c:
    for row in c.execute(text('SELECT aggregate_type, status, COUNT(*) FROM t_patrol_delivery_outbox GROUP BY aggregate_type, status')):
        print(row)
"
```

## 八、回滚点汇总

| 变更 | 回滚方式 |
|---|---|
| 阶段 2 SQL 清理 | `mysql < /root/systemd-backup-20260812/outbox_inspection_run_cleanup_*.sql` |
| 阶段 3 py 改动 | git 或 `.bak-*` 文件恢复 |
| 阶段 4 timeout 改动 | 重新编辑 yaml,或从 `config/settings.yaml.bak-20260812-165833` 恢复 |
| 今日 systemd 改动 | `/root/systemd-backup-20260812/*.service` 复制回 `/etc/systemd/system/` |

---
方案定稿,等待阶段 1 观察结果 → 你决定阶段 2 是否执行。
