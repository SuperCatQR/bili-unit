# auth — obtain / validate / provide bilibili-api-python Credential.

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from bilibili_api import Credential, login_v2

from .._env import get_settings, reload_settings
from . import AuthError

if TYPE_CHECKING:
    from .._env import BiliSettings

logger = logging.getLogger("bili.fetching.auth")


async def get_credential(settings: "BiliSettings | None" = None) -> Credential:
    """Return a Credential instance from env settings.

    ``settings`` lets a caller inject an explicit ``BiliSettings`` (e.g. the
    doctor preflight, which threads its own settings through every check); when
    omitted the process-global settings are used.

    Raises AuthError when mandatory fields are missing.
    Does NOT write back to .env on refresh.
    """
    if settings is None:
        settings = get_settings()

    sessdata = settings.bili_sessdata.strip()
    jct = settings.bili_jct.strip()

    if not sessdata:
        raise AuthError("Missing BILI_SESSDATA in .env")

    kwargs: dict = {
        "sessdata": sessdata,
    }
    if jct:
        kwargs["bili_jct"] = jct
    if settings.bili_buvid3:
        kwargs["buvid3"] = settings.bili_buvid3
    if settings.bili_buvid4:
        kwargs["buvid4"] = settings.bili_buvid4
    if settings.bili_dedeuserid:
        kwargs["dedeuserid"] = settings.bili_dedeuserid
    if settings.bili_ac_time_value:
        kwargs["ac_time_value"] = settings.bili_ac_time_value

    logger.debug("Credential constructed")
    return Credential(**kwargs)


async def reload_settings_and_credential() -> Credential:
    """Force reload .env and return a new Credential."""
    reload_settings()
    return await get_credential()


# ---------------------------------------------------------------------------
# QR code login
# ---------------------------------------------------------------------------


async def qr_login() -> Credential:
    """Interactive QR code login via terminal.

    Generates a QR code, prints it to terminal, polls until user scans & confirms.
    Returns a Credential on success, raises AuthError on timeout/failure.
    """
    qr = login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.WEB)
    await qr.generate_qrcode()

    terminal_qr = qr.get_qrcode_terminal()
    print("\n=== 请用 B 站 APP 扫描下方二维码 ===\n")
    print(terminal_qr)
    print("\n等待扫码...")

    while not qr.has_done():
        state = await qr.check_state()
        if state == login_v2.QrCodeLoginEvents.TIMEOUT:
            raise AuthError("二维码已过期，请重新登录")
        elif state == login_v2.QrCodeLoginEvents.SCAN:
            print("  状态：等待扫码...")
        elif state == login_v2.QrCodeLoginEvents.CONF:
            print("  状态：已扫码，等待确认...")
        await asyncio.sleep(1)

    cred = qr.get_credential()
    print("\n登录成功！")
    return cred


def save_credential_to_env(cred: Credential, env_path: str | Path = ".env") -> Path:
    """Write credential fields to .env file.

    Creates the file if it doesn't exist. Overwrites existing BILI_* fields.
    Returns the path to the written .env file.
    """
    env_path = Path(env_path)

    # Read existing content (if any)
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    # Fields to write
    fields = {
        "BILI_SESSDATA": cred.sessdata or "",
        "BILI_JCT": cred.bili_jct or "",
        "BILI_BUVID3": cred.buvid3 or "",
        "BILI_BUVID4": cred.buvid4 or "",
        "BILI_DEDEUSERID": cred.dedeuserid or "",
        "BILI_AC_TIME_VALUE": cred.ac_time_value or "",
    }

    # Remove old BILI_* lines
    new_lines = [line for line in existing_lines if not any(line.startswith(f"{k}=") for k in fields)]

    # Append new values
    for key, value in fields.items():
        new_lines.append(f"{key}={value}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    logger.info("Credential saved to %s", env_path)
    return env_path
