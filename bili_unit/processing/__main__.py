# python -m bili_unit.processing — processing CLI entry point.

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime

from . import ProcessingItemStatus, assemble


async def _run_process(uid: int, mode: str, item_types: list[str] | None) -> None:
    cmd, qry, data, error = await assemble()
    try:
        result = await cmd.process_uid(uid, item_types=item_types, mode=mode)
        print(f"uid={uid}  status={result.status.value}")

        task = await qry.get_task(uid)
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
        # cmd.close() already closes data + error + fetching stores.
        del data, error


async def _run_query(uid: int) -> None:
    _, qry, _data, _error = await assemble()
    try:
        task = await qry.get_task(uid)
        if task is None:
            print(f"uid={uid}  (no processing task found)")
            return
        print(f"uid={uid}  processing_status={task.status.value}")
        for pname, pdto in task.pipelines.items():
            print(f"  pipeline {pname}: {pdto.status.value}")
            for it, counts in pdto.items.items():
                print(f"    {it}: {counts}")
    finally:
        await _data.close()
        await _error.close()


async def _run_list_uids() -> None:
    _, qry, _data, _error = await assemble()
    try:
        tasks = await qry.list_tasks()
        if not tasks:
            print("(no processing tasks found)")
            return
        print(f"已处理的目标用户 (共 {len(tasks)} 个):\n")
        for t in tasks:
            updated = t.get("updated_at")
            updated_str = "—"
            if updated:
                updated_str = datetime.fromtimestamp(updated / 1000).strftime(
                    "%Y-%m-%d %H:%M:%S",
                )
            print(
                f"  uid={t['uid']:<14} status={t['status'].value:<18} "
                f"pipelines={t['pipeline_count']}  updated={updated_str}",
            )
    finally:
        await _data.close()
        await _error.close()


async def _run_video_full(uid: int, bvid: str) -> None:
    _, qry, _data, _error = await assemble()
    try:
        full = await qry.get_video_full(uid, bvid)
        if full is None:
            print(f"uid={uid} bvid={bvid}: 未找到视频")
            return
        meta = full.metadata
        if meta is None:
            print(f"uid={uid} bvid={bvid}: 元数据未处理")
        else:
            r = meta.result or {}
            print(f"uid={uid} bvid={bvid}  status={meta.status.value}")
            print(f"  title: {r.get('title')}")
            print(f"  duration: {r.get('duration')}s")
            print(f"  tags: {', '.join(r.get('tags', []))}")
        if full.transcription is None:
            print("  transcription: (none)")
        else:
            tr = full.transcription
            chars = len((tr.result or {}).get("text", "")) if tr.result else 0
            print(f"  transcription: {tr.status.value}  chars={chars}")
    finally:
        await _data.close()
        await _error.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m bili_unit.processing",
        description="Bilibili user data processing CLI.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "process",
        help="Run processing for a uid (default: all registered transform handlers)",
    )
    p.add_argument("uid", type=int)
    p.add_argument(
        "--mode", "-m", choices=["incremental", "full"], default="incremental",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--item-types", "-t", nargs="+", default=None, metavar="TYPE",
        help="Subset of item_types to run (debug; mutually exclusive with -x).",
    )
    g.add_argument(
        "--exclude-item-types", "-x", nargs="+", default=None, metavar="TYPE",
        help="item_types to skip; everything else runs.",
    )

    pq = sub.add_parser("query", help="Show processing status for a uid")
    pq.add_argument("uid", type=int)

    sub.add_parser("list-uids", help="List all uids with a processing task")

    pv = sub.add_parser("video-full", help="Show full result for a bvid")
    pv.add_argument("uid", type=int)
    pv.add_argument("bvid")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.command == "process":
        item_types = args.item_types
        if item_types is None and args.exclude_item_types is not None:
            from .transform import HANDLERS
            all_types = HANDLERS.names()
            excluded = set(args.exclude_item_types)
            unknown = [n for n in args.exclude_item_types if n not in set(all_types)]
            if unknown:
                parser.error(
                    f"unknown item_type(s) in --exclude-item-types: {', '.join(unknown)}",
                )
            item_types = [n for n in all_types if n not in excluded]
            if not item_types:
                parser.error("--exclude-item-types removed every handler; nothing to run")
        asyncio.run(_run_process(args.uid, args.mode, item_types))
    elif args.command == "query":
        asyncio.run(_run_query(args.uid))
    elif args.command == "list-uids":
        asyncio.run(_run_list_uids())
    elif args.command == "video-full":
        asyncio.run(_run_video_full(args.uid, args.bvid))


if __name__ == "__main__":
    main()


__all__ = [
    "ProcessingItemStatus",  # re-export for CLI users that import this module
]
