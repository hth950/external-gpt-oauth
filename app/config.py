"""Runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


REASONING_EFFORT_VALUES = {"none", "minimal", "low", "medium", "high", "xhigh"}


def _reasoning_effort_env(name: str, default: str | None) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value == "":
        return None
    if value not in REASONING_EFFORT_VALUES:
        allowed = ", ".join(sorted(REASONING_EFFORT_VALUES))
        raise ValueError(f"{name} must be one of: {allowed}")
    return value


@dataclass(frozen=True)
class Settings:
    api_key: str
    require_api_key: bool
    enable_docs: bool
    default_reasoning_effort: str | None
    db_path: Path
    queue_concurrency: int
    max_queue_size: int
    job_timeout_seconds: int
    job_max_attempts: int
    job_retention_hours: int
    worker_poll_interval_seconds: float
    upstream_base_url: str
    oauth_command: str
    oauth_file: Path
    oauth_host: str
    oauth_port: int
    oauth_startup_timeout_seconds: float
    auth_watch_interval_seconds: float
    auth_refresh_margin_seconds: int
    auth_refresh_loop_interval_seconds: int


def get_settings() -> Settings:
    oauth_port = _int_env("GPT_OAUTH_PORT", 10531)
    return Settings(
        api_key=os.getenv("DOGOK_PROXY_API_KEY", ""),
        require_api_key=_bool_env("DOGOK_PROXY_REQUIRE_API_KEY", True),
        enable_docs=_bool_env("DOGOK_PROXY_ENABLE_DOCS", False),
        default_reasoning_effort=_reasoning_effort_env(
            "GPT_DEFAULT_REASONING_EFFORT", "low"
        ),
        db_path=Path(os.getenv("GPT_QUEUE_DB_PATH", "/app/data/jobs.sqlite3")),
        queue_concurrency=max(1, _int_env("GPT_QUEUE_CONCURRENCY", 2)),
        max_queue_size=max(1, _int_env("GPT_MAX_QUEUE_SIZE", 1000)),
        job_timeout_seconds=max(1, _int_env("GPT_JOB_TIMEOUT_SECONDS", 900)),
        job_max_attempts=max(1, _int_env("GPT_JOB_MAX_ATTEMPTS", 3)),
        job_retention_hours=max(1, _int_env("GPT_JOB_RETENTION_HOURS", 24)),
        worker_poll_interval_seconds=max(
            0.1, _float_env("GPT_WORKER_POLL_INTERVAL_SECONDS", 0.5)
        ),
        upstream_base_url=os.getenv(
            "GPT_UPSTREAM_BASE_URL", f"http://127.0.0.1:{oauth_port}/v1"
        ).rstrip("/"),
        oauth_command=os.getenv("GPT_OAUTH_COMMAND", "openai-oauth"),
        oauth_file=Path(os.getenv("GPT_OAUTH_FILE", "/auth/auth.json")),
        oauth_host=os.getenv("GPT_OAUTH_HOST", "127.0.0.1"),
        oauth_port=oauth_port,
        oauth_startup_timeout_seconds=max(
            1.0, _float_env("GPT_OAUTH_STARTUP_TIMEOUT_SECONDS", 20.0)
        ),
        auth_watch_interval_seconds=max(
            0.5, _float_env("GPT_AUTH_WATCH_INTERVAL_SECONDS", 3.0)
        ),
        auth_refresh_margin_seconds=max(
            60, _int_env("GPT_AUTH_REFRESH_MARGIN_SECONDS", 7 * 24 * 60 * 60)
        ),
        auth_refresh_loop_interval_seconds=max(
            60, _int_env("GPT_AUTH_REFRESH_LOOP_INTERVAL_SECONDS", 3600)
        ),
    )
