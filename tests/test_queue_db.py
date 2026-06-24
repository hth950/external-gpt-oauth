from app.db import JobStore


def test_enqueue_idempotency_and_complete(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.init_schema()

    first = store.enqueue(
        endpoint="responses",
        request_json={"model": "gpt-5.4", "input": "hello"},
        max_attempts=3,
        max_queue_size=10,
        idempotency_key="abc",
    )
    second = store.enqueue(
        endpoint="responses",
        request_json={"model": "gpt-5.4", "input": "hello again"},
        max_attempts=3,
        max_queue_size=10,
        idempotency_key="abc",
    )

    assert first.id == second.id
    claimed = store.claim_next(lease_seconds=60)
    assert claimed is not None
    assert claimed.status == "in_progress"

    store.complete(claimed.id, {"id": claimed.id, "status": "completed"})
    done = store.get(claimed.id)
    assert done is not None
    assert done.status == "completed"
    assert done.response_json["status"] == "completed"


def test_cancel_queued_job(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.init_schema()
    job = store.enqueue(
        endpoint="chat.completions",
        request_json={"model": "gpt-5.4", "messages": []},
        max_attempts=3,
        max_queue_size=10,
    )

    cancelled = store.request_cancel(job.id)

    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert store.claim_next(lease_seconds=60) is None

