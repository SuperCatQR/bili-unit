# python -m bili_unit — unified CLI for the bili unit.
#
# Usage:
#   python -m bili_unit fetch     <uid> [options]   — run fetching
#   python -m bili_unit parse     <uid> [options]   — run parsing
#   python -m bili_unit process   <uid> [options]   — run processing
#   python -m bili_unit query     <uid>              — query all results
#   python -m bili_unit login                        — QR code login
#   python -m bili_unit init-mimo                    — interactive MiMo ASR setup
#   python -m bili_unit list-uids                    — list fetched uids
#   python -m bili_unit delete-uid  <uid> [-y]       — delete all data for a uid
#   python -m bili_unit video-full  <uid> <bvid>     — show full video result
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
    from bili_unit import EndpointStatus, session
    from bili_unit.fetching._endpoint_catalog import ENDPOINTS, resolve_profile

    all_endpoints = [ep.name for ep in ENDPOINTS]

    # Resolve endpoint subset: -e / -x take precedence; fall back to --profile.
    if args.endpoints is not None or args.exclude_endpoints is not None:
        endpoints = _resolve_subset(
            flag_label="endpoint",
            all_names=all_endpoints,
            include=args.endpoints,
            exclude=args.exclude_endpoints,
        )
    else:
        endpoints = resolve_profile(args.profile)  # None for "all"

    async with session() as (cmd, qry):
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


async def _handle_parse(args: argparse.Namespace) -> None:
    """Run parsing via the unit-level BiliCommand / BiliQuery."""
    from bili_unit import session

    async with session() as (cmd, qry):
        result = await cmd.parse(
            args.uid,
            mode=args.mode,
            download_images=args.download_images,
        )
        print(f"uid={args.uid}  status={result.status.value}")

        task = await qry.parsing.get_task(args.uid)
        if task is not None:
            for model_name, model_dto in task.models.items():
                print(f"  {model_name}: {model_dto.status.value}  count={model_dto.count}")
            if task.images is not None:
                img = task.images
                print(
                    f"  images: total={img.total}  ok={img.ok}  "
                    f"skipped={img.skipped}  failed={img.failed}",
                )
                if img.failed_urls:
                    for url in img.failed_urls[:5]:
                        print(f"    failed_url: {url}")
                    if len(img.failed_urls) > 5:
                        print(f"    ... and {len(img.failed_urls) - 5} more")


async def _handle_query(args: argparse.Namespace) -> None:
    """Query both fetching and processing results for a uid."""
    from bili_unit import session

    async with session() as (cmd, qry):
        task = await qry.fetching.get_task(args.uid)
        if task is None:
            print(f"uid={args.uid}  (no task found)")
            return
        print(f"uid={args.uid}  fetching_status={task.status.value}")
        for ep_name, ep_dto in task.endpoints.items():
            status = ep_dto.status.value
            print(f"  {ep_name}: {status}")


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
    from bili_unit import session

    async with session() as (cmd, qry):
        tasks = await qry.fetching.list_tasks()
        if not tasks:
            print("(no tasks found)")
            return
        for t in tasks:
            print(f"  uid={t['uid']}  status={t['status'].value}")


async def _handle_delete_uid(args: argparse.Namespace) -> None:
    """Delete all state for a uid across fetching, parsing, and processing."""
    from bili_unit import session

    async with session() as (cmd, qry):
        # Check fetching side first to confirm uid existence
        task = await qry.fetching.get_task(args.uid)
        if task is None:
            print(f"uid={args.uid}: 未找到该用户的抓取数据")
            return

        if not args.yes:
            print(f"即将删除 uid={args.uid} 在 fetching / parsing / processing 三个阶段的所有数据")
            print("（任务、端点结果、进度、错误记录、解析对象、图片、转写结果、临时文件、ASR 缓存等）")
            answer = input("确认删除? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("已取消")
                return

        stats = await cmd.delete_uid(args.uid)

        # 简洁汇报，每 stage 一行
        for stage_name in ("fetching", "parsing", "processing"):
            stage_stats = stats.get(stage_name, {})
            parts = ", ".join(f"{k}={v}" for k, v in stage_stats.items())
            print(f"  {stage_name}: {parts}")


async def _handle_video_full(args: argparse.Namespace) -> None:
    """Show full video result (metadata + transcription)."""
    from bili_unit import session

    async with session() as (cmd, qry):
        full = await qry.get_video_full(args.uid, args.bvid)
        if full is None:
            print(f"uid={args.uid} bvid={args.bvid}: 未找到视频")
            return
        meta = full.metadata
        if meta is None:
            print(f"uid={args.uid} bvid={args.bvid}: 元数据未处理")
        else:
            print(f"uid={args.uid} bvid={args.bvid}")
            print(f"  title: {meta.get('title')}")
            print(f"  duration: {meta.get('duration')}s")
            print(f"  tags: {', '.join(meta.get('tags', []))}")
        if full.transcription is None:
            print("  transcription: (none)")
        else:
            tr = full.transcription
            chars = len((tr.result or {}).get("text", "")) if tr.result else 0
            print(f"  transcription: {tr.status.value}  chars={chars}")


async def _handle_process(args: argparse.Namespace) -> None:
    """Run processing via the unit-level BiliCommand / BiliQuery."""
    from bili_unit import session

    async with session(asr_backend_override=getattr(args, "asr_backend", None)) as (cmd, qry):
        result = await cmd.process(args.uid, mode=args.mode)
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

    # --- delete-uid ---
    p_del = sub.add_parser("delete-uid", help="Delete all data for a uid")
    p_del.add_argument("uid", type=int, help="Target Bilibili user uid")
    p_del.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt",
    )

    # --- video-full ---
    p_vf = sub.add_parser("video-full", help="Show full result for a video")
    p_vf.add_argument("uid", type=int, help="Target Bilibili user uid")
    p_vf.add_argument("bvid", help="Video bvid")

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
        "query": _handle_query,
        "login": _handle_login,
        "init-mimo": _handle_init_mimo,
        "list-uids": _handle_list_uids,
        "delete-uid": _handle_delete_uid,
        "video-full": _handle_video_full,
    }

    handler = handlers[args.command]
    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
