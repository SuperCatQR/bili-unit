# python -m bili_unit.fetching — CLI entry point.

import argparse
import asyncio
import logging
from datetime import datetime
from pathlib import Path

from .. import _logging as _bili_logging
from . import EndpointStatus, assemble

logger = logging.getLogger("bili.fetching.cli")


async def _run_login() -> None:
    """QR code login: scan → save credential to .env."""
    from .auth import qr_login, save_credential_to_env

    cred = await qr_login()
    path = save_credential_to_env(cred)
    print(f"凭据已保存到 {path}")


async def _run_fetch(uid: int, endpoints: list[str] | None, mode: str = "incremental") -> None:
    """Run fetching for a uid and print the result."""
    cmd, qry, data, error = await assemble()
    try:
        result = await cmd.fetch_uid(uid, endpoints=endpoints, mode=mode)
        print(f"uid={uid}  status={result.status.value}")

        # show endpoint details
        task = await qry.get_task(uid)
        if task is not None:
            for ep_name, ep_dto in task.endpoints.items():
                status = ep_dto.status.value
                pages = None
                if ep_dto.raw_payload and "pages" in ep_dto.raw_payload:
                    pages = len(ep_dto.raw_payload["pages"])
                extra = f"  pages={pages}" if pages is not None else ""
                errors = len(ep_dto.errors)
                err_info = f"  errors={errors}" if errors else ""
                # item-level endpoint: show item_progress from task
                if ep_name == "video_detail" and ep_dto.progress:
                    item_count = ep_dto.progress.get("completed_items", "?")
                    total_count = ep_dto.progress.get("total_items", "?")
                    extra += f"  items={item_count}/{total_count}"
                print(f"  {ep_name}: {status}{extra}{err_info}")

        # show video_detail summary if available
        details = await qry.list_video_details(uid)
        if details:
            success_count = sum(1 for _, s in details if s == EndpointStatus.SUCCESS)
            print(f"  video_detail items: {success_count}/{len(details)} stored")
    finally:
        await data.close()
        await error.close()


async def _run_query(uid: int) -> None:
    """Query existing fetching results for a uid."""
    _, qry, data, error = await assemble()
    try:
        task = await qry.get_task(uid)
        if task is None:
            print(f"uid={uid}  (no task found)")
            return
        print(f"uid={uid}  status={task.status.value}")
        for ep_name, ep_dto in task.endpoints.items():
            status = ep_dto.status.value
            available = "available" if ep_dto.available else "unavailable"
            progress_done = None
            if ep_dto.progress:
                progress_done = ep_dto.progress.get("done")
            prog_info = f"  done={progress_done}" if progress_done is not None else ""
            print(f"  {ep_name}: {status}  ({available}){prog_info}")

        # show video_detail summary
        details = await qry.list_video_details(uid)
        if details:
            success_count = sum(1 for _, s in details if s == EndpointStatus.SUCCESS)
            print(f"  video_detail items: {success_count}/{len(details)} stored")
    finally:
        await data.close()
        await error.close()


async def _run_list_uids() -> None:
    """List all uids that have fetching tasks in the store."""
    _, qry, data, error = await assemble()
    try:
        tasks = await qry.list_tasks()
        if not tasks:
            print("(no tasks found)")
            return

        print(f"已抓取的目标用户 (共 {len(tasks)} 个):\n")
        # header
        print(f"  {'uid':<18} {'状态':<20} {'端点':>4}  {'视频详情':<10}  更新时间")
        print(f"  {'─' * 18} {'─' * 20} {'─' * 4}  {'─' * 10}  {'─' * 19}")

        for t in tasks:
            uid = t["uid"]
            status = t["status"].value
            ep_count = t["endpoint_count"]
            vd = t["video_detail_items"] or "—"
            updated = t["updated_at"]
            if updated:
                dt = datetime.fromtimestamp(updated / 1000)
                updated_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                updated_str = "—"
            print(f"  {uid:<18} {status:<20} {ep_count:>4}  {vd:<10}  {updated_str}")
    finally:
        await data.close()
        await error.close()


async def _run_delete_uid(uid: int, yes: bool = False) -> None:
    """Delete all data for a uid from both data and error stores."""
    _, qry, data, error = await assemble()
    try:
        # Check if the uid exists
        task = await qry.get_task(uid)
        if task is None:
            print(f"uid={uid}: 未找到该用户的抓取数据")
            return

        if not yes:
            print(f"即将删除 uid={uid} 的所有抓取数据（任务、端点结果、进度、错误记录等）")
            answer = input("确认删除? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("已取消")
                return

        # Delete all keys with prefix uid:{uid}:
        all_rows = await data.list_prefix(f"uid:{uid}:")
        count = 0
        for key, _ in all_rows:
            await data.delete(key)
            count += 1

        # Delete error records
        err_count = await error.delete_by_uid(uid)

        print(f"uid={uid}: 已删除 {count} 条数据记录, {err_count} 条错误记录")
    finally:
        await data.close()
        await error.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m bili_unit.fetching",
        description="Bilibili user data fetching CLI.",
    )
    parser.add_argument(
        "uid", type=int, nargs="?", default=None,
        help="Target Bilibili user uid (required unless --login)",
    )
    parser.add_argument(
        "--login", "-l",
        action="store_true",
        help="QR code login: scan with Bilibili app, save credential to .env",
    )
    parser.add_argument(
        "--list-uids",
        action="store_true",
        help="List all target uids with fetching tasks in the store",
    )
    parser.add_argument(
        "--delete-uid",
        action="store_true",
        help="Delete all data for the specified uid",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt (for destructive operations like --delete-uid)",
    )
    parser.add_argument(
        "--endpoints", "-e",
        nargs="+",
        default=None,
        metavar="EP",
        help="Endpoint names to fetch (debug; mutually exclusive with -x).",
    )
    parser.add_argument(
        "--exclude-endpoints", "-x",
        nargs="+",
        default=None,
        metavar="EP",
        help=(
            "Endpoint names to skip; everything else is fetched. "
            "Mutually exclusive with -e."
        ),
    )
    parser.add_argument(
        "--query", "-q",
        action="store_true",
        help="Query existing results without triggering fetch",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["incremental", "refresh", "full"],
        default="incremental",
        help=(
            "Fetching mode: 'incremental' scans for new content (default), "
            "'refresh' also re-fetches stale items older than the configured window, "
            "'full' re-fetches everything"
        ),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only show warnings and errors (overrides --verbose)",
    )
    parser.add_argument(
        "--log-file", default=None, metavar="PATH",
        help="Also write DEBUG-level JSON Lines to PATH",
    )

    args = parser.parse_args()

    _bili_logging.configure_logging(
        verbose=args.verbose,
        quiet=args.quiet,
        log_file=Path(args.log_file) if args.log_file else None,
    )

    if args.login:
        asyncio.run(_run_login())
        return

    if args.list_uids:
        asyncio.run(_run_list_uids())
        return

    if args.delete_uid:
        if args.uid is None:
            parser.error("--delete-uid requires uid")
        asyncio.run(_run_delete_uid(args.uid, yes=args.yes))
        return

    if args.uid is None:
        parser.error("uid is required (or use --login / --list-uids)")

    if args.query:
        asyncio.run(_run_query(args.uid))
    else:
        if args.endpoints is not None and args.exclude_endpoints is not None:
            parser.error("--endpoints/-e and --exclude-endpoints/-x are mutually exclusive")
        endpoints = args.endpoints
        if endpoints is None and args.exclude_endpoints is not None:
            from .client import ENDPOINTS
            all_eps = [ep.name for ep in ENDPOINTS]
            excluded = set(args.exclude_endpoints)
            unknown = [n for n in args.exclude_endpoints if n not in set(all_eps)]
            if unknown:
                parser.error(
                    f"unknown endpoint(s) in --exclude-endpoints: {', '.join(unknown)}",
                )
            endpoints = [n for n in all_eps if n not in excluded]
            if not endpoints:
                parser.error("--exclude-endpoints removed every endpoint; nothing to fetch")
        asyncio.run(_run_fetch(args.uid, endpoints, mode=args.mode))


if __name__ == "__main__":
    main()
