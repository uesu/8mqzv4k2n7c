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
BASIC_INFO_URL = (
    "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getAllGameBasicInfo"
    "?launcher_id=VYTpXlbWo8&language=en-us"
)


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


def parse_basic_info(data: dict) -> dict[str, list[dict]]:
    """Structured HoYoPlay game backgrounds keyed by both `game.id` and `game.biz`.

    Each entry contains raw URLs from `backgrounds[]`: `video`, `background` (WebP/image),
    `theme`, and associated structured `links` without modifying query strings.
    """
    out: dict[str, list[dict]] = {}
    for info in ((data or {}).get("data") or {}).get("game_info_list") or []:
        if not isinstance(info, dict):
            continue
        game = info.get("game") or {}
        keys = [str(v) for v in (game.get("id"), game.get("biz")) if v]
        if not keys:
            continue
        entries: list[dict] = []
        for bg in info.get("backgrounds") or []:
            if not isinstance(bg, dict):
                continue
            video_obj = bg.get("video") if isinstance(bg.get("video"), dict) else {}
            bg_obj = bg.get("background") if isinstance(bg.get("background"), dict) else {}
            video = str(video_obj.get("url") or "").strip()
            image = str(bg_obj.get("url") or "").strip()
            theme = str((bg.get("theme") or {}).get("url") or "").strip()
            links = [
                str(u).strip()
                for u in (
                    bg_obj.get("link"),
                    (bg.get("icon") or {}).get("link"),
                )
                if u
            ]
            if video or image or theme:
                entries.append({
                    "id": str(bg.get("id") or ""),
                    "type": str(bg.get("type") or ""),
                    "video": video,
                    "video_md5": str(video_obj.get("md5") or video_obj.get("hash") or "").strip(),
                    "background": image,
                    "background_md5": str(bg_obj.get("md5") or bg_obj.get("hash") or "").strip(),
                    "theme": theme,
                    "links": links,
                })
        for key in keys:
            out[key] = entries
    return out


async def hoyoplay_versions(fetcher: Fetcher) -> dict[str, dict]:
    data = await fetcher.get_json(BRANCHES_URL, source="hoyoplay", retries=1)
    return parse_branches(data) if data else {}


async def hoyoplay_basic_info(fetcher: Fetcher) -> dict[str, list[dict]]:
    data = await fetcher.get_json(BASIC_INFO_URL, source="hoyoplay-media", retries=1)
    return parse_basic_info(data) if data else {}
