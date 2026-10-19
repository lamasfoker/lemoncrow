"""Run one service (Review, Dashboard) as a supervised worker process.

The worker binds an ephemeral loopback port, prints ``READY <url>`` once, and
then serves until its stdin closes -- which the core's exit guarantees even
when the core is killed, so a worker never outlives it. The token that guards
the worker (and the core's back-channel) is minted per spawn and passed by
environment, never argv.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from .errors import AgentAction, ErrorCode, ServerError
from .service_protocol import CORE_URL_ENV, SERVICE_TOKEN_ENV

__all__ = ["ServiceEndpoint", "ServiceProcess", "service_command"]

_LOG: Final[logging.Logger] = logging.getLogger("lemoncrow.server.services")
_READY_PREFIX: Final[str] = "READY "
_BACKOFF_MIN_S: Final[float] = 0.5
_BACKOFF_MAX_S: Final[float] = 30.0
#: A worker that stayed up this long crashed for a new reason: restart promptly.
_STABLE_AFTER_S: Final[float] = 60.0


@dataclass(frozen=True, slots=True)
class ServiceEndpoint:
    url: str
    token: str


def service_command(name: str, *, extra: Sequence[str] = ()) -> tuple[str, ...]:
    return (sys.executable, "-m", "lemoncrow_server_core", "service", "--name", name, *extra)


def _unavailable(name: str, reason: str) -> ServerError:
    return ServerError(
        ErrorCode.DISPATCHER_UNAVAILABLE,
        f"the LemonCrow {name} service is {reason}; retry shortly",
        details={"service": name},
        retryable=True,
        action=AgentAction.RETRY_LATER,
    )


class ServiceProcess:
    """Spawn, watch and restart one service worker."""

    def __init__(
        self,
        name: str,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        ready_timeout_s: float = 60.0,
        stop_timeout_s: float = 5.0,
    ) -> None:
        self.name = name
        self._command = tuple(command)
        self._env = dict(env or {})
        self._ready_timeout_s = ready_timeout_s
        self._stop_timeout_s = stop_timeout_s
        self._core_url = ""
        self._process: asyncio.subprocess.Process | None = None
        self._endpoint: ServiceEndpoint | None = None
        self._ready = asyncio.Event()
        self._stopping = False
        self._supervisor: asyncio.Task[None] | None = None
        self.restarts = 0

    @property
    def pid(self) -> int | None:
        process = self._process
        return None if process is None or process.returncode is not None else process.pid

    def token(self) -> str:
        endpoint = self._endpoint
        return "" if endpoint is None else endpoint.token

    def start(self, core_url: str) -> None:
        if self._supervisor is not None:
            raise RuntimeError(f"{self.name} service is already started")
        self._core_url = core_url
        self._supervisor = asyncio.create_task(self._supervise(), name=f"lemoncrow-service-{self.name}")

    async def endpoint(self, *, timeout_s: float) -> ServiceEndpoint:
        if self._supervisor is None or self._stopping:
            raise _unavailable(self.name, "not running")
        if not self._ready.is_set():
            try:
                await asyncio.wait_for(self._ready.wait(), timeout_s)
            except TimeoutError:
                raise _unavailable(self.name, "starting") from None
        endpoint = self._endpoint
        if endpoint is None:
            raise _unavailable(self.name, "restarting")
        return endpoint

    async def _supervise(self) -> None:
        backoff = _BACKOFF_MIN_S
        while not self._stopping:
            started = time.monotonic()
            try:
                process, endpoint = await self._spawn()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("%s service failed to start; retrying in %.1fs", self.name, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)
                continue
            self._process, self._endpoint = process, endpoint
            self._ready.set()
            _LOG.info("%s service ready at %s (pid %d)", self.name, endpoint.url, process.pid)
            returncode = await process.wait()
            self._ready.clear()
            self._endpoint = None
            if self._stopping:
                return
            self.restarts += 1
            if time.monotonic() - started > _STABLE_AFTER_S:
                backoff = _BACKOFF_MIN_S
            _LOG.warning("%s service exited with %s; restarting in %.1fs", self.name, returncode, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)

    async def _spawn(self) -> tuple[asyncio.subprocess.Process, ServiceEndpoint]:
        token = secrets.token_urlsafe(32)
        env = {**os.environ, **self._env, SERVICE_TOKEN_ENV: token, CORE_URL_ENV: self._core_url}
        process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            env=env,
        )
        try:
            url = await asyncio.wait_for(self._await_ready(process), self._ready_timeout_s)
        except BaseException:
            await self._terminate(process)
            raise
        return process, ServiceEndpoint(url=url, token=token)

    async def _await_ready(self, process: asyncio.subprocess.Process) -> str:
        assert process.stdout is not None
        while True:
            raw = await process.stdout.readline()
            if not raw:
                raise RuntimeError(f"{self.name} service exited before it was ready ({await process.wait()})")
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith(_READY_PREFIX):
                url = line[len(_READY_PREFIX) :].strip()
                if not url.startswith("http://127.0.0.1:"):
                    raise RuntimeError(f"{self.name} service announced an unexpected URL: {url!r}")
                return url
            if line:
                _LOG.info("%s service: %s", self.name, line)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        # Closing stdin is the worker's own shutdown signal; escalate only if it hangs.
        if process.stdin is not None:
            with contextlib.suppress(Exception):
                process.stdin.close()
        for escalate in (None, process.terminate, process.kill):
            if escalate is not None:
                with contextlib.suppress(ProcessLookupError):
                    escalate()
            try:
                await asyncio.wait_for(process.wait(), self._stop_timeout_s)
                return
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self._stopping = True
        self._ready.clear()
        self._endpoint = None
        process = self._process
        if process is not None:
            await self._terminate(process)
        supervisor = self._supervisor
        if supervisor is not None and not supervisor.done():
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
