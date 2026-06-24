"""HTTP client for the local openai-oauth proxy."""

from __future__ import annotations

from typing import Any

import httpx


class UpstreamClient:
    def __init__(self, *, base_url: str, timeout_seconds: int):
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(
            float(timeout_seconds), connect=10.0, read=float(timeout_seconds)
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(method, url, json=json_body)
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code, _safe_json(response))
        return _safe_json(response)

    async def get_models(self) -> dict[str, Any]:
        return await self.request("GET", "/models")

    async def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/responses", json_body=payload)

    async def create_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/chat/completions", json_body=payload)


class UpstreamHTTPError(RuntimeError):
    def __init__(self, status_code: int, payload: dict[str, Any]):
        super().__init__(f"upstream returned HTTP {status_code}")
        self.status_code = status_code
        self.payload = payload


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        data = {"text": response.text}
    return data if isinstance(data, dict) else {"data": data}

