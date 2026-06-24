"""SQLite-backed durable job queue."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_STATUSES = {"queued", "in_progress"}


def now_ts() -> float:
    return time.time()


@dataclass(frozen=True)
class JobRecord:
    id: str
    endpoint: str
    request_json: dict[str, Any]
    status: str
    attempt_count: int
    max_attempts: int
    created_at: float
    updated_at: float
    started_at: float | None
    finished_at: float | None
    leased_until: float | None
    response_json: dict[str, Any] | None
    error_json: dict[str, Any] | None
    idempotency_key: str | None
    cancel_requested: bool


class QueueFullError(RuntimeError):
    pass


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    endpoint TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    leased_until REAL,
                    response_json TEXT,
                    error_json TEXT,
                    idempotency_key TEXT UNIQUE,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                    ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_lease
                    ON jobs(status, leased_until);
                """
            )

    def enqueue(
        self,
        *,
        endpoint: str,
        request_json: dict[str, Any],
        max_attempts: int,
        max_queue_size: int,
        idempotency_key: str | None = None,
    ) -> JobRecord:
        existing = self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing

        ts = now_ts()
        job_id = self._new_id(endpoint)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                active_count = conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'in_progress')"
                ).fetchone()[0]
                if active_count >= max_queue_size:
                    raise QueueFullError("queue is full")
                conn.execute(
                    """
                    INSERT INTO jobs (
                        id, endpoint, request_json, status, attempt_count,
                        max_attempts, created_at, updated_at, idempotency_key
                    )
                    VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        endpoint,
                        json.dumps(request_json, ensure_ascii=False),
                        max_attempts,
                        ts,
                        ts,
                        idempotency_key,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        record = self.get(job_id)
        if record is None:
            raise RuntimeError("failed to read enqueued job")
        return record

    def claim_next(self, *, lease_seconds: int) -> JobRecord | None:
        ts = now_ts()
        lease_until = ts + lease_seconds
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = 'queued'
                    ORDER BY created_at ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'in_progress',
                        attempt_count = attempt_count + 1,
                        started_at = COALESCE(started_at, ?),
                        updated_at = ?,
                        leased_until = ?
                    WHERE id = ?
                    """,
                    (ts, ts, lease_until, row["id"]),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get(row["id"])

    def complete(self, job_id: str, response_json: dict[str, Any]) -> None:
        ts = now_ts()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'completed',
                    response_json = ?,
                    error_json = NULL,
                    updated_at = ?,
                    finished_at = ?,
                    leased_until = NULL
                WHERE id = ?
                """,
                (json.dumps(response_json, ensure_ascii=False), ts, ts, job_id),
            )

    def fail_or_retry(self, job_id: str, error_json: dict[str, Any]) -> None:
        job = self.get(job_id)
        if job is None:
            return
        ts = now_ts()
        terminal = job.attempt_count >= job.max_attempts
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    error_json = ?,
                    updated_at = ?,
                    finished_at = CASE WHEN ? THEN ? ELSE finished_at END,
                    leased_until = NULL
                WHERE id = ?
                """,
                (
                    "failed" if terminal else "queued",
                    json.dumps(error_json, ensure_ascii=False),
                    ts,
                    1 if terminal else 0,
                    ts,
                    job_id,
                ),
            )

    def request_cancel(self, job_id: str) -> JobRecord | None:
        job = self.get(job_id)
        if job is None:
            return None
        ts = now_ts()
        if job.status == "queued":
            with self.connect() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'cancelled',
                        cancel_requested = 1,
                        updated_at = ?,
                        finished_at = ?,
                        leased_until = NULL
                    WHERE id = ? AND status = 'queued'
                    """,
                    (ts, ts, job_id),
                )
        elif job.status == "in_progress":
            with self.connect() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET cancel_requested = 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (ts, job_id),
                )
        return self.get(job_id)

    def restore_stale_running(self, *, max_attempts: int) -> int:
        ts = now_ts()
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = CASE
                        WHEN attempt_count >= max_attempts THEN 'failed'
                        ELSE 'queued'
                    END,
                    error_json = CASE
                        WHEN attempt_count >= max_attempts THEN ?
                        ELSE error_json
                    END,
                    updated_at = ?,
                    finished_at = CASE
                        WHEN attempt_count >= max_attempts THEN ?
                        ELSE finished_at
                    END,
                    leased_until = NULL
                WHERE status = 'in_progress'
                """,
                (
                    json.dumps(
                        {
                            "type": "server_restart",
                            "message": "job was in progress during server restart",
                        },
                        ensure_ascii=False,
                    ),
                    ts,
                    ts,
                ),
            )
            return cur.rowcount

    def prune_old_terminal(self, *, retention_hours: int) -> int:
        cutoff = now_ts() - retention_hours * 3600
        with self.connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM jobs
                WHERE status IN ('completed', 'failed', 'cancelled')
                  AND finished_at IS NOT NULL
                  AND finished_at < ?
                """,
                (cutoff,),
            )
            return cur.rowcount

    def get(self, job_id: str) -> JobRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def get_by_idempotency_key(self, key: str | None) -> JobRecord | None:
        if not key:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
        result = {status: 0 for status in ["queued", "in_progress", *TERMINAL_STATUSES]}
        for row in rows:
            result[row["status"]] = int(row["count"])
        return result

    @staticmethod
    def _new_id(endpoint: str) -> str:
        prefix = "resp" if endpoint == "responses" else "job"
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            endpoint=row["endpoint"],
            request_json=json.loads(row["request_json"]),
            status=row["status"],
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            leased_until=row["leased_until"],
            response_json=json.loads(row["response_json"]) if row["response_json"] else None,
            error_json=json.loads(row["error_json"]) if row["error_json"] else None,
            idempotency_key=row["idempotency_key"],
            cancel_requested=bool(row["cancel_requested"]),
        )

