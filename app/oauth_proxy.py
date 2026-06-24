"""Lifecycle management for the openai-oauth subprocess."""

from __future__ import annotations

import asyncio
import logging
import shutil
from asyncio.subprocess import Process
from pathlib import Path

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class OAuthProxyManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._proc: Process | None = None
        self._lock = asyncio.Lock()
        self._watch_task: asyncio.Task | None = None
        self._last_mtime: float | None = None

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc and proc.returncode is None else None

    async def start(self) -> bool:
        async with self._lock:
            if self._is_running():
                return True
            if not self.settings.oauth_file.exists():
                logger.warning("OAuth auth file is missing: %s", self.settings.oauth_file)
                return False
            binary = shutil.which(self.settings.oauth_command)
            if not binary:
                logger.warning("openai-oauth command not found: %s", self.settings.oauth_command)
                return False
            cmd = [
                binary,
                "--oauth-file",
                str(self.settings.oauth_file),
                "--host",
                self.settings.oauth_host,
                "--port",
                str(self.settings.oauth_port),
            ]
            logger.info("starting openai-oauth: %s", " ".join(cmd))
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        healthy = await self.wait_healthy(self.settings.oauth_startup_timeout_seconds)
        if not healthy:
            logger.warning("openai-oauth did not become healthy")
        return healthy

    async def stop(self) -> None:
        async with self._lock:
            proc = self._proc
            if proc is None or proc.returncode is not None:
                self._proc = None
                return
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        async with self._lock:
            if self._proc is proc:
                self._proc = None

    async def restart(self) -> bool:
        await self.stop()
        return await self.start()

    async def wait_healthy(self, timeout_seconds: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if await self.is_healthy():
                return True
            await asyncio.sleep(0.5)
        return False

    async def is_healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self.settings.upstream_base_url}/models")
            return response.status_code == 200
        except Exception:
            return False

    def start_watchdog(self) -> None:
        if self._watch_task is None or self._watch_task.done():
            self._last_mtime = _mtime(self.settings.oauth_file)
            self._watch_task = asyncio.create_task(self._watch_auth_file())

    async def stop_watchdog(self) -> None:
        task = self._watch_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._watch_task = None

    async def _watch_auth_file(self) -> None:
        while True:
            await asyncio.sleep(self.settings.auth_watch_interval_seconds)
            current = _mtime(self.settings.oauth_file)
            if current != self._last_mtime:
                self._last_mtime = current
                if current is None:
                    await self.stop()
                else:
                    await self.restart()

    def _is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None

