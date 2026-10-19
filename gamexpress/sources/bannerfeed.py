"""Community banner feed — 5★ rate-up lineups from ertezy.github.io/Kitsudock-data/hub.json.

Rebuilt hourly by GitHub Actions from the fandom wikis (CC BY-SA 3.0), no key required,
one small JSON file for all games (genshin, hsr, zzz, wuthering + endfield).

Rules:
  * banner lineups sit at PRIORITY['bannerfeed'] = 5, the lowest priority in the monitor.
  * fills ONLY empty banner phases (phase1, phase2). It never overwrites official data or overrides.
  * 4★ rate-ups and re-runs are not present in the feed and remain TBA.
  * Stale payloads (>14 days) are refused.
  * BANNER_FEED=0 switches the fill-in off completely.
"""

from __future__ import annotations

import logging
from typing import Any

from ..http import Fetcher

log = logging.getLogger("gamexpress.sources.bannerfeed")

HUB_URL = "https://ertezy.github.io/Kitsudock-data/hub.json"
MAX_AGE_SECONDS = 14 * 86400  # 14 days

GAME_MAP: dict[str, str] = {
    "genshin": "genshin",
    "starrail": "hsr",
    "zzz": "zzz",
    "wuwa": "wuthering",
}


def parse_hub(data: dict[str, Any], now: int) -> dict[str, list[dict]]:
    """Parse hub.json into {game_key: [banner, ...]}. Drop stale payloads (>14 days)."""
    if not isinstance(data, dict):
        return {}
    updated_at = data.get("updatedAt")
    if updated_at and (now - int(updated_at) > MAX_AGE_SECONDS):
        log.warning("banner feed is stale (updatedAt: %s, now: %s) — ignored", updated_at, now)
        return {}
    rev_map = {v: k for k, v in GAME_MAP.items()}
    out: dict[str, list[dict]] = {}
    for b in data.get("banners") or []:
        if not isinstance(b, dict):
            continue
        gid = b.get("gameId")
        if gid not in rev_map:
            continue
        gk = rev_map[gid]
        rarity = b.get("rarity")
        if rarity is not None and rarity < 5:
            continue
        starts_at = b.get("startsAt")
        if not starts_at:
            continue
        featured = [n for n in (b.get("featured") or []) if n and isinstance(n, str)]
        if not featured:
            continue
        out.setdefault(gk, []).append({
            "title": str(b.get("title") or ""),
            "featured": featured,
            "rarity": rarity,
            "startsAt": int(starts_at),
            "endsAt": int(b.get("endsAt") or 0),
            "url": str(b.get("url") or ""),
            "image": b.get("image"),
        })
    return out


def banner_feed_for(banners: list[dict], release_ts: int | None) -> dict[str, list[str]]:
    """Phase-split banner lineups for one version around its release / maintenance timestamp.

    Phase 1: banners starting at release_ts (± 3 days).
    Phase 2: banners starting 1 to 5 weeks after release_ts (+7d to +35d).
    """
    if not banners or not release_ts:
        return {}
    p1: list[str] = []
    p2: list[str] = []
    for b in banners:
        starts = b.get("startsAt", 0)
        featured = b.get("featured", [])
        if abs(starts - release_ts) <= 3 * 86400:
            for name in featured:
                if name not in p1:
                    p1.append(name)
        elif release_ts + 7 * 86400 < starts <= release_ts + 35 * 86400:
            for name in featured:
                if name not in p2:
                    p2.append(name)
    res: dict[str, list[str]] = {}
    if p1:
        res["phase1"] = p1
    if p2:
        res["phase2"] = p2
    # Every banner NAME this game is running. The hub is the only source that tells a banner's
    # title apart from the character featured on it, so callers use this as a reject-list.
    titles = [str(b.get("title")) for b in banners if b.get("title")]
    if titles:
        res["titles"] = list(dict.fromkeys(titles))
    return res


async def fetch_banners(fetcher: Fetcher, now: int) -> dict[str, list[dict]]:
    """Fetch and parse hub.json. Returns {game_key: [banners...]} or {} on error."""
    if not fetcher:
        return {}
    try:
        data = await fetcher.get_json(HUB_URL, source="bannerfeed", retries=1)
        if isinstance(data, dict):
            return parse_hub(data, now)
    except Exception as e:
        log.warning("banner feed request failed: %s", e)
    return {}
