"""Countdown / timer sites — an ESTIMATE of the program and maintenance times.

Why this exists: the official maintenance notice (HoYoLAB / X / Kuro) usually arrives days
after the Special Program. Until then the card can only show TBA. Community countdown sites
extrapolate the patch cycle (42 days for HoYoverse, ~42 days for Kuro) and are good enough
to show a real countdown — never good enough to REPLACE the official times.

Rules:
  * an estimate only fills a field NO official source has given yet
    (schedule.PRIORITY['countdown'] is the lowest priority in the whole bot);
  * the card says which fields are estimated and where they came from (🕒 line);
  * as soon as the official notice is seen, it overwrites the estimate and the 🕒 line
    disappears on the same silent edit;
  * COUNTDOWN_ESTIMATES=0 switches the whole thing off; no source, no requests.

Sources (checked 2026-09-25; the pages are server-rendered WordPress / Next.js, so the
plain text is enough — no JS, no API key):
  https://{game}-countdown.gengamer.in/livestream   "Release Date & Time: Friday, October 23 at 8:00 AM EDT"
  https://{game}-countdown.gengamer.in/             version + countdown block
  https://gachacountdown.online/games/{game}/       "39 Days 14 Hours 09 Minutes 08 Seconds"
Prepared but empty: hna, ananta (nothing tracks them before release — fill the lists in).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from html import unescape

from ..timeparse import find_datetimes

log = logging.getLogger("gamexpress.countdown")

# game key -> [{url, label, kind}]   kind: program = livestream/broadcast, update = version release
SOURCES: dict[str, list[dict]] = {
    "genshin": [
        {"url": "https://genshin-countdown.gengamer.in/livestream", "label": "genshin-countdown", "kind": "program"},
        {"url": "https://genshin-countdown.gengamer.in/", "label": "genshin-countdown", "kind": "update"},
        {"url": "https://gachacountdown.online/games/genshin/", "label": "Gacha Countdown", "kind": "update"},
    ],
    "starrail": [
        {"url": "https://hsr-countdown.gengamer.in/livestream", "label": "hsr-countdown", "kind": "program"},
        {"url": "https://hsr-countdown.gengamer.in/", "label": "hsr-countdown", "kind": "update"},
        {"url": "https://gachacountdown.online/games/hsr/", "label": "Gacha Countdown", "kind": "update"},
    ],
    "zzz": [
        {"url": "https://zenless-countdown.gengamer.in/livestream", "label": "zenless-countdown", "kind": "program"},
        {"url": "https://zenless-countdown.gengamer.in/", "label": "zenless-countdown", "kind": "update"},
        {"url": "https://gachacountdown.online/games/zzz/", "label": "Gacha Countdown", "kind": "update"},
    ],
    "wuwa": [
        {"url": "https://wuthering-countdown.gengamer.in/livestream", "label": "wuthering-countdown", "kind": "program"},
        {"url": "https://wuthering-countdown.gengamer.in/", "label": "wuthering-countdown", "kind": "update"},
        {"url": "https://gachacountdown.online/games/wuwa/", "label": "Gacha Countdown", "kind": "update"},
    ],
    "hna": [],      # not released yet — no countdown site tracks it
    "ananta": [],
}

_SCRIPT = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_RELEASE_LINE = re.compile(r"(?:release|start|maintenance|update|live)[^\n:]{0,40}?(?:date|time)\s*[&+]?\s*"
                           r"(?:time)?\s*[：:]\s*(.+)", re.I)
_DELTA = re.compile(r"(\d{1,3})\s*Days?\s*(\d{1,2})\s*Hours?\s*(\d{1,2})\s*Minutes?\s*(\d{1,2})\s*Seconds?", re.I)
_VERSION = re.compile(r"(?:version\s*[:\-]?\s*|v\.?\s*)(\d+\.\d+)(?![\d.])", re.I)
_VERSION_TITLE = re.compile(r"\b(\d+\.\d+)\s+(?:livestream|release|update|countdown)", re.I)
# The ONE marker that proves a page is counting down to a VERSION RELEASE.
#
# A gengamer landing page is a *banner* countdown, not a version countdown: Genshin's reads
# "7.1 Banner Countdown" and times Skirk + Escoffier on Oct 13 — nowhere near 7.2's release.
# Wiring that timestamp to maint_start_ts put a banner date on the maintenance line.
_VERSION_RELEASE = re.compile(
    r"countdown\s+to\s+version\s*(\d+\.\d+)"
    r"|\b(\d+\.\d+)\s+(?:update\s+)?(?:is\s+set|is\s+expected|is\s+due)\s+to\s+release", re.I)


def version_release_hit(text: str) -> str | None:
    """The version this page says it is counting down to, or None when it never says so."""
    m = _VERSION_RELEASE.search(text or "")
    return (m.group(1) or m.group(2)) if m else None


def strip_html(html: str) -> str:
    """Server-rendered page -> plain text (scripts, tags and entities removed)."""
    text = _SCRIPT.sub(" ", html or "")
    text = _TAG.sub("\n", text)
    text = unescape(text)
    return re.sub(r"[ \t]+", " ", text)


def find_version(text: str) -> str | None:
    """'Genshin Impact 7.2 Livestream …' / 'Countdown to Version 7.2' -> '7.2'."""
    hits = [m.group(1) for m in _VERSION.finditer(text or "")]
    hits += [m.group(1) for m in _VERSION_TITLE.finditer(text or "")]
    return Counter(hits).most_common(1)[0][0] if hits else None


def parse_page(kind: str, html: str, now: int) -> dict | None:
    """-> {'version', 'ts'} or None. An absolute 'Release Date & Time:' line wins over a
    live countdown block (which is only a delta from now, so it drifts with the request).

    Two guards, both for real misreads seen on live pages:

    * a non-program page must SAY it is counting down to a version release. Without that
      marker the page is a banner countdown and its timestamp has nothing to do with the
      next maintenance;
    * these pages are server-rendered with every timer at "0 Days 0 Hours 0 Minutes
      0 Seconds" and filled in by JavaScript. A zero delta means UNKNOWN, not "now". The
      guard has to live here, because apply_estimates()'s window accepts ts == now.
    """
    text = strip_html(html)
    release_version = version_release_hit(text)
    if kind != "program" and not release_version:
        return None
    ts = None
    m = _RELEASE_LINE.search(text)
    if m:
        found = find_datetimes(m.group(1)[:120], now)
        if found:
            ts = found[0].ts
    if ts is None:
        d = _DELTA.search(text)
        if d:
            parts = [int(d[1]), int(d[2]), int(d[3]), int(d[4])]
            if not any(parts):
                return None                      # server-rendered placeholder, not "right now"
            ts = int(now) + parts[0] * 86400 + parts[1] * 3600 + parts[2] * 60 + parts[3]
    if not ts or int(ts) <= int(now):
        return None
    return {"version": release_version or find_version(text), "ts": int(ts), "kind": kind}


async def fetch_game(fetcher, game_key: str, now: int) -> dict | None:
    """-> {'version': '7.2'|None, 'program_ts': ts?, 'maint_start_ts': ts?, 'labels': [...]}"""
    specs = SOURCES.get(game_key) or []
    if not specs or fetcher is None:
        return None
    out: dict = {}
    labels: list[str] = []
    versions: list[str] = []
    for spec in specs:
        html = await fetcher.get_text(spec["url"], source=f"countdown:{spec['label']}", retries=1)
        if not html:
            continue
        hit = parse_page(spec["kind"], html, now)
        if not hit:
            continue
        key = "program_ts" if spec["kind"] == "program" else "maint_start_ts"
        out.setdefault(key, hit["ts"])
        if hit["version"]:
            versions.append(hit["version"])
        if spec["label"] not in labels:
            labels.append(spec["label"])
    if not out:
        return None
    out["version"] = Counter(versions).most_common(1)[0][0] if versions else None
    out["labels"] = labels
    return out
