from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

LEGACY_SQLITE_PATHS = (
    "/api/users",
    "/api/reports",
    "/api/asins",
    "/api/fixture",
    "/api/sync-health",
    "/api/batch/inspect",
    "/api/inspect",
    "/api/judge",
    "/api/products/action",
    "/api/review",
    "/api/tasks",
    "/api/anomalies",
    "/api/history",
    "/api/dashboard",
)


class LegacySqliteReadOnlyMiddleware:
    """永久退役旧 SQLite API；不存在配置或环境变量旁路。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._blocks(scope):
            body = (
                b'{"detail":{"code":"SQLITE_RETIRED",'
                b'"message":"SQLite is not supported; use the MySQL V2 API"}}'
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 410,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)

    def _blocks(self, scope: Scope) -> bool:
        if scope.get("type") != "http":
            return False
        path = str(scope.get("path") or "")
        return any(path == prefix or path.startswith(f"{prefix}/") for prefix in LEGACY_SQLITE_PATHS)
