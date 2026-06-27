"""Repo-root pytest config.

Puts the independently-distributable ``bili_worker`` package (its own ``pyproject.toml``
+ GPL-3.0 ``LICENSE``, contract §12) on ``sys.path`` for the dev test run, without
installing it into the main ``bili_unit`` environment. In production the worker is a
separate ``pip install``; the main process never imports it (it spawns it as a
subprocess). This shim is dev/test only.
"""

from __future__ import annotations

import sys
from pathlib import Path

_WORKER_PKG_ROOT = Path(__file__).parent / "bili_worker"
if _WORKER_PKG_ROOT.is_dir():
    sys.path.insert(0, str(_WORKER_PKG_ROOT))
