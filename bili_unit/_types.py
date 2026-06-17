# bili_unit/_types — cross-stage shared type aliases.
#
# Types that more than one stage needs to refer to live here, away from
# any single stage's __init__, so the stages don't have to import each
# other for types alone.

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bilibili_api import Credential


# Async callable returning a Bilibili Credential (or None when the user
# is browsing anonymously). The default implementation lives in
# bili_unit.fetching.auth.get_credential; advanced callers can pass their own
# provider to assemble()/session() to source credentials elsewhere.
CredentialProvider = Callable[[], Awaitable["Credential | None"]]


__all__ = ["CredentialProvider"]
