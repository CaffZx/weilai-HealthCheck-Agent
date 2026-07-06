# weilai-HealthCheck-Agent

亚马逊业务巡检 Agent。围绕"频繁修改知识库 / MCP 工具"设计，配置、知识库、数据接入、判定核心、Web 测试页彼此解耦。

## 目录

```
weilai-HealthCheck-Agent/
├── config/         # 网关、数据源、运行参数（改动最勤，独立版本管理）
├── knowledge/      # 巡检知识库（8 个模块 MD，热加载）
├── data/           # 数据接入层：MCP / ERP / StarRocks / State
├── core/           # 判定编排：规则引擎 + LLM 判定 + 巡检主循环
├── web/            # 测试页（FastAPI backend + 前端）
├── scripts/        # 一次性脚本（同步 shop 映射、导入知识库等）
├── tests/          # smoke test / 契约测试
└── logs/
```

## 数据链路

| 组件 | 地址 | 用途 |
|---|---|---|
| ERP MySQL | `10.0.0.0:3306 / app_db` | 配置表 + 4 张核心落库表 |
| StarRocks | `10.0.0.0:9030 / app_db` | 大盘指标、`dwd_shop` 店铺映射 |
| State DB  | `127.0.0.1:3307 / app_db` | agent 中间态 |
| MCP 网关（内） | `http://10.0.0.0:7089/mcp` | 51 个业务工具，优先走此 |
| MCP 网关（外） | `http://mcp-gateway.example.com/mcp` | 备用 |

## 快速开始

```bash
cp .env.example .env  # 填密钥
pip install -r requirements.txt
python -m web.backend.main   # 启动测试页
```

## 三地备份

- 本地：`/Users/weilai/work_flod/weilai-HealthCheck-Agent`
- 服务器：`36.140.52.167:/opt/weilai-HealthCheck-Agent`
- Git：见根目录 `.git/config` 的 remote
