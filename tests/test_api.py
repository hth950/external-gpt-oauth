import importlib
import asyncio

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def test_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DOGOK_PROXY_API_KEY", "test-key")
    monkeypatch.setenv("GPT_QUEUE_DB_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("GPT_QUEUE_CONCURRENCY", "1")
    monkeypatch.setenv("GPT_OAUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setenv("GPT_OAUTH_STARTUP_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("GPT_JOB_TIMEOUT_SECONDS", "5")

    import app.config
    import app.main

    (tmp_path / "auth.json").write_text("{}", encoding="utf-8")

    importlib.reload(app.config)
    main = importlib.reload(app.main)

    async def fake_start():
        return True

    async def fake_stop():
        return None

    async def fake_healthy():
        return True

    main.oauth_proxy.start = fake_start
    main.oauth_proxy.stop = fake_stop
    main.oauth_proxy.is_healthy = fake_healthy
    main.oauth_proxy.start_watchdog = lambda: None
    main.oauth_proxy.stop_watchdog = fake_stop

    async def fake_create_chat_completion(payload):
        assert payload["messages"] == [{"role": "user", "content": "hello"}]
        return {
            "id": "chatcmpl-upstream-id",
            "object": "chat.completion",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"}}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    main.upstream.create_chat_completion = fake_create_chat_completion

    async with main.app.router.lifespan_context(main.app):
        yield main.app


@pytest.mark.asyncio
async def test_response_enqueue_and_poll(test_app):
    transport = ASGITransport(app=test_app)
    headers = {"Authorization": "Bearer test-key"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/responses",
            json={"model": "gpt-5.4", "input": "hello"},
            headers=headers,
        )
        assert created.status_code == 202
        response_id = created.json()["id"]

        final = None
        for _ in range(20):
            polled = await client.get(f"/v1/responses/{response_id}", headers=headers)
            assert polled.status_code == 200
            final = polled.json()
            if final["status"] == "completed":
                break
            await asyncio.sleep(0.05)
        assert final is not None
        assert final["status"] == "completed"
        assert final["output_text"] == "ok"
        assert final["metadata"]["upstream_chat_completion_id"] == "chatcmpl-upstream-id"


@pytest.mark.asyncio
async def test_auth_required(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 401
