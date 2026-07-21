# 巡检 Agent 修改执行方案（决策已定版）

> 依据：《巡检 Agent 工业化落地评估报告》（评估对象为 `source 20260720-153705` 冻结快照）
> 复核基线：当前工作区 HEAD（2026-07-21），对报告每一条断言逐一对照现网代码后编写。
> 本版特点：**所有原「需拍板」的口径已按推荐方案锁定**，可直接照此执行，无需再做选择。

---

## 〇、先明确：报告哪些已经不需要做了

复核后确认以下报告内容**已在 0720 之后修复**，本方案不再处理：

| 报告内容 | 现网状态 | 证据 |
|---|---|---|
| 第三节「双状态机不同步、事件池只进不出」（报告标为最高优先级） | **已修复** | `web/backend/routers/tasks.py:157/253/427/491` 每次动作都走 `流转状态_事务`；`下一次复查时间/复查时间来源` 已写入；复盘确认变好走 `流转状态_事务(…,"已关闭","复查确认恢复")` |
| 第四节 #1 标题异常检测缩进嵌套 bug | **已修复** | `inspector/inspector_main.py:334` `detect_标题异常` 已正确落在 `if product_info:` 块内，不在 `for variant_type` 循环里 |

> ⚠️ 唯一保留的相关缺口：现网关闭事件是**运营复盘手动驱动**的，缺「批巡本批未命中 → 自动恢复关闭」这条自动收敛路径 → 见 **P1-3**。

---

## 一、批次总览

| 批次 | 目标 | 条目 |
|---|---|---|
| **P0** 低风险，直接改 | 消除明确 bug / 卫生问题 | ACOS 基线、`[DBG]` 清理、SQLite `busy_timeout`+WAL |
| **P1** 行为变更，灰度上 | 成本/打分/闭环，各自单独提交 + 灰度 | LLM 文案门槛、变体级降链接级、未命中自动恢复、校准报表 |
| **P2** 扩量前再做 | 当前规模非必需 | `inspection_batch` 落库、AI 补录入池、变更触发/心跳 |

---

## 二、P0：低风险，可直接改（单独一个提交，立即上线）

### P0-1 · ACOS「连续超标天数」基线口径纠正

- **问题**：`ACOS连续超标天数` 用「近 7 天自身均值」当基线，而 R3 判偏离比用「目标 ACOS」。序列天然约一半天数在自身均值上方，「连续 ≥3 天」门槛形同虚设。
- **改动**：`inspector/scheduler/daily_monitor.py:468`
  ```python
  # 现状
  ACOS连续超标天数 = _算连续天数(销售数据, "ACOS", 近7天平均ACOS, 方向="上升")
  # 改为
  ACOS连续超标天数 = _算连续天数(销售数据, "ACOS", 目标ACOS, 方向="上升")
  ```
- **说明**：`目标ACOS` 同作用域可用（`daily_monitor.py:615/748`）；`_算连续天数` 对 `基线 is None` 返回 0，目标未配置时自动退化为「不判连续」，安全。
- **验证**：取有 ACOS 序列且配置了目标 ACOS 的产品，`python -m inspector.scheduler.daily_monitor` 自检对比改前后；确认目标=None 的产品该值恒为 0。

### P0-2 · 清理 `[DBG]` 生产调试输出

- **问题**：聚合入口逐产品刷 `stderr.write("[DBG] …")`。
- **改动**：`inspector/scheduler/daily_monitor.py:439` 与 `:443` 两行，**改成 `log.debug(...)`**（不直接删，保留排查能力；默认 INFO 级不输出）。
  ```python
  # 439/443 两处 _sys.stderr.write(f"[DBG] …\n"); _sys.stderr.flush()
  # 改为
  log.debug("聚合入口 pa=%s shop=%s 数据天数=%s", 父ASIN, 店铺账号, 数据天数)
  log.debug("早return pa=%s 目标ACOS=%s 目标预算=%s", 父ASIN, _目标ACOS_early, _目标预算_early)
  ```
- **验证**：`grep -rn "\[DBG\]" inspector/` 无结果；跑一次批巡确认 stderr 干净。

### P0-3 · SQLite 加 `busy_timeout` + WAL（收敛到统一工厂）

- **问题**：批巡 8 线程写 + FastAPI 读写同一 `healthcheck.db`，全库无 `busy_timeout`，偶发 `database is locked`。全库 **~35 处** `sqlite3.connect` 直连点，多数绕过 `_conn()`。
- **锁定做法：收敛到工厂 + 全面替换直连点**（不用「逐点补 PRAGMA」这种半吊子方案）。
  1. `data/local_store.py:22 _conn()` 升级为唯一工厂：
     ```python
     def _conn() -> sqlite3.Connection:
         DB_PATH.parent.mkdir(parents=True, exist_ok=True)
         c = sqlite3.connect(DB_PATH, timeout=30)
         c.row_factory = sqlite3.Row
         c.execute("PRAGMA busy_timeout=30000")   # 锁等待 30s，而非立刻报错
         c.execute("PRAGMA journal_mode=WAL")      # 读写不互斥，显著降低 locked
         c.execute("PRAGMA foreign_keys=ON")
         return c
     ```
  2. `inspector/scheduler/event_pool.py:110 _conn()` 与其事务连接（`:111`）同样补 `busy_timeout` + WAL。
  3. 其余直连点（`tasks.py`/`common.py`/`users.py`/`history.py`/`fixtures.py`/各 sync）统一改为调用 `local_store._conn()`。逐文件替换、逐文件自测。
- **部署前置**：确认线上 DB 在**本地盘**（WAL 不支持网络文件系统）；WAL 会新增 `-wal/-shm` 文件，纳入部署忽略清单。
- **验证**：批巡运行期间高频请求 `/api/today/*` 压测，日志不再出现 `database is locked`。

---

## 三、P1：行为变更，各自单独提交 + 一周灰度

### P1-1 · LLM 文案生成加门槛（降每日最大可变成本）

- **问题**：`inspector/inspector_main.py:700` 只要产品有异常就每次批巡必调 LLM，日批全量下绝大多数产品每天各打一次文案。
- **锁定口径**：
  - 门槛**直接复用** `config/settings.yaml` → `llm.trigger`（`最低分数:90` / `最少S0异常数:1` / `最少异常数:2`），与 `llm_judge` 深度分析保持一致。
  - 未达门槛产品文案**走 R6 模板**（S2 轻微异常足够）。回退链路本就存在（`inspector_main.py:710-713`，`R6.选取` 独立可用），跳过 LLM 时 `llm_results=[]` 自动落 R6，无需额外兜底代码。
- **改动**：在 `suggest_batch` 调用处（`inspector_main.py:700`）加门槛判断：
  ```python
  def _should_call_llm(产品打分, anomaly_data, trigger_cfg) -> bool:
      分数达标 = 产品打分.产品执行分数 >= trigger_cfg["最低分数"]
      s0数 = sum(1 for a in anomaly_data if a["严重度"] == "S0")
      s0达标 = s0数 >= trigger_cfg["最少S0异常数"] and len(anomaly_data) >= trigger_cfg["最少异常数"]
      return 分数达标 or s0达标

  llm_results = (
      llm_suggester.suggest_batch(anomaly_data, product_context)
      if anomaly_data and _should_call_llm(产品打分, anomaly_data, _trigger_cfg) else []
  )
  ```
  （`_trigger_cfg` 从 settings.yaml `llm.trigger` 读入，与深度分析同源。）
- **验证**：改前后统计一批巡检的 LLM 调用次数（应从「异常产品数」降到「重点产品数」）；抽查未达门槛产品文案由 R6 正常产出。

### P1-2 · 变体级点位暂按链接级打分（消除 P0 虚高）

- **问题**：`inspector/inspector_main.py:306/315/345/352/359/366/437` 对库存/主图/图片/A+/不可售/变体价差硬传 `子体ASIN=父ASIN, 变体重要性="主要色"`，R4 变体级基础分永远取最高档，分数系统性偏高，虚高 P0 = 无效干预。
- **锁定做法：方案 A —— 显式标注链接级，`变体重要性=None`**（真实子体粒度接入前不假装有变体分层；方案 B 恢复变体分层归入 P2-3）。
  1. 上述各调用处把 `变体重要性="主要色"` 改为 `变体重要性=None`；`子体ASIN` 保持传父 ASIN 但语义按链接级处理。
  2. **前置校验**：确认 `score_calculator`/`severity_grader` 在 `变体重要性=None` 时取**链接级/保守档**基础分，而非回退到最高档。参照 `anomaly_detector.py:171-172`（变体重要性 None 时「保守不跳过、按父 ASIN 处理」），语义一致；若打分侧对 None 有异常回退，需一并修正。
- **灰度**：**先并列对比、不直接切**。上线一版「改前 P 分层 / 改后 P 分层」并列报表，跑满一周确认高优先级比例回落到合理区间、且没把真问题压没，再正式切换为改后口径。
- **验证**：选覆盖各点位的样本对比改前后 `产品执行分数` 与 `执行优先级`；确认原本清一色 P0 的比例下降。

### P1-3 · 批巡「未命中自动恢复关闭」（让池子自己收敛）

- **问题**：现网关闭全靠运营手动复盘，缺「上批开放态事件、本批未再命中 → 自动关闭」，池子只靠人工出清，长期积累陈旧异常。
- **可复用**：复发识别已实现（`event_pool.py`：关闭态再次命中走 `reappeared`、`复发次数++`、回「新发现」）；每产品巡检后有钩子 `inspector_main.py:853/866 store.link_inspection_events(...)`。
- **锁定做法**：在 `link_inspection_events` 相邻处加「恢复扫描」，**限定自动关闭范围 + 加防抖**：
  1. 查该 `父ASIN + 店铺` 下、状态属 **`新发现` / `已处理待复扫`** 的事件（**`处理中` 不自动关**，避免抢关运营正在处理的事件；`待观察` 由复盘链路管，也不自动关）。
  2. 若其 `问题点位` **不在本批命中集合**内 → 计数「连续未命中批次数」。**连续 ≥2 批未命中**才 `流转状态_事务(…, "已关闭", 变更原因="自动恢复：连续N批未命中")`（防单批数据抖动误关）。防抖阈值做成可配（默认 2）。
  3. 关闭原因标注区别于「复查确认恢复」，便于统计**自动 vs 人工出清率**。
- **兜底**：全程写 `event_state_log`；万一误关，下批命中会 `reappeared` 自动重新进池，不会永久丢失。
- **验证**：构造「首批命中→连续 2 批未命中」→ 确认自动进「已关闭(自动恢复)」；构造「关闭后再命中」→ 确认 `reappeared`、`复发次数=1`、回「新发现」并在今日任务池重新出现；构造「处理中事件本批未命中」→ 确认**不被自动关**。

### P1-4 · R3 阈值校准报表（配套验收，随 P1 一起上）

- **背景**：R3 阈值全标「初始建议值，需数据回流后校准」，是「减少无效干预」唯一可量化证据。`task_action` 已有 `result/effect` 字段，数据齐，缺一张报表。
- **改动**：新增一张报表/接口，按点位统计「命中率 × 处理率 × 不处理率」（数据源 `event_pool` + `task_action`）。上线后 2～4 周为校准期，处理率过低的点位调阈值或降档。
- **验证**：报表能按点位输出三率；与人工抽样一致。

---

## 四、P2：扩量前再做

### P2-1 · `inspection_batch` 批次元数据落库
- **问题**：`inspection_batch`（`data/schema.sql:276`）全库**从未 INSERT**，`触发类型` 枚举属「设计已画、代码未接」。
- **改动**：`巡检批量`（`inspector_main.py:807`）开始/结束各写一条批次记录（时间、`触发类型='daily'`、巡检范围、参数版本、统计 JSON、状态），为后续分层触发预留。

### P2-2 · AI 补录异常入池
- **问题**：`core/llm_judge.py:136` 的 `AI补录:true` 只存 `batch_cache`，不进 `event_pool`，无法跟踪/复查。
- **锁定做法**：**补录也入 `event_pool`**（打 `来源=AI补录` 标记，纳入生命周期），而非关闭该能力。

### P2-3 · 扩量演进项
- 变更触发：价格/标题/主图 URL 指纹 diff（成本极低、信号极准）。
- 健康商品降频心跳（如 7 天），异常高危加密。
- 真实子体粒度巡检接入后，恢复 P1-2 的变体分层打分（方案 B）。

---

## 五、执行顺序与验收

1. **P0 三条一次做完**（半天），合成一个提交，回归自检后立即上线。
2. **P1 逐条单独提交**：P1-1、P1-4 风险低可较快切；**P1-2、P1-3 必须走一周灰度对比**（分数与自动关闭都可能误伤），确认无回归再正式切口径。
3. **P2 排入扩量前迭代**。

**统一验收口径**：上线后 2～4 周为 R3 阈值校准期，用 P1-4 报表持续监控每个点位的三率，据此调阈值/降档——这是工业化落地「减少无效干预」的量化闭环。
