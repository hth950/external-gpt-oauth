"""FastAPI entrypoint for the dogok GPT OAuth queue proxy."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth_refresh import AuthRefreshLoop
from app.config import Settings, get_settings
from app.db import JobRecord, JobStore, QueueFullError
from app.oauth_proxy import OAuthProxyManager
from app.queue_worker import QueueWorkerPool
from app.schemas import (
    ChatCompletionCreateRequest,
    CompletedResponse,
    QueuedResponse,
    ResponseCreateRequest,
)
from app.upstream import UpstreamClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()
store = JobStore(settings.db_path)
upstream = UpstreamClient(
    base_url=settings.upstream_base_url,
    timeout_seconds=settings.job_timeout_seconds,
)
oauth_proxy = OAuthProxyManager(settings)
worker_pool = QueueWorkerPool(store=store, upstream=upstream, settings=settings)
auth_refresh_loop = AuthRefreshLoop(settings, oauth_proxy.restart)
bearer_scheme = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.require_api_key and not settings.api_key:
        raise RuntimeError("DOGOK_PROXY_API_KEY is required")
    store.init_schema()
    restored = store.restore_stale_running(max_attempts=settings.job_max_attempts)
    pruned = store.prune_old_terminal(retention_hours=settings.job_retention_hours)
    if restored or pruned:
        logger.info("queue startup cleanup: restored=%s pruned=%s", restored, pruned)
    await oauth_proxy.start()
    oauth_proxy.start_watchdog()
    auth_refresh_loop.start()
    worker_pool.start()
    try:
        yield
    finally:
        await worker_pool.stop()
        await auth_refresh_loop.stop()
        await oauth_proxy.stop_watchdog()
        await oauth_proxy.stop()


app = FastAPI(
    title="dogok external GPT OAuth proxy",
    description=(
        "LAN-only queued GPT proxy backed by ChatGPT/Codex OAuth. "
        "Use `Authorization: Bearer <DOGOK_PROXY_API_KEY>` for API requests."
    ),
    version="0.2.0",
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
    lifespan=lifespan,
)


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    if not settings.require_api_key:
        return
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "type": "authentication_error",
                    "message": "missing or invalid bearer token",
                }
            },
        )
    if credentials.credentials != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "type": "authentication_error",
                    "message": "missing or invalid bearer token",
                }
            },
        )


@app.get("/health/live")
async def health_live() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/health", dependencies=[Depends(require_auth)])
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "queue": store.counts(),
        "oauth_proxy": {
            "pid": oauth_proxy.pid,
            "healthy": await oauth_proxy.is_healthy(),
            "auth_file_exists": settings.oauth_file.exists(),
        },
        "settings": {
            "queue_concurrency": settings.queue_concurrency,
            "max_queue_size": settings.max_queue_size,
            "job_timeout_seconds": settings.job_timeout_seconds,
        },
    }


@app.get("/v1/models", dependencies=[Depends(require_auth)], tags=["models"])
async def models() -> dict[str, Any]:
    if not await oauth_proxy.is_healthy():
        await oauth_proxy.start()
    return await upstream.get_models()


@app.post(
    "/v1/responses",
    dependencies=[Depends(require_auth)],
    tags=["responses"],
    summary="Create a queued response job",
    description=(
        "Stores the request in SQLite and immediately returns a response id. "
        "Poll `GET /v1/responses/{response_id}` until status is `completed`."
    ),
    response_model=QueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_response(
    request: Request,
    body: ResponseCreateRequest = Body(
        ...,
        openapi_examples={
            "system_usr": {
                "summary": "Simplified system/usr payload",
                "value": {
                    "model": "gpt-5.4-mini",
                    "system": "You are a concise assistant.",
                    "usr": "Reply with exactly: hello",
                },
            },
            "responses_input": {
                "summary": "OpenAI-style Responses input",
                "value": {
                    "model": "gpt-5.4-mini",
                    "input": [
                        {"role": "system", "content": "You are concise."},
                        {"role": "user", "content": "Reply with exactly: hello"},
                    ],
                },
            },
        },
    ),
) -> JSONResponse:
    await _require_oauth_ready()
    payload = _model_payload(body)
    if payload.get("stream") is True:
        raise _bad_request("stream=true is not supported by this queued proxy")
    job = _enqueue(
        endpoint="responses",
        payload=payload,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    return JSONResponse(_pending_response(job), status_code=status.HTTP_202_ACCEPTED)


@app.get(
    "/v1/responses/{response_id}",
    dependencies=[Depends(require_auth)],
    tags=["responses"],
    summary="Retrieve a queued response result",
    response_model=CompletedResponse | QueuedResponse,
)
async def retrieve_response(response_id: str) -> dict[str, Any]:
    job = _get_job_or_404(response_id)
    if job.endpoint != "responses":
        raise HTTPException(status_code=404, detail="response not found")
    return _job_response(job)


@app.post(
    "/v1/responses/{response_id}/cancel",
    dependencies=[Depends(require_auth)],
    tags=["responses"],
    summary="Cancel a queued or running response job",
    response_model=CompletedResponse | QueuedResponse,
)
async def cancel_response(response_id: str) -> dict[str, Any]:
    job = store.request_cancel(response_id)
    if job is None or job.endpoint != "responses":
        raise HTTPException(status_code=404, detail="response not found")
    return _job_response(job)


@app.post(
    "/v1/chat/completions",
    dependencies=[Depends(require_auth)],
    tags=["chat"],
    summary="Create a queued chat completion job",
    description=(
        "Stores a Chat Completions request in SQLite and returns a job id. "
        "Poll `GET /v1/jobs/{job_id}` for the final result."
    ),
    response_model=QueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_chat_completion(
    request: Request,
    body: ChatCompletionCreateRequest = Body(
        ...,
        openapi_examples={
            "basic_chat": {
                "summary": "Basic chat completion payload",
                "value": {
                    "model": "gpt-5.4-mini",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            }
        },
    ),
) -> JSONResponse:
    await _require_oauth_ready()
    payload = _model_payload(body)
    if payload.get("stream") is True:
        raise _bad_request("stream=true is not supported by this queued proxy")
    job = _enqueue(
        endpoint="chat.completions",
        payload=payload,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    return JSONResponse(_pending_job(job), status_code=status.HTTP_202_ACCEPTED)


@app.get(
    "/v1/jobs/{job_id}",
    dependencies=[Depends(require_auth)],
    tags=["jobs"],
    summary="Retrieve a queued job result",
    response_model=CompletedResponse | QueuedResponse,
)
async def retrieve_job(job_id: str) -> dict[str, Any]:
    return _job_response(_get_job_or_404(job_id))


@app.post(
    "/v1/jobs/{job_id}/cancel",
    dependencies=[Depends(require_auth)],
    tags=["jobs"],
    summary="Cancel a queued or running job",
    response_model=CompletedResponse | QueuedResponse,
)
async def cancel_job(job_id: str) -> dict[str, Any]:
    job = store.request_cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_response(job)


def _enqueue(
    *, endpoint: str, payload: dict[str, Any], idempotency_key: str | None
) -> JobRecord:
    try:
        return store.enqueue(
            endpoint=endpoint,
            request_json=payload,
            max_attempts=settings.job_max_attempts,
            max_queue_size=settings.max_queue_size,
            idempotency_key=idempotency_key,
        )
    except QueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


async def _require_oauth_ready() -> None:
    if not settings.oauth_file.exists():
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "oauth_not_configured",
                    "message": "ChatGPT/Codex OAuth auth file is missing",
                }
            },
        )
    if not await oauth_proxy.is_healthy():
        await oauth_proxy.start()
    if not await oauth_proxy.is_healthy():
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "oauth_proxy_unavailable",
                    "message": "openai-oauth proxy is not healthy",
                }
            },
        )


def _model_payload(body: ResponseCreateRequest | ChatCompletionCreateRequest) -> dict[str, Any]:
    return body.model_dump(mode="json", exclude_none=True)


def _get_job_or_404(job_id: str) -> JobRecord:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _pending_response(job: JobRecord) -> dict[str, Any]:
    return {
        "id": job.id,
        "object": "response",
        "created_at": int(job.created_at),
        "status": job.status,
        "background": True,
        "metadata": {
            "job_id": job.id,
            "poll_url": f"/v1/responses/{job.id}",
        },
    }


def _pending_job(job: JobRecord) -> dict[str, Any]:
    return {
        "id": job.id,
        "object": "job",
        "endpoint": job.endpoint,
        "created_at": int(job.created_at),
        "status": job.status,
        "metadata": {
            "job_id": job.id,
            "poll_url": f"/v1/jobs/{job.id}",
        },
    }


def _job_response(job: JobRecord) -> dict[str, Any]:
    if job.status == "completed" and job.response_json is not None:
        response = dict(job.response_json)
        response.setdefault("id", job.id)
        response.setdefault("status", "completed")
        response.setdefault("metadata", {})
        if isinstance(response["metadata"], dict):
            response["metadata"].setdefault("job_id", job.id)
        return response
    if job.endpoint == "responses":
        response = _pending_response(job)
    else:
        response = _pending_job(job)
    response["status"] = job.status
    response["attempt_count"] = job.attempt_count
    response["max_attempts"] = job.max_attempts
    if job.error_json:
        response["error"] = job.error_json
    return response


def _bad_request(message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": {"type": "invalid_request_error", "message": message}},
    )
