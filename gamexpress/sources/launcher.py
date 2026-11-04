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

# Server maintenance status — the cloud-gaming gateway every HoYoverse client asks before it logs
# in. `data.application_maintenance` is an object while the servers are closed and `null` once
# they reopen; that `object -> null` flip is the moment the version goes live (the launcher's
# `main.tag` flips at maintenance START, so it can never tell when servers reopen). Same endpoint,
# headers and game_biz values as seria's hoyo-update-notifier (`hun/utils.py`) and yeci226/ZZZ
# (`news/sources.ts`), both of which run it in production. Star Rail's global cloud gateway is the
# shared `hkrpg_cn` host in both projects; HoYoverse maintains every region in one window, so the
# flag is still the global answer.
MAINT_STATUS = {
    "gopR6Cufr3": ("https://sg-cg-api.hoyoverse.com/hk4e_global/cg/gamer/api/getGlobalApplicationMaintenance",
                   "hk4e_global"),
    "4ziysqXOQ8": ("https://cg-hkrpg-api.mihoyo.com/hkrpg_cn/cg/gamer/api/getGlobalApplicationMaintenance",
                   "hkrpg_cn"),
    "U5hbdsT9W7": ("https://sg-cg-nap-api.hoyoverse.com/nap_global/cg/gamer/api/getGlobalApplicationMaintenance",
                   "nap_global"),
}


def parse_branches(data: dict) -> dict[str, dict]:
    """{game_id: {'live': '7.1', 'pre': '7.2' | None, 'live_tag': '7.1.0'}}"""
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    branches = inner.get("game_branches") if isinstance(inner, dict) else None
    out: dict[str, dict] = {}
    for b in branches or []:
        if not isinstance(b, dict):
            continue
        game_obj = b.get("game")
        gid = game_obj.get("id") if isinstance(game_obj, dict) else None
        if not gid:
            continue
        main = b.get("main") if isinstance(b.get("main"), dict) else {}
        pre = b.get("pre_download") if isinstance(b.get("pre_download"), dict) else None
        out[gid] = {"live": short_version(main.get("tag")),
                    "pre": short_version(pre.get("tag")) if isinstance(pre, dict) else None,
                    "live_tag": str(main.get("tag") or "").strip()}
    return out


def parse_maintenance_status(data) -> bool | None:
    """True while `application_maintenance` is an object, False once it is `null`, None otherwise.

    Anything that is not the documented shape (non-zero `retcode`, missing key, a list or a string
    where the object should be) is *unknown*, never "servers open": a transient gateway error must
    not be read as the end of maintenance.
    """
    if not isinstance(data, dict):
        return None
    try:
        if int(data.get("retcode", 0) or 0) != 0:
            return None
    except (TypeError, ValueError):
        return None
    inner = data.get("data")
    if not isinstance(inner, dict) or "application_maintenance" not in inner:
        return None
    flag = inner.get("application_maintenance")
    if flag is None:
        return False
    return True if isinstance(flag, dict) else None


def parse_basic_info(data: dict) -> dict[str, list[dict]]:
    """Structured HoYoPlay game backgrounds keyed by both `game.id` and `game.biz`.

    Each entry contains raw URLs from `backgrounds[]`: `video`, `background` (WebP/image),
    `theme`, and associated structured `links` without modifying query strings.
    """
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    info_list = inner.get("game_info_list") if isinstance(inner, dict) else None
    out: dict[str, list[dict]] = {}
    for info in info_list or []:
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


async def hoyoplay_maintenance_status(fetcher: Fetcher, game_id: str, version_tag: str) -> bool | None:
    """One `getGlobalApplicationMaintenance` read for `game_id`; None when unsupported or unknown."""
    entry = MAINT_STATUS.get(str(game_id or ""))
    if not entry:
        return None
    url, biz = entry
    headers = {"x-rpc-app_version": str(version_tag or "").strip() or "1.0.0",
               "x-rpc-client_type": "1", "x-rpc-cg_game_biz": biz}
    data = await fetcher.get_json(url, source="hoyo-maint", headers=headers, retries=1)
    return parse_maintenance_status(data)
