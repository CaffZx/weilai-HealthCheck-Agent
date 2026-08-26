# 经营模式 Agent 批量查询与自动评估接入说明

## 接入结果

- 默认先用 `get_current_operating_modes` 批量查询。
- 单条兼容方法 `get_current_operating_mode` 保留，供旧调用方和兼容测试使用。
- 未命中的经营单元必须调用 `start_operating_mode_evaluation_job`，轮询 `get_operating_mode_evaluation_job` 到终态后，再查询一次当前经营模式。
- 禁止调用手动工具 `evaluate_operating_mode`。

## 批量合同

请求字段：`operating_units`，每次 1–10000 条，每条必须包含 `shop_id`、`parent_asin`、`parent_seller_sku`。

响应校验：

1. 校验 `requested_count/found_count/missing_count` 与数组长度一致；
2. 校验 `results[]` 和 `missing_operating_units[]` 的业务键不重复；
3. 校验命中与未命中键的并集恰好等于请求键集合；
4. 按请求顺序返回 `CurrentOperatingMode | None`，未命中项为 `None`；
5. 任意店铺、父 ASIN 或父 SKU 错配都拒绝整批，不能按 ASIN 猜测。

## 中控行为

- 运行态中控 Publisher 执行“批量查询 → 缺失项自动评估 → 等待终态 → 缺失项复查”闭环，再逐条应用 `DECIDED` 结果。
- 自动评估单次最多 100 个唯一经营单元；超过 100 个时分批创建任务并等待全部任务结束。
- 同一客户端中的评估闭环串行执行；同一经营单元在默认 60 分钟冷却期内只触发一次，但每轮仍先查最新权威结果。
- `DECIDED` 才写入权威经营模式；`BLOCKED/NEEDS_HUMAN_REVIEW/null` 按既有规则走 `advert_config.operatingMode` 兜底。
- MCP 调用失败、合同错配或等待超时会阻止入中控队列；任务正常结束但仍无当前记录时保留 `null`，明确走广告配置兜底。
- 手工线上提交脚本同样默认使用批量查询；用户明确授权时可使用显式广告配置兜底模式。

## 真实验证

2026-08-06 使用新 Token 对两个真实经营单元执行批量只读查询：

- `B0EXAMPLE0 / FS04084-XIN`：`DECIDED / NEW_PRODUCT_VALIDATION / 新品验证`；
- `B0EXAMPLE0 / tuan-fu-new1`：未命中，按合同返回 `None`。

该验证发生在自动评估接入之前；当前运行链路会对未命中项触发自动评估。
