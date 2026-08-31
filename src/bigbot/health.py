from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import web

StatusProvider = Callable[[], Awaitable[dict[str, object]]]


class HealthServer:
    def __init__(self, host: str, port: int, status_provider: StatusProvider) -> None:
        self.host = host
        self.port = port
        self._status_provider = status_provider
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/healthz", self._health)
        app.router.add_get("/readyz", self._ready)
        app.router.add_get("/status", self._status)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()

    async def close(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _ready(self, request: web.Request) -> web.Response:
        status = await self._status_provider()
        ready = bool(status.get("ready", False))
        return web.json_response(status, status=200 if ready else 503)

    async def _status(self, request: web.Request) -> web.Response:
        return web.json_response(await self._status_provider())
