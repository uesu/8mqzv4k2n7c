"""Datetime extraction from official announcement text + Discord timestamp helpers.

Every format below was taken from a REAL official post (see tests/fixtures):

  HSR tweet     "2026/08/14 19:30 (UTC+8)" / "will release on 2026/08/14 at 19:30 (UTC+8)"
  HSR notice    "pre-installation will begin at 2026/09/24 14:00 (UTC+8)"
  GI article    "on 09/12/2026 at 08:00 AM (UTC-4)"          (MM/DD/YYYY + AM/PM)
  WW tweet      "on September 19, 2026, at 19:00 (UTC+8)"
  ZZZ tweet     "will begin on December 19 at 19:30 (UTC+8)"  (NO year -> inferred)

Rules (accuracy first):
  * A datetime WITHOUT an explicit UTC/GMT offset is ignored for schedule
    fields (e.g. "(server time)" differs per region) unless the caller passes
    a default offset (Kuro's website timestamps are documented UTC+8).
  * Missing years are inferred from the post's publish time (closest date,
    preferring the future), never from "today".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_TIME = r"(?P<h>\d{1,2})[:：](?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*(?P<ampm>[AaPp]\.?\s?[Mm]\.?)?"
# Named zones: countdown sites write "8:00 AM EDT" instead of "(UTC-4)". US zones use the
# fixed offset of the abbreviation — good enough for an estimate; official posts that carry a
# real offset keep winning through schedule.PRIORITY. Ambiguous ones (CST, IST) are left out.
ZONES = {
    "EDT": -4, "EST": -5, "CDT": -5, "MDT": -6, "PDT": -7, "PST": -8,
    "UTC": 0, "GMT": 0, "BST": 1, "CET": 1, "CEST": 2,
    "ICT": 7, "WIB": 7, "HKT": 8, "SGT": 8, "PHT": 8, "JST": 9, "KST": 9,
    "AEST": 10, "AEDT": 11,
}
_TZ = (r"(?:\s*[\(（]?\s*(?:(?P<tzname>UTC|GMT)\s*(?P<tzsign>[+\-−–])\s*(?P<tzh>\d{1,2})"
       r"(?::?(?P<tzm>\d{2}))?|(?P<zone>" + "|".join(sorted(ZONES, key=len, reverse=True)) + r")"
       r"|(?P<server>server\s+time))\s*[\)）]?)?")
_JOIN = r"(?:\s*,?\s*(?:at|@|-|–|,)?\s*)"
_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))

# YYYY/MM/DD HH:MM  (also YYYY-MM-DD, YYYY.MM.DD)
_RE_YMD = re.compile(
    r"(?P<y>\d{4})[/\-.](?P<mo>\d{1,2})[/\-.](?P<d>\d{1,2})" + _JOIN + _TIME + _TZ, re.I)
# MM/DD/YYYY HH:MM AM (UTC-4)   — Genshin's US-style format
_RE_MDY = re.compile(
    r"(?<!\d)(?P<mo>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})" + _JOIN + _TIME + _TZ, re.I)
# September 19, 2026, at 19:00 (UTC+8)   /   December 19 at 19:30 (UTC+8)
_RE_NAMED = re.compile(
    r"\b(?P<mon>" + _MONTH_NAMES + r")\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*,?\s*(?P<y>\d{4}))?" + _JOIN + _TIME + _TZ, re.I)
# 19 September 2026 19:00 (UTC+8)
_RE_DNAMED = re.compile(
    r"\b(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?P<mon>" + _MONTH_NAMES + r")\.?"
    r"(?:\s*,?\s*(?P<y>\d{4}))?" + _JOIN + _TIME + _TZ, re.I)


@dataclass(frozen=True)
class FoundTime:
    ts: int            # unix seconds (UTC)
    start: int         # index in text
    end: int
    raw: str
    year_inferred: bool = False


def _offset(m: re.Match, default_offset: float | None) -> float | None:
    if m.group("tzname"):
        sign = -1 if m.group("tzsign") in "-−–" else 1
        hours = int(m.group("tzh")) + (int(m.group("tzm") or 0) / 60)
        if hours > 14:
            return None
        return sign * hours
    if m.groupdict().get("zone"):
        return ZONES.get(m.group("zone").upper())
    return default_offset


def _hour24(h: int, ampm: str | None) -> int | None:
    if ampm:
        a = ampm.lower().replace(".", "").replace(" ", "")
        if h < 1 or h > 12:
            return None
        if a.startswith("p") and h != 12:
            h += 12
        elif a.startswith("a") and h == 12:
            h = 0
    return h if 0 <= h <= 23 else None


def _infer_year(month: int, day: int, ref: datetime) -> int | None:
    best = None
    for y in (ref.year, ref.year + 1, ref.year - 1):
        try:
            cand = datetime(y, month, day, tzinfo=timezone.utc)
        except ValueError:
            continue
        delta = (cand - ref).total_seconds() / 86400
        # prefer upcoming dates (announcements talk about the future), allow a
        # small past window for "today/yesterday" wording
        score = abs(delta) if delta >= -3 else abs(delta) + 400
        if best is None or score < best[0]:
            best = (score, y)
    return best[1] if best else None


def find_datetimes(text: str, ref_ts: int | float | None = None,
                   default_offset: float | None = None,
                   require_tz: bool = True) -> list[FoundTime]:
    """Return every datetime found in *text*, sorted by position.

    require_tz=True drops datetimes without an explicit offset (unless
    default_offset is given). "(server time)" never counts as a timezone.
    """
    if not text:
        return []
    ref = datetime.fromtimestamp(ref_ts or datetime.now(timezone.utc).timestamp(), timezone.utc)
    found: list[FoundTime] = []
    taken: list[tuple[int, int]] = []

    def overlaps(a: int, b: int) -> bool:
        return any(a < e and s < b for s, e in taken)

    for rx in (_RE_YMD, _RE_MDY, _RE_NAMED, _RE_DNAMED):
        for m in rx.finditer(text):
            if overlaps(m.start(), m.end()):
                continue
            gd = m.groupdict()
            try:
                if "mon" in gd and gd.get("mon"):
                    month = MONTHS[gd["mon"].lower().rstrip(".")]
                else:
                    month = int(gd["mo"])
                day = int(gd["d"])
                year_inferred = False
                if gd.get("y"):
                    year = int(gd["y"])
                else:
                    year = _infer_year(month, day, ref)
                    year_inferred = True
                    if year is None:
                        continue
                hour = _hour24(int(gd["h"]), gd.get("ampm"))
                minute = int(gd["mi"])
                sec = int(gd.get("s") or 0)
                if hour is None or minute > 59:
                    continue
                if gd.get("server"):
                    off = default_offset if not require_tz else None
                else:
                    off = _offset(m, default_offset)
                if off is None and require_tz:
                    continue
                tz = timezone(timedelta(hours=off or 0))
                dt = datetime(year, month, day, hour, minute, sec, tzinfo=tz)
            except (ValueError, KeyError):
                continue
            found.append(FoundTime(int(dt.timestamp()), m.start(), m.end(), m.group(0).strip(),
                                   year_inferred=year_inferred))
            taken.append((m.start(), m.end()))
    found.sort(key=lambda f: f.start)
    return found


_RANGE_SEP = r"\s*(?:-|–|—|~|〜|to|until)\s*"
_TZ_REQ = r"\s*[\(（]?\s*(?:UTC|GMT)\s*(?P<tzsign>[+\-−–])\s*(?P<tzh>\d{1,2})(?::?(?P<tzm>\d{2}))?\s*[\)）]?"
# 2026/09/30 04:00 - 11:00 (UTC+8)   |   2026/09/30 04:00 - 2026/09/30 11:00 (UTC+8)
_RE_RANGE_YMD = re.compile(
    r"(?P<y>\d{4})[/\-.](?P<mo>\d{1,2})[/\-.](?P<d>\d{1,2})" + _JOIN +
    r"(?P<h>\d{1,2}):(?P<mi>\d{2})" + _RANGE_SEP +
    r"(?:(?P<y2>\d{4})[/\-.](?P<mo2>\d{1,2})[/\-.](?P<d2>\d{1,2})" + _JOIN + r")?"
    r"(?P<h2>\d{1,2}):(?P<mi2>\d{2})" + _TZ_REQ, re.I)
# September 30, 2026, 04:00 - 11:00 (UTC+8)
_RE_RANGE_NAMED = re.compile(
    r"\b(?P<mon>" + _MONTH_NAMES + r")\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(?P<y>\d{4}))?" + _JOIN +
    r"(?P<h>\d{1,2}):(?P<mi>\d{2})" + _RANGE_SEP + r"(?P<h2>\d{1,2}):(?P<mi2>\d{2})" + _TZ_REQ, re.I)


def find_time_ranges(text: str, ref_ts: int | float | None = None) -> list[tuple[int, int]]:
    """Maintenance windows written as ranges where the offset follows the END time."""
    ref = datetime.fromtimestamp(ref_ts or datetime.now(timezone.utc).timestamp(), timezone.utc)
    out: list[tuple[int, int]] = []
    for rx in (_RE_RANGE_YMD, _RE_RANGE_NAMED):
        for m in rx.finditer(text or ""):
            gd = m.groupdict()
            try:
                month = MONTHS[gd["mon"].lower()] if gd.get("mon") else int(gd["mo"])
                day = int(gd["d"])
                year = int(gd["y"]) if gd.get("y") else _infer_year(month, day, ref)
                if year is None:
                    continue
                sign = -1 if gd["tzsign"] in "-−–" else 1
                tz = timezone(timedelta(hours=sign * (int(gd["tzh"]) + int(gd.get("tzm") or 0) / 60)))
                start = datetime(year, month, day, int(gd["h"]), int(gd["mi"]), tzinfo=tz)
                if gd.get("y2"):
                    end = datetime(int(gd["y2"]), int(gd["mo2"]), int(gd["d2"]), int(gd["h2"]), int(gd["mi2"]), tzinfo=tz)
                else:
                    end = start.replace(hour=int(gd["h2"]), minute=int(gd["mi2"]))
                    if end <= start:
                        end += timedelta(days=1)
            except (ValueError, KeyError):
                continue
            if 0 < (end - start).total_seconds() <= 48 * 3600:
                out.append((int(start.timestamp()), int(end.timestamp())))
    return out


_DURATION_RE = re.compile(
    r"(?:take|last|lasts|lasting|duration(?:\s+is|:)?|estimated(?:\s+to\s+take)?|expected\s+to\s+take|approximately|about|around)"
    r"\s*(?:approximately|about|around|roughly)?\s*(?P<n>\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b",
    re.I)


_DUR_LEAD = (r"(?:take|last|lasts|lasting|duration(?:\s+is|:)?|estimated(?:\s+to\s+take)?|"
             r"expected\s+to\s+take|approximately|about|around)")
_DURATION_WORDS_RE = re.compile(
    _DUR_LEAD + r"\s*(?:approximately|about|around|roughly)?\s*"
    r"(?P<w>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*(?:hours?|hrs?)\b",
    re.I)
# ZZZ spells the window out ("We estimate this will take five hours.", HoYoLAB 46604333) where
# Genshin writes "estimated to take 5 hours" -- both are official notices, both must parse.
_WORD_HOURS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
               "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


def find_duration_hours(text: str) -> float | None:
    """'The update will take approximately 5 hours.' -> 5.0; '...take five hours.' -> 5.0"""
    m = _DURATION_RE.search(text or "")
    if m:
        n = float(m.group("n"))
        return n if 0 < n <= 48 else None
    w = _DURATION_WORDS_RE.search(text or "")
    if w:
        return float(_WORD_HOURS[w.group("w").lower()])
    return None


def discord_ts(ts: int | None, style: str = "F") -> str:
    """<t:unix:style> — Discord renders it in every reader's local timezone."""
    if ts is None:
        return "TBA"
    return f"<t:{int(ts)}:{style}>"


def parse_kuro_time(value: str) -> int | None:
    """Kuro website times: 'YYYY-MM-DD HH:MM:SS' in UTC+8 (documented by RSSHub)."""
    try:
        dt = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return None
    return int(dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp())


def parse_iso(value: str) -> int | None:
    if not value:
        return None
    try:
        v = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None
