import asyncio

import httpx

from api import create_app


def test_api_run_lifecycle_and_persistence(tmp_path, monkeypatch):
    app = create_app(tmp_path / "runs", tmp_path / "api.sqlite3")
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    monkeypatch.undo()

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).json()["status"] == "ok"

            response = await client.post("/runs", json={
                "goal": "screen EGFR",
                "mock": True,
                "steps": 2,
                "max_steps": 4,
            })
            assert response.status_code == 202
            task_id = response.json()["task_id"]

            state = await client.get(f"/runs/{task_id}")
            assert state.status_code == 200
            assert state.json()["task_id"] == task_id

            candidates = await client.get(f"/runs/{task_id}/candidates")
            assert candidates.status_code == 200
            assert len(candidates.json()["candidates"]) in {0, 3}

            approval = await client.post(f"/runs/{task_id}/approvals", json={
                "decision": "review",
                "reason": "manual check",
                "reviewer": "tester",
            })
            assert approval.status_code == 200

            assert (await client.post(f"/runs/{task_id}/pause")).status_code == 200
            assert (await client.post(f"/runs/{task_id}/cancel")).status_code == 200

    asyncio.run(run())


def test_api_missing_run_returns_404(tmp_path, monkeypatch):
    app = create_app(tmp_path / "runs", tmp_path / "api.sqlite3")
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    monkeypatch.undo()

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/runs/missing")).status_code == 404
            assert (await client.get("/runs/missing/candidates")).status_code == 404

    asyncio.run(run())
