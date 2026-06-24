"""Codex/ChatGPT OAuth token refresh for the auth.json file."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_EXCHANGE_URL = "https://auth.openai.com/oauth/token"


class AuthRefreshLoop:
    def __init__(self, settings: Settings, restart_proxy):
        self.settings = settings
        self.restart_proxy = restart_proxy
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def ensure_valid(self) -> bool:
        async with self._lock:
            auth = read_auth_file(self.settings.oauth_file)
            if auth is None:
                return False
            if not refresh_due(auth, self.settings.auth_refresh_margin_seconds):
                return True
            try:
                tokens = await refresh_access_token(auth["refresh_token"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to refresh OAuth token: %s", exc)
                return False

            merged = {
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token") or auth["refresh_token"],
                "id_token": tokens.get("id_token") or auth.get("id_token"),
                "account_id": tokens.get("account_id") or auth.get("account_id") or "",
            }
            write_auth_file(self.settings.oauth_file, merged)
            try:
                await self.restart_proxy()
            except Exception as exc:  # noqa: BLE001
                logger.warning("OAuth proxy restart after refresh failed: %s", exc)
            return True

    async def _loop(self) -> None:
        while True:
            try:
                await self.ensure_valid()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("auth refresh loop error: %s", exc)
            await asyncio.sleep(self.settings.auth_refresh_loop_interval_seconds)


def read_auth_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    tokens = raw.get("tokens") if isinstance(raw, dict) else None
    if not isinstance(tokens, dict):
        return None
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not access_token or not refresh_token:
        return None
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": tokens.get("id_token"),
        "account_id": tokens.get("account_id") or extract_account_id(tokens),
        "expires_at": extract_expiry_seconds(access_token),
    }


def refresh_due(auth: dict[str, Any], margin_seconds: int) -> bool:
    expires_at = auth.get("expires_at")
    if not expires_at:
        return True
    return float(expires_at) - time.time() < margin_seconds


async def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            TOKEN_EXCHANGE_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", refresh_token),
        "id_token": data.get("id_token"),
        "account_id": extract_account_id(data),
    }


def write_auth_file(path: Path, tokens: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": tokens.get("id_token"),
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "account_id": tokens.get("account_id") or "",
        },
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def extract_expiry_seconds(token: str | None) -> int | None:
    claims = decode_jwt_claims(token)
    exp = claims.get("exp") if claims else None
    if isinstance(exp, (int, float)):
        return int(exp)
    return None


def extract_account_id(data: dict[str, Any]) -> str | None:
    for token_key in ("id_token", "access_token"):
        claims = decode_jwt_claims(data.get(token_key))
        if not claims:
            continue
        if isinstance(claims.get("chatgpt_account_id"), str):
            return claims["chatgpt_account_id"]
        auth_claim = claims.get("https://api.openai.com/auth")
        if isinstance(auth_claim, dict) and isinstance(
            auth_claim.get("chatgpt_account_id"), str
        ):
            return auth_claim["chatgpt_account_id"]
        orgs = claims.get("organizations")
        if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict):
            org_id = orgs[0].get("id")
            if isinstance(org_id, str):
                return org_id
    return None


def decode_jwt_claims(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None

