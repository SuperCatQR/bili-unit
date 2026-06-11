# python -m bili_unit — unified CLI for the bili unit.
#
# Usage:
#   python -m bili_unit fetch     <uid> [options]   — run fetching
#   python -m bili_unit process   <uid> [options]   — run processing
#   python -m bili_unit query     <uid>              — query all results
#   python -m bili_unit login                        — QR code login
#   python -m bili_unit list-uids                    — list fetched uids
#
# Internally goes through ``bili_unit.assemble()`` →
# ``BiliCommand`` / ``BiliQuery`` (the unit-level entries). The legacy
# ``python -m bili_unit.fetching`` entry point still works.

from __future__ import annotations

import argparse
import asyncio
import logging

# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

async def _handle_fetch(args: argparse.Namespace) -> None:
    """Run fetching via the unit-level BiliCommand / BiliQuery."""
    from bili_unit import EndpointStatus, assemble

    cmd, qry, data, error = await assemble()
    try:
        result = await cmd.fetch(args.uid, endpoints=args.endpoints, mode=args.mode)
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

    cmd, qry, data, error = await assemble()
    try:
        result = await cmd.process(args.uid, mode=args.mode, item_types=args.item_types)
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
    sub = parser.add_subparsers(dest="command", required=True)

    # --- fetch ---
    p_fetch = sub.add_parser("fetch", help="Run fetching for a uid")
    p_fetch.add_argument("uid", type=int, help="Target Bilibili user uid")
    p_fetch.add_argument(
        "--endpoints", "-e", nargs="+", default=None,
        help="Endpoint names to fetch (default: all registered)",
    )
    p_fetch.add_argument(
        "--mode", "-m",
        choices=["incremental", "refresh", "full"],
        default="incremental",
        help="Fetching mode (default: incremental)",
    )

    # --- process ---
    p_proc = sub.add_parser("process", help="Run processing for a uid")
    p_proc.add_argument("uid", type=int, help="Target Bilibili user uid")
    p_proc.add_argument(
        "--mode", "-m",
        choices=["incremental", "full"],
        default="incremental",
        help="Processing mode (default: incremental)",
    )
    p_proc.add_argument(
        "--item-types", "-t", nargs="+", default=None,
        help="Subset of transform item_types (default: all registered)",
    )

    # --- query ---
    p_query = sub.add_parser("query", help="Query results for a uid")
    p_query.add_argument("uid", type=int, help="Target Bilibili user uid")

    # --- login ---
    sub.add_parser("login", help="QR code login to Bilibili")

    # --- list-uids ---
    sub.add_parser("list-uids", help="List all fetched uids")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    handlers = {
        "fetch": _handle_fetch,
        "process": _handle_process,
        "query": _handle_query,
        "login": _handle_login,
        "list-uids": _handle_list_uids,
    }

    handler = handlers[args.command]
    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
