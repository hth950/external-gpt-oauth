"""Background workers that execute queued GPT jobs."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.config import Settings
from app.db import JobRecord, JobStore
from app.upstream import UpstreamClient, UpstreamHTTPError

logger = logging.getLogger(__name__)


class QueueWorkerPool:
    def __init__(self, *, store: JobStore, upstream: UpstreamClient, settings: Settings):
        self.store = store
        self.upstream = upstream
        self.settings = settings
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        for index in range(self.settings.queue_concurrency):
            self._tasks.append(asyncio.create_task(self._worker_loop(index)))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def _worker_loop(self, index: int) -> None:
        logger.info("queue worker %s started", index)
        lease_seconds = self.settings.job_timeout_seconds + 60
        while not self._stop.is_set():
            job = self.store.claim_next(lease_seconds=lease_seconds)
            if job is None:
                await asyncio.sleep(self.settings.worker_poll_interval_seconds)
                continue
            await self._run_job(job)

    async def _run_job(self, job: JobRecord) -> None:
        current = self.store.get(job.id)
        if current and current.cancel_requested:
            self.store.request_cancel(job.id)
            return
        try:
            response = await asyncio.wait_for(
                self._dispatch(job), timeout=self.settings.job_timeout_seconds
            )
        except asyncio.TimeoutError:
            self.store.fail_or_retry(
                job.id,
                {
                    "type": "timeout",
                    "message": f"job exceeded {self.settings.job_timeout_seconds}s",
                },
            )
            return
        except UpstreamHTTPError as exc:
            self.store.fail_or_retry(
                job.id,
                {
                    "type": "upstream_http_error",
                    "status_code": exc.status_code,
                    "payload": exc.payload,
                },
            )
            return
        except (httpx.HTTPError, OSError) as exc:
            self.store.fail_or_retry(
                job.id,
                {"type": exc.__class__.__name__, "message": str(exc)},
            )
            return
        except Exception as exc:  # noqa: BLE001 - persist unexpected worker errors
            logger.exception("job %s failed unexpectedly", job.id)
            self.store.fail_or_retry(
                job.id,
                {"type": exc.__class__.__name__, "message": str(exc)},
            )
            return

        latest = self.store.get(job.id)
        if latest and latest.cancel_requested:
            self.store.request_cancel(job.id)
            return
        self.store.complete(job.id, response)

    async def _dispatch(self, job: JobRecord) -> dict[str, Any]:
        payload = dict(job.request_json)
        payload.pop("stream", None)
        payload.pop("background", None)
        if job.endpoint == "responses":
            chat_payload = _responses_to_chat_payload(payload)
            chat_response = await self.upstream.create_chat_completion(chat_payload)
            return _chat_completion_to_response(job, payload, chat_response)
        if job.endpoint == "chat.completions":
            return await self.upstream.create_chat_completion(payload)
        raise RuntimeError(f"unsupported endpoint: {job.endpoint}")


def _responses_to_chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
    chat_payload: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": _responses_payload_to_messages(payload),
    }
    for key in (
        "frequency_penalty",
        "max_completion_tokens",
        "max_tokens",
        "n",
        "presence_penalty",
        "stop",
        "temperature",
        "top_p",
    ):
        if key in payload:
            chat_payload[key] = payload[key]
    return chat_payload


def _responses_payload_to_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    if "input" in payload:
        return _responses_input_to_messages(payload.get("input"))

    system_prompt = _first_string(payload, ("system", "system_prompt"))
    user_prompt = _first_string(payload, ("usr", "user", "user_prompt", "prompt"))
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if user_prompt:
        messages.append({"role": "user", "content": user_prompt})
    return messages or [{"role": "user", "content": ""}]


def _responses_input_to_messages(input_value: Any) -> list[dict[str, str]]:
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if not isinstance(input_value, list):
        return [{"role": "user", "content": ""}]

    messages: list[dict[str, str]] = []
    for item in input_value:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            messages.append({"role": "user", "content": str(item)})
            continue
        role = item.get("role") or "user"
        if role == "developer":
            role = "system"
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        messages.append({"role": role, "content": _content_to_text(item.get("content"))})
    return messages or [{"role": "user", "content": ""}]


def _first_string(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return str(content)


def _chat_completion_to_response(
    job: JobRecord, original_payload: dict[str, Any], chat_response: dict[str, Any]
) -> dict[str, Any]:
    choice = (chat_response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output_text = message.get("content") or ""
    return {
        "id": job.id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": chat_response.get("model") or original_payload.get("model"),
        "output": [
            {
                "id": f"msg_{job.id}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": output_text,
                        "annotations": [],
                    }
                ],
            }
        ],
        "output_text": output_text,
        "usage": _responses_usage(chat_response.get("usage") or {}),
        "metadata": {
            "job_id": job.id,
            "upstream_chat_completion_id": chat_response.get("id"),
        },
    }


def _responses_usage(chat_usage: dict[str, Any]) -> dict[str, Any]:
    input_tokens = chat_usage.get("prompt_tokens", chat_usage.get("input_tokens", 0))
    output_tokens = chat_usage.get(
        "completion_tokens", chat_usage.get("output_tokens", 0)
    )
    total_tokens = chat_usage.get("total_tokens", input_tokens + output_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
