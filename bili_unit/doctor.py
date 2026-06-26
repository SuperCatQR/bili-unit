# bili_unit.doctor — read-only preflight checks for fetch / asr runs.
#
# ``bili-unit doctor [uid] [--check-asr]`` verifies, before a long / paid run,
# that the operator's credential, storage directory, and (optionally) ASR
# backend are ready.  It is a *preflight* command, not part of any pipeline:
#
#   * ZERO writes — never touches .env, never creates the DB dir, never
#     writes ``*.raw.db``.  ``db_dir`` "WILL CREATE" is probed with
#     ``os.access`` on the nearest existing ancestor; nothing is created.
#   * No secrets echoed — SESSDATA / bili_jct / ASR api_key are never printed,
#     and the credential line does not reverse-resolve the logged-in mid.
#   * Network only on demand — the credential check is a single GET nav
#     (``Credential.check_valid``); the ASR probe runs only with ``--check-asr``.
#
# The exit-code contract (handled by the CLI layer) is:
#   0  every check passed (may include SKIPPED / WILL CREATE / WARN)
#   1  any check is MISSING / INVALID / NOT CONFIGURED / FAIL / ERROR
#
# Each check is isolated: an exception inside one check is captured as ERROR
# and the remaining checks still run, so ``doctor`` always emits a full report.

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ._env import BiliSettings

logger = logging.getLogger("bili.doctor")


class CheckStatus(StrEnum):
    """Per-check outcome. ``_FAILING`` decides the process exit code."""

    OK = "OK"
    SKIPPED = "SKIPPED"
    WILL_CREATE = "WILL CREATE"
    WARN = "WARN"
    MISSING = "MISSING"
    INVALID = "INVALID"
    NOT_CONFIGURED = "NOT CONFIGURED"
    FAIL = "FAIL"
    ERROR = "ERROR"


# Statuses that count as a doctor failure (→ exit 1). Everything else is
# advisory: OK / SKIPPED / WILL CREATE pass cleanly, and WARN is reported
# but does NOT fail the run (an active task is a transient runtime state, not
# a config problem — the real exclusion is enforced at fetch/asr entry).
_FAILING: frozenset[CheckStatus] = frozenset(
    {
        CheckStatus.MISSING,
        CheckStatus.INVALID,
        CheckStatus.NOT_CONFIGURED,
        CheckStatus.FAIL,
        CheckStatus.ERROR,
    },
)


@dataclass(frozen=True)
class CheckResult:
    """One preflight check's result line."""

    name: str
    status: CheckStatus
    detail: str = ""

    @property
    def is_failure(self) -> bool:
        return self.status in _FAILING


@dataclass
class DoctorReport:
    """Aggregate of all checks run this invocation."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no check failed (the exit-code-0 condition)."""
        return not any(result.is_failure for result in self.results)


# ---------------------------------------------------------------------------
# Individual checks — each returns a CheckResult; raising is allowed (the
# orchestrator wraps every call and converts an exception into ERROR).
# ---------------------------------------------------------------------------

async def _check_credential(settings: BiliSettings) -> CheckResult:
    """Validate the stored credential with one read-only GET nav.

    MISSING  — no SESSDATA in .env (``AuthError`` from ``get_credential``).
    OK       — ``Credential.check_valid()`` returned True (logged in).
    INVALID  — check_valid returned False (expired / revoked).

    The logged-in mid is intentionally NOT resolved or echoed (product
    decision §7.2): "OK" means "logged in", nothing more.
    """
    from .fetching import AuthError
    from .fetching.auth import get_credential

    try:
        cred = await get_credential(settings)
    except AuthError:
        return CheckResult(
            "credential",
            CheckStatus.MISSING,
            "no SESSDATA — run 'bili-unit login'",
        )

    valid = await cred.check_valid()
    if valid:
        return CheckResult("credential", CheckStatus.OK, "logged in")
    return CheckResult(
        "credential",
        CheckStatus.INVALID,
        "expired or revoked — run 'bili-unit login'",
    )


def _check_db_dir(settings: BiliSettings) -> CheckResult:
    """Confirm the DB root is writable, without creating anything.

    OK           — directory exists and is writable.
    WILL CREATE  — does not exist yet but the nearest existing ancestor is
                   writable (doctor does NOT create it; fetch/asr will).
    FAIL         — exists but not writable, or no writable ancestor exists.

    Note: on Windows ``os.access(W_OK)`` does not consult ACLs, so an OK here
    can be a false positive on ACL-restricted dirs. Acceptable for a preflight
    hint — the authoritative check is fetch/asr actually opening the DB.
    """
    db_dir = Path(settings.bili_db_dir)

    if db_dir.exists():
        if not db_dir.is_dir():
            return CheckResult(
                "db_dir",
                CheckStatus.FAIL,
                f"not a directory: {db_dir}",
            )
        if os.access(db_dir, os.W_OK):
            return CheckResult("db_dir", CheckStatus.OK, f"{db_dir}, writable")
        return CheckResult("db_dir", CheckStatus.FAIL, f"not writable: {db_dir}")

    # Does not exist — probe the nearest existing ancestor for writability.
    ancestor = db_dir.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if ancestor.exists() and os.access(ancestor, os.W_OK):
        return CheckResult("db_dir", CheckStatus.WILL_CREATE, str(db_dir))
    return CheckResult(
        "db_dir",
        CheckStatus.FAIL,
        f"parent not writable: {db_dir}",
    )


async def _check_asr_backend(settings: BiliSettings) -> CheckResult:
    """Probe the configured ASR backend (only reached when --check-asr).

    SKIPPED         — backend is ``mock`` (no network needed).
    NOT CONFIGURED  — mimo backend selected but api_key empty (or custom
                      profile missing base_url).
    OK              — ``probe_mimo_model()`` succeeded.
    FAIL            — probe hit a network or API error (e.g. 401 wrong key,
                      non-200, or an unexpected response shape).
    """
    from .processing import ASRAPIError, ASRConfigError, ASRConnectionError
    from .processing.audio._init_wizard import probe_mimo_model

    backend = (settings.bili_processing_asr_backend or "").strip().lower()
    if backend in ("", "mock"):
        return CheckResult(
            "asr_backend",
            CheckStatus.SKIPPED,
            "mock backend needs no network",
        )

    if backend == "mimo" and not settings.bili_processing_asr_api_key.strip():
        return CheckResult(
            "asr_backend",
            CheckStatus.NOT_CONFIGURED,
            "no API key — run 'bili-unit init-mimo'",
        )

    try:
        result = await probe_mimo_model(settings=settings)
    except ASRConfigError as exc:
        return CheckResult(
            "asr_backend",
            CheckStatus.NOT_CONFIGURED,
            f"{exc} — run 'bili-unit init-mimo'",
        )
    except ASRConnectionError as exc:
        return CheckResult("asr_backend", CheckStatus.FAIL, str(exc))
    except ASRAPIError as exc:
        # Non-200 (e.g. 401 wrong key), refusal, or unexpected response shape:
        # the backend is reachable but the request was rejected. Spec maps this
        # to FAIL (<http status / error>), not ERROR. LengthTruncatedError /
        # EmptyTranscriptError subclass this too — for a tiny probe tone any of
        # them means "configured backend did not yield a usable probe" → FAIL.
        return CheckResult("asr_backend", CheckStatus.FAIL, str(exc))

    model = result.model or settings.bili_processing_asr_model or "unknown"
    return CheckResult("asr_backend", CheckStatus.OK, f"model={model}")


async def _check_task_lock(settings: BiliSettings, uid: int) -> CheckResult:
    """Report whether ``uid`` already has an active fetch/asr run.

    OK   — no active run; safe to start.
    WARN — a stage is already running (advisory only, does not fail doctor).

    Read-only: the workbench is assembled with the ``mock`` ASR backend so
    this check never validates ASR config or touches the network. The real
    exclusion lock lives at fetch/asr entry, not here.
    """
    from .workbench import workbench_session

    async with workbench_session(settings, asr_backend_override="mock") as wb:
        check = await wb.can_start_task(uid)

    if check.can_start:
        return CheckResult("task_lock", CheckStatus.OK, "no active run")
    return CheckResult("task_lock", CheckStatus.WARN, check.reason or "active run")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def _guard(name: str, call: Callable[[], Awaitable[CheckResult]]) -> CheckResult:
    """Run one check, converting any exception into an ERROR result.

    A single check blowing up must not abort the whole report — doctor's
    contract is to always emit every line and then decide the exit code.
    """
    try:
        return await call()
    except Exception as exc:  # noqa: BLE001 — deliberately broad: isolate the check
        logger.debug("doctor_check_failed", extra={"check": name}, exc_info=True)
        return CheckResult(name, CheckStatus.ERROR, str(exc) or exc.__class__.__name__)


async def run_doctor(
    settings: BiliSettings,
    *,
    uid: int | None = None,
    check_asr: bool = False,
) -> DoctorReport:
    """Run all applicable preflight checks and return an aggregate report.

    ``credential`` and ``db_dir`` always run. ``asr_backend`` runs only when
    ``check_asr`` is set (otherwise reported SKIPPED). ``task_lock`` runs only
    when ``uid`` is given. No check writes to disk or .env.
    """
    report = DoctorReport()

    report.results.append(
        await _guard("credential", lambda: _check_credential(settings)),
    )
    report.results.append(
        await _guard("db_dir", lambda: _as_async(_check_db_dir(settings))),
    )

    if check_asr:
        report.results.append(
            await _guard("asr_backend", lambda: _check_asr_backend(settings)),
        )
    else:
        report.results.append(
            CheckResult(
                "asr_backend",
                CheckStatus.SKIPPED,
                "use --check-asr to probe",
            ),
        )

    if uid is not None:
        report.results.append(
            await _guard("task_lock", lambda: _check_task_lock(settings, uid)),
        )

    return report


async def _as_async(value: CheckResult) -> CheckResult:
    """Adapt a sync check result into the awaitable shape ``_guard`` expects."""
    return value


__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "run_doctor",
]
