"""NTE English news reader for the opt-in candidate monitor.

Identical bounded double-walks reduce, but cannot rule out, upstream pagination races.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ..models import Item
from ..textutil import news_blocks

_PATH = re.compile(r"/en/article/news/(?:gamenews|gamebroad|gameevent)/[0-9]{8}/([0-9]{1,32})\.html\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_HOST = "https://nte.perfectworld.com"
_VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"))


def _day(value):
    if not _DATE.fullmatch(value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


class _Listing(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.found = 0
        self.closed = 0
        self.invalid = False
        self.current = None
        self.capture = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if not self.stack:
            if tag == "div" and "listNews" in classes:
                self.found += 1
                self.stack.append(tag)
            return
        if tag == "a":
            if self.current is not None:
                self.invalid = True
            href = attributes.get("href") or ""
            path = href[len(_HOST):] if href.startswith(_HOST + "/") else href
            match = _PATH.fullmatch(path)
            if not match:
                self.invalid = True
            self.current = {"id": match[1] if match else "", "title": None, "date": None, "path": path}
        if self.current is not None and ((tag == "h2" and "title" in classes) or
                                         (tag == "p" and "date" in classes)):
            key = "title" if tag == "h2" else "date"
            if self.capture is not None or self.current[key] is not None:
                self.invalid = True
            self.capture = (key, len(self.stack), tag)
            self.current[key] = ""
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID and self.stack:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.capture and self.current is not None:
            self.current[self.capture[0]] += data

    def handle_endtag(self, tag):
        if not self.stack or tag in _VOID:
            return
        if self.stack[-1] != tag:
            self.invalid = True
            return
        self.stack.pop()
        if self.capture and len(self.stack) == self.capture[1] and tag == self.capture[2]:
            self.capture = None
        if tag == "a":
            if self.current is None or self.capture:
                self.invalid = True
            else:
                self.rows.append(self.current)
                self.current = None
        if not self.stack:
            self.closed += 1


def parse_list_page(html):
    """Return validated ID/title/day rows or None; never infer a time from the URL."""
    if not isinstance(html, str) or not html or len(html) > 1024 * 1024:
        return None
    parser = _Listing()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, RecursionError):
        return None
    if (parser.invalid or parser.found != 1 or parser.closed != 1 or parser.stack
            or parser.current is not None or not 1 <= len(parser.rows) <= 100):
        return None
    result = []
    seen = set()
    for row in parser.rows:
        cid, title, day = row["id"], row["title"], row["date"]
        if (cid in seen or not isinstance(title, str) or not title.strip() or len(title) > 512
                or not isinstance(day, str) or not _day(day.strip())):
            return None
        seen.add(cid)
        result.append({"id": cid, "title": title.strip(), "date": day.strip()})
    return result


MAX_PAGES = 100
BASE = _HOST + "/en/article/news/"


class _Pager(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = self.found = 0
        self.href = None
        self.label = ""
        self.numbers = []
        self.invalid = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if not self.depth:
            if tag == "ul" and "pagination" in (attrs.get("class") or "").split():
                self.depth, self.found = 1, self.found + 1
            return
        if tag == "ul":
            self.depth += 1
        if tag == "a":
            if self.href is not None:
                self.invalid = True
            self.href, self.label = attrs.get("href", ""), ""

    def handle_data(self, data):
        if self.href is not None:
            self.label += data

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == "a" and self.href is not None:
            label = self.label.strip()
            if re.fullmatch(r"[0-9]{1,3}", label):
                n = int(label)
                if self.href != ("#" if n == 1 else f"index{n - 1}.html"):
                    self.invalid = True
                self.numbers.append(n)
            self.href = None
        elif tag == "ul":
            self.depth -= 1


def _page_count(html):
    if not isinstance(html, str) or not html or len(html) > 1024 * 1024:
        return None
    parser = _Pager()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, RecursionError):
        return None
    n = len(parser.numbers)
    if (parser.invalid or parser.found != 1 or parser.depth or parser.href is not None
            or not 1 <= n <= MAX_PAGES or parser.numbers != list(range(1, n + 1))):
        return None
    return n


def _rows_with_urls(html):
    clean = parse_list_page(html)
    if clean is None:
        return None
    parser = _Listing()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, RecursionError):
        return None
    if len(parser.rows) != len(clean):
        return None
    result = []
    for row, raw in zip(clean, parser.rows):
        path = raw.get("path")
        match = _PATH.fullmatch(path) if isinstance(path, str) else None
        if not match or match.group(1) != row["id"]:
            return None
        result.append({**row, "url": _HOST + path})
    return result


async def _walk(fetcher):
    async def read(index):
        url = BASE + ("index.html" if index == 0 else f"index{index}.html")
        return await fetcher.get_text(url, source="news-nte", retries=1, allow_redirects=False)

    first = await read(0)
    count, initial = _page_count(first), _rows_with_urls(first)
    if count is None or initial is None:
        return None
    # Keep concurrency bounded while awaiting the Fetcher's own request gate.
    sem = asyncio.Semaphore(3)

    async def gated(index):
        async with sem:
            await asyncio.sleep(0.1)
            return await read(index)

    pages = [first, *await asyncio.gather(*(gated(i) for i in range(1, count)))]
    result, seen = [], set()
    for index, html in enumerate(pages):
        rows = _rows_with_urls(html)
        if rows is None or (index < count - 1 and len(rows) != len(initial)) or len(rows) > len(initial):
            return None
        for row in rows:
            if row["id"] in seen:
                return None
            seen.add(row["id"])
            result.append(row)
    end = await read(0)
    if _page_count(end) != count or _rows_with_urls(end) != initial:
        return None
    return result


async def snapshot(fetcher):
    """Fail closed for partial or nonmatching complete scans; never infer publication time."""
    first = await _walk(fetcher)
    if first is None:
        return None
    second = await _walk(fetcher)
    return first if second == first else None


class _Body(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.depth = self.found = self.closed = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if not self.depth:
            if tag == "div" and "articleContent" in (dict(attrs).get("class") or "").split():
                self.depth, self.found = 1, self.found + 1
            return
        if tag == "div":
            self.depth += 1
        self.parts.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        if self.depth:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == "div":
            self.depth -= 1
            if not self.depth:
                self.closed += 1
                return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def handle_entityref(self, name):
        if self.depth:
            self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if self.depth:
            self.parts.append(f"&#{name};")


def _encode_official_media_spaces(url):
    """Encode literal spaces only in an official NTE CDN path; leave other URLs unchanged."""
    if not isinstance(url, str) or " " not in url or any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return url
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != "ntevmg.perfectworld.com"
                or parts.username or parts.password or parts.port not in (None, 443)
                or " " in parts.netloc or any(ch.isspace() for ch in parts.query + parts.fragment)
                or any(ch.isspace() and ch != " " for ch in parts.path)):
            return url
        return parts._replace(path=parts.path.replace(" ", "%20")).geturl()
    except ValueError:
        return url


def _body(html):
    if not isinstance(html, str) or not html or len(html) > 2 * 1024 * 1024:
        return None
    parser = _Body()
    try:
        parser.feed(html)
        parser.close()
        if parser.found != 1 or parser.closed != 1 or parser.depth:
            return None
        blocks = news_blocks("".join(parser.parts), escape_literal_tildes=True, join_table_cells=True)
        for block in blocks:
            if block.get("kind") in ("image", "video"):
                block["url"] = _encode_official_media_spaces(block.get("url"))
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None
    return blocks or None


async def news_article(fetcher, game, row):
    """Read only a validated canonical listing URL; no redirects or synthetic timestamp."""
    if not isinstance(row, dict) or set(row) != {"id", "title", "date", "url"}:
        return None
    cid, title, day, url = (row.get(k) for k in ("id", "title", "date", "url"))
    if (not isinstance(cid, str) or not isinstance(title, str) or not title.strip() or len(title) > 512
            or not isinstance(day, str) or not _day(day) or not isinstance(url, str)
            or not url.startswith(_HOST + "/")):
        return None
    match = _PATH.fullmatch(url[len(_HOST):])
    if not match or match.group(1) != cid:
        return None
    html = await fetcher.get_text(url, source="news-nte", retries=1, allow_redirects=False)
    blocks = _body(html)
    if not blocks:
        return None
    return Item(source="nte", game=game.key, id=cid, title=title.strip(), url=url, text="",
                published_ts=0, article_blocks=blocks, author="Neverness to Everness")
