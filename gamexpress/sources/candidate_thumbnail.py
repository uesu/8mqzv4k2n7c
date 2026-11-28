"""Verified YouTube-cover fallback for official news without native media.

A YouTube image is used only when no source image or native video exists,
and only after the caller's normal media preflight accepts the derived thumbnail.
"""
from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from ..cards import _youtube_watch_url

CANDIDATES = frozenset(("nte", "endfield", "silverpalace"))


def fallback_url(item):
    trusted_source = (item.source in CANDIDATES and item.game == item.source) or item.source in {"hoyolab", "kuro"}
    if (not trusted_source or not item.official or item.images
            or any(b.get("kind") == "image"
                   or (b.get("kind") == "video" and not _youtube_watch_url(b.get("url", "")))
                   or "[Native video unavailable — open the original article.]" in b.get("text", "")
                   for b in item.article_blocks)):
        return None
    for block in item.article_blocks:
        kind = block.get("kind")
        if kind in ("link", "video"):
            urls = [block.get("url", "")]
        elif kind == "text":
            # News parsers keep ordinary anchors and bare URLs inline as text.
            # Never extract an ID from arbitrary prose or an unverified domain.
            urls = re.findall(r"https?://[^\s<>\[\]()]+", block.get("text", ""))
        else:
            continue
        for url in urls:
            watch = _youtube_watch_url(url.rstrip(".,!?;"))
            if watch:
                video_id = parse_qs(urlsplit(watch).query)["v"][0]
                return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    return None


async def with_verified_fallback(fetcher, item):
    """Copy the item only when a thumbnail passes preflight; never mutate the original."""
    max_url = fallback_url(item)
    if not max_url:
        return item
    # YouTube may return a tiny 120x90 placeholder with HTTP 200 when a size
    # does not exist. Check actual dimensions before trusting max/SD variants;
    # portrait thumbnails are accepted too. The regular signature/MIME check
    # is still required. Bounded requests only, with the existing host policy.
    for suffix, minimum in (("maxresdefault.jpg", (1280, 720)),
                            ("sddefault.jpg", (640, 360)),
                            ("hqdefault.jpg", None)):
        url = max_url.rsplit("/", 1)[0] + "/" + suffix
        if minimum is not None:
            dims = await fetcher.media_dimensions(url)
            if not dims or max(dims) < minimum[0] or min(dims) < minimum[1]:
                continue
        if await fetcher.verify_media(url):
            return replace(item, images=[url])
    return item
