"""HoYoLAB official news — official API first, c3kay JSON-Feed mirror as fallback.

Verified live 2026-09-25:
  getNewsList?gids=6&page_size=12&type=1  -> "Version 4.6 Update and Maintenance Notice"
  getPostFull?gids=6&post_id=46814308     -> content == "en-us" (quirk) -> body in structured_content
  getPostFull?gids=2&post_id=46604275     -> "Genshin Impact Version 7.1 Special Program Preview"
  https://feeds.c3kay.de/starrail.json    -> JSON Feed 1.1 with full content_html
Game ids (gids): 1 HI3 · 2 Genshin · 6 Star Rail · 8 ZZZ · 9 Nexus Anima.
News types: 1 Notices · 2 Events · 3 Info.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable

from ..config import Game
from ..http import Fetcher
from ..models import Item
from ..schedule import NOT_PROGRAM_TITLE
from ..textutil import find_version, html_to_text, structured_to_text, youtube_video_url
from ..timeparse import parse_iso

log = logging.getLogger("gamexpress.hoyolab")

API = "https://bbs-api-os.hoyolab.com/community/post/wapi/"
HEADERS = {"Origin": "https://www.hoyolab.com", "Referer": "https://www.hoyolab.com/",
           "x-rpc-language": "en-us", "Accept": "application/json"}
ARTICLE_URL = "https://www.hoyolab.com/article/{}"
NEWS_TYPES = (1, 3, 2)


def _post_text(post: dict) -> tuple[str, list[str], list[str]]:
    content = (post.get("content") or "").strip()
    if re.fullmatch(r"[a-z]{2}-[a-z]{2}", content) or not content:
        return structured_to_text(post.get("structured_content") or "[]")
    if "<" in content:
        return html_to_text(content)
    return content, [], []


def _images(wrapper: dict) -> list[str]:
    """Post pictures, dropping a smaller upload when two same-aspect-ratio entries are the same art."""
    out: list[str] = []
    dims: dict[str, tuple[int, int]] = {}
    for group in ("cover_list", "image_list"):
        for c in wrapper.get(group) or []:
            u = c.get("url")
            if not u or u.split("?")[0] in {o.split("?")[0] for o in out}:
                continue
            w, h = int(c.get("width") or 0), int(c.get("height") or 0)
            if w and h:
                dims[u] = (w, h)
            out.append(u)

    def ratio(u: str) -> float:
        w, h = dims[u]
        return round(w / h, 3)

    drop = set()
    for a in out:
        for b in out:
            if a == b or a not in dims or b not in dims or a in drop or b in drop:
                continue
            wa, ha = dims[a]
            wb, hb = dims[b]
            if ratio(a) == ratio(b) and wb >= wa and hb >= ha and (wb, hb) != (wa, ha):
                drop.add(a)
    return [u for u in out if u not in drop]


async def _full_post(fetcher: Fetcher, gid: int, post_id: str) -> dict | None:
    data = await fetcher.get_json(API + "getPostFull", source="hoyolab",
                                  headers=HEADERS, params={"gids": gid, "post_id": post_id, "read": 1})
    if not isinstance(data, dict) or data.get("retcode") != 0:
        return None
    inner = data.get("data")
    post_obj = inner.get("post") if isinstance(inner, dict) else None
    return post_obj if isinstance(post_obj, dict) else None


async def official_items(fetcher: Fetcher, game: Game, want: Callable[[str], bool],
                         since_ts: int) -> list[Item] | None:
    """None = the API is unreachable (caller falls back to c3kay). The news lists and the
    matching full posts are fetched concurrently."""
    gid = game.hoyolab_gid
    if not gid:
        return []
    lists = await asyncio.gather(*(
        fetcher.get_json(API + "getNewsList", source="hoyolab", headers=HEADERS,
                         params={"gids": gid, "page_size": 15, "type": news_type})
        for news_type in NEWS_TYPES))
    any_ok = False
    picked: dict[str, tuple[dict, dict, int, str]] = {}
    for data in lists:
        if not isinstance(data, dict) or data.get("retcode") != 0:
            continue
        any_ok = True
        inner = data.get("data")
        for wrapper in (inner.get("list") if isinstance(inner, dict) else None) or []:
            if not isinstance(wrapper, dict):
                continue
            post = wrapper.get("post")
            if not isinstance(post, dict):
                continue
            pid = str(post.get("post_id") or "")
            created = int(post.get("created_at") or 0)
            subject = (post.get("subject") or "").strip()
            if not pid or pid in picked or created < since_ts:
                continue
            preview = f"{subject}\n{post.get('content') or ''}\n{post.get('desc') or ''}"
            if want(preview):
                picked[pid] = (wrapper, post, created, subject)
    if not any_ok:
        return None
    fulls = await asyncio.gather(*(_full_post(fetcher, gid, pid) for pid in picked))
    items: list[Item] = []
    for (pid, (wrapper, post, created, subject)), full in zip(picked.items(), fulls):
        if full:
            full_post = full.get("post") if isinstance(full.get("post"), dict) else {}
            text, links, imgs = _post_text(full_post)
            images = _images(full) or imgs or _images(wrapper)
        else:  # list preview is truncated but usually holds the key sentences
            text, links, imgs = (post.get("content") or post.get("desc") or ""), [], []
            images = _images(wrapper)
        items.append(Item(source="hoyolab", game=game.key, id=pid, url=ARTICLE_URL.format(pid),
                          title=subject, text=text, published_ts=created, images=images,
                          links=links))
    return items


async def c3kay_items(fetcher: Fetcher, game: Game, want: Callable[[str], bool],
                      since_ts: int) -> list[Item]:
    if not game.c3kay_feed:
        return []
    feed = await fetcher.get_json(game.c3kay_feed, source="c3kay")
    if not isinstance(feed, dict):
        return []
    out = []
    for it in feed.get("items") or []:
        if not isinstance(it, dict):
            continue
        ts = parse_iso(it.get("date_published") or "") or 0
        if ts < since_ts:
            continue
        title = it.get("title") or ""
        text, links, imgs = html_to_text(it.get("content_html") or "")
        if not want(f"{title}\n{text}"):
            continue
        images = [it["image"]] if it.get("image") else []
        images += [i for i in imgs if i not in images]
        out.append(Item(source="hoyolab", game=game.key, id=str(it.get("id")), url=it.get("url") or "",
                        title=title, text=text, published_ts=ts, images=images, links=links))
    return out


async def fetch_items(fetcher: Fetcher, game: Game, want: Callable[[str], bool],
                      since_ts: int) -> list[Item]:
    items = await official_items(fetcher, game, want, since_ts)
    if items is None:
        log.warning("[%s] HoYoLAB API unreachable — using the c3kay mirror", game.key)
        items = await c3kay_items(fetcher, game, want, since_ts)
    return items


# --------------------------------------------------------------------------- program lookup
PROGRAM_TITLE = re.compile(r"special\s+(?:program|broadcast)|livestream\s+preview", re.I)


def pick_program(lists: list[dict], version: str | None, patterns: list[str]) -> tuple[str, dict, dict] | None:
    """The version's program article out of raw getNewsList pages -> (post_id, wrapper, post).

    Deliberately ignores `created_at`: the whole point is to reach an announcement that has
    already fallen out of the run's lookback window (HSR 4.6: the Special Program preview was
    11 days older than the maintenance notice that the card had linked to)."""
    pats = [p.lower() for p in patterns]
    best = None
    for data in lists:
        if not isinstance(data, dict) or data.get("retcode") != 0:
            continue
        for wrapper in (data.get("data") or {}).get("list") or []:
            post = wrapper.get("post") or {}
            subject = (post.get("subject") or "").strip()
            if not post.get("post_id") or not subject:
                continue
            lead = subject.lower()
            if not (PROGRAM_TITLE.search(subject) or any(p in lead for p in pats)):
                continue
            if NOT_PROGRAM_TITLE.search(subject):
                continue
            if version and find_version(subject) not in (version, None):
                continue
            created = int(post.get("created_at") or 0)
            if best is None or created > int(best[1].get("post", {}).get("created_at") or 0):
                best = (str(post["post_id"]), wrapper, post)
    return best


async def find_program(fetcher: Fetcher, game: Game, version: str | None, pages: int = 3) -> dict | None:
    """-> {'url','title','images','ts','source'} for the version's Special Program article, or
    None. Pages the official news list back until it finds one (page_size 20 x `pages`)."""
    gid = game.hoyolab_gid
    if not gid:
        return None
    last_id, lists = "", []
    for _ in range(max(1, pages)):
        params = {"gids": gid, "page_size": 20, "type": 1}
        if last_id:
            params["last_id"] = last_id
        data = await fetcher.get_json(API + "getNewsList", source="hoyolab", headers=HEADERS, params=params)
        if not data or data.get("retcode") != 0:
            break
        rows = (data.get("data") or {}).get("list") or []
        lists.append(data)
        if not rows:
            break
        last_id = str((rows[-1].get("post") or {}).get("post_id") or "")
        if not last_id:
            break
    hit = pick_program(lists, version, game.program_patterns)
    if not hit:
        return None
    pid, wrapper, post = hit
    full = await _full_post(fetcher, gid, pid)
    images = _images(wrapper)
    text, youtube = (post.get("content") or post.get("desc") or ""), None
    if full:
        text, links, imgs = _post_text(full.get("post") or {})
        images = _images(full) or imgs or images
        youtube = youtube_video_url(links, text)
    log.info("[%s] HoYoLAB program article: %s", game.key, post.get("subject"))
    return {"url": ARTICLE_URL.format(pid), "title": (post.get("subject") or "").strip(),
            "images": images, "text": text, "youtube": youtube,
            "ts": int(post.get("created_at") or 0), "source": "HoYoLAB"}


async def discover_news(fetcher: Fetcher, game: Game, since: int) -> list[dict] | None:
    """Official news lists only, never keyword/community search. Partial failure is not a baseline.

    Three pages per category bound traffic; a saturated last page raises a coverage warning
    rather than pretending all eligible posts were discovered.
    """
    if not game.hoyolab_gid:
        return None
    picked = {}
    for category in NEWS_TYPES:
        last = ""
        for page in range(3):
            params = {"gids": game.hoyolab_gid, "type": category, "page_size": 20}
            if last:
                params["last_id"] = last
            data = await fetcher.get_json(API + "getNewsList", source="news-hoyolab", headers=HEADERS,
                                          params=params, retries=1)
            if not isinstance(data, dict) or data.get("retcode") != 0:
                return None
            payload = data.get("data")
            rows = payload.get("list") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                return None
            for wrapper in rows:
                post = wrapper.get("post") if isinstance(wrapper, dict) else None
                if not isinstance(post, dict):
                    return None
                pid, created = str(post.get("post_id") or ""), post.get("created_at")
                gid = post.get("game_id")
                if gid is not None and str(gid) != str(game.hoyolab_gid):
                    continue
                if not pid.isdigit() or not str(created).isdigit():
                    return None
                if int(created) >= since:
                    picked[pid] = {"id": pid, "ts": int(created)}
            # Lists are newest-first (pinned entries can lead). Use the trailing row,
            # not the minimum timestamp, to decide the requested window is covered.
            tail = rows[-1].get("post") if rows and isinstance(rows[-1], dict) else None
            boundary = tail.get("created_at") if isinstance(tail, dict) else None
            if not rows or len(rows) < 20 or (str(boundary).isdigit() and int(boundary) < since):
                break
            cursor = str((rows[-1].get("post") or {}).get("post_id") or "")
            if not cursor or cursor == last:
                return None
            last = cursor
            if page == 2:
                log.warning("[%s] official news discovery reached pagination bound; retry without advancing baseline", game.key)
                return None
    return sorted(picked.values(), key=lambda row: (row["ts"], row["id"]))


async def news_article(fetcher: Fetcher, game: Game, row: dict) -> Item | None:
    from ..textutil import news_blocks

    full = await _full_post(fetcher, game.hoyolab_gid, row["id"])
    if not isinstance(full, dict) or not isinstance(full.get("post"), dict):
        return None
    post = full["post"]
    if (str(post.get("post_id")) != row["id"] or str(post.get("game_id")) != str(game.hoyolab_gid)
            or not str(post.get("subject") or "").strip()):
        return None
    published = post.get("created_at")
    if not str(published).isdigit() or int(published) != row["ts"]:
        return None
    video_urls, videos = [], {}
    # Native VOD metadata is outside structured_content; resolve its IDs in-place.
    # Highest supplied resolution is selected, never an arbitrary 720p ceiling.
    for field in ("vod_list", "video"):
        vods = full.get(field) or []
        if isinstance(vods, dict):
            vods = [vods]
        for vod in vods:
            resolutions = vod.get("resolutions") or vod.get("resolution") or []
            candidates = [r for r in resolutions if isinstance(r, dict) and r.get("url")]
            if not candidates:
                video_urls.append(None)
                continue
            best = max(candidates, key=lambda r: (int(r.get("height") or 0), int(r.get("bitrate") or 0)))
            url = best["url"]
            video_urls.append(url)
            for key in ("id", "video_id", "vod_id"):
                if vod.get(key) is not None:
                    videos[str(vod[key])] = url
    content = post.get("content") or ""
    if post.get("view_type") == 5 and content.lstrip().startswith("{"):
        content = json.loads(content).get("describe") or ""
    blocks = news_blocks(content, post.get("structured_content") or "", videos)
    seen_videos = {b.get("url") for b in blocks if b["kind"] == "video"}
    for url in video_urls:
        if not url:
            blocks.append({"kind": "text", "text": "\n[Native video unavailable — open the original article.]\n"})
            continue
        if url not in seen_videos:
            blocks.append({"kind": "video", "url": url})
            seen_videos.add(url)
    # Full-response image_list supplements body media; covers are only a last resort.
    seen = {b.get("url") for b in blocks}
    for image in full.get("image_list") or []:
        url = image.get("url") if isinstance(image, dict) else None
        if url and url not in seen:
            blocks.append({"kind": "image", "url": url})
            seen.add(url)
    if not blocks:
        return None  # never publish a truncated list preview as a full article
    user = full.get("user") or {}
    return Item(source="hoyolab", game=game.key, id=row["id"], url=ARTICLE_URL.format(row["id"]),
                title=post["subject"], text="", published_ts=row["ts"], article_blocks=blocks,
                author=str(user.get("nickname") or game.name),
                images=[entry["url"] for entry in (full.get("cover_list") or [])
                        if isinstance(entry, dict) and entry.get("url")][:1])
