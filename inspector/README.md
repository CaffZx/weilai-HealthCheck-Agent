# inspector/ 巡检子系统

按业务巡检 Agent 三份规格文档实现（代码判定优先，LLM 只做语义/复杂根因/文案）。

## 文档规格 → 目录映射

三份文档是本目录的"契约规格"，改代码前先查文档：

| 文档 | 位置 | 谁读 |
|---|---|---|
| **代码实现版**（阈值/公式/查证表） | `docs/业务巡检Agent-代码实现版.md` | `inspector/engine/` |
| **巡检频次方案**（触发/心跳/事件生命周期） | `docs/业务巡检Agent_巡检频次最终方案*.md` | `inspector/scheduler/` |
| **知识库 v2 切片**（LLM 用的话术+说明） | `knowledge/*.md` | `prompts/` |

## 目录职责

### `rules/` — 判定标准表（yaml 配置，改数字不改代码）

| 文件 | 对应文档章节 | 内容 |
|---|---|---|
| `R2_异常字典.yaml` | 代码实现版 模块 2 | 7 大类 31 点位定义、作用层级、跳过规则 |
| `R3_严重度阈值.yaml` | 代码实现版 模块 3 | ACOS/销量/目标/自然/卡位/转化/评分/退款/库存积压/滞销 各专项阈值表 |
| `R4_打分参数.yaml` | 代码实现版 模块 4 | 基础分二维表、定位/阶段/淡旺季系数、并发权重、P 阈值 |
| `R5_升降级规则.yaml` | 代码实现版 模块 5 | 升级/降级/解除规则、超期硬跳档 |
| `R6_查证表.yaml` | 代码实现版 模块 6.0 | 表现型异常查证四列表（可能原因/验证方式/成立条件/成立后输出）|
| `R6_处理建议.yaml` | 代码实现版 模块 6.2/6.3 | 处理建议话术库 + 大类兜底 |
| `R_巡检频次.yaml` | 巡检频次方案 §6/§7/§9 | 健康商品基础频次、模块巡检策略、异常复查频次 |

### `engine/` — 判定引擎（读 rules/ 执行，一个模块一个文件）

| 文件 | 职责 |
|---|---|
| `anomaly_detector.py` | 模块 2 异常识别（现象即原因型 → 直接判定；表现型 → 交给 severity_grader） |
| `severity_grader.py` | 模块 3 严重度打档（S0/S1/S2） |
| `score_calculator.py` | 模块 4 执行分数公式 + P0/P1/P2 阈值映射 + 破平 |
| `event_lifecycle.py` | 模块 5 事件升级/降级/解除、超期硬跳档 |
| `cause_analyzer.py` | 模块 6.0 表现型异常查因编排（前 3 列代码执行；6.0.6/6.0.7 部分交 LLM） |
| `suggestion_picker.py` | 模块 6.1/6.2/6.3 处理建议选取 |

### `scheduler/` — 调度层（巡检频次方案）

| 文件 | 职责 |
|---|---|
| `trigger_registry.py` | 5 类触发合并（变更/表现/广告/高风险/人工/心跳），生成当日巡检队列 |
| `event_pool.py` | 事件池（首发/最近/复查/关闭时间管理） |
| `daily_monitor.py` | 每日聚合任务（销量/CVR/ACOS/评分/退款 → 判定"连续 ≥3 天"等 window 条件） |

### `inspector_main.py` — 巡检主入口

编排上面所有模块，一次巡检的完整链路：
```
trigger_registry → 选出今日待巡 →
  anomaly_detector → 命中的现象即原因型
    severity_grader → 打档
  daily_monitor → 表现型信号
    cause_analyzer → 查因（可能调 LLM node）
    severity_grader → 打档
  score_calculator → 单异常分/产品分/P 档
  event_lifecycle → 更新事件池
  suggestion_picker → 补处理建议
  → 输出符合模块 7 的父 ASIN 任务卡
```

## 命名约定

- **变量名复用 MCP 原生字段**：`全部单量`、`ACOS`、`广告花费`、`FBA可售库存`、`链接转化率`——不换成英文，跟 `data/local_store.py` 生成列一致
- **一个 yaml 一个模块**，不要跨模块塞
- **engine/ 里不写数字，全从 yaml 读**

## 开发进度

- [ ] R4_打分参数.yaml（进行中）
- [ ] score_calculator.py + 单元测试（用文档 4.8 算例 T1 收割 旺季前期 → 192.82 → P0）
- [ ] R3_严重度阈值.yaml
- [ ] severity_grader.py
- [ ] R2_异常字典.yaml
- [ ] anomaly_detector.py
- [ ] event_pool + trigger_registry + daily_monitor
- [ ] cause_analyzer + suggestion_picker
- [ ] R5 + event_lifecycle
- [ ] inspector_main 端到端
- [ ] 前端接入代码判定结果（保留 LLM 结果并列展示）
