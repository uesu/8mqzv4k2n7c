"""Full-size media for the schedule card.

Every source hands back a *small* rendition by default, and a schedule card is the one place
where the picture is the point (it is the program's key art). This module upgrades whatever a
source gave us to the biggest rendition that source serves, without ever inventing a URL that
the source does not have:

  pbs.twimg.com/media/X.jpg              -> ?name=orig              (X gives `small` by default)
  nitter.<instance>/pic/media%2FX.jpg    -> https://pbs.twimg.com/media/X.jpg  (+ ?name=orig)
  i.ytimg.com/vi/ID/hqdefault.jpg        -> maxresdefault.jpg       (1280x720 instead of 480x360)
  fastcdn.hoyoverse.com/content-v2/...   -> kept as-is (already the full-size article cover)

Ranking puts the rendition most likely to be program key art first: a YouTube thumbnail (the
livestream's own artwork) beats a tweet photo, which beats a small article cover.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

from .textutil import youtube_id

TWIMG = re.compile(r"^https?://(?:pbs\.twimg\.com|ton\.twimg\.com)/", re.I)
NITTER_PIC = re.compile(r"/pic/(?:orig/)?(.+)$")
YOUTUBE_IMG = re.compile(r"^https?://i\.ytimg\.com/vi/([^/]+)/", re.I)
_MEDIA_PREFIXES = ("media/", "ext_twvideo_thumb/", "ext_tw_video_thumb/", "tweet_video_thumb/",
                   "amplify_video_thumb/", "card_img/")


def nitter_pic_to_twimg(url: str) -> str:
    """https://nitter.cf/pic/media%2FHR70hTAaoAA8Dzz.jpg -> https://pbs.twimg.com/media/HR70hTAaoAA8Dzz.jpg"""
    m = NITTER_PIC.search(url or "")
    if not m:
        return url
    raw = unquote(m.group(1))
    path, _, query = raw.partition("?")
    if path.startswith(_MEDIA_PREFIXES):
        keep = f"?{query}" if path.startswith("card_img/") and query else ""
        return f"https://pbs.twimg.com/{path}{keep}"
    # nitter's RSS and status feeds carry the whole twimg address, not just the media path:
    # /pic/https%3A%2F%2Fpbs%2Etwimg%2Ecom%2Fmedia%2F<id>.jpg  (seen 2026-10-09 on nitter.cf)
    if path.startswith("https://pbs.twimg.com/"):
        return raw if path.startswith("https://pbs.twimg.com/card_img/") and query else path
    return url


def twimg_orig(url: str) -> str:
    """A tweet photo without a size hint is served at `small`. `name=orig` is the full upload.
    A URL that already asks for a size is left exactly as the source gave it."""
    if not TWIMG.match(url or ""):
        return url
    if "name=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "name=orig"


def youtube_thumb(video_or_id: str) -> str:
    """A watch URL, a /live/ URL, a youtu.be link, or a bare video id -> the 1280x720 thumbnail.

    "" when no 11-character id can be found: a thumbnail URL with a whole URL inside it is
    worse than no image at all, because Discord renders it as a broken embed instead of falling
    back to the card's other art."""
    text = (video_or_id or "").strip()
    vid = youtube_id(text) or (text if re.fullmatch(r"[A-Za-z0-9_-]{11}", text) else None)
    return f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg" if vid else ""


def upgrade(url: str) -> str:
    """Biggest rendition of one URL. Never returns a host the source did not already give us."""
    url = (url or "").strip()
    if not url:
        return ""
    if "/pic/" in url:
        url = nitter_pic_to_twimg(url)
    if TWIMG.match(url):
        return twimg_orig(url)
    m = YOUTUBE_IMG.match(url)
    if m:
        return f"https://i.ytimg.com/vi/{m.group(1)}/maxresdefault.jpg"
    return url


def sane_aspect(width, height) -> bool:
    """True unless the picture's shape says it is not key art.

    Key art for a version announcement is 16:9 (all four 2026-09 announcements are: 1920x1080,
    or 1200x675 for GI). The failure this guards against is a news page serving a whole article
    as ONE stitched vertical strip — the 2026-09-27 run put a 1440x29482 image on the WW card,
    which Discord renders as an unreadable sliver. A missing size is not a reason to reject: most
    sources do not report dimensions at all.
    """
    try:
        w, h = float(width or 0), float(height or 0)
    except (TypeError, ValueError):
        return True
    if w <= 0 or h <= 0:
        return True
    ratio = max(w, h) / min(w, h)
    return ratio <= 3.5


def _score(url: str) -> int:
    """Program key art first: a livestream thumbnail, then full-size X media, then official cover/VOD."""
    u = url.lower()
    if "i.ytimg.com" in u and "maxresdefault" in u:
        return 3
    if "pbs.twimg.com" in u or "video.twimg.com" in u:
        return 2
    if any(
        h in u
        for h in (
            "fastcdn.hoyoverse.com",
            "hoyolab.com",
            "hoyoverse.com",
            "mihoyo.com",
            "miyoushe.com",
            "kurogame.com",
            "kurogames.com",
            "aki-game.net",
            "aki-game.com",
        )
    ):
        return 1
    return 0


def _same_file(url: str) -> str:
    """The URL without its size hints, so alternate renditions of one upload compare equal."""
    base, _, query = url.partition("?")
    norm = base.rstrip("/").lower()
    if TWIMG.match(url or "") and "/media/" in norm and "." not in norm.rsplit("/", 1)[-1]:
        m_fmt = re.search(r"(?:^|&)format=([a-z0-9]+)(?:&|$)", query, re.I)
        if m_fmt:
            norm = f"{norm}.{m_fmt.group(1).lower()}"
    return norm


_SMALL_RENDITION = re.compile(r"resize|name=(?:small|thumb|medium|large)|/s_\d+|quality", re.I)


def rank(images: list[str] | None, limit: int = 4) -> list[str]:
    """Dedupe, upgrade, best rendition first. Keep one, full-size rendition per upload."""
    best: dict[str, str] = {}
    order: list[str] = []
    for u in images or []:
        big = upgrade(u)
        if not big:
            continue
        key = _same_file(big)
        if key not in best:
            best[key], order = big, order + [key]
        elif _SMALL_RENDITION.search(best[key]) and not _SMALL_RENDITION.search(big):
            best[key] = big
    out = [best[k] for k in order]
    out.sort(key=lambda u: -_score(u))
    return out[:limit]


async def news_media_available(fetcher, payloads: list[dict], *, max_urls: int = 40) -> bool:
    """Check only media actually included (not omitted overflow media); preserve URLs.

    Sequential and capped by the caller's explicit operational media bound (40 by
    default; raised explicitly when expanded full-article delivery is opted into).
    No URL guessing, fixture replacements, screenshot bytes, arbitrary proxies or
    changes to schedule/codes.
    """
    urls = list(dict.fromkeys(entry["media"]["url"]
                             for payload in payloads for card in payload["components"]
                             for component in card["components"] if component["type"] == 12
                             for entry in component["items"]))
    if len(urls) > max_urls:
        return False
    for url in urls:
        if not await fetcher.verify_media(url):
            return False
    return True


def image_dimensions(raw: bytes) -> tuple[int, int] | None:
    """Original PNG/GIF/JPEG/WebP header dimensions; no decode/dependency/URL hints.

    Truncated, unsupported (including videos/AVIF), or malformed headers are unknown.
    JPEG markers are scanned only inside the caller's bounded prefix.
    """
    import struct

    size = None
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24 and raw[12:16] == b"IHDR":
        size = struct.unpack(">II", raw[16:24])
    elif raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
        size = struct.unpack("<HH", raw[6:10])
    elif raw[:2] == b"\xff\xd8":
        i = 2
        while i + 1 < len(raw):
            if raw[i] != 255:
                return None
            while i < len(raw) and raw[i] == 255:
                i += 1
            if i >= len(raw):
                return None
            marker, i = raw[i], i + 1
            if marker in (0xD9, 0xDA):
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                continue
            if i + 2 > len(raw):
                break
            length = int.from_bytes(raw[i:i + 2], "big")
            if length < 2 or i + length > len(raw):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and length >= 8:
                height, width = struct.unpack(">HH", raw[i + 3:i + 7])
                size = width, height
                break
            i += length
    elif len(raw) >= 30 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        kind = raw[12:16]
        if kind == b"VP8X":
            size = (1 + int.from_bytes(raw[24:27], "little"),
                    1 + int.from_bytes(raw[27:30], "little"))
        elif kind == b"VP8 " and raw[23:26] == b"\x9d\x01\x2a":
            size = tuple(n & 0x3FFF for n in struct.unpack("<HH", raw[26:30]))
        elif kind == b"VP8L" and raw[20] == 0x2F:
            bits = int.from_bytes(raw[21:25], "little")
            size = (1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF))
    return size if size and all(n > 0 for n in size) else None


def _news_urls(payloads: list[dict]) -> list[str]:
    return [entry["media"]["url"] for payload in payloads for co in payload["components"]
            for c in co["components"] if c["type"] == 12 for entry in c["items"]]


def group_measured_news(payloads: list[dict], dimensions: dict) -> list[dict]:
    """Pure optional proposal; all media known, original order and boundaries intact.

    Keep original messages/containers/labels/controls. Revert the whole proposal on
    unknown dimensions, overflow, or validation/order failure; never modify inputs.
    """
    import math
    from copy import deepcopy

    from .cards import sep, validate_payload

    urls = _news_urls(payloads)
    if not urls or any("**Excerpt —" in c.get("content", "") for p in payloads
                       for co in p["components"] for c in co["components"]):
        return payloads
    for url in urls:
        d = dimensions.get(url)
        if (not isinstance(d, (tuple, list)) or len(d) != 2
                or any(not isinstance(n, (int, float)) or not math.isfinite(n) or n <= 0 for n in d)):
            return payloads

    def long(c):
        if c["type"] != 12 or len(c["items"]) != 1:
            return False
        w, h = dimensions[c["items"][0]["media"]["url"]]
        return h / w > 3.5

    proposal = deepcopy(payloads)
    changed = False
    for payload in proposal:
        for co in payload["components"]:
            kids, out, i = co["components"], [], 0
            while i < len(kids):
                c = kids[i]
                i += 1
                if not long(c):
                    out.append(c)
                    continue
                run = [c]
                while len(run) < 10 and i < len(kids) and long(kids[i]):
                    run.append(kids[i])
                    i += 1
                if len(run) == 1:
                    out.append(c)
                    continue
                if not out or out[-1]["type"] != 14:
                    out.append(sep())
                out.append({"type": 12, "items": [g["items"][0] for g in run]})
                changed = True
            co["components"] = out
    if (not changed or any(validate_payload(p) for p in proposal)
            or _news_urls(proposal) != urls):
        return payloads
    return proposal


async def group_news_media(
    fetcher,
    payloads: list[dict],
    *,
    enabled: bool = False,
    max_urls: int = 40,
) -> list[dict]:
    """Runner adapter: optional bounded reads, <=20s/article; off = no I/O.

    Availability preflight/refresh runs FIRST; optional measurement never holds a
    deliverable article. Group only after all dimensions are known. No cross-tick
    cache or persistent state. Existing partial-delivery signature guards apply.
    """
    import asyncio

    if not enabled or any("**Excerpt —" in c.get("content", "") for p in payloads
                          for co in p["components"] for c in co["components"]):
        return payloads
    urls = list(dict.fromkeys(_news_urls(payloads)))
    if not urls or len(urls) > max_urls:
        return payloads

    async def measure():
        dimensions = {}
        for url in urls:
            d = await fetcher.media_dimensions(url)
            if d is None:
                return None
            dimensions[url] = d
        return dimensions

    try:
        dimensions = await asyncio.wait_for(measure(), timeout=20)
    except (asyncio.TimeoutError, OSError, ValueError, TypeError, AttributeError):
        return payloads
    return group_measured_news(payloads, dimensions) if dimensions else payloads
