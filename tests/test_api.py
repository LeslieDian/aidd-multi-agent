import asyncio
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import httpx

from api import create_app


def test_api_run_lifecycle_and_persistence(tmp_path, monkeypatch):
    app = create_app(tmp_path / "runs", tmp_path / "api.sqlite3")
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

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
                "decision": "approve",
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

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/runs/missing")).status_code == 404
            assert (await client.get("/runs/missing/candidates")).status_code == 404

    asyncio.run(run())


def test_api_rejects_invalid_approval_and_terminal_resume(tmp_path):
    app = create_app(tmp_path / "runs", tmp_path / "api.sqlite3")

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/runs", json={"goal": "screen", "mock": True, "steps": 1})
            task_id = created.json()["task_id"]
            invalid = await client.post(f"/runs/{task_id}/approvals", json={
                "decision": "review", "reason": "x", "reviewer": "tester"})
            assert invalid.status_code == 422
            await client.post(f"/runs/{task_id}/cancel")
            resumed = await client.post(f"/runs/{task_id}/resume")
            assert resumed.status_code == 409

    asyncio.run(run())


def test_api_resume_preserves_results_and_applies_instruction(tmp_path):
    app = create_app(tmp_path / "runs", tmp_path / "api.sqlite3")

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/runs", json={
                "goal": "screen", "mock": True, "steps": 1, "max_steps": 10,
                "seed_smiles": ["CCO"]})
            task_id = created.json()["task_id"]
            before = (await client.get(f"/runs/{task_id}")).json()
            before_candidates = (await client.get(f"/runs/{task_id}/candidates")).json()["candidates"]
            resumed = await client.post(f"/runs/{task_id}/resume",
                                        json={"instruction": "Keep the existing parent and continue"})
            assert resumed.status_code == 202
            after = (await client.get(f"/runs/{task_id}")).json()
            after_candidates = (await client.get(f"/runs/{task_id}/candidates")).json()["candidates"]
            assert after["revision"] == before["revision"] + 1
            assert after["steps_used"] > before["steps_used"]
            assert {c["candidate_id"] for c in before_candidates} <= {
                c["candidate_id"] for c in after_candidates}
            persisted = app.state.repository.load_state(task_id)
            assert persisted.instructions[-1]["text"] == "Keep the existing parent and continue"

    asyncio.run(run())


def test_api_allows_only_one_process_local_writer_per_task(tmp_path, monkeypatch):
    import api as api_module
    from agents.harness import CheckpointStore, TaskState

    app = create_app(tmp_path / "runs", tmp_path / "api.sqlite3")
    task_id = "same-task"
    store = CheckpointStore(tmp_path / "runs" / task_id, repository=app.state.repository)
    store.save(TaskState(task_id=task_id, goal="test", config={}))
    entered, release = Event(), Event()
    guard = Lock()
    calls = []

    class BlockingHarness:
        def __init__(self, store):
            self.store = store

        def run(self, max_actions):
            with guard:
                calls.append(max_actions)
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(api_module, "Harness", BlockingHarness)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(app.state.execute_run, task_id, 1)
        assert entered.wait(5)
        second = pool.submit(app.state.execute_run, task_id, 1)
        second.result(timeout=5)
        release.set()
        first.result(timeout=5)
    assert calls == [1]
    assert app.state.single_writer_scope == "one FastAPI process"
