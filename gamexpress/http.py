"""Tiny resilient HTTP layer: timeouts, retries with backoff, per-source health log."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger("gamexpress.http")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
# No version in the User-Agent, on purpose. It used to include the old "/1.1" suffix — a
# hand-maintained number that stopped being maintained the day `__version__` was retired in
# favour of build_id() (see __init__.py), so it sat frozen, announcing something to every site
# the monitor touches that had not been true for a long time. A bare product token is valid
# (RFC 9110: the "/version" part is optional) and can never go stale. The contact URL is the
# half that actually matters to anyone reading a log. Same string in sources/codes.py and
# sources/twitter.py.
BOT_UA = "Game-Express (+https://github.com/uesu/Game-Express)"
MAX_PARALLEL = 12          # polite global cap on simultaneous requests (all games fetch in parallel)
MAX_BYTES = 8 * 1024 * 1024
"""Hard cap on one response body (8 MiB).

Almost everything this monitor reads belongs to a third party nobody here controls: community
nitter and Redlib mirrors, rich-embed proxies, wikis, and code APIs. A hostile or broken host
can answer with an endless stream, and `resp.text()` would buffer all of it until the runner
runs out of memory or the job hits its timeout. Reading a bounded number of bytes turns that
into one ordinary source failure instead. The largest real body seen in production is ~1.3 MB.
"""
CHUNK_BYTES = 64 * 1024
"""Read size per iteration while streaming a body (see get_text)."""


@dataclass
class SourceHealth:
    ok: int = 0
    fail: int = 0
    last_error: str = ""


@dataclass
class Fetcher:
    session: aiohttp.ClientSession
    timeout: int = 20
    health: dict[str, SourceHealth] = field(default_factory=dict)
    parallel: int = MAX_PARALLEL
    _sem: asyncio.Semaphore | None = field(default=None, repr=False)

    def _gate(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.parallel)
        return self._sem

    def _mark(self, source: str, ok: bool, err: str = "") -> None:
        h = self.health.setdefault(source, SourceHealth())
        if ok:
            h.ok += 1
        else:
            h.fail += 1
            h.last_error = err[:200]

    async def get_text(self, url: str, *, source: str, headers: dict | None = None,
                       params: dict | None = None, retries: int = 2,
                       ok_statuses: tuple[int, ...] = (200,), timeout: float | None = None) -> str | None:
        hdrs = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
        if headers:
            hdrs.update(headers)
        delay = 1.5
        last = ""
        for attempt in range(retries + 1):
            try:
                async with self._gate(), self.session.get(url, headers=hdrs, params=params,
                                            timeout=aiohttp.ClientTimeout(total=timeout or self.timeout)) as r:
                    if r.status in ok_statuses:
                        # read in chunks, NOT `content.read(MAX_BYTES + 1)`: aiohttp's read(n)
                        # returns whatever is buffered RIGHT NOW (up to n), so a body that
                        # arrives in several TCP reads came back truncated — a valid feed then
                        # failed as "invalid JSON" / "0 entries". Loop until EOF or the cap.
                        raw = bytearray()
                        oversize = False
                        async for part in r.content.iter_chunked(CHUNK_BYTES):
                            raw += part
                            if len(raw) > MAX_BYTES:
                                oversize = True
                                break
                        if oversize:
                            last = f"response larger than {MAX_BYTES // (1024 * 1024)} MiB"
                            break   # a source this broken will not fix itself on a retry
                        body = bytes(raw).decode(r.charset or "utf-8", errors="replace")
                        self._mark(source, True)
                        return body
                    last = f"HTTP {r.status}"
                    if r.status in (400, 401, 403, 404, 410):
                        break  # permanent for this run — don't hammer
            except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError) as e:
                last = re.sub(r"\?[^\s)'\"]+", "", f"{type(e).__name__}: {e}")
            if attempt < retries:
                await asyncio.sleep(delay)
                delay *= 2
        log.info("[%s] %s -> %s", source, url.split("?")[0], last)
        self._mark(source, False, last)
        return None

    async def get_json(self, url: str, *, source: str, headers: dict | None = None,
                       params: dict | None = None, retries: int = 2, timeout: float | None = None):
        body = await self.get_text(url, source=source, headers=headers, params=params, retries=retries,
                                   timeout=timeout)
        if body is None:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            # some mirrors prepend comments/BOMs
            start = min([i for i in (body.find("{"), body.find("[")) if i >= 0], default=-1)
            if start > 0:
                try:
                    return json.loads(body[start:])
                except json.JSONDecodeError:
                    pass
            self._mark(source, False, "invalid JSON")
            log.info("[%s] invalid JSON from %s", source, url.split("?")[0])
            return None

    async def _news_media_prefix(self, url: str, *, dimensions: bool = False):
        """Allowlisted HTTPS GET, <=4 hops, <=256 KiB, bounded by the caller.

        Stop as soon as dimensions are known; do not decode or transform images.
        Unknown/unsupported formats are not evidence for grouping.
        """
        import re
        from urllib.parse import urljoin, urlsplit

        from .media import image_dimensions

        hosts = ("hoyolab.com", "hoyoverse.com", "hoyocdn.com", "mihoyo.com",
                 "mihoyocdn.com", "kurogame.com", "kurogames.com", "aki-game.net",
                 "aki-game.com", "easebar.com", "anantagame.com", "netease.com",
                 "163.com", "ytimg.com", "twimg.com", "redd.it", "redditmedia.com",
                 "vxreddit.com", "embedez.com", "deltandy.me")
        try:
            for _ in range(4):
                parts = urlsplit(url)
                host = (parts.hostname or "").lower()
                if (parts.scheme != "https" or parts.username or parts.password
                        or parts.port not in (None, 443)
                        or not any(host == h or host.endswith("." + h) for h in hosts)):
                    log.warning("news media rejected: host=%s reason=URL policy", host)
                    return None
                async with self._gate(), self.session.get(
                        url, headers={"User-Agent": BROWSER_UA}, allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=8)) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            return None
                        url = urljoin(url, location)
                        continue
                    if response.status == 206:
                        # Some CDNs return partial media even for an ordinary GET.
                        # Only a declared prefix from offset zero can be sniffed.
                        content_range = response.headers.get("Content-Range", "")
                        match = re.fullmatch(r"bytes 0-(\d+)/(\d+|\*)", content_range.strip())
                        if (not match or (match[2] != "*" and int(match[2]) <= int(match[1]))):
                            log.warning("news media rejected: host=%s status=206 reason=non-prefix/invalid range", host)
                            return None
                    elif response.status != 200:
                        log.warning("news media rejected: host=%s status=%s", host, response.status)
                        return None
                    cap = 256 * 1024 if dimensions else 64
                    prefix = bytearray()
                    async for chunk in response.content.iter_chunked(2048 if dimensions else 64):
                        prefix += chunk[:cap - len(prefix)]
                        if len(prefix) >= cap or (dimensions and image_dimensions(prefix)):
                            break
                    mime = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    # HoYoLAB CDN can label real GIFs as bare "gif" (#205,
                    # HSR 47141115). Normalize only this observed alias, and
                    # only with GIF magic; never trust an extension/header alone.
                    # Shared here so availability and dimensions agree.
                    if mime == "gif" and prefix.startswith((b"GIF87a", b"GIF89a")):
                        mime = "image/gif"
                    return bytes(prefix), mime
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("news media rejected: reason=%s", type(exc).__name__)
        return None

    async def verify_media(self, url: str) -> bool:
        """Bounded GET/status/signature preflight; never a Discord decode guarantee."""
        result = await self._news_media_prefix(url)
        if result:
            raw, mime = result
            image = (raw.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a"))
                     or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"))
            video = raw[4:8] == b"ftyp" or raw.startswith(b"\x1aE\xdf\xa3")
            # Binary MIME is used by some object-storage CDNs. Still require
            # recognized bytes: octet-stream alone is never proof of media.
            binary = mime.strip() == "application/octet-stream"
            if (image and (mime.startswith("image/") or binary)) or (
                    video and (mime.startswith(("video/", "image/avif")) or binary)):
                self._mark("news-media", True)
                return True
            from urllib.parse import urlsplit

            log.warning("news media rejected: host=%s mime=%s prefix=%s reason=signature/MIME",
                        urlsplit(url).hostname, mime, raw[:12].hex())
        self._mark("news-media", False, "media GET/status/signature validation failed")
        return False

    async def media_dimensions(self, url: str) -> tuple[int, int] | None:
        """Optional measurement: <=8s total including redirects; failures are unknown."""
        from .media import image_dimensions

        try:
            result = await asyncio.wait_for(self._news_media_prefix(url, dimensions=True), timeout=8)
            if result and result[1].startswith("image/"):
                return image_dimensions(result[0])
        except asyncio.TimeoutError:
            pass
        return None

    def summary(self) -> str:
        parts = []
        for name, h in sorted(self.health.items()):
            parts.append(f"{name}: {h.ok} ok / {h.fail} fail" + (f" ({h.last_error})" if h.fail and not h.ok else ""))
        return "; ".join(parts) if parts else "no requests"


class Probe:
    """Wraps the shared Fetcher for ONE game so 'did any of this game's sources answer?'
    stays correct while every game is gathered concurrently."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.ok = 0

    async def get_text(self, *a, **k):
        r = await self.inner.get_text(*a, **k)
        if r is not None:
            self.ok += 1
        return r

    async def get_json(self, *a, **k):
        r = await self.inner.get_json(*a, **k)
        if r is not None:
            self.ok += 1
        return r

    def __getattr__(self, name):          # health / summary / session … pass through
        return getattr(self.inner, name)
