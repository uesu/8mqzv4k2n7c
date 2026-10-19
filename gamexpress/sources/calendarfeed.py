"""Official game calendars from api.ennead.cc, read as banner lineups.

The calendar is a reputable source like the hub, the wiki and the official posts. Any two sources that
name the same banner character confirm it (schedule._confirm), so a calendar name fills an empty banner
and locks once one other source agrees -- for every game, for 5★ and 4★ (A-rank in Zenless) alike.

It never writes a time: air times stay with the official posts and the countdown rules.

Phases are split exactly as the hub's are (`bannerfeed.banner_feed_for`), by the card's release
timestamp. The 4★ rule is the wiki's: a phase's 4★ list is written only when it has exactly the game's
rate-up count (`four_star_count`); any other count is left as TBA rather than published half-right.
Light cones, weapons and W-Engines are not banner characters and are never read.
"""
from __future__ import annotations

import logging

from ..http import Fetcher
from .bannerfeed import banner_feed_for

log = logging.getLogger(__name__)

CALENDAR_URL = "https://api.ennead.cc/mihoyo/{slug}/calendar"
# game key -> the calendar's path segment. WuWa has no calendar here (its hub entry is the only one).
SLUGS = {"genshin": "genshin", "starrail": "starrail", "zzz": "zenless"}


def _rarity(value) -> int:
    """Genshin and Star Rail write 5 / 4 (int or text); Zenless Zone Zero writes S (5★) and A (4★)."""
    text = str(value).strip()
    if text in ("5", "S"):
        return 5
    if text in ("4", "A"):
        return 4
    return 0


def parse_banners(data: dict) -> list[dict]:
    """Calendar JSON -> one entry per banner that features a 4★ or 5★ character, in hub shape plus the
    4★ list: {"version", "featured" (5★), "featured4" (4★), "startsAt", "endsAt", "title"}. The title is
    left empty: a calendar banner name is generic ("Character Event Wish"), so it never feeds the hub's
    title reject-list."""
    out: list[dict] = []
    for b in (data or {}).get("banners") or []:
        if not isinstance(b, dict):
            continue
        version = str(b.get("version") or "").strip()
        starts = int(b.get("start_time") or b.get("startsAt") or 0)   # zenless writes start_time too
        if not version or not starts:
            continue
        # Genshin and Star Rail list "characters"; Zenless Zone Zero lists "agents". Weapons, light
        # cones and W-Engines are deliberately not read.
        people = b.get("characters") if b.get("characters") is not None else b.get("agents")
        five: list[str] = []
        four: list[str] = []
        for c in people or []:
            if not isinstance(c, dict):
                continue
            rarity = _rarity(c.get("rarity"))
            name = str(c.get("name") or "").strip()
            bucket = five if rarity == 5 else four if rarity == 4 else None
            if bucket is not None and name and name not in bucket:
                bucket.append(name)
        if not five and not four:
            continue
        out.append({
            "version": version,
            "featured": five,
            "featured4": four,
            "startsAt": starts,
            "endsAt": int(b.get("end_time") or b.get("endsAt") or 0),
            "title": "",
        })
    return out


def lineup(banners: list[dict], release_ts: int | None, four_star_count: int | None) -> dict[str, list[str]]:
    """One version's calendar banners -> {phase1, phase2, phase1_4, phase2_4}. Only the keys with names
    are returned. A 4★ list with any count other than `four_star_count` is dropped (TBA, as the wiki)."""
    if not banners or not release_ts:
        return {}
    five = [{"featured": b.get("featured") or [], "startsAt": b["startsAt"], "endsAt": b.get("endsAt", 0),
             "title": ""} for b in banners if b.get("featured")]
    four = [{"featured": b.get("featured4") or [], "startsAt": b["startsAt"], "endsAt": b.get("endsAt", 0),
             "title": ""} for b in banners if b.get("featured4")]
    out = {k: v for k, v in banner_feed_for(five, release_ts).items() if k in ("phase1", "phase2")}
    four_split = banner_feed_for(four, release_ts)
    for k in ("phase1", "phase2"):
        names = four_split.get(k)
        if names and four_star_count and len(names) == four_star_count:
            out[f"{k}_4"] = names
    return out


async def fetch_banners(fetcher: Fetcher, game_key: str) -> list[dict] | None:
    """The game's calendar banners, or None when the request failed (so the caller keeps what it has)."""
    slug = SLUGS.get(game_key)
    if not fetcher or not slug:
        return None
    try:
        data = await fetcher.get_json(CALENDAR_URL.format(slug=slug), source="ennead", retries=1)
    except Exception as e:                                       # noqa: BLE001 — one source, never fatal
        log.warning("%s calendar request failed: %s", game_key, e)
        return None
    if not isinstance(data, dict):
        return None
    return parse_banners(data)
