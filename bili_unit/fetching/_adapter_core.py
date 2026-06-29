"""Shared bilibili-api adapter helpers."""

from __future__ import annotations

import contextlib
from enum import Enum
from typing import Any

from . import (
    AuthError,
    Http5xxError,
    Http412Error,
    InvalidRequestError,
    RequestError,
    ResourceUnavailableError,
)

_PERMANENT_BUSINESS_CODES: frozenset[int] = frozenset({
    -400, 22115, 22118, 53013, 53016, 88214,
})


@contextlib.asynccontextmanager
async def map_bilibili_errors(
    label: str,
    *,
    passthrough: tuple[type[BaseException], ...] = (),
):
    """Map bilibili-api exceptions onto fetching-layer exceptions.

    The SDK exception classes are imported lazily here so that merely importing
    this module does not load ``bilibili_api`` (F2 IPC §8: main process zero
    SDK import).  They are only needed once a real fetch runs.
    """
    from bilibili_api.exceptions import (
        ApiException,
        ArgsException,
        CredentialNoBiliJctException,
        CredentialNoSessdataException,
        NetworkException,
        ResponseCodeException,
    )

    try:
        yield
    except TimeoutError as exc:
        raise Http5xxError(f"{label}: timeout") from exc
    except ResponseCodeException as exc:
        if exc.code == 412:
            raise Http412Error(f"{label}: 412") from exc
        if exc.code in _PERMANENT_BUSINESS_CODES:
            raise ResourceUnavailableError(
                f"{label}: code={exc.code}: {exc.msg}",
            ) from exc
        raise RequestError(f"{label}: code={exc.code}: {exc.msg}") from exc
    except NetworkException as exc:
        status = getattr(exc, "status", 0) or 0
        if status == 404:
            raise ResourceUnavailableError(
                f"{label}: HTTP 404 (route gone): {exc}",
            ) from exc
        if 400 <= status < 500:
            raise RequestError(f"{label}: HTTP {status}: {exc}") from exc
        raise Http5xxError(f"{label}: network error {exc}") from exc
    except passthrough:
        raise
    except (CredentialNoSessdataException, CredentialNoBiliJctException) as exc:
        raise AuthError(f"{label}: credential missing: {exc}") from exc
    except ArgsException as exc:
        raise InvalidRequestError(f"{label}: invalid SDK arguments: {exc}") from exc
    except ApiException as exc:
        raise RequestError(f"{label}: {exc}") from exc
    except Exception as exc:
        raise RequestError(f"{label}: unexpected: {exc}") from exc


def resolve_dot_path(data: dict[str, Any], path: str) -> Any:
    """Navigate a nested dict using a dot-separated path."""
    current: Any = data
    for seg in path.split("."):
        if not seg:
            continue
        if isinstance(current, dict) and seg in current:
            current = current[seg]
        else:
            return None
    return current


def json_safe(value: Any) -> Any:
    """Convert bilibili-api return objects into JSON-serialisable values."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [json_safe(v) for v in value]
    if hasattr(value, "__dict__"):
        return {
            str(k): json_safe(v)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return str(value)


def extract_list_items(data: dict[str, Any], path: str | None = None) -> list:
    """Best-effort extraction for common B站 paginated list shapes."""
    container: Any = resolve_dot_path(data, path) if path else None
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        for key in ("list", "items", "archives", "data", "media_list"):
            value = container.get(key)
            if isinstance(value, list):
                return value
        collected: list = []
        for value in container.values():
            if isinstance(value, list):
                collected.extend(value)
        return collected

    for key in ("list", "items", "archives", "data", "media_list", "cards"):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested in ("list", "items", "archives", "data", "media_list"):
                nested_value = value.get(nested)
                if isinstance(nested_value, list):
                    return nested_value
    return []


def extract_total_count(data: dict[str, Any]) -> int:
    """Best-effort total-count extraction across known B站 shapes."""
    for path in (
        "page.count",
        "page.total",
        "items_lists.page.total",
        "total",
        "count",
        "total_count",
        "totalSize",
    ):
        value = resolve_dot_path(data, path)
        if isinstance(value, int):
            return value
    return 0


def normalise_api_result(result: Any, key: str = "data") -> dict[str, Any]:
    safe = json_safe(result)
    if isinstance(safe, dict):
        return safe
    return {key: safe}


__all__ = [
    "extract_list_items",
    "extract_total_count",
    "json_safe",
    "map_bilibili_errors",
    "normalise_api_result",
    "resolve_dot_path",
]
