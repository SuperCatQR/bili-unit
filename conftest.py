"""Repo-root pytest config.

The ``bili_worker`` package is now an independently-distributable GPL-3.0 component
installed as a dependency (``bili-worker @ git+https://github.com/SuperCatQR/bili-worker.git``,
see ``pyproject.toml``). The main process never imports it (it spawns it as a
subprocess per arm's-length IPC contract). Test files that need the worker for integration
tests import it normally via the installed package.
"""

from __future__ import annotations
