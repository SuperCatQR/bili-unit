# python -m bili_unit — unified CLI for the bili unit.
#
# Phase 5 contract: read-side commands removed. Consumers query the SQLite
# database file directly (see ``bili_unit.db_path``). The CLI keeps only
# write-side actions plus auth helpers:
#
#   python -m bili_unit fetch        <uid> [options]   — run fetching
#   python -m bili_unit parse        <uid> [options]   — run parsing
#   python -m bili_unit process      <uid> [options]   — run processing
#   python -m bili_unit delete-uid   <uid> [-y]        — delete all data for a uid
#   python -m bili_unit login                          — QR code login
#   python -m bili_unit init-mimo                      — interactive MiMo ASR setup

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ._logging import configure_logging

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

    Unknown names in either list raise SystemExit(2) with a helpful message — typos
    here would silently change which endpoints/handlers run.
    """
    known = set(all_names)

    if include is not None:
        unknown = [n for n in include if n not in known]
        if unknown:
            raise SystemExit(
                f"unknown {flag_label}(s): {', '.join(unknown)}. "
                f"Known: {', '.join(all_names)}",
            )
        return list(include)

    if exclude is not None:
        unknown = [n for n in exclude if n not in known]
        if unknown:
            raise SystemExit(
                f"unknown {flag_label}(s) in --exclude: {', '.join(unknown)}. "
                f"Known: {', '.join(all_names)}",
            )
        excluded = set(exclude)
        kept = [n for n in all_names if n not in excluded]
        if not kept:
            raise SystemExit(
                f"--exclude removed every {flag_label}; nothing to run.",
            )
        return kept

    return None


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

async def _handle_fetch(args: argparse.Namespace) -> None:
    """Run fetching via the unit-level BiliCommand."""
    from bili_unit import session
    from bili_unit.fetching._endpoint_catalog import ENDPOINTS, resolve_profile

    all_endpoints = [ep.name for ep in ENDPOINTS]

    if args.endpoints is not None or args.exclude_endpoints is not None:
        endpoints = _resolve_subset(
            flag_label="endpoint",
            all_names=all_endpoints,
            include=args.endpoints,
            exclude=args.exclude_endpoints,
        )
    else:
        endpoints = resolve_profile(args.profile)

    async with session() as cmd:
        result = await cmd.fetch(args.uid, endpoints=endpoints, mode=args.mode)
        print(f"uid={args.uid}  status={result.status.value}")


async def _handle_parse(args: argparse.Namespace) -> None:
    """Run parsing via the unit-level BiliCommand."""
    from bili_unit import session

    async with session() as cmd:
        result = await cmd.parse(
            args.uid,
            mode=args.mode,
            download_images=args.download_images,
        )
        print(f"uid={args.uid}  status={result.status.value}")


async def _handle_process(args: argparse.Namespace) -> None:
    """Run processing via the unit-level BiliCommand."""
    from bili_unit import session

    if args.retry_failed_only and args.mode == "full":
        raise SystemExit(
            "--retry-failed-only conflicts with --mode full "
            "(it can only re-process FAILED items, which requires incremental mode).",
        )
    effective_mode = "incremental" if args.retry_failed_only else args.mode

    async with session(asr_backend_override=getattr(args, "asr_backend", None)) as cmd:
        result = await cmd.process(
            args.uid,
            mode=effective_mode,
            limit=args.limit,
            only_bvids=args.only_bvids,
            retry_failed_only=args.retry_failed_only,
            dry_run=args.dry_run,
        )

        if args.dry_run:
            candidates = result.dry_run_candidates or []
            print(
                f"uid={args.uid}  status={result.status.value}  "
                f"(dry_run, {len(candidates)} candidates)",
            )
            if candidates:
                print(f"  candidates: {', '.join(candidates)}")
            return

        print(f"uid={args.uid}  status={result.status.value}")


async def _handle_delete_uid(args: argparse.Namespace) -> None:
    """Delete all on-disk artefacts for a uid (main DB / raw DB / workdir)."""
    from bili_unit import db_path, raw_db_path, session

    main = db_path(args.uid)
    if not main.exists() and not raw_db_path(args.uid).exists():
        print(f"uid={args.uid}: 未找到该用户的数据")
        return

    if not args.yes:
        print(
            f"即将删除 uid={args.uid} 的全部数据："
            f"\n  {main}"
            f"\n  {raw_db_path(args.uid)}"
            f"\n  {main.parent / str(args.uid)}/  (images / audio caches)",
        )
        answer = input("确认删除? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消")
            return

    async with session() as cmd:
        stats = await cmd.delete_uid(args.uid)
    parts = ", ".join(f"{k}={v}" for k, v in stats.items())
    print(f"  {parts}")


async def _handle_login(_args: argparse.Namespace) -> None:
    """QR code login."""
    from bili_unit.fetching.auth import qr_login, save_credential_to_env

    cred = await qr_login()
    path = save_credential_to_env(cred)
    print(f"凭据已保存到 {path}")


async def _handle_init_mimo(_args: argparse.Namespace) -> None:
    """Interactive MiMo ASR backend configuration."""
    from bili_unit.processing.audio._init_wizard import run_wizard

    run_wizard()
    print(
        "\n下次跑 `python -m bili_unit process <uid>` 时将默认走 MiMo ASR。"
        "\n要临时跳过 ASR，使用 `-b mock`。",
    )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bili_unit",
        description="Bilibili data unit — unified CLI.",
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
        help="Run fetching for a uid (default: all registered endpoints)",
    )
    p_fetch.add_argument("uid", type=int, help="Target Bilibili user uid")
    fetch_group = p_fetch.add_mutually_exclusive_group()
    fetch_group.add_argument(
        "--exclude-endpoints", "-x", nargs="+", default=None, metavar="EP",
        help=(
            "Endpoint names to skip; everything else is fetched. "
            "Recommended way to drop heavy/expensive endpoints (e.g. -x video_detail)."
        ),
    )
    fetch_group.add_argument(
        "--endpoints", "-e", nargs="+", default=None, metavar="EP",
        help="Endpoint names to fetch (debug; mutually exclusive with -x).",
    )
    fetch_group.add_argument(
        "--profile", "-p",
        choices=["all", "parsing", "minimal"],
        default="all",
        help=(
            "Endpoint set preset (mutually exclusive with -e/-x):\n"
            "  all     — 所有已注册端点（默认；中等账号 ~17 分钟）\n"
            "  parsing — parsing 层实际消费的 11 个端点（≈ 2-3 分钟，推荐）\n"
            "  minimal — 5 个 listing 端点，用于 smoke / CI"
        ),
    )
    p_fetch.add_argument(
        "--mode", "-m",
        choices=["incremental", "refresh", "full"],
        default="incremental",
        help="Fetching mode (default: incremental)",
    )

    # --- parse ---
    p_parse = sub.add_parser(
        "parse",
        help="Run parsing for a uid (converts raw payloads to typed objects)",
    )
    p_parse.add_argument("uid", type=int, help="Target Bilibili user uid")
    p_parse.add_argument(
        "--mode", "-m",
        choices=["full", "incremental"],
        default="full",
        help="Parsing mode (default: full)",
    )
    p_parse.add_argument(
        "--download-images", "-i",
        action="store_true",
        default=False,
        help="Download images (avatar, covers, dynamic pics) after parsing",
    )

    # --- process ---
    p_proc = sub.add_parser(
        "process",
        help="Run processing for a uid (audio pipeline)",
    )
    p_proc.add_argument("uid", type=int, help="Target Bilibili user uid")
    p_proc.add_argument(
        "--mode", "-m",
        choices=["incremental", "full"],
        default="incremental",
        help="Processing mode (default: incremental)",
    )
    p_proc.add_argument(
        "--asr-backend", "-b",
        choices=["mock", "mimo", "whisper"],
        default=None,
        help=(
            "Override BILI_PROCESSING_ASR_BACKEND for this run "
            "(e.g. -b mock to skip MiMo without editing .env)."
        ),
    )
    p_proc.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Process only the first N discovered bvids (after other filters).",
    )
    p_proc.add_argument(
        "--only-bvids", nargs="+", default=None, metavar="BVID",
        help="Process only the given bvid(s); combinable with --limit.",
    )
    p_proc.add_argument(
        "--retry-failed-only", action="store_true",
        help=(
            "Only process bvids whose previous processing status is FAILED. "
            "Implies --mode incremental; conflicts with --mode full."
        ),
    )
    p_proc.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Discover candidates and print them without dispatching workers. "
            "Task / progress are still written; status is SUCCESS."
        ),
    )

    # --- login / init-mimo ---
    sub.add_parser("login", help="QR code login to Bilibili")
    sub.add_parser(
        "init-mimo",
        help="Interactive MiMo ASR backend setup (writes BILI_PROCESSING_ASR_* to .env)",
    )

    # --- delete-uid ---
    p_del = sub.add_parser("delete-uid", help="Delete all data for a uid")
    p_del.add_argument("uid", type=int, help="Target Bilibili user uid")
    p_del.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    configure_logging(
        verbose=args.verbose,
        quiet=args.quiet,
        log_file=Path(args.log_file) if args.log_file else None,
    )

    handlers = {
        "fetch": _handle_fetch,
        "parse": _handle_parse,
        "process": _handle_process,
        "delete-uid": _handle_delete_uid,
        "login": _handle_login,
        "init-mimo": _handle_init_mimo,
    }

    handler = handlers[args.command]
    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
