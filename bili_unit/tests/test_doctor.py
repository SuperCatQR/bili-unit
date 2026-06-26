from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from bili_unit import __main__ as cli
from bili_unit import doctor
from bili_unit._cli_render import CliRenderer
from bili_unit.doctor import (
    CheckResult,
    CheckStatus,
    DoctorReport,
    run_doctor,
)
from bili_unit.fetching import AuthError
from bili_unit.processing import ASRConfigError, ASRConnectionError


class _Settings:
    """Minimal settings stub for doctor checks (only the fields doctor reads)."""

    def __init__(self, *, db_dir: str, backend: str = "mock", api_key: str = "") -> None:
        self.bili_db_dir = db_dir
        self.bili_processing_asr_backend = backend
        self.bili_processing_asr_api_key = api_key
        self.bili_processing_asr_model = "mimo-v2.5-asr"


class _FakeCredential:
    def __init__(self, *, valid: bool) -> None:
        self._valid = valid

    async def check_valid(self) -> bool:
        return self._valid


def _result(report: DoctorReport, name: str) -> CheckResult:
    return next(r for r in report.results if r.name == name)


# ---------------------------------------------------------------------------
# credential
# ---------------------------------------------------------------------------

async def test_credential_ok_when_valid(monkeypatch, tmp_path: Path) -> None:
    async def fake_get_credential() -> _FakeCredential:
        return _FakeCredential(valid=True)

    monkeypatch.setattr("bili_unit.fetching.auth.get_credential", fake_get_credential)

    report = await run_doctor(_Settings(db_dir=str(tmp_path)))
    cred = _result(report, "credential")
    assert cred.status is CheckStatus.OK
    # Logged-in mid must never be echoed (product decision §7.2).
    assert "uid" not in cred.detail.lower()
    assert report.ok


async def test_credential_missing_when_no_sessdata(monkeypatch, tmp_path: Path) -> None:
    async def fake_get_credential():
        raise AuthError("Missing BILI_SESSDATA in .env")

    monkeypatch.setattr("bili_unit.fetching.auth.get_credential", fake_get_credential)

    report = await run_doctor(_Settings(db_dir=str(tmp_path)))
    assert _result(report, "credential").status is CheckStatus.MISSING
    assert not report.ok


async def test_credential_invalid_when_check_valid_false(monkeypatch, tmp_path: Path) -> None:
    async def fake_get_credential() -> _FakeCredential:
        return _FakeCredential(valid=False)

    monkeypatch.setattr("bili_unit.fetching.auth.get_credential", fake_get_credential)

    report = await run_doctor(_Settings(db_dir=str(tmp_path)))
    assert _result(report, "credential").status is CheckStatus.INVALID
    assert not report.ok


async def test_credential_error_when_check_valid_raises(monkeypatch, tmp_path: Path) -> None:
    class _BoomCredential:
        async def check_valid(self) -> bool:
            raise RuntimeError("network down")

    async def fake_get_credential() -> _BoomCredential:
        return _BoomCredential()

    monkeypatch.setattr("bili_unit.fetching.auth.get_credential", fake_get_credential)

    report = await run_doctor(_Settings(db_dir=str(tmp_path)))
    cred = _result(report, "credential")
    assert cred.status is CheckStatus.ERROR
    assert "network down" in cred.detail
    # One check erroring must not drop the others.
    assert _result(report, "db_dir") is not None
    assert not report.ok


# ---------------------------------------------------------------------------
# db_dir
# ---------------------------------------------------------------------------

async def _ok_credential(monkeypatch) -> None:
    async def fake_get_credential() -> _FakeCredential:
        return _FakeCredential(valid=True)

    monkeypatch.setattr("bili_unit.fetching.auth.get_credential", fake_get_credential)


async def test_db_dir_ok_when_existing_writable(monkeypatch, tmp_path: Path) -> None:
    await _ok_credential(monkeypatch)
    report = await run_doctor(_Settings(db_dir=str(tmp_path)))
    db = _result(report, "db_dir")
    assert db.status is CheckStatus.OK
    assert report.ok


async def test_db_dir_will_create_when_absent_but_parent_writable(monkeypatch, tmp_path: Path) -> None:
    await _ok_credential(monkeypatch)
    target = tmp_path / "nested" / "bili"
    report = await run_doctor(_Settings(db_dir=str(target)))
    db = _result(report, "db_dir")
    assert db.status is CheckStatus.WILL_CREATE
    # WILL CREATE must not be a failure and must NOT actually create the dir.
    assert report.ok
    assert not target.exists()


async def test_db_dir_fail_when_path_is_a_file(monkeypatch, tmp_path: Path) -> None:
    await _ok_credential(monkeypatch)
    file_path = tmp_path / "not_a_dir"
    file_path.write_text("x", encoding="utf-8")
    report = await run_doctor(_Settings(db_dir=str(file_path)))
    assert _result(report, "db_dir").status is CheckStatus.FAIL
    assert not report.ok


# ---------------------------------------------------------------------------
# asr_backend
# ---------------------------------------------------------------------------

async def test_asr_skipped_by_default(monkeypatch, tmp_path: Path) -> None:
    await _ok_credential(monkeypatch)
    report = await run_doctor(_Settings(db_dir=str(tmp_path)), check_asr=False)
    asr = _result(report, "asr_backend")
    assert asr.status is CheckStatus.SKIPPED
    assert "--check-asr" in asr.detail


async def test_asr_skipped_for_mock_backend_even_with_flag(monkeypatch, tmp_path: Path) -> None:
    await _ok_credential(monkeypatch)
    report = await run_doctor(
        _Settings(db_dir=str(tmp_path), backend="mock"),
        check_asr=True,
    )
    assert _result(report, "asr_backend").status is CheckStatus.SKIPPED


async def test_asr_not_configured_when_mimo_key_missing(monkeypatch, tmp_path: Path) -> None:
    await _ok_credential(monkeypatch)
    report = await run_doctor(
        _Settings(db_dir=str(tmp_path), backend="mimo", api_key=""),
        check_asr=True,
    )
    assert _result(report, "asr_backend").status is CheckStatus.NOT_CONFIGURED
    assert not report.ok


async def test_asr_ok_when_probe_succeeds(monkeypatch, tmp_path: Path) -> None:
    await _ok_credential(monkeypatch)

    class _Probe:
        model = "mimo-v2.5-asr"

    async def fake_probe(*, settings):
        return _Probe()

    monkeypatch.setattr("bili_unit.processing.audio._init_wizard.probe_mimo_model", fake_probe)

    report = await run_doctor(
        _Settings(db_dir=str(tmp_path), backend="mimo", api_key="tp-xxx"),
        check_asr=True,
    )
    asr = _result(report, "asr_backend")
    assert asr.status is CheckStatus.OK
    assert "model=mimo-v2.5-asr" in asr.detail
    assert report.ok


async def test_asr_fail_on_connection_error(monkeypatch, tmp_path: Path) -> None:
    await _ok_credential(monkeypatch)

    async def fake_probe(*, settings):
        raise ASRConnectionError("HTTP 401 unauthorized")

    monkeypatch.setattr("bili_unit.processing.audio._init_wizard.probe_mimo_model", fake_probe)

    report = await run_doctor(
        _Settings(db_dir=str(tmp_path), backend="mimo", api_key="tp-bad"),
        check_asr=True,
    )
    assert _result(report, "asr_backend").status is CheckStatus.FAIL
    assert not report.ok


async def test_asr_not_configured_on_config_error(monkeypatch, tmp_path: Path) -> None:
    await _ok_credential(monkeypatch)

    async def fake_probe(*, settings):
        raise ASRConfigError("profile 'custom' requires base_url")

    monkeypatch.setattr("bili_unit.processing.audio._init_wizard.probe_mimo_model", fake_probe)

    report = await run_doctor(
        _Settings(db_dir=str(tmp_path), backend="mimo", api_key="tp-x"),
        check_asr=True,
    )
    assert _result(report, "asr_backend").status is CheckStatus.NOT_CONFIGURED
    assert not report.ok


# ---------------------------------------------------------------------------
# task_lock
# ---------------------------------------------------------------------------

class _TaskCheck:
    def __init__(self, *, can_start: bool, reason: str | None = None) -> None:
        self.can_start = can_start
        self.reason = reason


class _FakeWorkbench:
    def __init__(self, check: _TaskCheck) -> None:
        self._check = check
        self.override: str | None = None

    async def can_start_task(self, uid: int) -> _TaskCheck:
        return self._check

    async def __aenter__(self) -> _FakeWorkbench:
        return self

    async def __aexit__(self, *exc) -> None:
        return None


def _patch_workbench(monkeypatch, check: _TaskCheck) -> dict:
    captured: dict = {}

    def fake_session(settings, *, asr_backend_override=None):
        captured["override"] = asr_backend_override
        return _FakeWorkbench(check)

    monkeypatch.setattr("bili_unit.workbench.workbench_session", fake_session)
    return captured


async def test_task_lock_absent_when_no_uid(monkeypatch, tmp_path: Path) -> None:
    await _ok_credential(monkeypatch)
    report = await run_doctor(_Settings(db_dir=str(tmp_path)))
    assert all(r.name != "task_lock" for r in report.results)


async def test_task_lock_ok_when_idle(monkeypatch, tmp_path: Path) -> None:
    await _ok_credential(monkeypatch)
    captured = _patch_workbench(monkeypatch, _TaskCheck(can_start=True))
    report = await run_doctor(_Settings(db_dir=str(tmp_path)), uid=123)
    assert _result(report, "task_lock").status is CheckStatus.OK
    # Read-only contract: workbench must be built with the mock backend.
    assert captured["override"] == "mock"
    assert report.ok


async def test_task_lock_warn_does_not_fail_run(monkeypatch, tmp_path: Path) -> None:
    await _ok_credential(monkeypatch)
    _patch_workbench(
        monkeypatch,
        _TaskCheck(can_start=False, reason="stage already running: fetching"),
    )
    report = await run_doctor(_Settings(db_dir=str(tmp_path)), uid=123)
    task = _result(report, "task_lock")
    assert task.status is CheckStatus.WARN
    # WARN is advisory — product decision §7.1: WARN → exit 0.
    assert report.ok


# ---------------------------------------------------------------------------
# rendering + CLI handler exit code
# ---------------------------------------------------------------------------

def test_doctor_report_render(capsys) -> None:
    report = DoctorReport(
        results=[
            CheckResult("credential", CheckStatus.OK, "logged in"),
            CheckResult("db_dir", CheckStatus.WILL_CREATE, "/tmp/out"),
            CheckResult("asr_backend", CheckStatus.SKIPPED, "use --check-asr to probe"),
        ],
    )
    CliRenderer().doctor_report(report)
    assert capsys.readouterr().out.splitlines() == [
        "  credential: OK (logged in)",
        "  db_dir: WILL CREATE (/tmp/out)",
        "  asr_backend: SKIPPED (use --check-asr to probe)",
    ]


async def test_handle_doctor_exits_1_on_failure(monkeypatch) -> None:
    async def fake_run_doctor(settings, *, uid, check_asr):
        return DoctorReport(results=[CheckResult("credential", CheckStatus.MISSING)])

    monkeypatch.setattr(doctor, "run_doctor", fake_run_doctor)
    monkeypatch.setattr(cli, "get_settings", lambda: object())

    args = argparse.Namespace(uid=None, check_asr=False)
    with pytest.raises(SystemExit) as exc:
        await cli._handle_doctor(args)
    assert exc.value.code == 1


async def test_handle_doctor_exits_0_on_pass(monkeypatch) -> None:
    async def fake_run_doctor(settings, *, uid, check_asr):
        return DoctorReport(results=[CheckResult("credential", CheckStatus.OK)])

    monkeypatch.setattr(doctor, "run_doctor", fake_run_doctor)
    monkeypatch.setattr(cli, "get_settings", lambda: object())

    args = argparse.Namespace(uid=None, check_asr=False)
    # Must NOT raise SystemExit when all checks pass.
    await cli._handle_doctor(args)
