"""HoYoPlay launcher API — the exact live version + pre-install availability.

getGameBranches (Sophon) is the current source of truth; the legacy
getGamePackages list is stale for games that moved to Sophon (verified: it
still reports GI 5.5.0 while the live game is 7.1). Same endpoint seria's
hoyo-update-notifier uses. Verified live 2026-09-25 (ZZZ main tag 3.2.0).
"""

from __future__ import annotations

from ..http import Fetcher
from ..textutil import short_version

BRANCHES_URL = "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGameBranches?launcher_id=VYTpXlbWo8"


def parse_branches(data: dict) -> dict[str, dict]:
    """{game_id: {'live': '7.1', 'pre': '7.2' | None}}"""
    out: dict[str, dict] = {}
    for b in ((data or {}).get("data") or {}).get("game_branches") or []:
        gid = (b.get("game") or {}).get("id")
        if not gid:
            continue
        main = b.get("main") or {}
        pre = b.get("pre_download") or None
        out[gid] = {"live": short_version(main.get("tag")),
                    "pre": short_version(pre.get("tag")) if isinstance(pre, dict) else None}
    return out


async def hoyoplay_versions(fetcher: Fetcher) -> dict[str, dict]:
    data = await fetcher.get_json(BRANCHES_URL, source="hoyoplay", retries=1)
    return parse_branches(data) if data else {}
