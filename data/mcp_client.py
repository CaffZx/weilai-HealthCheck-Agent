"""统一 MCP JSON-RPC 客户端。配置读 config/mcp_tools.yaml，无硬编码工具名。"""
from __future__ import annotations
import os, json, yaml, httpx
from pathlib import Path

CFG = yaml.safe_load(Path(__file__).resolve().parent.parent.joinpath("config/mcp_tools.yaml").read_text())


class MCPClient:
    def __init__(self, gateway: str = "primary"):
        gw = CFG["gateways"][gateway]
        self.url = gw["url"]
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            gw["header"]: os.environ[gw["api_key_env"]],
        }
        self.timeout = gw["timeout_seconds"]
        self.session_id: str | None = None
        self._client = httpx.Client(timeout=self.timeout)

    def _post(self, payload: dict) -> httpx.Response:
        h = dict(self.headers)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return self._client.post(self.url, headers=h, content=json.dumps(payload))

    def initialize(self) -> None:
        resp = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "weilai-agent", "version": "1"}},
        })
        self.session_id = resp.headers.get("Mcp-Session-Id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, arguments: dict) -> dict:
        if self.session_id is None:
            self.initialize()
        resp = self._post({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        })
        text = resp.text
        # streamable-http 返回 `data: {...}` SSE-like 单帧
        i = text.find("data:")
        payload = json.loads(text[i + 5:]) if i >= 0 else resp.json()
        inner = payload["result"]["content"][0]["text"]
        return json.loads(json.loads(inner)["content"][0]["text"])
