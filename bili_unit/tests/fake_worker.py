"""FakeWorker — in-memory worker stub for test migration.

Provides a drop-in replacement for ``WorkerClient`` that responds with
pre-configured data or error packs without spawning a real subprocess.
Tests can set ``responses`` / ``errors`` to simulate specific scenarios
(stdout EOF, crash, timeout, specific error packs).

Contract: docs/ipc-contract-f2.md §13 Q2 (test migration).
"""

from __future__ import annotations

from typing import Any

from bili_unit.fetching._error_pack import ErrorPack, fetching_exception_from_pack
from bili_unit.fetching._protocol import ProtocolError


class FakeWorker:
    """In-memory worker that responds with pre-configured data/errors.

    Usage::

        fake = FakeWorker()
        fake.responses["handshake"] = {"protocol_version": "1.0", ...}
        fake.responses["describe_catalog"] = {"endpoints": [...]}
        fake.responses["credential_open"] = {"credential_ref": "cred-1"}
        fake.responses["fetch_page"] = {"raw_payload": {...}, "is_last_page": True}

        # Simulate an error:
        fake.errors["fetch_page"] = ErrorPack(
            type="Http412Error", classification="retryable",
            code=412, message="videos: 412", retryable_hint=True,
        )

        # Simulate crash:
        fake.crash_after_op = "fetch_page"
    """

    def __init__(self) -> None:
        self.responses: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, ErrorPack] = {}
        self.crash_after_op: str | None = None
        self.simulate_eof: bool = False
        self.simulate_timeout: bool = False
        self.simulate_nonzero_exit: bool = False

        # Call tracking
        self.calls: list[dict[str, Any]] = []
        self._started = False
        self._shutdown = False

    # ------------------------------------------------------------------
    # Lifecycle (mirrors WorkerClient)
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        http_backend: str = "aiohttp",
        impersonate: str = "chrome131",
        env_path: str | None = None,
    ) -> None:
        """Simulate worker startup (no-op)."""
        if self.simulate_eof:
            raise WorkerCrashedError("worker stdout EOF during startup")
        if self.simulate_nonzero_exit:
            raise WorkerCrashedError("worker exited with code 1")
        self._started = True

    async def shutdown(self) -> None:
        self._shutdown = True
        self._started = False

    # ------------------------------------------------------------------
    # Op-level API (mirrors WorkerClient)
    # ------------------------------------------------------------------

    async def fetch_page(
        self,
        uid: int,
        endpoint: str,
        credential_ref: str | None,
        request_params: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self._dispatch("fetch_page", {
            "uid": uid, "endpoint": endpoint,
            "credential_ref": credential_ref,
            "request_params": request_params,
        })

    async def fetch_item(
        self,
        item_id: str,
        endpoint: str,
        credential_ref: str | None,
        extra: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self._dispatch("fetch_item", {
            "item_id": item_id, "endpoint": endpoint,
            "credential_ref": credential_ref, "extra": extra,
        })

    async def resolve_audio_url(
        self,
        bvid: str,
        credential_ref: str | None,
        audio_quality: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self._dispatch("resolve_audio_url", {
            "bvid": bvid, "credential_ref": credential_ref,
            "audio_quality": audio_quality,
        })

    async def login_qr_start(self, timeout: float | None = None) -> dict[str, Any]:
        return await self._dispatch("login_qr_start", {})

    async def login_qr_poll(
        self, qrcode_key: str, timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self._dispatch("login_qr_poll", {"qrcode_key": qrcode_key})

    async def login_save_env(self, timeout: float | None = None) -> dict[str, Any]:
        return await self._dispatch("login_save_env", {})

    async def credential_status(self, timeout: float | None = None) -> dict[str, Any]:
        return await self._dispatch("credential_status", {})

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _dispatch(self, op: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"op": op, "params": params})

        if self.simulate_timeout:
            raise TimeoutError(f"FakeWorker timeout on {op}")

        if self.crash_after_op == op:
            raise WorkerCrashedError(f"FakeWorker crashed after {op}")

        if op in self.errors:
            pack = self.errors[op]
            raise fetching_exception_from_pack(pack)

        if op in self.responses:
            return self.responses[op]

        raise ProtocolError(f"FakeWorker: no response configured for op={op!r}")

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def credential_ref(self) -> str | None:
        cred_data = self.responses.get("credential_open", {})
        return cred_data.get("credential_ref")

    @property
    def catalog(self) -> dict[str, Any] | None:
        return self.responses.get("describe_catalog")

    @property
    def started(self) -> bool:
        return self._started

    def configure_catalog(self, uid_count: int = 33, item_count: int = 30) -> None:
        """Set up a minimal valid catalog response."""
        uid_eps = [
            {"name": f"uid_ep_{i}", "kind": "uid", "credential_required": False}
            for i in range(uid_count)
        ]
        item_eps = [
            {"name": f"item_ep_{i}", "kind": "item", "credential_required": False}
            for i in range(item_count)
        ]
        self.responses["describe_catalog"] = {"endpoints": uid_eps + item_eps}

    def configure_handshake(self, protocol_version: str = "1.0") -> None:
        self.responses["handshake"] = {
            "protocol_version": protocol_version,
            "worker_version": "0.1.0",
            "bilibili_api_version": "17.0.0",
            "capabilities": ["fetch_page", "fetch_item", "resolve_audio_url", "login_qr", "credential_ref"],
        }


# Re-export for convenience
WorkerCrashedError = __import__("bili_unit.fetching.worker_client", fromlist=["WorkerCrashedError"]).WorkerCrashedError

__all__ = ["FakeWorker"]
