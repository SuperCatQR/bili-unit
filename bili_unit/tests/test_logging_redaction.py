"""test_logging_redaction — self-contained tests for RedactingFilter + configure_logging.

Uses tmp_path to write a log file; resets the 'bili_unit' logger after each
test to avoid handler/level pollution.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

import pytest

from bili_unit._logging import RedactingFilter, configure_logging

# ---------------------------------------------------------------------------
# Fixture: restore the bili_unit logger after each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_bili_unit_logger():
    """Remove all handlers from the bili_unit logger after each test."""
    yield
    root = logging.getLogger("bili_unit")
    for h in list(root.handlers):
        root.removeHandler(h)
        with contextlib.suppress(Exception):
            h.close()
    root.setLevel(logging.WARNING)  # back to a quiet default


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_redacting_filter_rewrites_secret_record():
    """A log record containing a secret keyword is rewritten to [REDACTED secret]."""
    flt = RedactingFilter()
    record = logging.LogRecord(
        name="bili_unit.test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="SESSDATA=abcdef token",
        args=None,
        exc_info=None,
    )
    result = flt.filter(record)
    assert result is True
    assert record.msg == "[REDACTED secret]"
    assert record.args is None


def test_redacting_filter_passes_normal_record():
    """A normal log record is not modified."""
    flt = RedactingFilter()
    original_msg = "hello world"
    record = logging.LogRecord(
        name="bili_unit.test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=original_msg,
        args=None,
        exc_info=None,
    )
    result = flt.filter(record)
    assert result is True
    assert record.msg == original_msg


def test_configure_logging_redacts_secret_in_file(tmp_path: Path):
    """After configure_logging, a message with 'sessdata' is redacted in the log file."""
    log_file = tmp_path / "x.log"
    configure_logging(log_file=log_file, verbose=True)

    logger = logging.getLogger("bili_unit.test_redact_write")
    logger.info("SESSDATA=abcdef")

    # Flush all handlers
    root = logging.getLogger("bili_unit")
    for h in root.handlers:
        h.flush()

    content = log_file.read_text(encoding="utf-8")
    assert "[REDACTED secret]" in content
    assert "abcdef" not in content


def test_configure_logging_normal_message_not_redacted(tmp_path: Path):
    """After configure_logging, a normal message is written as-is to the log file."""
    log_file = tmp_path / "normal.log"
    configure_logging(log_file=log_file, verbose=True)

    logger = logging.getLogger("bili_unit.test_normal")
    logger.info("hello world normal message")

    root = logging.getLogger("bili_unit")
    for h in root.handlers:
        h.flush()

    content = log_file.read_text(encoding="utf-8")
    assert "hello world normal message" in content
    assert "[REDACTED secret]" not in content
