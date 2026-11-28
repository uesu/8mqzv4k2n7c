"""Silver Palace listing snapshot for the opt-in candidate monitor.

Publication dates are day-only; this reader intentionally returns no timestamp and
cannot be plugged into the timestamp-floor news runner. Two identical walks reduce
some pagination races; they cannot prove the upstream offers a consistent snapshot.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import replace
from datetime import date
from urllib.parse import urlsplit

import aiohttp

from ..models import Item
from ..textutil import news_blocks

BASE = "https://silverpalace.elementagames.com/server/index.php/home/news_list"
DETAIL = "https://silverpalace.elementagames.com/server/index.php/home/news_detail"
ARTICLE = "https://silverpalace.elementagames.com/en-us/newsDetail?id={}"
MAX_PAGES = 30
MAX_ROWS_PER_PAGE = 100
_ID = re.compile(r"[0-9]{1,32}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _valid_date(value):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


async def _walk(fetcher):
    pages, first_size, found = None, None, {}
    number = 1
    while pages is None or number <= pages:
        response = await fetcher.get_json(BASE, source="news-silverpalace", retries=1,
                                          params={"lang": "en-us", "page": number}, allow_redirects=False)
        if (not isinstance(response, dict) or type(response.get("code")) is not int
                or response["code"] != 0 or not isinstance(response.get("data"), dict)):
            return None
        data = response["data"]
        total, current, rows = data.get("total_page"), data.get("current_page"), data.get("list")
        if (type(total) is not int or not 1 <= total <= MAX_PAGES
                or not isinstance(current, str) or current != str(number)
                or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS_PER_PAGE):
            return None
        if pages is None:
            pages, first_size = total, len(rows)
        elif pages != total:
            return None
        if (number < pages and len(rows) != first_size) or len(rows) > first_size:
            return None
        for row in rows:
            if not isinstance(row, dict):
                return None
            cid, title, day = row.get("id"), row.get("title"), row.get("publish_date")
            if (not isinstance(cid, str) or not _ID.fullmatch(cid) or cid in found
                    or not isinstance(title, str) or not title.strip() or not _valid_date(day)):
                return None
            found[cid] = {"id": cid, "title": title.strip(), "date": day}
        number += 1
    return list(found.values())


async def snapshot(fetcher):
    """Return two matching complete scans, or None for any ambiguity or drift.

    The caller must not interpret a missing snapshot as an empty listing or use
    day-only dates as exact instants. No state or delivery is implemented.
    """
    first = await _walk(fetcher)
    if first is None:
        return None
    second = await _walk(fetcher)
    if second is None or first != second:
        return None
    return first


def _official_cover(value):
    """Only accept the exact observed image host; no network or redirects here."""
    if (not isinstance(value, str) or not value
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme == "https" and parts.hostname == "s.elementagames.com"
                and not parts.username and not parts.password and parts.port in (None, 443)
                and bool(parts.path) and not parts.fragment)
    except ValueError:
        return False


async def detail(fetcher, row):
    """Fetch matching publisher detail as ordered blocks, without inventing a time.

    This is exploratory, not an article-fidelity or Discord-readiness guarantee.
    Delivery must separately validate every media URL and its redirect chain.
    """
    if not isinstance(row, dict) or set(row) != {"id", "title", "date"}:
        return None
    cid, title, day = row.get("id"), row.get("title"), row.get("date")
    if (not isinstance(cid, str) or not _ID.fullmatch(cid) or not isinstance(title, str)
            or not title.strip() or len(title) > 512 or not _valid_date(day)):
        return None
    response = await fetcher.get_json(DETAIL, source="news-silverpalace", retries=1,
                                      params={"lang": "en-us", "id": cid}, allow_redirects=False)
    if (not isinstance(response, dict) or type(response.get("code")) is not int
            or response["code"] != 0 or not isinstance(response.get("data"), dict)):
        return None
    data = response["data"]
    detail_title, content, cover = data.get("title"), data.get("content"), data.get("cover")
    if (data.get("id") != cid or data.get("publish_date") != day
            or not isinstance(detail_title, str) or detail_title.strip() != title.strip()
            or not isinstance(content, str) or not content.strip() or len(content) > 1024 * 1024
            or (cover not in (None, "") and not _official_cover(cover))):
        return None
    try:
        blocks = news_blocks(content)
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None
    if not blocks:
        return None
    return {"id": cid, "title": detail_title.strip(), "date": day,
            "url": ARTICLE.format(cid), "cover": cover, "blocks": blocks}


async def without_duplicate_leading_cover(fetcher, item):
    """Drop only a cover byte-identical to the first body image, keeping that body image.

    Some Silver Palace entries publish the same file under different cover and
    article URLs. Compare complete bounded responses from the official image
    host, without redirects. An unavailable comparison preserves both images;
    image/video blocks and distinct covers are never removed speculatively.
    """
    if (item.source != "silverpalace" or item.game != "silverpalace"
            or len(item.images) != 1 or not item.article_blocks
            or item.article_blocks[0].get("kind") != "image"):
        return item
    cover, body = item.images[0], item.article_blocks[0].get("url")
    if cover == body or not _official_cover(cover) or not _official_cover(body):
        # The shared renderer already handles identical URLs; never fetch foreign hosts.
        return item

    async def digest(url):
        async with fetcher.session.get(url, allow_redirects=False) as response:
            if (response.status != 200
                    or not response.headers.get("Content-Type", "").lower().startswith("image/")):
                return None
            length = 0
            digest = hashlib.sha256()
            async for chunk in response.content.iter_chunked(65536):
                length += len(chunk)
                if length > 8 * 1024 * 1024:
                    return None
                digest.update(chunk)
            return (length, digest.digest()) if length else None

    try:
        first, second = await asyncio.wait_for(asyncio.gather(digest(cover), digest(body)), timeout=12)
        if first is not None and first == second:
            return replace(item, images=[])
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, TypeError, AttributeError):
        pass
    # HTTP failures are non-destructive: original media remains available to the renderer.
    return item


async def news_article(fetcher, game, row):
    """Adapt a validated detail without inventing a time or a native cover."""
    data = await detail(fetcher, row)
    if data is None:
        return None
    return Item(source="silverpalace", game=game.key, id=data["id"], url=data["url"], title=data["title"],
                text="", published_ts=0, images=[data["cover"]] if data["cover"] else [],
                article_blocks=data["blocks"], author="Silver Palace")
