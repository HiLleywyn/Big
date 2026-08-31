from __future__ import annotations

from aiohttp import ClientSession

from bigbot.health import HealthServer


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
