# python -m bili_unit.processing — thin backward-compat wrapper.
#
# Maps the legacy processing sub-commands to the unified CLI.
# New scripts should use ``python -m bili_unit <sub>`` directly.

from __future__ import annotations

import sys


def main() -> None:
    """Translate legacy processing-CLI args into unified bili_unit sub-commands."""
    args = sys.argv[1:]

    # Strip the verbose flag; unified CLI handles it natively.
    verbose = []
    if "--verbose" in args or "-v" in args:
        verbose = ["--verbose"]
        args = [a for a in args if a not in ("--verbose", "-v")]

    if not args:
        sys.argv = ["bili_unit", *verbose, "process", "--help"]
    elif args[0] == "process":
        sys.argv = ["bili_unit", *verbose, *args]
    elif args[0] == "query":
        sys.argv = ["bili_unit", *verbose, "query", *args[1:]]
    elif args[0] == "list-uids":
        sys.argv = ["bili_unit", *verbose, "list-uids"]
    elif args[0] == "video-full":
        sys.argv = ["bili_unit", *verbose, "video-full", *args[1:]]
    else:
        # Assume bare uid → process
        sys.argv = ["bili_unit", *verbose, "process", *args]

    from bili_unit.__main__ import main as _uni_main
    _uni_main()


if __name__ == "__main__":
    main()
