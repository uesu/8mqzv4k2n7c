"""Banner announcements found by TITLE, for any version -- past, present or the next one.

Why this exists: the banner block used to read only the notices inside the 72 h lookback window.
A Phase I notice that was posted before the card existed never reached the card. HSR 4.6's
"Version 4.6 Event Warp: Phase I" (posted 2026-09-27) stayed a community guess for that reason.

How it works, per card version (`schedule.gather_banner_search` decides WHEN, throttled):
  * config/games.json `banner_titles` holds the title template of each game, e.g.
    "Version {v} Event Wishes Notice - Phase {p}". {v} is the card version, {p} is I or II.
    Changing the wording there is the only change needed for a new title style.
  * HoYoLAB games (Genshin, Star Rail, ZZZ): the HoYoLAB search endpoint, one request per phase
    keyword. Only posts by the game's OFFICIAL account (`official_uid`) count: fans repost the
    same titles. If search is unavailable, the official news list is paged back instead (the same
    list the Special Program lookup pages).
  * Wuthering Waves: the Kuro article menu (one request, the full archive), read in full. If Kuro is
    down, the Atom mirror listed in `program_feeds` is read instead.

Matching is strict. Case, dashes, spaces, brackets and colons are ignored, but the version must
match exactly (3.2 is not 3.21, and V3.2 and Version 3.2 both count), and the phase numeral must
match exactly (I is never II). The newest matching post wins per phase.

-> list of schedule-ready Item objects. Each one still goes through extract_banner() and the
   normal merge, so a title match alone never writes a name: the names must be read from the body.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import unescape

from ..config import Game
from ..http import Fetcher
from ..models import Item
from ..textutil import html_to_text
from ..timeparse import parse_kuro_time
from . import hoyolab, kuro
from .newspage import parse_feed_entries

log = logging.getLogger("gamexpress.sources.banner_search")

SEARCH_API = "https://bbs-api-os.hoyolab.com/community/search/wapi/search/post"
PHASE_NUMERALS = {1: "I", 2: "II"}
_ROMAN = {"i": 1, "ii": 2}
# punctuation that the title canon ignores: "Event Wishes Notice - Phase I" == "...Notice-Phase I"
_CANON_DROP = re.compile(r"[\s\-:()\[\]/_·•|\u2010-\u2015\u2212]+")
_TAGS = re.compile(r"<[^>]+>")


def canon(text: str) -> str:
    """Lower-case title with every space, dash, colon, bracket and slash removed."""
    return _CANON_DROP.sub("", _TAGS.sub("", unescape(text or "")).lower())


def title_rx(template: str, version: str) -> re.Pattern:
    """The regex that a canonical post title must fully match for (template, version)."""
    parts = []
    for tok in re.split(r"(\{v\}|\{p\})", canon(template)):
        if tok == "{v}":
            parts.append(r"(?:v|version)?" + re.escape(canon(version)))
        elif tok == "{p}":
            parts.append(r"(?P<p>ii|i)")
        else:
            parts.append(re.escape(tok))
    return re.compile("".join(parts))


def phase_of(subject: str, template: str, version: str) -> int | None:
    """1 or 2 when `subject` is the template's title for that version, else None.

    A template WITHOUT {p} is a Phase I title written with no phase suffix (ZZZ 3.1 Phase I was
    posted as just \"V3.1 Limited-Time Channels\"). It matches only the bare title, never a
    \"(Phase II)\" post, because the match is a full match on the canonical title."""
    m = title_rx(template, version).fullmatch(canon(subject))
    if not m:
        return None
    if "{p}" not in template:
        return 1
    return _ROMAN.get(m.group("p"))


def phases_for(template: str) -> tuple[int, ...]:
    """The phases a template can identify: both when it has {p}, Phase I alone when it does not."""
    return (1, 2) if "{p}" in template else (1,)


def keyword_for(template: str, version: str, phase: int) -> str:
    """The HoYoLAB search keyword: the title with the version and phase filled in."""
    return template.replace("{v}", version).replace("{p}", PHASE_NUMERALS[phase])


def _pick(rows: list[tuple[int, dict, dict]]) -> dict[int, tuple[int, dict, dict]]:
    """Newest post per phase: {phase: (created_at, wrapper_post, user)}."""
    best: dict[int, tuple[int, dict, dict]] = {}
    for phase, post, user in rows:
        created = int(post.get("created_at") or 0)
        if phase not in best or created > best[phase][0]:
            best[phase] = (created, post, user)
    return best


async def _search_rows(fetcher: Fetcher, game: Game, template: str, version: str) -> list[tuple[int, dict, dict]] | None:
    """(phase, post, user) for every official search hit. None = the search endpoint failed."""
    rows: list[tuple[int, dict, dict]] = []
    answered = False
    for phase in phases_for(template):
        data = await fetcher.get_json(SEARCH_API, source="hoyolab", headers=hoyolab.HEADERS,
                                      params={"keyword": keyword_for(template, version, phase),
                                              "gids": game.hoyolab_gid, "page": 1, "size": 10})
        if not isinstance(data, dict) or data.get("retcode") != 0:
            continue
        answered = True
        for hit in (data.get("data") or {}).get("list") or []:
            post, user = hit.get("post") or {}, hit.get("user") or {}
            p = phase_of(post.get("subject") or "", template, version)
            if p and str(user.get("uid") or post.get("uid") or "") == str(game.official_uid):
                rows.append((p, post, user))
    return rows if answered else None


async def _newslist_rows(fetcher: Fetcher, game: Game, template: str, version: str) -> list[tuple[int, dict, dict]]:
    """Fallback when search is down: the official news list, paged back (types 1 and 2)."""
    rows: list[tuple[int, dict, dict]] = []
    for news_type in (1, 2):
        last_id = ""
        for _ in range(3):
            params = {"gids": game.hoyolab_gid, "page_size": 20, "type": news_type}
            if last_id:
                params["last_id"] = last_id
            data = await fetcher.get_json(hoyolab.API + "getNewsList", source="hoyolab",
                                          headers=hoyolab.HEADERS, params=params)
            if not isinstance(data, dict) or data.get("retcode") != 0:
                break
            wrappers = (data.get("data") or {}).get("list") or []
            if not wrappers:
                break
            for w in wrappers:
                post = w.get("post") or {}
                p = phase_of(post.get("subject") or "", template, version)
                if p and str(post.get("uid") or "") == str(game.official_uid):
                    rows.append((p, post, w.get("user") or {}))
            last_id = str((wrappers[-1].get("post") or {}).get("post_id") or "")
            if not last_id:
                break
    return rows


async def _hoyolab_items(fetcher: Fetcher, game: Game, version: str) -> list[Item]:
    out: list[Item] = []
    if not game.hoyolab_gid or game.official_uid is None:
        return out
    rows: list[tuple[int, dict, dict]] = []
    for template in game.banner_titles:                  # every template feeds ONE newest-per-phase pick
        found = await _search_rows(fetcher, game, template, version)
        if found is None:
            log.warning("[%s] HoYoLAB search unavailable — paging the official news list", game.key)
            found = await _newslist_rows(fetcher, game, template, version)
        rows.extend(found)
    best = _pick(rows)
    for _phase, (created, post, _user) in sorted(best.items()):
        pid = str(post.get("post_id") or "")
        if not pid:
            continue
        subject = _TAGS.sub("", unescape(post.get("subject") or "")).strip()
        full = await hoyolab._full_post(fetcher, game.hoyolab_gid, pid)
        if full:
            text, links, _imgs = hoyolab._post_text(full.get("post") or {})
            images = hoyolab._images(full)
        else:                                   # list preview only: the names may be cut short
            text, links, images = (post.get("content") or post.get("desc") or ""), [], []
        out.append(Item(source="hoyolab", game=game.key, id=pid, url=hoyolab.ARTICLE_URL.format(pid),
                        title=subject, text=text, published_ts=created, images=images, links=links))
    return out


async def _kuro_items(fetcher: Fetcher, game: Game, version: str) -> list[Item]:
    articles = await kuro.menu_articles(fetcher)
    rows = []
    for aid, a in articles.items():
        title = (a.get("articleTitle") or "").strip()
        for template in game.banner_titles:
            p = phase_of(title, template, version)
            if p:
                ts = parse_kuro_time(a.get("createTime") or "") or 0
                rows.append((p, aid, a, ts, title))
    best: dict[int, tuple] = {}
    for row in rows:
        if row[0] not in best or row[3] > best[row[0]][3]:
            best[row[0]] = row
    out: list[Item] = []
    for _phase, (_p, aid, a, ts, title) in sorted(best.items()):
        detail = await fetcher.get_json(kuro.DETAIL.format(aid), source="kuro", retries=1)
        body = (detail or {}).get("articleContent") or a.get("articleContent") or ""
        text, links, imgs = html_to_text(body)
        cover = a.get("suggestCover") or ""
        out.append(Item(source="kuro", game=game.key, id=str(aid), url=kuro.LINK.format(aid), title=title,
                        text=text, published_ts=ts, images=([cover] if cover else []) + imgs, links=links))
    return out


async def _feed_items(fetcher: Fetcher, game: Game, version: str) -> list[Item]:
    """Kuro unreachable: the Atom mirror of the same article list (GitHub, see games.json)."""
    out: list[Item] = []
    for url in game.program_feeds:
        body = await fetcher.get_text(url, source="newspage", retries=1)
        if not body:
            continue
        entries = await asyncio.to_thread(parse_feed_entries, body)
        best: dict[int, dict] = {}
        for e in entries:
            for template in game.banner_titles:
                p = phase_of(e.get("title") or "", template, version)
                if p and (p not in best or e["ts"] > best[p]["ts"]):
                    best[p] = e
        for _p, e in sorted(best.items()):
            m = re.search(r"/detail/(\d+)", e.get("url") or "")
            out.append(Item(source="kuro", game=game.key, id=m.group(1) if m else e["url"], url=e["url"],
                            title=e["title"], text=e["text"], published_ts=e["ts"], images=e["images"]))
        if out:
            break
    return out


async def find(fetcher: Fetcher, game: Game, version: str) -> list[Item]:
    """Every official banner notice for `version` that the title templates identify.

    Empty when the game has no templates, or no source answered (the caller stamps the
    throttle either way, so an outage never turns into a request per run)."""
    if not game.banner_titles or not version:
        return []
    if game.hoyolab_gid:
        return await _hoyolab_items(fetcher, game, version)
    if game.kuro_news:
        menu_failed = False
        try:
            items = await _kuro_items(fetcher, game, version)
        except Exception as e:                       # noqa: BLE001 — one source, never fatal
            log.warning("[%s] Kuro banner menu failed: %s", game.key, e)
            menu_failed = True
            items = []
        if items:
            return items
        # Not a failure: the Kuro menu simply has no notice for this version yet (it is empty on most
        # runs for a version still in its first days). The mirror is tried either way, and only the
        # menu having been UNREACHABLE with the mirror also empty is worth a warning — a menu that
        # answered with nothing published yet is the pre-notice normal and logs at INFO with that
        # context (WuWa 3.7 warned on every run for a week before this distinction existed).
        log.info("[%s] Kuro banner menu has no notice for %s yet — trying the GitHub mirror", game.key, version)
        items = await _feed_items(fetcher, game, version)
        if not items:
            (log.warning if menu_failed else log.info)(
                "[%s] no banner notice for %s from the Kuro menu or the GitHub mirror%s",
                game.key, version,
                " (menu unreachable)" if menu_failed else " (menu answered — nothing published yet)")
        return items
    return []
