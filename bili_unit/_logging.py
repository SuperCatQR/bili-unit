# _logging — bili_unit CLI logging + lightweight progress bar.
#
# 设计要点：
#   - 现有代码到处用 ``logger.info("event_name", extra={...})`` 这种结构化写法；
#     stdlib 默认 Formatter 不会渲染 ``extra``，所以 CLI 里只看得到事件名。
#     ``HumanFormatter`` 把 extra 当 ``key=value`` 拼到行尾，``JsonFormatter``
#     则把整条记录序列化为一行 JSON（适合 ``--log-file`` 落盘）。
#   - 终端 handler 走 stderr，把 stdout 留给 CLI 的最终结果（``print(...)``），
#     这样 ``python -m bili_unit fetch 123 > result.txt`` 仍能拿到干净结果。
#   - ``Progress`` 是 stdlib-only 的进度条；与 ``ProgressAwareHandler`` 配合，
#     logger 写一行前会清掉当前进度条、写完日志后由 progress 在下次 update
#     重绘，避免 ``\r`` 把日志吃掉。

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TextIO

# LogRecord 自带的标准属性集合（用于挑出 extra 里塞进来的字段）
_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__,
) | {"message", "asctime"}

# 当前活跃的 Progress 列表 —— 由 ProgressAwareHandler 在写日志前后通知
# 它们 suspend/restore，避免日志行覆盖 \r 重绘的进度条。
_LIVE_PROGRESSES: list[Progress] = []
_LIVE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

class HumanFormatter(logging.Formatter):
    """终端可读：``HH:MM:SS LEVEL   logger.name  event  k=v k=v``"""

    LEVEL_COLORS = {
        "DEBUG": "\x1b[37m",     # gray
        "INFO": "\x1b[36m",      # cyan
        "WARNING": "\x1b[33m",   # yellow
        "ERROR": "\x1b[31m",     # red
        "CRITICAL": "\x1b[35m",  # magenta
    }
    RESET = "\x1b[0m"

    def __init__(self, *, color: bool) -> None:
        super().__init__()
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        level_text = f"{record.levelname:<7}"
        if self._color:
            level_text = (
                f"{self.LEVEL_COLORS.get(record.levelname, '')}{level_text}{self.RESET}"
            )

        extras = _extract_extras(record)
        kv = " ".join(f"{k}={_short(v)}" for k, v in extras.items())

        msg = record.getMessage()
        line = f"{ts} {level_text} {record.name}  {msg}"
        if kv:
            line += f"  {kv}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(logging.Formatter):
    """每行一个 JSON 对象，便于 ``--log-file`` 落盘后用 jq/grep 检索。"""

    def format(self, record: logging.LogRecord) -> str:
        extras = _extract_extras(record)
        payload: dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **extras,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _extract_extras(record: logging.LogRecord) -> dict[str, Any]:
    """挑出调用方通过 ``extra={...}`` 注入的字段。"""
    return {
        k: v
        for k, v in record.__dict__.items()
        if k not in _RESERVED and not k.startswith("_")
    }


def _short(v: Any) -> Any:
    """长 list/dict 折叠成形如 ``[12 items]`` 的占位，避免一行刷屏。"""
    if isinstance(v, (list, tuple)) and len(v) > 5:
        return f"[{len(v)} items]"
    if isinstance(v, dict) and len(v) > 5:
        return f"{{...{len(v)} keys}}"
    return v


# ---------------------------------------------------------------------------
# ProgressAwareHandler
# ---------------------------------------------------------------------------

class ProgressAwareHandler(logging.StreamHandler):
    """StreamHandler 变体：写日志前先清空进度条，写完让进度条重绘。"""

    def emit(self, record: logging.LogRecord) -> None:
        with _LIVE_LOCK:
            live = list(_LIVE_PROGRESSES)
        for prog in live:
            prog._suspend()
        try:
            super().emit(record)
        finally:
            for prog in live:
                prog._restore()


# ---------------------------------------------------------------------------
# RedactingFilter — drop records containing credential keywords
# ---------------------------------------------------------------------------

class RedactingFilter(logging.Filter):
    """Drop common credential keywords from log records before emission."""

    SECRETS = ("sessdata", "bili_jct", "buvid3", "api_key", "apikey", "authorization", "cookie")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage().lower()
        except Exception:
            return True
        for s in self.SECRETS:
            if s in msg:
                record.msg = "[REDACTED secret]"
                record.args = None
                break
        return True


# ---------------------------------------------------------------------------
# configure_logging — single CLI entry-point
# ---------------------------------------------------------------------------

def configure_logging(
    *,
    verbose: bool = False,
    quiet: bool = False,
    log_file: Path | None = None,
) -> None:
    """配置根 logger。CLI 入口处调一次即可，重复调用会清掉旧 handler。

    Args:
        verbose: ``True`` 时 root level = DEBUG，否则 INFO。
        quiet:   覆盖 verbose，只显示 WARNING 及以上。
        log_file: 额外把 DEBUG 级 JSON Lines 写到该文件，便于事后复查。
    """
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    root = logging.getLogger("bili_unit")
    root.setLevel(level)
    # Stop bili_unit records from bubbling to the actual root logger, so the
    # RedactingFilter on our handlers can't be bypassed via the root path
    # (e.g. logging.lastResort or a host app's own root handlers).
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = ProgressAwareHandler(sys.stderr)
    stream.setLevel(level)
    stream.setFormatter(HumanFormatter(color=sys.stderr.isatty()))
    stream.addFilter(RedactingFilter())
    root.addHandler(stream)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)  # 文件总是写全量，方便事后 grep
        fh.setFormatter(JsonFormatter())
        fh.addFilter(RedactingFilter())
        root.addHandler(fh)

    # third-party noise floor — 抑制第三方库噪音
    for noisy in ("aiohttp.access", "asyncio", "urllib3", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Progress — stdlib-only progress bar
# ---------------------------------------------------------------------------

class Progress:
    """简易进度条，写到 stderr。

    用法::

        with Progress(total=len(items), label="transform") as bar:
            for item in items:
                ...
                bar.update(1, postfix=f"item={item.id}")

    特性：
      - 非 TTY（``sys.stderr.isatty() == False``）时自动降级为静默，不刷屏；
        仍可在 ``close()`` 时输出一行汇总到 logger。
      - 与 ``ProgressAwareHandler`` 协作：logger 写日志时进度条会先清屏一次，
        日志输出完毕后下一次 ``update()`` 重绘。
      - 节流：连续两次 ``update()`` 间隔小于 ``MIN_INTERVAL`` 秒会跳过重绘，
        避免高速 worker 把终端刷爆。
    """

    MIN_INTERVAL = 0.1
    BAR_WIDTH = 24

    def __init__(
        self,
        total: int,
        label: str,
        *,
        stream: TextIO | None = None,
        enabled: bool | None = None,
        emit_summary: bool = True,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._label = label
        self._total = max(0, int(total))
        self._current = 0
        self._postfix: str | None = None
        self._lock = threading.Lock()
        self._last_render = 0.0
        self._closed = False
        self._emit_summary = emit_summary
        self._started = time.monotonic()
        self._suspended = False  # set by ProgressAwareHandler around log writes

        if enabled is None:
            enabled = bool(getattr(self._stream, "isatty", lambda: False)())
        self._enabled = enabled

        if self._enabled:
            with _LIVE_LOCK:
                _LIVE_PROGRESSES.append(self)
            self._render(force=True)

    # -- public API --------------------------------------------------------

    def update(self, n: int = 1, *, postfix: str | None = None) -> None:
        with self._lock:
            self._current += n
            if postfix is not None:
                self._postfix = postfix
        self._render()

    def set_total(self, total: int) -> None:
        with self._lock:
            self._total = max(0, int(total))
        self._render(force=True)

    def close(self, final_label: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True

        if self._enabled:
            with _LIVE_LOCK:
                if self in _LIVE_PROGRESSES:
                    _LIVE_PROGRESSES.remove(self)
            self._erase()

        if self._emit_summary:
            elapsed = time.monotonic() - self._started
            label = final_label or self._label
            logging.getLogger("bili.progress").info(
                "progress_done",
                extra={
                    "label": label,
                    "completed": self._current,
                    "total": self._total,
                    "elapsed_s": round(elapsed, 1),
                },
            )

    # context-manager sugar
    def __enter__(self) -> Progress:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _render(self, *, force: bool = False) -> None:
        if not self._enabled or self._suspended:
            return
        now = time.monotonic()
        if (
            not force
            and (now - self._last_render) < self.MIN_INTERVAL
            and (self._total == 0 or self._current < self._total)
        ):
            # 但要保证最后一帧 (current == total) 一定渲染
            return
        self._last_render = now

        line = self._format_line()
        try:
            self._stream.write("\r\x1b[2K" + line)
            self._stream.flush()
        except Exception:  # noqa: BLE001 — terminal might disappear; never crash worker
            pass

    def _erase(self) -> None:
        try:
            self._stream.write("\r\x1b[2K")
            self._stream.flush()
        except Exception:  # noqa: BLE001
            pass

    def _suspend(self) -> None:
        """ProgressAwareHandler 调用：日志写入前先清空进度条占用的那一行。"""
        if not self._enabled:
            return
        self._suspended = True
        self._erase()

    def _restore(self) -> None:
        """日志写完后由 handler 调用，触发一次重绘。"""
        if not self._enabled:
            return
        self._suspended = False
        self._render(force=True)

    def _format_line(self) -> str:
        if self._total > 0:
            ratio = min(1.0, self._current / self._total)
            filled = int(self.BAR_WIDTH * ratio)
            bar = "█" * filled + "░" * (self.BAR_WIDTH - filled)
            head = f"{self._label}  [{bar}] {self._current}/{self._total}"
        else:
            # 无总量：转圈圈式 spinner
            spinner = "|/-\\"[self._current % 4]
            head = f"{self._label}  [{spinner}] {self._current}"
        if self._postfix:
            return f"{head}  {self._postfix}"
        return head


# ---------------------------------------------------------------------------
# Helpers for callers
# ---------------------------------------------------------------------------

def progress_for(
    items: Iterable[Any] | None,
    *,
    total: int | None = None,
    label: str,
    enabled: bool | None = None,
) -> Progress:
    """构造一个 Progress；若调用方只有 iterable 没显式 total 也能用。

    若 ``items`` 实现了 ``__len__`` 且未显式给 total，则取其长度；
    否则 total=0（spinner 模式）。
    """
    if total is None:
        total = (
            len(items)  # type: ignore[arg-type]
            if items is not None and hasattr(items, "__len__")
            else 0
        )
    return Progress(total=total, label=label, enabled=enabled)


__all__ = [
    "HumanFormatter",
    "JsonFormatter",
    "Progress",
    "ProgressAwareHandler",
    "RedactingFilter",
    "configure_logging",
    "progress_for",
]
