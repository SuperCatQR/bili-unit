# python -m bili_unit — unified CLI for the bili unit.
#
# Usage:
#   python -m bili_unit fetch     <uid> [options]   — run fetching
#   python -m bili_unit process   <uid> [options]   — run processing
#   python -m bili_unit query     <uid>              — query all results
#   python -m bili_unit login                        — QR code login
#   python -m bili_unit init-mimo                    — interactive MiMo ASR setup
#   python -m bili_unit list-uids                    — list fetched uids
#
# Internally goes through ``bili_unit.assemble()`` →
# ``BiliCommand`` / ``BiliQuery`` (the unit-level entries). The legacy
# ``python -m bili_unit.fetching`` entry point still works.

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

    # Both None → keep downstream "all registered" behaviour.
    return None


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

async def _handle_fetch(args: argparse.Namespace) -> None:
    """Run fetching via the unit-level BiliCommand / BiliQuery."""
    from bili_unit import EndpointStatus, assemble
    from bili_unit.fetching.client import ENDPOINTS

    all_endpoints = [ep.name for ep in ENDPOINTS]
    endpoints = _resolve_subset(
        flag_label="endpoint",
        all_names=all_endpoints,
        include=args.endpoints,
        exclude=args.exclude_endpoints,
    )

    cmd, qry, data, error = await assemble()
    try:
        result = await cmd.fetch(args.uid, endpoints=endpoints, mode=args.mode)
        print(f"uid={args.uid}  status={result.status.value}")

        task = await qry.fetching.get_task(args.uid)
        if task is not None:
            for ep_name, ep_dto in task.endpoints.items():
                status = ep_dto.status.value
                pages = None
                if ep_dto.raw_payload and "pages" in ep_dto.raw_payload:
                    pages = len(ep_dto.raw_payload["pages"])
                extra = f"  pages={pages}" if pages is not None else ""
                errors = len(ep_dto.errors)
                err_info = f"  errors={errors}" if errors else ""
                if ep_name == "video_detail" and ep_dto.progress:
                    item_count = ep_dto.progress.get("completed_items", "?")
                    total_count = ep_dto.progress.get("total_items", "?")
                    extra += f"  items={item_count}/{total_count}"
                print(f"  {ep_name}: {status}{extra}{err_info}")

        details = await qry.fetching.list_video_details(args.uid)
        if details:
            success_count = sum(1 for _, s in details if s == EndpointStatus.SUCCESS)
            print(f"  video_detail items: {success_count}/{len(details)} stored")
    finally:
        await data.close()
        await error.close()


async def _handle_query(args: argparse.Namespace) -> None:
    """Query both fetching and processing results for a uid."""
    from bili_unit import assemble

    _, qry, data, error = await assemble()
    try:
        task = await qry.fetching.get_task(args.uid)
        if task is None:
            print(f"uid={args.uid}  (no task found)")
            return
        print(f"uid={args.uid}  fetching_status={task.status.value}")
        for ep_name, ep_dto in task.endpoints.items():
            status = ep_dto.status.value
            print(f"  {ep_name}: {status}")
    finally:
        await data.close()
        await error.close()


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


async def _handle_list_uids(_args: argparse.Namespace) -> None:
    """List all uids with fetching data."""
    from bili_unit import assemble

    _, qry, data, error = await assemble()
    try:
        tasks = await qry.fetching.list_tasks()
        if not tasks:
            print("(no tasks found)")
            return
        for t in tasks:
            print(f"  uid={t['uid']}  status={t['status'].value}")
    finally:
        await data.close()
        await error.close()


async def _handle_process(args: argparse.Namespace) -> None:
    """Run processing via the unit-level BiliCommand / BiliQuery."""
    from bili_unit import assemble
    from bili_unit.processing.transform import HANDLERS

    all_item_types = HANDLERS.names()
    item_types = _resolve_subset(
        flag_label="item_type",
        all_names=all_item_types,
        include=args.item_types,
        exclude=args.exclude_item_types,
    )

    cmd, qry, data, error = await assemble(
        asr_backend_override=getattr(args, "asr_backend", None),
    )
    try:
        result = await cmd.process(args.uid, mode=args.mode, item_types=item_types)
        print(f"uid={args.uid}  status={result.status.value}")
        task = await qry.processing.get_task(args.uid)
        if task is not None:
            for pname, pdto in task.pipelines.items():
                print(f"  pipeline {pname}: {pdto.status.value}")
                for it, counts in pdto.items.items():
                    total = counts.get("total", 0)
                    completed = counts.get("completed", 0)
                    failed = counts.get("failed", 0)
                    skipped = counts.get("skipped", 0)
                    print(
                        f"    {it}: {completed}/{total} done, "
                        f"{failed} failed, {skipped} skipped",
                    )
    finally:
        await cmd.close()
        del data, error


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
        help="Run fetching for a uid (default: all 25 endpoints)",
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
    p_fetch.add_argument(
        "--mode", "-m",
        choices=["incremental", "refresh", "full"],
        default="incremental",
        help="Fetching mode (default: incremental)",
    )

    # --- process ---
    p_proc = sub.add_parser(
        "process",
        help="Run processing for a uid (default: all registered transform handlers)",
    )
    p_proc.add_argument("uid", type=int, help="Target Bilibili user uid")
    p_proc.add_argument(
        "--mode", "-m",
        choices=["incremental", "full"],
        default="incremental",
        help="Processing mode (default: incremental)",
    )
    proc_group = p_proc.add_mutually_exclusive_group()
    proc_group.add_argument(
        "--exclude-item-types", "-x", nargs="+", default=None, metavar="TYPE",
        help=(
            "Transform item_types to skip; everything else runs. "
            "Recommended way to drop heavy handlers (e.g. -x video_metadata)."
        ),
    )
    proc_group.add_argument(
        "--item-types", "-t", nargs="+", default=None, metavar="TYPE",
        help="Subset of transform item_types to run (debug; mutually exclusive with -x).",
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

    # --- query ---
    p_query = sub.add_parser("query", help="Query results for a uid")
    p_query.add_argument("uid", type=int, help="Target Bilibili user uid")

    # --- login ---
    sub.add_parser("login", help="QR code login to Bilibili")

    # --- init-mimo ---
    sub.add_parser(
        "init-mimo",
        help="Interactive MiMo ASR backend setup (writes BILI_PROCESSING_ASR_* to .env)",
    )

    # --- list-uids ---
    sub.add_parser("list-uids", help="List all fetched uids")

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
        "process": _handle_process,
        "query": _handle_query,
        "login": _handle_login,
        "init-mimo": _handle_init_mimo,
        "list-uids": _handle_list_uids,
    }

    handler = handlers[args.command]
    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
