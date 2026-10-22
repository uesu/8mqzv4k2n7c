"""Optional news-description timestamp conversion into Discord ``<t:EPOCH:f>`` tokens.

Design invariants:
  * Disabled by default (`NEWS_TIMESTAMPS=0`): callers bypass scanning entirely,
    and calling `convert_news_timestamps(..., enabled=False)` returns input untouched.
  * Render-only: never mutates `Item`, `article_blocks`, raw HTML/JSON/XML, or state.
  * Never touches `gamexpress/timeparse.py` schedule defaults or consumers.
  * Explicit timezone in the source text (including bare `UTC` / `GMT` as `+00:00`)
    always wins over the approved `UTC+8` `"server time"` fallback; expressions with
    neither an explicit supported timezone nor `"server time"` are left unchanged.
  * Date-only expressions, `"After the update"`, approximate times, malformed timezone
    tokens, and invalid or partially resolved ranges are preserved verbatim without
    partial conversion.
  * Non-prose spans (existing Discord tokens, URLs, Markdown links, inline/fenced code)
    are protected verbatim before any prose scan runs.
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone

from .textutil import MARKDOWN_LINK_INSIDE

SERVER_TZ = timezone(timedelta(hours=8))
YEARLESS_MAX_DELTA_DAYS = 180
YEARLESS_MAX_DELTA_S = YEARLESS_MAX_DELTA_DAYS * 86400

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# Unambiguous fixed-offset timezone abbreviations matching gamexpress.timeparse.TZ_OFFSETS.
# Ambiguous abbreviations such as bare "CST", "PT", "ET", or "IST" are deliberately excluded.
_NAMED_TZ_HOURS: dict[str, float] = {
    "UTC": 0.0,
    "GMT": 0.0,
    "PST": -8.0,
    "PDT": -7.0,
    "MST": -7.0,
    "MDT": -6.0,
    "EST": -5.0,
    "EDT": -4.0,
    "JST": 9.0,
    "KST": 9.0,
    "SGT": 8.0,
    "HKT": 8.0,
    "AEST": 10.0,
    "AEDT": 11.0,
    "BST": 1.0,
    "CET": 1.0,
    "CEST": 2.0,
}

_PROTECTED_RE = re.compile(
    r"```[\s\S]*?```"                                       # fenced code block
    r"|`[^`\n]+`"                                           # inline code span
    r"|<t:-?\d+(?::[tTdDfFR])?>"                            # existing Discord timestamp
    r"|<a?:\w{2,32}:\d{15,25}>"                             # custom emoji
    r"|<@!?\d+>|<@&\d+>|<#\d+>"                             # user/role/channel mention
    rf"|\[{MARKDOWN_LINK_INSIDE}\]\(https?://[^)\s]+\)"     # markdown link [label](url)
    r"|<https?://[^>\s]+>"                                  # autolink <https://...>
    r"|https?://[^\s<>()]+"                                 # bare URL
)

_MONTH_PAT = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)

# Greedily consume trailing TZ tokens so malformed/ambiguous forms such as `(UTC+99)`,
# `(CST)`, `(local time)`, `UTC+123`, `UTC+8:5`, or `UTC+foo` cannot backtrack to `None`
# or bare `UTC` and allow a trailing range endpoint to substitute its own timezone.
# Bare range connectors `to` / `until` are excluded so omitted-first-zone ranges like
# `2026/09/28 06:00 to 11:00 (UTC+8)` still keep `tzb` as `None`.
_TZ_PAREN = r"[^()\n]{1,40}"
_TZ_BARE = (
    r"(?:"
    r"(?:UTC|GMT)(?:\s*[+−-]\s*\d+(?::\d+)?)?(?:[A-Za-z0-9_+:/-]|(?<!\.)\.(?=\w))*"
    r"|(?:server|local)\s+time(?![A-Za-z0-9_])"
    r"|(?!(?:to|until)\b)[A-Za-z]{2,5}(?:\s*[+−-]\s*\d+(?::\d+)?)?(?:/[A-Za-z_]+)?(?![A-Za-z0-9_+:-])"
    r")"
)
_TZ_CLAUSE = rf"(?:\s*(?:\(\s*(?P<tzp>{_TZ_PAREN})\s*\)|(?P<tzb>{_TZ_BARE})))?"

# Single-pass candidate matcher: runs ONCE per prose slice in strict O(N) time.
_CANDIDATE_DT_RE = re.compile(
    r"(?:"
    r"(?P<ymd>\d{4}[./-]\d{1,2}[./-]\d{1,2})"
    rf"|(?P<mdy>{_MONTH_PAT}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?)"
    rf"|(?P<dmy>\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_PAT}(?:,?\s+\d{{4}})?)"
    r"|(?P<md>(?<![/.\d-])\d{1,2}/\d{1,2}(?:/\d{4})?(?![/\d]))"
    r")"
    r"(?:\s*(?:,|\bat\b)\s*|\s+)"
    r"(?P<hh>\d{1,2}):(?P<mm>\d{2})(?::(?P<ss>\d{2}))?(?:\s*(?P<ampm>[AaPp]\.?[Mm]\.?))?"
    rf"{_TZ_CLAUSE}",
    re.I,
)

_RANGE_BETWEEN_RE = re.compile(r"^\s*(?:[–—~]|-(?!\d)|\bto\b|\buntil\b)\s*$", re.I)

# Anchored tail matcher checked right after `m.end()` for same-date ranges like:
#   "2026/09/28 06:00 – 11:00 (UTC+8)"
_SAME_DATE_TAIL_RE = re.compile(
    r"(?P<sep>\s*(?:[–—~]|-(?!\d)|\bto\b|\buntil\b)\s*)"
    r"(?P<hh>\d{1,2}):(?P<mm>\d{2})(?::(?P<ss>\d{2}))?(?:\s*(?P<ampm>[AaPp]\.?[Mm]\.?))?"
    rf"{_TZ_CLAUSE}",
    re.I,
)

# Catches non-clock unresolved range tails such as `– TBA`, `– TBD`, `– 2026/09/30`,
# or `– After the Version 3.3 update` so the start half is never converted in isolation.
_UNRESOLVED_RANGE_TAIL_RE = re.compile(
    r"\s*(?:[–—~]|-(?!\d)|\bto\b|\buntil\b)\s*"
    r"(?:\d{4}[./-]\d{1,2}[./-]\d{1,2}\b|\d{1,2}/\d{1,2}/\d{4}\b|TBA\b|TBD\b|permanently\b|indefinitely\b|further\s+notice\b|after\s+the\b)",
    re.I,
)

_APPROX_PREFIX_RE = re.compile(
    r"(?:around|approx(?:\.|imately)?|about|roughly|circa|est(?:\.|imated)?|[~≈])\s*$",
    re.I,
)
_APPROX_WORDS_RE = re.compile(
    r"\b(?:around|approx(?:\.|imately)?|about|roughly|circa|estimated)\b|[~≈]",
    re.I,
)


def _parse_tz(raw: str | None) -> timezone | None:
    """Resolve an explicit timezone string into a fixed ``datetime.timezone``.

    Bare ``UTC`` and ``GMT`` resolve directly to ``timezone.utc`` (zero offset).
    Explicit offsets ``UTC±H[:MM]`` / ``GMT±H[:MM]`` and unambiguous fixed zone names
    take precedence over ``server time`` (``UTC+8``). Ambiguous or malformed tokens
    return ``None``.
    """
    if not raw:
        return None
    cleaned = raw.strip().replace("−", "-")
    low = re.sub(r"\s+", " ", cleaned.lower())
    if low in ("utc", "gmt"):
        return timezone.utc
    m = re.fullmatch(r"(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::(\d{2}))?", low)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        mins = int(m.group(3) or 0)
        if hours > 14 or mins > 59 or (hours == 14 and mins > 0):
            return None
        delta = sign * timedelta(hours=hours, minutes=mins)
        return timezone.utc if delta == timedelta(0) else timezone(delta)
    upper = cleaned.upper()
    if upper in _NAMED_TZ_HOURS:
        hours_f = _NAMED_TZ_HOURS[upper]
        return timezone.utc if hours_f == 0.0 else timezone(timedelta(hours=hours_f))
    if low == "server time":
        return SERVER_TZ
    return None


def _parse_time_parts(gd: dict[str, str | None]) -> tuple[int, int, int] | None:
    hh = int(gd["hh"] or 0)
    mm = int(gd["mm"] or 0)
    ss = int(gd["ss"] or 0)
    ampm = (gd.get("ampm") or "").replace(".", "").upper()
    if mm > 59 or ss > 59:
        return None
    if ampm:
        if not 1 <= hh <= 12:
            return None
        if ampm == "AM":
            hh = 0 if hh == 12 else hh
        else:
            hh = 12 if hh == 12 else hh + 12
    elif not 0 <= hh <= 23:
        return None
    return hh, mm, ss


def _parse_date_parts(gd: dict[str, str | None]) -> tuple[int | None, int, int] | None:
    ymd = gd.get("ymd")
    if ymd:
        parts = re.split(r"[./-]", ymd)
        return int(parts[0]), int(parts[1]), int(parts[2])
    mdy = gd.get("mdy")
    if mdy:
        m = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?$", mdy.strip())
        if not m:
            return None
        month = _MONTHS.get(m.group(1).lower())
        if not month:
            return None
        return (int(m.group(3)) if m.group(3) else None), month, int(m.group(2))
    dmy = gd.get("dmy")
    if dmy:
        m = re.match(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?(?:,?\s+(\d{4}))?$", dmy.strip())
        if not m:
            return None
        month = _MONTHS.get(m.group(2).lower())
        if not month:
            return None
        return (int(m.group(3)) if m.group(3) else None), month, int(m.group(1))
    md = gd.get("md")
    if md:
        parts = md.split("/")
        return (int(parts[2]) if len(parts) == 3 else None), int(parts[0]), int(parts[1])
    return None


def _build_dt(
    year: int | None,
    month: int,
    day: int,
    hms: tuple[int, int, int],
    tz: timezone,
    published_ts: int,
) -> datetime | None:
    """Resolve calendar fields into a timezone-aware ``datetime``.

    * Explicit years (``year is not None``) are always authoritative and ignore
      ``published_ts``.
    * Yearless dates (``year is None``) require ``published_ts > 0`` and resolve to the
      unique calendar year in ``(pub.year - 1, pub.year, pub.year + 1)`` whose timestamp
      lies within ``±YEARLESS_MAX_DELTA_DAYS`` (``±180`` days) of ``published_ts``.
      Recent dates within the past 180 days (e.g. June 1 on an October 21 publication)
      stay in ``pub.year``; Dec/Jan year-boundary dates within ±180 days roll across the
      year boundary; dates outside ``±180`` days return ``None`` rather than guessing.
    """
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    hh, mm, ss = hms
    if year is not None:
        if not 1990 <= year <= 2100:
            return None
        max_day = calendar.monthrange(year, month)[1]
        if day > max_day:
            return None
        return datetime(year, month, day, hh, mm, ss, tzinfo=tz)
    if not published_ts or published_ts <= 0:
        return None
    pub = datetime.fromtimestamp(published_ts, tz)
    for cand_year in (pub.year - 1, pub.year, pub.year + 1):
        if day <= calendar.monthrange(cand_year, month)[1]:
            cand = datetime(cand_year, month, day, hh, mm, ss, tzinfo=tz)
            if abs((cand - pub).total_seconds()) <= YEARLESS_MAX_DELTA_S:
                return cand
    return None


def _resolve_two_date_range_dts(
    d1: tuple[int | None, int, int],
    t1: tuple[int, int, int],
    tz1: timezone,
    d2: tuple[int | None, int, int],
    t2: tuple[int, int, int],
    tz2: timezone,
    published_ts: int,
) -> tuple[datetime, datetime] | None:
    """Resolve ``(dt1, dt2)`` conservatively without repairing inverted yearless ranges.

    * Explicit years on either endpoint remain authoritative.
    * When both endpoints are yearless, each resolves independently within ``±180`` days
      of ``published_ts`` and must satisfy ``dt2 >= dt1`` without adding a year to
      inverted same-season ranges (e.g. September 28 -> September 20).
    * When only the second endpoint omits its year after an explicit first year (e.g.
      ``2026/12/25 10:00 – 01/12 03:59 (UTC+8)``), ``d1[0]`` is tried first and ``d1[0] + 1``
      is allowed solely across a genuine year-end month wrap (``d2[1] < d1[1]``) within
      ``180`` days of ``dt1`` (and within ``±180`` days of ``published_ts`` when present).
    """
    dt1 = _build_dt(d1[0], d1[1], d1[2], t1, tz1, published_ts)
    if dt1 is None:
        return None
    if d2[0] is not None or d1[0] is None:
        dt2 = _build_dt(d2[0], d2[1], d2[2], t2, tz2, published_ts)
        if dt2 is None or int(dt2.timestamp()) < int(dt1.timestamp()):
            return None
        return dt1, dt2

    cand_same = _build_dt(d1[0], d2[1], d2[2], t2, tz2, published_ts)
    if cand_same is not None and int(cand_same.timestamp()) >= int(dt1.timestamp()):
        return dt1, cand_same
    if d2[1] < d1[1]:
        cand_next = _build_dt(d1[0] + 1, d2[1], d2[2], t2, tz2, published_ts)
        if cand_next is not None:
            span_s = int(cand_next.timestamp()) - int(dt1.timestamp())
            pub_ok = (
                not published_ts
                or published_ts <= 0
                or abs((cand_next - datetime.fromtimestamp(published_ts, tz2)).total_seconds()) <= YEARLESS_MAX_DELTA_S
            )
            if 0 <= span_s <= YEARLESS_MAX_DELTA_S and pub_ok:
                return dt1, cand_next
    return None


def _is_approximate(segment: str, match_start: int, matched: str) -> bool:
    prefix = segment[max(0, match_start - 24):match_start]
    return bool(_APPROX_PREFIX_RE.search(prefix) or _APPROX_WORDS_RE.search(matched))


def _resolve_range_tzs(
    gd1: dict[str, str | None],
    gd2: dict[str, str | None],
) -> tuple[timezone, timezone] | None:
    """Resolve endpoint timezones for a two-endpoint range without masking invalid zones.

    * The second endpoint must carry an explicit supported timezone (`tz2`).
    * When the first endpoint genuinely omits a timezone (`raw_tz1 is None`), it inherits
      `tz2` as a shared trailing timezone.
    * When the first endpoint explicitly specifies a timezone (`raw_tz1 is not None`),
      `_parse_tz(raw_tz1)` must succeed on its own; any invalid or ambiguous explicit
      first timezone (e.g. `(UTC+99)`, `(CST)`, `local time`) rejects the whole range.
    """
    raw_tz2 = gd2.get("tzp") or gd2.get("tzb")
    tz2 = _parse_tz(raw_tz2)
    if tz2 is None:
        return None
    raw_tz1 = gd1.get("tzp") or gd1.get("tzb")
    if raw_tz1 is None:
        return tz2, tz2
    tz1 = _parse_tz(raw_tz1)
    if tz1 is None:
        return None
    return tz1, tz2


def _replace_prose_segment(segment: str, published_ts: int) -> str:
    """Single-pass O(N) scan across one unprotected prose slice."""
    if ":" not in segment:
        return segment
    matches = list(_CANDIDATE_DT_RE.finditer(segment))
    if not matches:
        return segment

    edits: list[tuple[int, int, str]] = []
    idx = 0
    total = len(matches)
    while idx < total:
        m1 = matches[idx]
        s1, e1 = m1.span()
        gd1 = m1.groupdict()

        # 1. Two-date range `<dt1> <sep> <dt2>`: both endpoints must resolve cleanly and
        #    satisfy `dt2 >= dt1`, otherwise skip BOTH endpoints so an invalid or partially
        #    qualified range is never half-converted.
        if idx + 1 < total:
            m2 = matches[idx + 1]
            s2, e2 = m2.span()
            between = segment[e1:s2]
            if _RANGE_BETWEEN_RE.match(between):
                if not _is_approximate(segment, s1, segment[s1:e2]):
                    gd2 = m2.groupdict()
                    tz_pair = _resolve_range_tzs(gd1, gd2)
                    d1 = _parse_date_parts(gd1)
                    t1 = _parse_time_parts(gd1)
                    d2 = _parse_date_parts(gd2)
                    t2 = _parse_time_parts(gd2)
                    if tz_pair and d1 and t1 and d2 and t2:
                        tz1, tz2 = tz_pair
                        resolved_pair = _resolve_two_date_range_dts(
                            d1, t1, tz1, d2, t2, tz2, published_ts
                        )
                        if resolved_pair is not None:
                            dt1, dt2 = resolved_pair
                            edits.append(
                                (s1, e2, f"<t:{int(dt1.timestamp())}:f>{between}<t:{int(dt2.timestamp())}:f>")
                            )
                idx += 2
                continue

        # 2. Same-date range `<dt1> <sep> <time2> [tz2]`: either both endpoints resolve cleanly
        #    with `dt2 >= dt1` or neither endpoint converts.
        tail = _SAME_DATE_TAIL_RE.match(segment, e1)
        if tail:
            if not _is_approximate(segment, s1, segment[s1:tail.end()]):
                gdt = tail.groupdict()
                tz_pair = _resolve_range_tzs(gd1, gdt)
                d1 = _parse_date_parts(gd1)
                t1 = _parse_time_parts(gd1)
                t2 = _parse_time_parts(gdt)
                if tz_pair and d1 and t1 and t2:
                    tz1, tz2 = tz_pair
                    dt1 = _build_dt(d1[0], d1[1], d1[2], t1, tz1, published_ts)
                    if dt1 is not None:
                        dt2 = _build_dt(dt1.year, d1[1], d1[2], t2, tz2, published_ts)
                        if dt2 is not None and int(dt2.timestamp()) >= int(dt1.timestamp()):
                            edits.append(
                                (s1, tail.end(), f"<t:{int(dt1.timestamp())}:f>{gdt['sep']}<t:{int(dt2.timestamp())}:f>")
                            )
            idx += 1
            continue

        # 3. Unresolved trailing range clause (`– TBA`, `– TBD`, `– 2026/09/30`, `– After ...`):
        #    never convert the start timestamp in isolation.
        if _UNRESOLVED_RANGE_TAIL_RE.match(segment, e1):
            idx += 1
            continue

        # 4. Standalone datetime with an explicit supported TZ or "server time".
        tz1 = _parse_tz(gd1.get("tzp") or gd1.get("tzb"))
        if tz1 and not _is_approximate(segment, s1, m1.group(0)):
            d1 = _parse_date_parts(gd1)
            t1 = _parse_time_parts(gd1)
            if d1 and t1:
                dt1 = _build_dt(d1[0], d1[1], d1[2], t1, tz1, published_ts)
                if dt1 is not None:
                    edits.append((s1, e1, f"<t:{int(dt1.timestamp())}:f>"))
        idx += 1

    if not edits:
        return segment
    out: list[str] = []
    pos = 0
    for s, e, rep in edits:
        out.append(segment[pos:s])
        out.append(rep)
        pos = e
    out.append(segment[pos:])
    return "".join(out)


def convert_news_timestamps(text: str, *, published_ts: int = 0, enabled: bool = False) -> str:
    """Convert exact news-description datetimes into Discord ``<t:EPOCH:f>`` tokens.

    When ``enabled`` is ``False`` (the default), returns ``text`` directly with zero
    regex scanning or allocation.
    """
    if not enabled or not text:
        return text
    out: list[str] = []
    pos = 0
    for m in _PROTECTED_RE.finditer(text):
        s, e = m.span()
        if s > pos:
            out.append(_replace_prose_segment(text[pos:s], published_ts))
        out.append(m.group(0))
        pos = e
    if pos < len(text):
        out.append(_replace_prose_segment(text[pos:], published_ts))
    return "".join(out)
