from __future__ import annotations

from aiohttp import ClientSession

from bigbot.health import HealthServer
from bigbot.public_api import StoryFeedQuery


async def test_health_server_reports_liveness_readiness_and_status() -> None:
    async def status() -> dict[str, object]:
        return {"status": "ok", "ready": True, "stories": 4}

    server = HealthServer("127.0.0.1", 0, status)
    await server.start()
    assert server._runner is not None
    port = int(server._runner.addresses[0][1])

    async with ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/healthz") as response:
            assert response.status == 200
            assert await response.json() == {"status": "ok"}
        async with session.get(f"http://127.0.0.1:{port}/readyz") as response:
            assert response.status == 200
            assert (await response.json())["stories"] == 4
        async with session.get(f"http://127.0.0.1:{port}/status") as response:
            assert response.status == 200
            assert (await response.json())["ready"] is True

    await server.close()
    await server.close()


async def test_readiness_fails_closed() -> None:
    async def status() -> dict[str, object]:
        return {"status": "starting", "ready": False}

    server = HealthServer("127.0.0.1", 0, status)
    await server.start()
    assert server._runner is not None
    port = int(server._runner.addresses[0][1])
    async with (
        ClientSession() as session,
        session.get(f"http://127.0.0.1:{port}/readyz") as response,
    ):
        assert response.status == 503
    await server.close()


async def test_public_story_feed_is_bounded_and_cors_is_allowlisted() -> None:
    async def status() -> dict[str, object]:
        return {"ready": True}

    async def stories(query: StoryFeedQuery) -> dict[str, object]:
        return {"stories": [{"id": query.limit, "q": query.search, "tags": query.tags}]}

    async def detail(story_id: int) -> dict[str, object] | None:
        return {"story": {"id": story_id}} if story_id == 12 else None

    server = HealthServer(
        "127.0.0.1",
        0,
        status,
        story_feed_provider=stories,
        story_detail_provider=detail,
        cors_origins=("http://127.0.0.1:4173",),
    )
    await server.start()
    assert server._runner is not None
    port = int(server._runner.addresses[0][1])
    async with ClientSession() as session:
        async with session.get(
            f"http://127.0.0.1:{port}/api/v1/stories?limit=12&q=vote&tag=World",
            headers={"Origin": "http://127.0.0.1:4173"},
        ) as response:
            assert response.status == 200
            assert (await response.json())["stories"] == [
                {"id": 12, "q": "vote", "tags": ["World"]}
            ]
            assert response.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:4173"
        async with session.get(f"http://127.0.0.1:{port}/api/v1/stories?limit=101") as response:
            assert response.status == 400
        async with session.get(f"http://127.0.0.1:{port}/api/v1/stories/12") as response:
            assert response.status == 200
            assert (await response.json())["story"]["id"] == 12
        async with session.get(f"http://127.0.0.1:{port}/api/v1/stories/13") as response:
            assert response.status == 404
    await server.close()
