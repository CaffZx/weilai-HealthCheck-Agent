# inspector/ 巡检子系统

代码判定管线：规则引擎 + 事件池 + LLM 建议兜底。

## 目录职责（实际实现）

### `rules/` — 判定标准表（yaml 配置，改数字不改代码）

| 文件 | 内容 |
|---|---|
| `R2_异常字典.yaml` | 异常大类/点位定义、作用层级、跳过规则 |
| `R3_严重度阈值.yaml` | ACOS/销量/目标偏离/自然流量/评分/退款/库存积压/滞销 各专项阈值表 |
| `R4_打分参数.yaml` | 基础分二维表、定位/阶段/淡旺季系数、并发权重、P 阈值 |
| `R6_处理建议.yaml` | 处理建议话术库 + 大类兜底 |

> R5 升降级规则、R_巡检频次 目前未落地，事件升降级/复查节奏由 `event_pool.py` 里的简化状态机代管。

### `engine/` — 判定引擎（读 rules/ 执行）

| 文件 | 职责 |
|---|---|
| `anomaly_detector.py` | R2 异常识别（现象即原因型 + 表现型分流） |
| `severity_grader.py` | R3 严重度打档（S0/S1/S2） |
| `score_calculator.py` | R4 执行分数公式 + P0/P1/P2 阈值映射 + 破平 |
| `suggestion_picker.py` | R6 处理建议选取（YAML 模板兜底） |
| `llm_suggester.py` | LLM 生成处理建议（优先），失败回退 R6 YAML |

> 计划中的 `event_lifecycle.py`（R5）、`cause_analyzer.py`（R6.0 查因编排）暂未拆出，相关逻辑内联在 `inspector_main.py` 与 `event_pool.py`。

### `scheduler/` — 调度层

| 文件 | 职责 |
|---|---|
| `event_pool.py` | 事件池 CRUD、首发/最近/复查/关闭时间、状态流转 |
| `daily_monitor.py` | 每日聚合任务（销量/CVR/ACOS/评分/退款 → 判定"连续 ≥3 天"等 window 条件） |

> 计划中的 `trigger_registry.py`（5 类触发合并）暂未实现，当前是每次全量巡检 `fixture_loader.list_keys()` 拿到的所有产品。

### `inspector_main.py` — 巡检主入口

一次巡检的实际链路：
```
fixture_loader.load_configs → daily_monitor 聚合 →
  anomaly_detector 现象即原因型 → severity_grader 打档
  anomaly_detector 表现型信号 → severity_grader 打档
  score_calculator → 单异常分 + 产品分 + P0/P1/P2
  event_pool.处理命中事件 → upsert event_pool 表（含 单异常执行分数）
  suggestion_picker + llm_suggester → 处理建议
  → 输出模块 7 父 ASIN 任务卡（前端渲染同一 schema）
```

两个入口函数：
- `巡检单产品(key, config_row, mcp_bundle=None)` — 单产品，前端点 ASIN 触发
- `巡检批量(产品列表, 批次号)` — 批量，crontab 或 API `/api/batch/inspect` 触发

## 命名约定

- **变量名复用 MCP 原生字段**：`全部单量`、`ACOS`、`广告花费`、`FBA可售库存`、`链接转化率` — 不换成英文，跟 `data/local_store.py` 生成列一致
- **一个 yaml 一个模块**，不跨模块塞
- **engine/ 里不写数字**，全从 yaml 读

## 相对完整规划的差距

以下是当前"阶段性实现"未覆盖的能力（不影响主流程运行，后续按需扩展）：

- R5 升降级规则（当前用 event_pool 简化状态机）
- R_巡检频次触发合并（当前每次都全量）
- cause_analyzer 表现型查因编排（当前直接给建议）
- event_lifecycle 复查/超期硬跳档（当前只在人工填 `review_at` 时启用）
