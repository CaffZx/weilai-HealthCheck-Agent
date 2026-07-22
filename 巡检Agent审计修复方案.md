# 巡检 Agent 全量审计 · 修复执行方案

依据 2026-07-22 全量审计。共 7 项代码问题 + 1 项安全事项。按优先级分 P0(上线前必修)/P1(一个迭代)/P2(可延后)。
每项含:问题 · 根因(file:line)· 改法 · 风险 · 验证。

---

## P0 —— 上线前必修

### P0-1 · 观察期整产品被移出巡检 → 新异常漏检

**问题**:产品进入维护观察期后,在 `next_inspection_at` 之前**完全不巡检**。期间若出现**另一个**新异常(链接下架/价格错乱/断货),要等到期才发现。观察期可设很长 → 盲区可能很长。

**根因**:`filter_due_products` 按**整产品**从巡检列表剔除:
- [inspector/inspector_main.py:830](inspector/inspector_main.py:830) `产品列表 = store.filter_due_products(产品列表)`
- [web/backend/batch_cache.py:109-118](web/backend/batch_cache.py:109) 同款跳过 + 写空缓存条目
- [web/backend/routers/judge.py:191](web/backend/routers/judge.py:191) 同款

**关键机制(已核实,决定改法)**:`event_pool.处理命中事件`([event_pool.py:197、228](inspector/scheduler/event_pool.py:197))——只有**关闭态(已关闭/误报/忽略)**再命中才复发回"新发现";**"已处理待复扫"再命中只更新指标、状态不变**。所以观察期内照常巡检是安全的:已完成的异常保持"已处理待复扫"(今日池本就隐藏,`event_display_status` 归为待复查),不会重新弹出;新异常正常进池。

**改法(推荐:去掉跳过,排期只用于观察比对)**:
1. 删除三处 `filter_due_products` 调用(inspector_main:830、judge.py:191、batch_cache 的跳过块 109-118)。产品恢复每日巡检。
2. `next_inspection_at` 语义从"在此之前不巡检"改为"观察比对基准时间"——**无需改** `_observe_maintenance`,它本就用 `created_at >= next_inspection_at` 取到期后的巡检结果做比对。
3. `inspection_schedule_map` / `filter_due_products` 若无他用可保留函数、仅去掉调用(减小改动面),或一并删除。

**为什么不用"逐异常抑制"方案**:因为"已处理待复扫"再命中本来就不弹回今日池,等价效果已经天然成立,不必额外写抑制逻辑。

**风险**:低。仅代价是观察期内产品照常参与巡检(本地代码判定近零成本);LLM 文案成本另见 P2-7 一并收口。

**验证**:
- 造一个维护(观察期3天)→ 运行巡检 → 确认该产品**被巡检**、其"已处理待复扫"异常仍隐藏在今日池外。
- 给该产品注入一个**新异常** → 确认立即出现在今日任务。
- 到 `next_inspection_at` 后打开 `/review/observations` → 确认 `_observe_maintenance` 仍正常生成效果结论。

---

### P0-2 · SQLite 无 busy_timeout/WAL(并发写 `database is locked`)

**问题**:批巡 8 线程写 + FastAPI 读写 + 新增维护/观察写路径,同库并发,偶发 `database is locked`。新功能上线后写路径更多,风险更高。

**根因**:[data/local_store.py](data/local_store.py) 的 `_conn()` 与 [inspector/scheduler/event_pool.py](inspector/scheduler/event_pool.py) 的连接都没设 `busy_timeout`。

**改法**:
1. `local_store._conn()` 升级为唯一工厂:
   ```python
   c = sqlite3.connect(DB_PATH, timeout=30)
   c.row_factory = sqlite3.Row
   c.execute("PRAGMA busy_timeout=30000")
   c.execute("PRAGMA journal_mode=WAL")
   c.execute("PRAGMA foreign_keys=ON")
   ```
2. `event_pool.py` 的连接同样补 `busy_timeout` + WAL(或统一改调 `local_store._conn()`)。
3. 其余散落的 `sqlite3.connect(DB_PATH)` 直连点(tasks.py/common.py/各 sync)尽量收敛到 `_conn()`。

**注意**:WAL 要求 DB 在本地盘;新增 `-wal/-shm` 文件,加入部署忽略清单。

**验证**:批巡运行期间高频打 `/api/tasks/today`,日志不再出现 `database is locked`。

---

## P1 —— 一个迭代内

### P1-3 · ACOS「连续超标天数」基线口径错

**问题**:用"近7天自身均值"当基线,序列天然约半数天在自身均值上方,"连续≥3天"门槛形同虚设,与"连续超目标"的设计语义不符。

**根因**:[inspector/scheduler/daily_monitor.py:468](inspector/scheduler/daily_monitor.py:468)
```python
ACOS连续超标天数 = _算连续天数(销售数据, "ACOS", 近7天平均ACOS, 方向="上升")
```

**改法**:基线改为 `目标ACOS`(同作用域可用;`_算连续天数` 对 `基线=None` 返回 0,目标未配置时自动退化"不判连续",安全)。

**验证**:取有 ACOS 序列且配了目标 ACOS 的产品,对比改前后连续天数;目标=None 的产品该值恒为 0。

---

### P1-4 · 变体重要性硬编码"主要色"→ 伪造变体数据(非分数虚高)

**审计更正**:核对 [R4 异常基础分表](inspector/rules/R4_打分参数.yaml) 后发现 **`链接级` 与 `变体级·主要色` 数值完全相等**(S0=100/100、S1=60/60、S2=30/30)。所以"按链接级打分"与现在的"主要色"**分数完全相同**——**在接入真实子体粒度数据前,分数无法安全降低**(要给次要色/长尾色打低分,必须先知道命中的是哪个变体;硬压低会误伤真实主色异常)。真正"去虚高"属扩量阶段的"接子体数据"(原 P2-3),非本次代码可解。

**因此本次的真实问题不是分数,而是"伪造变体数据"**:6 处 `变体重要性="主要色"、子体ASIN=父ASIN` 让 event_pool 的 `命中变体/变体重要性` 维度是假的(命中变体=父ASIN、重要性凭空=主要色),误导展示与后续按变体聚合。

**改法(诚实化表示,分数不变)**:
1. [score_calculator.py `取异常基础分`](inspector/engine/score_calculator.py:143) 加回退:`作用层级=变体级` 且 `变体重要性` 缺失 → 取**链接级**分(数值等于主要色,不掉分),说明标注"变体级点位缺变体数据,按链接级保守取分"。
2. [inspector_main.py](inspector/inspector_main.py) 6 处 `变体重要性="主要色"` → `None`、`子体ASIN=父ASIN` → 传 `命中变体=None`,不再伪造变体维度。
3. `是否跳过表现型` 只在 anomaly_detector 自检里调用、未进生产管线,`None` 不影响它。

**验证**:改前后跑同一批巡检,确认 `产品执行分数/执行优先级` **不变**(证明无功能损失),且 event_pool 的 `命中变体` 不再等于父ASIN、`变体重要性` 为空。

---

### P1-5 · `[DBG]` 生产调试输出

**问题**:聚合入口逐产品 `stderr.write("[DBG] …")`,生产日志刷屏。

**根因**:[inspector/scheduler/daily_monitor.py:439、443](inspector/scheduler/daily_monitor.py:439)

**改法**:两处改 `log.debug(...)`(保留排查能力,默认 INFO 不输出)。

**验证**:`grep -rn "\[DBG\]" inspector/` 无结果;跑一次批巡 stderr 干净。

---

## P2 —— 可延后

### P2-6 · `inspection_batch` 表从未写入

**问题**:schema 定义了批次元数据表(触发类型/巡检范围/统计),但代码从未 INSERT,"设计已画代码未接"。当前规模低危,但扩量/审计时缺批次追溯。

**改法**:`巡检批量`([inspector_main.py:807](inspector/inspector_main.py:807))开始/结束各写一条:批次号、开始/结束时间、触发类型='daily'、巡检范围 JSON、参数版本、统计(扫描数/命中数/观察数/数据不足数)、状态。

**验证**:跑一次批巡,`inspection_batch` 有对应记录。

---

### P2-7 · LLM 文案无门槛必调

**问题**:每个有异常的产品每次巡检都调一次 `suggest_batch` 生成文案,是每日最大可变成本。

**根因**:[inspector/inspector_main.py:700](inspector/inspector_main.py:700)
```python
llm_results = llm_suggester.suggest_batch(anomaly_data, product_context) if anomaly_data else []
```
(注:代码巡检的**深度分析**已在 [batch_cache.py:79 `_should_call_llm`](web/backend/batch_cache.py:79) 加了门槛;这条是**文案生成**路径,仍无门槛。)

**改法**:复用 `settings.yaml → llm.trigger`(最低分数/最少S0/最少异常数),不达门槛走 R6 模板(回退链路 inspector_main:710-713 已存在,跳过时 `llm_results=[]` 自动落 R6)。

**验证**:统计改前后一批的 LLM 调用次数,应从"异常产品数"降到"重点产品数";抽查未达门槛产品文案由 R6 正常产出。

---

## 安全事项(非代码,需你操作)

git 远程 URL 硬编码了明文 `ghp_` 令牌。请去 GitHub **吊销该令牌**,改用 SSH key 或 credential helper。任何能看到仓库配置/shell 历史的人都能拿到它。

---

## 执行顺序建议

1. **P0 两项先做**(观察期漏检 + busy_timeout)——风险最高、改动都不大,合一个提交。
2. **P1 三项**:P1-3/P1-5 低风险可较快切;**P1-4 变体降级打分必须走一周灰度对比**。
3. **P2 两项**排扩量前迭代。
