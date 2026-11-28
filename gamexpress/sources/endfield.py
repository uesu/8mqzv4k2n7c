"""Endfield official-news reader for the opt-in candidate monitor.

A bounded, all-or-nothing walk is intentional: sticky articles make date-based early
termination unsafe, and partial pages must never be treated as a complete baseline.
"""
from __future__ import annotations

import re

from ..models import Item
from ..textutil import news_blocks

BASE = "https://web-news.gryphline.com/api/bulletin"
ARTICLE = "https://endfield.gryphline.com/en-us/news/{}"
PARAMS = {"lang": "en-us", "code": "arknights_endfield_official"}
PAGE_SIZE = 20
MAX_PAGES = 20
_ID = re.compile(r"[0-9]{1,32}\Z")


def _envelope(value):
    if (not isinstance(value, dict) or type(value.get("code")) is not int or value["code"] != 0
            or not isinstance(value.get("data"), dict)):
        return None
    return value["data"]


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


async def discover_news(fetcher, game, since: int) -> list[dict] | None:
    """Read every advertised page, or return None on ambiguity/incomplete coverage."""
    picked = {}
    total = None
    pages = 1
    page = 1
    while page <= pages:
        params = {**PARAMS, "page": page, "pageSize": PAGE_SIZE}
        response = await fetcher.get_json(BASE, source="news-endfield", params=params, retries=1, allow_redirects=False)
        data = _envelope(response)
        if data is None:
            return None
        count, current, size, rows = (data.get(k) for k in ("total", "current", "pageSize", "list"))
        if (not _integer(count) or current != page or type(current) is not int
                or type(size) is not int or size != PAGE_SIZE or not isinstance(rows, list)):
            return None
        if total is None:
            total = count
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            if pages > MAX_PAGES:
                return None
        elif count != total:
            return None
        if len(rows) != min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE)):
            return None
        for row in rows:
            if not isinstance(row, dict):
                return None
            cid, title, timestamp = row.get("cid"), row.get("title"), row.get("displayTime")
            if (not isinstance(cid, str) or not _ID.fullmatch(cid) or cid in picked
                    or not isinstance(title, str) or not title.strip() or not _integer(timestamp, 1)):
                return None
            picked[cid] = {"id": cid, "ts": timestamp, "title": title.strip()}
        page += 1
    return sorted((row for row in picked.values() if row["ts"] >= since),
                  key=lambda row: (row["ts"], row["id"]))


def _escape_unmatched_brackets(value: str) -> str:
    """Treat unmatched publisher brackets as literal text, preserving real links.

    Apply only to Endfield text blocks, after HTML has been converted to Discord
    Markdown. Do not invent or delete source characters; an escape is invisible
    when Discord displays it. Balanced bracket pairs are left unchanged.
    """
    stack, unmatched = [], set()
    backslashes = 0
    for index, char in enumerate(value):
        escaped = backslashes % 2 == 1
        if char == "[" and not escaped:
            stack.append(index)
        elif char == "]" and not escaped:
            if stack:
                stack.pop()
            else:
                unmatched.add(index)
        backslashes = backslashes + 1 if char == "\\" else 0
    unmatched.update(stack)
    return "".join(("\\" if index in unmatched else "") + char for index, char in enumerate(value))


async def news_article(fetcher, game, row: dict) -> Item | None:
    """Read and validate the matching detail, never substitute the list preview."""
    cid = row.get("id")
    if not isinstance(cid, str) or not _ID.fullmatch(cid) or not _integer(row.get("ts"), 1):
        return None
    data = _envelope(await fetcher.get_json(f"{BASE}/{cid}", source="news-endfield",
                                            params=PARAMS, retries=1, allow_redirects=False))
    if (data is None or data.get("cid") != cid or data.get("displayTime") != row["ts"]
            or type(data.get("displayTime")) is not int):
        return None
    title, content = data.get("title"), data.get("data")
    if (not isinstance(title, str) or not title.strip() or not isinstance(content, str) or not content.strip()
            or len(content) > 2 * 1024 * 1024
            or ("title" in row and (not isinstance(row["title"], str) or title.strip() != row["title"].strip()))):
        return None
    try:
        blocks = news_blocks(content)
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None
    if not blocks:
        return None
    for block in blocks:
        if block.get("kind") == "text":
            block["text"] = _escape_unmatched_brackets(block.get("text", ""))
    cover = data.get("cover")
    return Item(source="endfield", game=game.key, id=cid, url=ARTICLE.format(cid), title=title.strip(),
                text="", published_ts=row["ts"], article_blocks=blocks,
                images=[cover] if isinstance(cover, str) and cover else [], author="GRYPHLINE")


async def snapshot(fetcher, game):
    """Two identical complete scans; does not prove publisher snapshot atomicity."""
    first = await discover_news(fetcher, game, 0)
    if first is None:
        return None
    second = await discover_news(fetcher, game, 0)
    return first if first == second else None
