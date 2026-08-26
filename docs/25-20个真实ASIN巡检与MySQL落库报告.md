# 20 个真实 ASIN 巡检与 MySQL 落库报告

> 执行日期：2026-08-05  
> 环境：本地巡检 V2 + 测试 MySQL + AZListing MCP  
> 安全阶段：`INTERNAL_ONLY`，未启动 Scheduler，未向中控投递

## 1. 执行结论

20 个不同的真实父 ASIN 均先通过 `erp_listing_product_info` 在线验真，再执行正式巡检。
最终每个父 ASIN 都有一个可用的成功 Job，并真实写入 MySQL：10 条原始事实记录、1 个事实
快照、信号及发生记录、1 条巡检结果 Outbox。20 条 Outbox 均为 `SUPPRESSED`，没有外发中控。

主批次 `batch_a221ad88f6dd450f804f7ccd` 完成 19 个，`B0EXAMPLE0` 首次采集时因
`PRODUCT_CHILD_ASIN_MISSING:0` 正确失败，主批次状态为 `PARTIAL_SUCCESS`。重新在线验真确认
该父体返回 20 个有效子 ASIN 后，通过补跑批次 `batch_c26a05389ede46e2952840c1` 成功落库。
原失败记录保留用于审计，没有删除、改写或伪造成成功。

## 2. 测试清单

| 父 ASIN | Shop ID | 站点 | 父 Seller SKU | 有效批次 |
|---|---:|---|---|---|
| `B0EXAMPLE0` | 34455 | `AMAZON_FR` | `yuwang-fr` | 主批次 |
| `B0EXAMPLE0` | 34454 | `AMAZON_DE` | `GS-7HA4-8FTY` | 主批次 |
| `B0EXAMPLE0` | 34504 | `AMAZON_UK` | `fu-tight85` | 主批次 |
| `B0EXAMPLE0` | 1561 | `AMAZON_UK` | `MR04603PP07` | 主批次 |
| `B0EXAMPLE0` | 1556 | `AMAZON_DE` | `N08658-02-DE-PP` | 主批次 |
| `B0EXAMPLE0` | 1596 | `AMAZON_US` | `WY03134-05-SP-KOKUS` | 主批次 |
| `B0EXAMPLE0` | 1562 | `AMAZON_US` | `SP-WY03139-HIUS` | 主批次 |
| `B0EXAMPLE0` | 1596 | `AMAZON_US` | `FS04084-XIN` | 主批次 |
| `B0EXAMPLE0` | 33451 | `AMAZON_US` | `LickMat_2pcs` | 主批次 |
| `B0EXAMPLE0` | 1596 | `AMAZON_US` | `Q0-3INS-6L7O` | 主批次 |
| `B0EXAMPLE0` | 55454 | `AMAZON_US` | `FS02997-FU` | 主批次 |
| `B0EXAMPLE0` | 1562 | `AMAZON_US` | `FS03040-PP` | 主批次 |
| `B0EXAMPLE0` | 72530 | `AMAZON_DE` | `gen-JJ13149-03-DE` | 主批次 |
| `B0EXAMPLE0` | 1596 | `AMAZON_US` | `0P-BJAZ-IVS0` | 主批次 |
| `B0EXAMPLE0` | 1562 | `AMAZON_US` | `RJ-FS02824-1` | 主批次 |
| `B0EXAMPLE0` | 1578 | `AMAZON_US` | `2-Pack-Swim-Caps` | 主批次 |
| `B0EXAMPLE0` | 1596 | `AMAZON_US` | `SP-FS03344-KOKUS-1pcs` | 主批次 |
| `B0EXAMPLE0` | 1562 | `AMAZON_US` | `FS04026-zhu` | 主批次 |
| `B0EXAMPLE0` | 36451 | `AMAZON_US` | `FS03291-0827WHITE` | 主批次 |
| `B0EXAMPLE0` | 1596 | `AMAZON_US` | `LZH-FS03001` | 补跑批次 |

## 3. MySQL 核验结果

按每个父 ASIN 选择最新的成功 Job 后，只读交叉核验结果如下：

| 核验项 | 结果 |
|---|---:|
| 不同父 ASIN | 20 |
| 成功 Job | 20 |
| 具有结果引用的 Run | 20 |
| 原始事实记录 | 200（每个 Run 10 条） |
| 成功事实调用 | 165 |
| 失败事实调用 | 35 |
| 事实快照 | 20 |
| 信号发生记录 | 328 |
| Outbox | 20 |
| `SUPPRESSED` Outbox | 20 |
| 持久化结构违规 | 0 |
| 当前活跃 Job | 0 |

主批次保留 1 个 `DEAD` Job，补跑批次保留同一 ASIN 的成功 Job。这是故障与恢复的完整审计
轨迹，不代表仍有未处理队列任务。

## 4. 数据质量边界

20 个有效 Run 的状态均为 `BLOCKED`，不能解释为 20 份完整、可执行的经营结论。主要原因是
核心事实缺口：`erp_asin_full_detail` 在 20 个 Run 中均超时、限流或返回 `isError=true`；其
调用参数已与线上工具文档核对，使用 `asin`、`siteCode`、`includeReviews`，未发现本地参数
错误。部分业务报告、库存、自然广告流量、库存费用和退款率调用也发生超时或业务错误。

因此，本次已验证的是“20 个真实经营单元的完整巡检控制流和 MySQL 持久化链路”，尚未验证
“20 个经营单元都有 10/10 可用事实并产出可执行建议”。在 AZListing 修复
`erp_asin_full_detail` 的 SLA 前，继续保持 `INTERNAL_ONLY`，不得把 `BLOCKED` 结果外发为
正式经营建议。

## 5. 复现与审计

一次性驱动脚本为 `scripts/run_real_patrol_batch.py`。它强制要求 `INTERNAL_ONLY`、关闭所有
外部投递路径、检查无活跃/死亡任务、实时验真父 ASIN，并输出每个 Job 的事实、快照、信号和
Outbox 证据。凭据仅从运行环境读取，不写入脚本、报告或数据库业务载荷。

## 6. 2026-08-05 重采结果

为提高真实事实完整率，详情调用默认关闭不稳定的深度评论抓取，并在本次重采中使用 90 秒
超时、低并发和 3 次尝试。MCP 运行参数支持环境覆盖，常规配置仍使用保守默认值。重采驱动
为 `scripts/retry_real_patrol_batch.py`，它从指定来源批次读取已验证经营单元，不调用当前无
权限的清单 Tool，不修改旧批次。

20-ASIN 重采批次 `batch_41c4c1937c57470c87a0d483` 的 20 个 Job 全部成功，首次达到
199/200 项事实成功。唯一缺项是法国站点 `B0EXAMPLE0` 的 `erp_asin_full_detail`；验证发现
该站点需要显式邮编。系统增加可审计的 `AZLISTING_STOREFRONT_ZIP_CODES_JSON` 配置后，使用
法国测试邮编通过定点补采批次 `batch_23308a029b90496fa9cdc755` 成功取得第 200 项事实。

按每个父 ASIN 选择最新成功 Run 后，最终统一核验为：

| 核验项 | 最终结果 |
|---|---:|
| 不同父 ASIN | 20 |
| 成功事实调用 | 200/200 |
| 失败事实调用 | 0 |
| 事实快照 | 20 |
| 信号发生记录 | 332 |
| `SUPPRESSED` Outbox | 20 |
| `BLOCKED` Run | 0 |
| 持久化结构违规 | 0 |
| 活跃 Job | 0 |

20 个 Run 均为 `COMPLETED_WITH_GAPS`，不是 `COMPLETED`。剩余缺口不属于工具调用失败：20 个
单元均存在未映射字段、净现金回收缺失、流量 Tool 只有窗口聚合而无逐日序列，以及规则点位
覆盖限制；2 个单元缺库存天数，1 个单元缺广告目标配置。这些需要补字段合同、成本 Ontology、
逐日流量源、广告配置或规则覆盖，不能通过重复请求伪造。

`includeReviews=true` 即使在 90 秒超时下，对美站和法站仍连续返回 MCP 业务错误。因此本次
200/200 表示十个正式巡检事实 Tool 都成功返回并通过身份合同，不表示深度评论外部能力已
可用。需要深度评论时仍应由 AZListing 修复该能力并冻结 SLA。

工作台当前态口径已于 2026-08-05 修正：批次筛选只展示该批次最后一次 Run 实际再次检出的
`DETECTED/DATA_GAP/RECURRED`，不再把 `MISSED/RECOVERED` 或仅因其它数据不完整而扫描到的
历史信号算作本轮异常。按此口径，主重采批次当前检出 8 条业务异常、79 条数据缺口；法国
定点补采当前检出 0 条业务异常、4 条数据缺口。合计为 8 条当前业务异常和 83 条当前数据缺口。
此前记录的 332 条是不可变信号发生记录总数，包含检测、缺口扫描、未再次命中与恢复等生命
周期事件，不能解释为 332 条当前异常。
