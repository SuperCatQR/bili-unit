"""WorkerClient — main-side IPC client for the bili-worker subprocess.

Spawns ``bili-worker`` as a subprocess, runs the startup sequence
(handshake → describe_catalog → init_http_backend → credential_open),
and proxies ``fetch_page`` / ``fetch_item`` / ``resolve_audio_url`` calls
through stdio NDJSON frames.

Contract: docs/ipc-contract-f2.md §4, §5, §9.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from typing import Any

from ._error_pack import fetching_exception_from_pack
from ._protocol import ProtocolError, Request, Response, decode_frame

logger = logging.getLogger("bili.fetching.worker_client")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKER_MODULE = "bili_worker.__main__"
PROTOCOL_VERSION = "1.0"
HANDSHAKE_TIMEOUT = 10.0
REQUEST_TIMEOUT = 30.0
MAX_RESTART_ATTEMPTS = 3
RESTART_BACKOFF_BASE = 2.0  # seconds, exponential


# ---------------------------------------------------------------------------
# WorkerClient
# ---------------------------------------------------------------------------


class WorkerClient:
    """IPC client that spawns and manages the bili-worker subprocess.

    Usage::

        client = WorkerClient()
        await client.start()
        result = await client.fetch_page(uid, spec_name, cred_ref, params)
        await client.shutdown()
    """

    def __init__(
        self,
        *,
        request_timeout: float = REQUEST_TIMEOUT,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
        max_restart_attempts: int = MAX_RESTART_ATTEMPTS,
        worker_module: str = WORKER_MODULE,
    ) -> None:
        self._request_timeout = request_timeout
        self._handshake_timeout = handshake_timeout
        self._max_restart_attempts = max_restart_attempts
        self._worker_module = worker_module

        self._process: asyncio.subprocess.Process | None = None
        self._next_id: int = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._started = False
        self._restart_count = 0
        self._catalog: dict[str, Any] | None = None
        self._credential_ref: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        http_backend: str = "aiohttp",
        impersonate: str = "chrome131",
        env_path: str | None = None,
    ) -> None:
        """Spawn worker and run the startup sequence.

        Startup: handshake → describe_catalog → init_http_backend → credential_open.
        On failure, retries with exponential backoff up to ``max_restart_attempts``.
        """
        if self._started:
            return

        for attempt in range(self._max_restart_attempts + 1):
            try:
                await self._spawn()
                await self._handshake()
                await self._describe_catalog()
                await self._init_http_backend(http_backend, impersonate)
                await self._credential_open(env_path)
                self._started = True
                self._restart_count = 0
                logger.info("WorkerClient started (restarts=%d)", self._restart_count)
                return
            except Exception as exc:
                logger.warning(
                    "WorkerClient start attempt %d/%d failed: %s",
                    attempt + 1, self._max_restart_attempts + 1, exc,
                )
                await self._cleanup()
                if attempt < self._max_restart_attempts:
                    delay = RESTART_BACKOFF_BASE ** (attempt + 1)
                    logger.info("Retrying in %.1fs...", delay)
                    await asyncio.sleep(delay)
                else:
                    raise WorkerStartError(
                        f"WorkerClient failed to start after "
                        f"{self._max_restart_attempts + 1} attempts"
                    ) from exc

    async def shutdown(self) -> None:
        """Send ``shutdown`` op and wait for worker to exit gracefully."""
        if not self._started or self._process is None:
            return
        with contextlib.suppress(Exception):
            await self._send_op("shutdown", {}, timeout=5.0)
        await self._cleanup()
        self._started = False

    async def _cleanup(self) -> None:
        """Kill the worker process and cancel the reader task."""
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        # Fail all pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(WorkerCrashedError("worker process terminated"))
        self._pending.clear()

        if self._process is not None:
            proc = self._process
            self._process = None
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

    # ------------------------------------------------------------------
    # Op-level API
    # ------------------------------------------------------------------

    async def fetch_page(
        self,
        uid: int,
        endpoint: str,
        credential_ref: str | None,
        request_params: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Proxy ``fetch_page`` op to worker. Returns ``{"raw_payload": ..., "is_last_page": ..., "next_request": ...}``."""
        params: dict[str, Any] = {
            "uid": uid,
            "endpoint": endpoint,
            "request_params": request_params,
        }
        if credential_ref is not None:
            params["credential_ref"] = credential_ref
        return await self._send_op("fetch_page", params, timeout=timeout)

    async def fetch_item(
        self,
        item_id: str,
        endpoint: str,
        credential_ref: str | None,
        extra: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Proxy ``fetch_item`` op to worker. Returns ``{"raw_payload": ...}``."""
        params: dict[str, Any] = {
            "item_id": item_id,
            "endpoint": endpoint,
        }
        if credential_ref is not None:
            params["credential_ref"] = credential_ref
        if extra:
            params["extra"] = extra
        return await self._send_op("fetch_item", params, timeout=timeout)

    async def resolve_audio_url(
        self,
        bvid: str,
        credential_ref: str | None,
        audio_quality: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Proxy ``resolve_audio_url`` op to worker."""
        params: dict[str, Any] = {"bvid": bvid}
        if credential_ref is not None:
            params["credential_ref"] = credential_ref
        if audio_quality is not None:
            params["audio_quality"] = audio_quality
        return await self._send_op("resolve_audio_url", params, timeout=timeout)

    async def login_qr_start(self, timeout: float | None = None) -> dict[str, Any]:
        """Proxy ``login_qr_start`` op to worker."""
        return await self._send_op("login_qr_start", {}, timeout=timeout)

    async def login_qr_poll(
        self, qrcode_key: str, timeout: float | None = None,
    ) -> dict[str, Any]:
        """Proxy ``login_qr_poll`` op to worker."""
        return await self._send_op(
            "login_qr_poll", {"qrcode_key": qrcode_key}, timeout=timeout,
        )

    async def login_save_env(self, timeout: float | None = None) -> dict[str, Any]:
        """Proxy ``login_save_env`` op to worker."""
        return await self._send_op("login_save_env", {}, timeout=timeout)

    async def credential_status(self, timeout: float | None = None) -> dict[str, Any]:
        """Proxy ``credential_status`` op to worker."""
        return await self._send_op("credential_status", {}, timeout=timeout)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def credential_ref(self) -> str | None:
        """The current credential_ref (opaque handle, no plaintext)."""
        return self._credential_ref

    @property
    def catalog(self) -> dict[str, Any] | None:
        """The endpoint catalog manifest from ``describe_catalog``."""
        return self._catalog

    @property
    def started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _spawn(self) -> None:
        """Spawn the bili-worker subprocess."""
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")

        self._process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", self._worker_module,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._next_id = 0
        self._pending.clear()

        # Start the stdout reader
        self._reader_task = asyncio.create_task(self._read_responses())

        logger.debug("Worker spawned (pid=%d)", self._process.pid)

    async def _read_responses(self) -> None:
        """Read NDJSON frames from worker stdout, resolve pending futures."""
        assert self._process is not None and self._process.stdout is not None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                # stdout EOF — worker crashed
                logger.warning("Worker stdout EOF (crashed)")
                self._fail_all_pending(WorkerCrashedError("worker stdout closed"))
                return

            line_str = line.decode("utf-8")
            try:
                obj = decode_frame(line_str)
                resp = Response.from_obj(obj)
            except ProtocolError as exc:
                logger.error("Protocol error reading worker response: %s", exc)
                continue

            fut = self._pending.pop(resp.id, None)
            if fut is None:
                logger.warning("Unexpected response id=%d (no pending request)", resp.id)
                continue

            if resp.status == "ok":
                fut.set_result(resp.data or {})
            else:
                error_pack = resp.error or {}
                exc = fetching_exception_from_pack(error_pack)
                fut.set_exception(exc)

    def _fail_all_pending(self, exc: BaseException) -> None:
        """Fail all in-flight requests (worker crashed)."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _send_op(
        self,
        op: str,
        params: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send an op to the worker and wait for the response."""
        if not self._started or self._process is None:
            raise WorkerNotStartedError("WorkerClient not started")

        req_id = self._next_id
        self._next_id += 1

        req = Request(id=req_id, op=op, params=params)
        frame = req.to_frame()

        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        try:
            assert self._process.stdin is not None
            self._process.stdin.write(frame.encode("utf-8"))
            await self._process.stdin.drain()
        except Exception as exc:
            self._pending.pop(req_id, None)
            raise WorkerCrashedError(f"failed to write to worker stdin: {exc}") from exc

        t = timeout if timeout is not None else self._request_timeout
        try:
            return await asyncio.wait_for(fut, timeout=t)
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise WorkerTimeoutError(f"op={op} timed out after {t}s") from None

    # Startup sequence helpers

    async def _handshake(self) -> None:
        data = await self._send_op(
            "handshake",
            {"protocol_version": PROTOCOL_VERSION, "client": "bili_unit/0.1.0"},
            timeout=self._handshake_timeout,
        )
        worker_proto = data.get("protocol_version", "")
        if worker_proto != PROTOCOL_VERSION:
            raise WorkerStartError(
                f"protocol version mismatch: worker={worker_proto}, "
                f"client={PROTOCOL_VERSION}"
            )
        logger.info(
            "Handshake OK: worker=%s, bilibili_api=%s",
            data.get("worker_version", "?"),
            data.get("bilibili_api_version", "?"),
        )

    async def _describe_catalog(self) -> None:
        data = await self._send_op("describe_catalog", {}, timeout=self._handshake_timeout)
        endpoints = data.get("endpoints", [])
        if not isinstance(endpoints, list):
            raise WorkerStartError(f"describe_catalog: endpoints must be a list, got {type(endpoints).__name__}")
        uid_count = sum(1 for ep in endpoints if ep.get("kind") == "uid")
        item_count = sum(1 for ep in endpoints if ep.get("kind") == "item")
        total = len(endpoints)
        if total != 63 or uid_count != 33 or item_count != 30:
            raise WorkerStartError(
                f"describe_catalog: expected 63 endpoints (33 uid + 30 item), "
                f"got {total} ({uid_count} uid + {item_count} item)"
            )
        self._catalog = data
        logger.info("Catalog OK: %d endpoints (%d uid + %d item)", total, uid_count, item_count)

    async def _init_http_backend(self, backend: str, impersonate: str) -> None:
        await self._send_op(
            "init_http_backend",
            {"backend": backend, "impersonate": impersonate},
            timeout=self._handshake_timeout,
        )
        logger.info("HTTP backend: %s", backend)

    async def _credential_open(self, env_path: str | None = None) -> None:
        params: dict[str, Any] = {}
        if env_path is not None:
            params["env_path"] = env_path
        data = await self._send_op(
            "credential_open", params, timeout=self._handshake_timeout,
        )
        self._credential_ref = data.get("credential_ref")
        logger.info("Credential opened: ref=%s", self._credential_ref)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WorkerError(Exception):
    """Base for all WorkerClient errors."""


class WorkerStartError(WorkerError):
    """Worker failed to start or startup sequence failed."""


class WorkerNotStartedError(WorkerError):
    """Operation attempted before worker was started."""


class WorkerCrashedError(WorkerError):
    """Worker process terminated unexpectedly."""


class WorkerTimeoutError(WorkerError):
    """Worker did not respond within the timeout."""


__all__ = [
    "WorkerClient",
    "WorkerCrashedError",
    "WorkerError",
    "WorkerNotStartedError",
    "WorkerStartError",
    "WorkerTimeoutError",
]
