from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import web

from bigbot.public_api import StoryFeedQuery

StatusProvider = Callable[[], Awaitable[dict[str, object]]]
StoryFeedProvider = Callable[[StoryFeedQuery], Awaitable[dict[str, object]]]
StoryDetailProvider = Callable[[int], Awaitable[dict[str, object] | None]]


class HealthServer:
    def __init__(
        self,
        host: str,
        port: int,
        status_provider: StatusProvider,
        *,
        story_feed_provider: StoryFeedProvider | None = None,
        story_detail_provider: StoryDetailProvider | None = None,
        cors_origins: tuple[str, ...] = (),
    ) -> None:
        self.host = host
        self.port = port
        self._status_provider = status_provider
        self._story_feed_provider = story_feed_provider
        self._story_detail_provider = story_detail_provider
        self._cors_origins = frozenset(origin.rstrip("/") for origin in cors_origins)
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/healthz", self._health)
        app.router.add_get("/readyz", self._ready)
        app.router.add_get("/status", self._status)
        if self._story_feed_provider is not None:
            app.router.add_get("/api/v1/stories", self._stories)
        if self._story_detail_provider is not None:
            app.router.add_get("/api/v1/stories/{story_id}", self._story_detail)
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

    async def _stories(self, request: web.Request) -> web.Response:
        if self._story_feed_provider is None:
            raise web.HTTPNotFound()
        try:
            limit = int(request.query.get("limit", "15"))
        except ValueError as exc:
            raise web.HTTPBadRequest(text="limit must be an integer") from exc
        if not 1 <= limit <= 50:
            raise web.HTTPBadRequest(text="limit must be between 1 and 50")
        search = request.query.get("q", "").strip()
        if len(search) > 100:
            raise web.HTTPBadRequest(text="q must be at most 100 characters")
        tags = tuple(dict.fromkeys(tag.strip() for tag in request.query.getall("tag", [])))
        if len(tags) > 5 or any(len(tag) > 40 for tag in tags):
            raise web.HTTPBadRequest(text="use at most 5 tags of 40 characters each")
        query = StoryFeedQuery(
            limit=limit,
            cursor=request.query.get("cursor"),
            search=search,
            tags=tags,
        )
        try:
            payload = await self._story_feed_provider(query)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return self._public_json(request, payload)

    async def _story_detail(self, request: web.Request) -> web.Response:
        if self._story_detail_provider is None:
            raise web.HTTPNotFound()
        try:
            story_id = int(request.match_info["story_id"])
        except ValueError as exc:
            raise web.HTTPNotFound() from exc
        payload = await self._story_detail_provider(story_id)
        if payload is None:
            raise web.HTTPNotFound(text="story not found")
        return self._public_json(request, payload)

    def _public_json(self, request: web.Request, payload: dict[str, object]) -> web.Response:
        response = web.json_response(payload)
        response.headers["Cache-Control"] = "public, max-age=30, stale-while-revalidate=120"
        response.headers["X-Content-Type-Options"] = "nosniff"
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin in self._cors_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response
