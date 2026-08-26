# AZListing MCP 内部冻结基线与外部门禁

> 日期：2026-08-04  
> 内部合同：`amazon_ops.azlisting_mcp_input.internal.v2`  
> 外部状态：`PENDING_OWNER_FREEZE`  
> 结论：巡检侧已冻结可执行的请求、身份和最小事实基线；MCP Owner 尚未签字，不能进入 SHADOW。

## 1. 两种“冻结”必须分开

巡检侧机器合同位于 `contracts/azlisting-mcp-input.internal.v2.json`，v1 保留为历史基线，代码源位于
`clients/azlisting_contract.py`。它固定 2 个互斥经营单元 Tool、10 个事实 Tool、请求字段、核心级别、
身份规则和最小事实字段组，并由合同测试防止漂移。

这不等于外部合同已冻结。完整 33 Tool 清单和 `outputFields()` 字段字典已经收到；其中 #30
全量清单声明 `shopId/userId`，但没有声明 `parentAsin/parentSellerSku`；#9 备用 Provider 已实现，
但依赖权威负责人/店铺/站点映射，且其响应只有负责人姓名查询上下文，没有数字负责人 ID。
测试网关的 #30 仍缺权限，父体详情仍有超时，真实金额/比例口径、时区、SLA 和运行枚举策略也
没有 Owner 签字。因此配置和代码均保持 `external_contract_status=PENDING_OWNER_FREEZE`。

## 2. 运行时失败关闭

所有 MCP 原始事实在进入 Normalizer 前统一执行 `facts.contract_validator`：

- 产品子体每行必须显式返回当前 `parentAsin` 和子 `asin`；
- 父体详情必须恰好一行并显式返回当前父 ASIN，不允许请求参数兜底；
- 任一 Tool 若携带 `shopId/shopAccount/siteCode/parentAsin/parentSellerSku`，必须与经营单元一致；
- #30 每条活动记录必须携带正整数 `userId`；相同经营单元的多个 `userId` 聚合成负责人集合，
  不参与经营单元 ID 派生，也不按负责人拆分经营单元；
- 四个核心 Tool 的非空结果必须命中冻结的最小字段组；
- Tool 名与采集键不一致、非对象行、串店、串站点、串父体和串父 SKU 均返回
  `MCP_CONTRACT_INVALID`，不进入快照和规则；
- 可降级 Tool 的未知字段继续作为未解析/数据缺口处理，但身份串号仍失败关闭。

该校验位于 Normalizer 入口，因此实时采集和 MySQL 原始事实复用使用同一规则。事实快照 `_meta`
记录内部合同版本和外部冻结状态，便于审计历史运行按哪版字段解释。

## 3. 配置与启用门禁

`config/settings.yaml` 必须声明与代码一致的内部版本和外部状态。组合根在创建 MCP Client 前检查
两者，预检也会报告版本或状态漂移。即使 MCP URL、Token、租约和白名单全部就绪，只要外部
状态不是代码审计确认的 `FROZEN`，启用 Scheduler 就会得到
`MCP_EXTERNAL_CONTRACT_NOT_FROZEN`。

不得只修改 YAML 把状态改成 `FROZEN`。外部冻结必须同时具备：

1. 经营单元枚举采用“#30 补父字段”或“#9 + 非 SQLite 权威负责人/店铺/站点映射”，并具备只读权限；
   如果采用 #9，还必须提供可核验的数字负责人 ID，不能将负责人姓名直接回传中控；
2. 分页稳定、Inactive、重复父体和冲突绑定的真实脱敏响应样本；
3. 以已提供的十个事实 Tool 字段字典为基线，验证单位、币种、比例、时区、空值和截止时间；
4. `erp_asin_full_detail` SLA 与超时修复；
5. 脱敏样本验收无阻断缺口；
6. MCP Owner 与巡检 Owner 对同一合同版本签字。

满足后应以新变更同时更新代码状态、机器合同、配置、合同测试和本报告，不允许单点解锁。
