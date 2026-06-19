# bili-unit - unified CLI for the bili unit.
#
# Read-side commands removed: consumers query the SQLite database file
# directly (see ``bili_unit.db_path``). The CLI keeps only write-side
# actions plus auth helpers:
#
#   bili-unit fetch        <uid> [options]   run fetching
#   bili-unit asr          <uid> [options]   run audio ASR
#   bili-unit delete-uid   <uid> [-y]        delete all data for a uid
#   bili-unit tui                            open local dashboard TUI
#   bili-unit login                          QR code login
#   bili-unit init-mimo [--test]             interactive MiMo ASR setup

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from ._cli_render import CliRenderer
from ._env import get_settings
from ._logging import configure_logging
from ._selection import SelectionError, resolve_subset
from .observability import RunSummary, load_run_summary

logger = logging.getLogger("bili.cli")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_subset(
    *,
    flag_label: str,
    all_names: list[str],
    include: list[str] | None,
    exclude: list[str] | None,
) -> list[str] | None:
    """Translate (include, exclude) CLI flags into the include-list passed downstream.

    Default behaviour is "run everything", expressed as ``None`` so downstream layers
    keep using their own "all registered" expansion. ``--exclude`` removes names from
    that full set; ``--include`` selects an explicit subset (kept for debugging).
    The two flags are mutually exclusive at the argparse layer.

    Unknown names in either list raise SystemExit(2) with a helpful message; typos
    here would silently change which endpoints/handlers run.
    """
    try:
        return resolve_subset(
            flag_label=flag_label,
            all_names=all_names,
            include=include,
            exclude=exclude,
        )
    except SelectionError as exc:
        raise SystemExit(str(exc)) from exc


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

async def _handle_fetch(args: argparse.Namespace) -> None:
    """Run fetching via the unit-level BiliCommand."""
    from bili_unit import session

    endpoints = _resolve_fetch_endpoints(args)

    renderer = CliRenderer()
    async with session() as cmd:
        result = await cmd.fetch(args.uid, endpoints=endpoints, mode=args.mode)
    summary = await _load_cli_summary(args.uid, run_id=result.run_id)
    if summary is None:
        renderer.fetch_result(uid=args.uid, status=result.status)
    else:
        renderer.fetch_summary(summary, fallback_status=result.status)


def _resolve_fetch_endpoints(args: argparse.Namespace) -> list[str] | None:
    from bili_unit.fetching._endpoint_catalog import ENDPOINTS, resolve_profile

    all_endpoints = [ep.name for ep in ENDPOINTS]

    if args.endpoints is not None or args.exclude_endpoints is not None:
        return _resolve_subset(
            flag_label="endpoint",
            all_names=all_endpoints,
            include=args.endpoints,
            exclude=args.exclude_endpoints,
        )
    return resolve_profile(args.profile)


async def _handle_asr(args: argparse.Namespace) -> None:
    """Run audio ASR via the unit-level BiliCommand."""
    from bili_unit import session

    if args.retry_failed_only and args.mode == "full":
        raise SystemExit(
            "--retry-failed-only conflicts with --mode full "
            "(it can only re-process FAILED items, which requires incremental mode).",
        )
    effective_mode = "incremental" if args.retry_failed_only else args.mode

    renderer = CliRenderer()
    async with session(asr_backend_override=getattr(args, "asr_backend", None)) as cmd:
        result = await cmd.asr(
            args.uid,
            mode=effective_mode,
            limit=args.limit,
            only_bvids=args.only_bvids,
            exclude_bvids=args.exclude_bvids,
            retry_failed_only=args.retry_failed_only,
            dry_run=args.dry_run,
            max_audio_seconds=args.max_audio_seconds,
            max_audio_tokens=args.max_audio_tokens,
        )

        candidates = (
            result.dry_run_candidates or []
            if args.dry_run or result.budget_exceeded
            else None
        )
    summary = await _load_cli_summary(args.uid, run_id=result.run_id)
    if summary is None:
        renderer.asr_result(
            uid=args.uid,
            status=result.status,
            candidates=candidates,
            estimate=result.estimate,
            budget_exceeded=result.budget_exceeded,
            coverage=result.coverage,
        )
    else:
        renderer.asr_summary(
            summary,
            fallback_status=result.status,
            candidates=candidates,
            estimate=result.estimate,
            budget_exceeded=result.budget_exceeded,
        )


async def _load_cli_summary(
    uid: int,
    *,
    run_id: str | None = None,
    filter_events_to_run: bool = True,
) -> RunSummary | None:
    try:
        kwargs = {
            "uid": uid,
            "root": get_settings().bili_db_dir,
            "run_id": run_id,
            "recent_limit": 12,
        }
        if not filter_events_to_run:
            kwargs["filter_events_to_run"] = False
        return await load_run_summary(**kwargs)
    except Exception as exc:
        logger.debug(
            "cli_summary_load_failed",
            extra={"uid": uid, "run_id": run_id, "error": str(exc)},
            exc_info=True,
        )
        return None


async def _handle_delete_uid(args: argparse.Namespace) -> None:
    """Delete all on-disk artefacts for a uid (raw DB / workdir)."""
    from bili_unit import db_path, session

    raw = db_path(args.uid)
    workdir = raw.parent / str(args.uid)
    renderer = CliRenderer()
    if not raw.exists() and not workdir.exists():
        renderer.delete_missing(uid=args.uid)
        return

    if not args.yes:
        renderer.delete_plan(
            uid=args.uid,
            raw=raw,
            workdir=workdir,
        )
        answer = input("Confirm delete? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            renderer.delete_cancelled()
            return

    async with session() as cmd:
        stats = await cmd.delete_uid(args.uid)
    renderer.delete_stats(stats)

async def _handle_login(_args: argparse.Namespace) -> None:
    """QR code login."""
    from bili_unit.fetching.auth import qr_login, save_credential_to_env

    cred = await qr_login()
    path = save_credential_to_env(cred)
    print(f"Credential saved to {path}")


async def _handle_init_mimo(args: argparse.Namespace) -> None:
    """Interactive MiMo ASR backend configuration."""
    from bili_unit._env import reload_settings
    from bili_unit.processing.audio._init_wizard import probe_mimo_model, run_wizard

    run_wizard()
    if args.test:
        reload_settings()
        result = await probe_mimo_model()
        preview = (result.text or "").replace("\n", " ")[:80]
        print(
            "\nMiMo probe OK: "
            f"model={result.model or 'unknown'} "
            f"seconds={result.duration if result.duration is not None else 'unknown'} "
            f"audio_tokens={result.audio_tokens if result.audio_tokens is not None else 'unknown'}",
        )
        if preview:
            print(f"  preview: {preview}")
    print(
        "\nNext run `uv run bili-unit asr <uid>` to use MiMo ASR by default."
        "\nUse `-b mock` for a temporary no-network ASR run.",
    )


async def _handle_tui(_args: argparse.Namespace) -> None:
    """Open the local dashboard TUI."""
    from bili_unit.tui import run_tui

    await run_tui()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bili-unit",
        description="Bilibili data unit - unified CLI.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Only show warnings and errors (overrides --verbose)",
    )
    parser.add_argument(
        "--log-file", default=None, metavar="PATH",
        help="Also write DEBUG-level JSON Lines to PATH (handy for post-mortem)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- fetch ---
    p_fetch = sub.add_parser(
        "fetch",
        help="Run fetching for a uid (writes raw_payload rows)",
    )
    p_fetch.add_argument("uid", type=int, help="Target Bilibili user uid")
    _add_fetch_selection_args(p_fetch)
    p_fetch.add_argument(
        "--mode", "-m",
        choices=["incremental", "refresh", "full"],
        default="incremental",
        help="Fetching mode (default: incremental)",
    )

    # --- asr ---
    p_proc = sub.add_parser(
        "asr",
        help="Run audio ASR for a uid",
    )
    p_proc.add_argument("uid", type=int, help="Target Bilibili user uid")
    p_proc.add_argument(
        "--mode", "-m",
        choices=["incremental", "full"],
        default="incremental",
        help="ASR mode (default: incremental)",
    )
    p_proc.add_argument(
        "--asr-backend", "-b",
        choices=["mock", "mimo"],
        default=None,
        help=(
            "Override BILI_PROCESSING_ASR_BACKEND for this run "
            "(e.g. -b mock to skip MiMo without editing .env)."
        ),
    )
    p_proc.add_argument(
        "--limit", type=_positive_int, default=None, metavar="N",
        help="Process only the first N discovered bvids (after other filters).",
    )
    _add_asr_selection_args(p_proc)
    p_proc.add_argument(
        "--retry-failed-only", action="store_true",
        help=(
            "Only process bvids whose previous ASR status is FAILED. "
            "Implies --mode incremental; conflicts with --mode full."
        ),
    )
    p_proc.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Discover candidates and print them without dispatching workers. "
            "Does not update ASR task progress."
        ),
    )
    p_proc.add_argument(
        "--max-audio-seconds", type=_positive_float, default=None, metavar="SECONDS",
        help=(
            "Stop before ASR dispatch if discovered audio exceeds this many seconds."
        ),
    )
    p_proc.add_argument(
        "--max-audio-tokens", type=_positive_int, default=None, metavar="TOKENS",
        help=(
            "Stop before ASR dispatch if estimated audio tokens exceed this cap."
        ),
    )

    # --- login / init-mimo ---
    sub.add_parser("login", help="QR code login to Bilibili")
    p_init_mimo = sub.add_parser(
        "init-mimo",
        help="Interactive MiMo ASR backend setup (writes BILI_PROCESSING_ASR_* to .env)",
    )
    p_init_mimo.add_argument(
        "--test",
        action="store_true",
        help="After writing .env, call MiMo once with a tiny WAV probe.",
    )

    sub.add_parser("tui", help="Open the local dashboard TUI")

    # --- delete-uid ---
    p_del = sub.add_parser("delete-uid", help="Delete all data for a uid")
    p_del.add_argument("uid", type=int, help="Target Bilibili user uid")
    p_del.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt",
    )

    return parser


def _add_fetch_selection_args(parser: argparse.ArgumentParser) -> None:
    fetch_group = parser.add_mutually_exclusive_group()
    fetch_group.add_argument(
        "--exclude", "--exclude-endpoints", "-x",
        dest="exclude_endpoints",
        nargs="+",
        default=None,
        metavar="EP",
        help=(
            "Endpoint names to skip; everything else is fetched. "
            "Useful for dropping heavy endpoints (e.g. -x video_detail)."
        ),
    )
    fetch_group.add_argument(
        "--include", "--endpoints", "-e",
        dest="endpoints",
        nargs="+",
        default=None,
        metavar="EP",
        help="Endpoint names to fetch (debug; mutually exclusive with -x).",
    )
    fetch_group.add_argument(
        "--profile", "-p",
        choices=["all", "minimal"],
        default="all",
        help=(
            "Endpoint set preset (mutually exclusive with -e/-x):\n"
            "  all      all registered endpoints\n"
            "  minimal  lightweight listing endpoints for smoke / CI"
        ),
    )


def _add_asr_selection_args(parser: argparse.ArgumentParser) -> None:
    bvid_group = parser.add_mutually_exclusive_group()
    bvid_group.add_argument(
        "--include", "--only-bvids", "-e",
        dest="only_bvids",
        nargs="+",
        default=None,
        metavar="BVID",
        help="Process only the given bvid(s); combinable with --limit.",
    )
    bvid_group.add_argument(
        "--exclude", "--exclude-bvids", "-x",
        dest="exclude_bvids",
        nargs="+",
        default=None,
        metavar="BVID",
        help="Skip the given bvid(s); combinable with --limit.",
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    configure_logging(
        verbose=args.verbose,
        quiet=args.quiet,
        log_file=Path(args.log_file) if args.log_file else None,
    )

    import sys
    if sys.platform == "win32":
        # Proactor's __del__ logs noisy ConnectionResetError tracebacks during
        # interpreter shutdown when aiohttp transports outlive the loop.
        # Selector loop has none of that and is fine for our workload (no UDP).
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    handlers = {
        "fetch": _handle_fetch,
        "asr": _handle_asr,
        "delete-uid": _handle_delete_uid,
        "tui": _handle_tui,
        "login": _handle_login,
        "init-mimo": _handle_init_mimo,
    }

    handler = handlers[args.command]
    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
