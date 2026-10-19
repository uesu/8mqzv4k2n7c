"""Redemption-code sources (all verified live 2026-09-25 unless noted).

  hoyolab       official HoYoLAB game-page module — codes appear ONLY during livestreams
                .../circle/channel/guide/material?game_id=2 -> data.modules[].exchange_group.bonuses[]
  seria         https://hoyo-codes.seria.moe/codes?game=genshin|hkrpg|nap   (redeem-validated OK / NOT_OK)
  humbao        github.com/Hum-Bao/hoyoverse-codes  GENSHIN|HSR|ZZZ.txt  (redeem-validated daily, US region)
  ogc           Open Gacha Codes  https://api.ennead.cc/codes/genshin|starrail|zenless|wuwa
                (NEW 2026: multi-source collector by the ennead author — the only JSON API with
                Wuthering Waves; NOISY: it also lists stale / concatenated entries, so it can only
                CONFIRM a code, never post one alone)
  ennead        https://api.ennead.cc/mihoyo/{genshin|starrail|zenless}/codes  ({active, inactive})
  fandom        MediaWiki API wikitext (same query seria + PromoGacha use); 'valid until' dates
                are parsed — a passed date marks the code expired even under 'Active'
  codehub       PromoGacha's GitHub-hosted data/codes.json — an AGGREGATOR of seria + the wikis
                that never deletes entries: each hit counts as its upstream (CodeHit.origin)
  wuthering.gg  https://wuthering.gg/codes  (HTML table; marks expired codes explicitly)
  x             official tweets (codes are extracted only with explicit code wording)

Every parser also reports codes a source explicitly marks EXPIRED (seria NOT_OK, ennead
`inactive`, fandom expired rows, wuthering.gg "Expired") as CodeHit(expired=True): the
code gate never posts a code that a source calls expired unless an official source or a
redeem-validator vouches for it.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
import time

from ..http import Fetcher
from ..models import CodeHit

log = logging.getLogger("gamexpress.codes")

HOYOLAB_MATERIAL = "https://bbs-api-os.hoyolab.com/community/painter/wapi/circle/channel/guide/material"
SERIA = "https://hoyo-codes.seria.moe/codes"
ENNEAD = "https://api.ennead.cc/mihoyo/{}/codes"
OGC = "https://api.ennead.cc/codes/{}"
FANDOM = ("https://{wiki}.fandom.com/api.php?action=query&prop=revisions&titles={page}"
          "&rvprop=content&rvslots=main&format=json")
WUTHERING_GG = "https://wuthering.gg/codes"
# GitHub-hosted data: raw.githubusercontent.com first, jsDelivr's GitHub mirror as the fallback
CODEHUB = ("gripcrip-blip/codehub", "main", "data/codes.json")
HUMBAO = ("Hum-Bao/hoyoverse-codes", "main", "{}.txt")

# Real codes are 5-20 chars (longest known: WUTHERINGWAVESGIFT = 18). Longer tokens are
# scraper artifacts such as two codes glued together (seen live in the OGC feed).
CODE_SHAPE = re.compile(r"^[A-Z0-9]{5,20}$")
JUNK_CODES = {"TEST", "TESTCODE", "EXAMPLE", "SAMPLE", "CODE", "REDEEM", "EXPIRED", "COPY", "NEW"}

# Which sources share an origin — codes need agreement between DIFFERENT families.
SOURCE_FAMILY = {"ogc": "ennead", "ennead": "ennead"}
VALIDATORS = {"seria", "humbao", "hoyolab"}      # a real redemption attempt / the official module


def family(source: str) -> str:
    return SOURCE_FAMILY.get(source, source)


def sanitize(code: str) -> str:
    code = (code or "").split("/")[0].split(";")[0]
    code = re.sub(r"\[\d+\]", "", code).replace("NEW!", "").replace("Quick Redeem", "")
    code = re.sub(r"[^A-Za-z0-9]", "", code).upper()
    if code in JUNK_CODES:
        return ""
    return code if CODE_SHAPE.match(code) and re.search(r"[A-Z]", code) else ""


def drop_concatenations(hits: list[CodeHit]) -> list[CodeHit]:
    """Remove 'AAAA…BBBB…' entries that are two other codes from the same batch glued
    together (a known collector artifact: XVIZDH2B9WGX + EHVE2TEAFY6O)."""
    codes = {h.code for h in hits}
    out = []
    for h in hits:
        glued = any(h.code.startswith(a) and h.code[len(a):] in codes
                    for a in codes if a != h.code and len(a) < len(h.code))
        if not glued:
            out.append(h)
    return out


# ------------------------------------------------------------------ parsers (pure, tested)
def parse_hoyolab_material(data: dict) -> list[CodeHit]:
    hits = []
    for module in ((data or {}).get("data") or {}).get("modules") or []:
        group = (module or {}).get("exchange_group") or {}
        for bonus in group.get("bonuses") or []:
            code = sanitize(bonus.get("exchange_code") or "")
            if not code:
                continue
            if str(bonus.get("code_status") or "ON").upper() in ("OFF", "EXPIRED", "INVALID"):
                continue
            rewards = [f"×{b.get('bonus_num')}" for b in (bonus.get("icon_bonuses") or []) if b.get("bonus_num")]
            hits.append(CodeHit(code, "hoyolab", rewards or None, verified=True))
    return hits


def parse_seria(data: dict) -> list[CodeHit]:
    hits = []
    for c in (data or {}).get("codes") or []:
        code = sanitize(c.get("code") or "")
        if not code:
            continue
        status = str(c.get("status")).upper()
        if status == "OK":
            hits.append(CodeHit(code, "seria", c.get("rewards") or None, verified=True))
        elif status in ("NOT_OK", "EXPIRED", "INVALID"):
            hits.append(CodeHit(code, "seria", None, expired=True))
    return hits


def parse_ennead(data: dict) -> list[CodeHit]:
    hits = []
    for c in (data or {}).get("active") or []:
        code = sanitize(c.get("code") or "")
        rewards = [r for r in (c.get("rewards") or []) if r and r.lower() != "free items"]
        if code:
            hits.append(CodeHit(code, "ennead", rewards or None))
    for c in (data or {}).get("inactive") or []:
        code = sanitize(c.get("code") or "")
        if code:
            hits.append(CodeHit(code, "ennead", None, expired=True))
    return hits


def parse_ogc(data) -> list[CodeHit]:
    """Open Gacha Codes: [{"code": "...", "rewards": ["Astrite x50", ...]}]  (active only)."""
    hits: list[CodeHit] = []
    seen: set[str] = set()
    for c in data if isinstance(data, list) else []:
        if not isinstance(c, dict):
            continue
        code = sanitize(c.get("code") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        hits.append(CodeHit(code, "ogc", clean_reward_list(c.get("rewards") or []) or None))
    return drop_concatenations(hits)


def clean_reward_list(rewards: list) -> list[str]:
    """['Credit x50,000', 'Credit x50000', 'Unknown reward (77cb..._640...) x100'] -> ['Credit ×50,000']
    (Open Gacha Codes merges several scrapers: it repeats items and leaves unmapped icon hashes)."""
    out: list[str] = []
    seen: set[str] = set()
    for r in rewards:
        text = re.sub(r"\s+", " ", str(r or "")).strip()
        if not text or "File:" in text or re.match(r"unknown\s+(?:reward|item)", text, re.I):
            continue
        text = re.sub(r"\s[x×]\s?(?=[\d,]+$)", " ×", text)
        key = re.sub(r"[^a-z0-9]", "", text.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def parse_humbao(text: str) -> list[CodeHit]:
    """Hum-Bao/hoyoverse-codes: one redeem-validated code per line."""
    hits = []
    for line in (text or "").splitlines():
        code = sanitize(line.strip())
        if code and not line.lstrip().startswith("#"):
            hits.append(CodeHit(code, "humbao", None, verified=True))
    return hits


# ----------------------------------------------------------------------------- expiry dates
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_ISO_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T]+(\d{1,2}):(\d{2}))?")
_NAMED_DATE = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})(?:[, ]+(?:at\s+)?(\d{1,2}):(\d{2}))?")
_NO_EXPIRY = re.compile(r"(?:unknown|indef(?:inite)?|none|n/?a|tba|permanent|\?+|-+)\.?")
_EXPIRED_WORD = re.compile(r"(?:exp(?:ired)?|ended|invalid)\.?")
_WIKI_MARKUP = re.compile(r"<[^>]+>|'{2,3}")


def _us_dst(y: int, m: int, d: int, hh: int) -> bool:
    """US daylight time: 2nd Sunday of March 02:00 -> 1st Sunday of November 02:00 (local)."""
    def nth_sunday(month: int, n: int) -> int:
        return 1 + (6 - calendar.weekday(y, month, 1)) % 7 + 7 * (n - 1)
    return (3, nth_sunday(3, 2), 2) <= (m, d, hh) < (11, nth_sunday(11, 1), 2)


def _tz_offset(text: str, y: int, m: int, d: int, hh: int) -> float:
    if re.search(r"\bPDT\b", text):
        return -7
    if re.search(r"\bPST\b", text):
        return -8
    if re.search(r"\bPT\b|Pacific", text, re.I):
        return -7 if _us_dst(y, m, d, hh) else -8
    if re.search(r"\bE[DS]?T\b|Eastern", text):
        return -4 if _us_dst(y, m, d, hh) else -5
    off = re.search(r"\b(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", text)
    if off:
        sign = 1 if off.group(1) == "+" else -1
        return sign * (int(off.group(2)) + int(off.group(3) or 0) / 60)
    return 0.0                                  # the wiki tables say "All times ... are UTC"


def looks_like_expiry(text: str) -> bool:
    t = _WIKI_MARKUP.sub(" ", text or "").strip().lower()
    return bool(_ISO_DATE.search(t) or _NAMED_DATE.search(t) or _NO_EXPIRY.fullmatch(t)
                or _EXPIRED_WORD.fullmatch(t))


def parse_expiry(text: str, now: float | None = None) -> tuple[bool, int | None]:
    """'2026-09-21' / '2025-08-03 23:59' / 'September 21, 2026 08:59 (PT)' / 'unknown' / 'exp'
    -> (expired?, valid-until unix time or None). A bare date means the END of that day."""
    now = time.time() if now is None else now
    t = _WIKI_MARKUP.sub(" ", text or "").strip()
    low = t.lower()
    if not t or _NO_EXPIRY.fullmatch(low):
        return False, None
    if _EXPIRED_WORD.fullmatch(low):
        return True, None
    m = _ISO_DATE.search(t)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        hh, mi = (int(m[4]), int(m[5])) if m[4] else (23, 59)
    else:
        m = _NAMED_DATE.search(t)
        if not m or m[1][:3].lower() not in _MONTHS:
            return False, None
        y, mo, d = int(m[3]), _MONTHS[m[1][:3].lower()], int(m[2])
        hh, mi = (int(m[4]), int(m[5])) if m[4] else (23, 59)
    if not (2020 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31 and 0 <= hh <= 23 and 0 <= mi <= 59):
        return False, None
    ts = int(calendar.timegm((y, mo, d, hh, mi, 59, 0, 0, 0)) - _tz_offset(t, y, mo, d, hh) * 3600)
    return ts <= now, ts


# ----------------------------------------------------------------------------- fandom wikitext
_NAMED_FIELD = re.compile(r"^\s*[A-Za-z_][\w ]*=")
_LIST_TEMPLATE = re.compile(r"\{\{\s*(?:Item|Card)\s+List\s*\|([^|}]+)", re.I)


def _templates(wikitext: str, name_re: str):
    """(position, body) of every {{Name|...}} template. Nested templates such as
    {{Item List|...}} stay inside the body (a plain regex would stop at their closing braces)."""
    for m in re.finditer(r"\{\{\s*(?:" + name_re + r")\s*\|", wikitext, re.I):
        depth, i = 1, m.end()
        while i < len(wikitext) and depth:
            if wikitext.startswith("{{", i):
                depth, i = depth + 1, i + 2
            elif wikitext.startswith("}}", i):
                depth, i = depth - 1, i + 2
            else:
                i += 1
        if depth == 0:
            yield m.start(), wikitext[m.end():i - 2]


def _split_top(body: str) -> list[str]:
    """Split template fields on '|' that are not inside {{...}} or [[...]]."""
    out, cur, depth, i = [], [], 0, 0
    while i < len(body):
        two = body[i:i + 2]
        if two in ("{{", "[["):
            depth, i = depth + 1, i + 2
            cur.append(two)
            continue
        if two in ("}}", "]]"):
            depth, i = max(0, depth - 1), i + 2
            cur.append(two)
            continue
        if body[i] == "|" and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(body[i])
        i += 1
    out.append("".join(cur))
    return [x.strip() for x in out]


def _wiki_rewards(fields: list[str]) -> str | None:
    for f in fields:
        m = _LIST_TEMPLATE.search(f)
        if m:
            return m.group(1).strip()
    for f in fields:
        if re.search(r"[A-Za-z][^*|]*\*\s*[\d,]+", f) and "{{" not in f and "=" not in f:
            return f.strip()
    return None


def parse_fandom(data: dict, now: float | None = None) -> list[CodeHit]:
    """Fandom wiki code tables (MediaWiki API wikitext).

    * template rows: {{Code Row|CODE[;CODE2...]|server|rewards|discovered|valid-until}} (Genshin)
      and {{Redemption Code Row|CODE|ref=...|server|{{Item List|...}}|discovered|valid-until}}
      (Star Rail / ZZZ). Several codes may share one row.
    * plain wikitable rows (Wuthering Waves): |CODE||server||{{Card List|...}} ... Valid until: <date>
    A row is EXPIRED when it sits under an Expired heading / comment, says 'exp', or its
    valid-until date has passed. Wiki editors often leave dead livestream codes under
    'Active' for days, so the date is what counts."""
    now = time.time() if now is None else now
    pages = ((data or {}).get("query") or {}).get("pages") or {}
    wikitext = ""
    for page in pages.values():
        revs = page.get("revisions") or []
        if revs:
            wikitext = (revs[0].get("slots") or {}).get("main", {}).get("*") or revs[0].get("*") or ""
            break
    if not wikitext:
        return []
    hits: list[CodeHit] = []

    # 1) template rows, honoring <!-- active --> / <!-- expired --> comments and headings
    markers = sorted(
        [(m.start(), "expired" not in m.group(0).lower()) for m in
         re.finditer(r"<!--[^>]*?(active|expired)[^>]*?-->", wikitext, re.I)]
        # (?m)^ not \n: the API returns the page from its first byte, so a page whose very
        # first line is "==Expired Codes==" has no newline in front of it -- the old anchor
        # skipped it and every dead code on that page was read as ACTIVE.
        + [(m.start(), not re.search(r"expired|inactive|invalid", m.group(0), re.I)) for m in
           re.finditer(r"(?m)^=+[^=\n]*(?:active|expired|inactive|invalid)[^=\n]*=+",
                       wikitext, re.I)])
    active = True
    for pos, body in _templates(wikitext, r"(?:Redemption\s+)?Code\s+Row"):
        while markers and markers[0][0] < pos:
            active = markers.pop(0)[1]
        fields = _split_top(body)
        named = {k.strip().lower(): v.strip() for k, _, v in
                 (f.partition("=") for f in fields if _NAMED_FIELD.match(f))}
        positional = [f for f in fields if not _NAMED_FIELD.match(f)]
        if not positional or named.get("notacode", "").lower() == "yes":
            continue
        if any(f.strip().upper() == "CN" for f in positional[1:3]):
            continue                                          # China-only code
        codes = [c for c in (sanitize(x) for x in re.split(r"[;,/]", positional[0])) if c]
        if not codes:
            continue
        # fields: code | server | rewards | discovered | valid-until. Only the 5th field is an expiry:
        # when editors leave it out, the last field is the DISCOVERED date, which must not expire a code.
        tail = next((named[k] for k in ("expiry", "expires", "valid", "until", "end") if named.get(k)),
                    positional[4] if len(positional) > 4 else "")
        date_expired, valid_until = parse_expiry(tail, now) if looks_like_expiry(tail) else (False, None)
        flagged = any(named.get(k, "").lower() in ("yes", "true", "1") for k in ("expired", "ended"))
        rewards = _wiki_rewards(fields)
        for code in codes:
            hits.append(CodeHit(code, "fandom", rewards, expired=(not active) or date_expired or flagged,
                                expires_at=valid_until))

    # 2) plain wikitable rows (Wuthering Waves page)
    if not hits:
        heading = re.search(r"\n=+\s*(?:Expired|Inactive|Invalid)\b[^\n]*", wikitext, re.I)
        expired_from = heading.start() if heading else len(wikitext)
        offset = 0
        for chunk in re.split(r"(\n\|-|\n\|\})", wikitext):
            pos, offset = offset, offset + len(chunk)
            m = re.search(r"(?m)^\|(?![-}+])\s*([^|\n]*?)\s*\|\|", chunk)
            if not m:
                continue
            raw = re.sub(r"'{2,3}|<[^>]+>|\[\[|\]\]", "", m.group(1)).strip(" *")
            if not re.fullmatch(r"[A-Z0-9]{5,20}", raw):     # headers, templates, prose
                continue
            code = sanitize(raw)
            if not code:
                continue
            until = re.search(r"Valid\s+until:?\s*(.+?)(?:'{2,3}|\n|$)", chunk, re.I)
            date_expired, valid_until = parse_expiry(until.group(1), now) if until else (False, None)
            hits.append(CodeHit(code, "fandom", _wiki_rewards([chunk]),
                                expired=pos >= expired_from or date_expired, expires_at=valid_until))
    return hits


def parse_codehub(data: dict, slug: str) -> list[CodeHit]:
    """PromoGacha's data/codes.json is an AGGREGATOR: every entry names where it was copied
    from (hoyo-codes = seria, Fandom Wiki) and entries are never removed. It therefore only
    counts as that upstream source (never as an extra, independent one)."""
    hits = []
    for c in (data or {}).get("codes") or []:
        if c.get("game") != slug:
            continue
        code = sanitize(c.get("code") or "")
        if not code:
            continue
        upstream = str((c.get("source") or {}).get("name") or "").lower()
        # Same rule for every known upstream, so a copy can never be counted as a SECOND
        # independent source: hoyo-codes = seria, a wiki = fandom, Open Gacha Codes /
        # api.ennead.cc = ennead (ogc and ennead are already one family).
        origin = "seria" if "hoyo-codes" in upstream or "seria" in upstream else (
            "fandom" if "fandom" in upstream or "wiki" in upstream else (
                "ennead" if "ennead" in upstream or "open gacha" in upstream
                or "ogc" in upstream else ""))
        hits.append(CodeHit(code, "codehub", c.get("reward") or None, origin=origin))
    return hits


_TR = re.compile(r"<tr\b([^>]*)>(.*?)</tr>", re.I | re.S)
_TD = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)
_LI = re.compile(r"<li\b[^>]*>(.*?)</li>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")


def parse_wuthering_gg(html: str) -> list[CodeHit]:
    """wuthering.gg/codes table rows: code | COPY button (active) or 'Expired' badge | rewards."""
    hits: list[CodeHit] = []
    seen: set[str] = set()
    for attrs, body in _TR.findall(html or ""):
        cells = _TD.findall(body)
        if len(cells) < 2:
            continue
        code = sanitize(_TAGS.sub("", cells[0]).strip())
        if not code or code in seen:
            continue
        row_text = _TAGS.sub(" ", body)
        cls = (re.search(r'class\s*=\s*"([^"]*)"', attrs) or [None, ""])[1].lower()
        if "expired" in cls or re.search(r"\bexpired\b", row_text, re.I):
            expired = True
        elif "active" in cls or re.search(r"\bcopy\b", row_text, re.I):
            expired = False
        else:
            continue                                      # unknown status -> ignore
        seen.add(code)
        rewards = None
        if not expired and len(cells) >= 3:
            items = [re.sub(r"\s+", " ", _TAGS.sub("", li)).strip() for li in _LI.findall(cells[2])]
            rewards = [i for i in items if i] or None
        hits.append(CodeHit(code, "wuthering.gg", rewards, expired=expired))
    return hits


_CODE_CONTEXT = re.compile(r"(?:redemption|redeem|promo|gift|exchange)\s*codes?|\bcodes?\s*[:：]|\bcode\s*>>", re.I)
_TOKEN = re.compile(r"(?<![#@/\w.])([A-Z0-9]{6,20})(?![\w/.])")
_STOP = {
    "YOUTUBE", "TWITCH", "TIKTOK", "HOYOVERSE", "HOYOLAB", "VERSION", "SPECIAL", "PROGRAM",
    "BROADCAST", "PREVIEW", "OFFICIAL", "LIVESTREAM", "REDEEM", "REDEMPTION", "EXPIRE", "EXPIRES",
    "REWARDS", "PRIMOGEMS", "POLYCHROME", "ASTRITE", "GENSHIN", "HONKAI", "ZENLESS", "WUTHERING",
    "WAVES", "ANANTA", "IMPORTANT", "REMINDER", "UPDATE", "ANNIVERSARY", "CONGRATULATIONS",
    "NETEASE", "NEXUS", "ANIMA", "TRAILER", "TEASER", "GIVEAWAY", "WINNERS",
}


def extract_codes_from_text(text: str) -> list[str]:
    """Codes from an OFFICIAL post. Requires explicit code wording; tokens must follow it
    within 400 chars, be ALL-CAPS/digits (6-20), not hashtags/URLs/stop-words."""
    m = _CODE_CONTEXT.search(text or "")
    if not m:
        return []
    window = text[m.start(): m.start() + 400]
    explicit = bool(re.search(r"redemption\s+codes?|redeem\s+codes?", text, re.I))
    out: list[str] = []
    for tok in _TOKEN.findall(window):
        if tok in _STOP or tok in JUNK_CODES or not re.search(r"[A-Z]", tok) or tok.isdigit():
            continue
        if not explicit and not re.search(r"\d", tok):
            continue
        if tok not in out:
            out.append(tok)
    return out[:10]


# ------------------------------------------------------------------ fetchers
class CodeSources:
    """Per-run cache + in-flight de-duplication: every spec is fetched at most once per
    run even when all games prefetch concurrently (codehub's JSON is shared by 4 games)."""

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self._inflight: dict[str, asyncio.Task] = {}

    async def _once(self, key: str, factory):
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(factory())
            self._inflight[key] = task
        return await task

    async def _github_raw(self, repo_ref_path: tuple[str, str, str], arg: str = "", *, source: str,
                          as_json: bool = False):
        repo, ref, path = repo_ref_path
        path = path.format(arg) if "{}" in path else path
        urls = (f"https://raw.githubusercontent.com/{repo}/{ref}/{path}",
                f"https://cdn.jsdelivr.net/gh/{repo}@{ref}/{path}")
        for url in urls:
            if as_json:
                d = await self.fetcher.get_json(url, source=source, retries=1)
            else:
                d = await self.fetcher.get_text(url, source=source, retries=1)
            if d is not None:
                return d
        return None

    async def fetch(self, spec: str) -> list[CodeHit] | None:
        """spec examples: 'hoyolab:2' 'seria:genshin' 'ogc:wuwa' 'ennead:starrail' 'humbao:HSR'
        'fandom:genshin-impact/Promotional_Code' 'codehub:genshin-impact' 'wuthering.gg'.
        None = source unreachable this run."""
        return await self._once(f"spec:{spec}", lambda: self._fetch(spec))

    async def _fetch(self, spec: str) -> list[CodeHit] | None:
        kind, _, arg = spec.partition(":")
        f = self.fetcher
        if kind == "hoyolab":
            d = await f.get_json(HOYOLAB_MATERIAL, source="codes:hoyolab", params={"game_id": arg},
                                 headers={"Origin": "https://www.hoyolab.com", "x-rpc-language": "en-us"})
            return parse_hoyolab_material(d) if d is not None else None
        if kind == "seria":
            d = await f.get_json(SERIA, source="codes:seria", params={"game": arg})
            return parse_seria(d) if d is not None else None
        if kind == "ogc":
            d = await f.get_json(OGC.format(arg), source="codes:ogc")
            return parse_ogc(d) if d is not None else None
        if kind == "ennead":
            d = await f.get_json(ENNEAD.format(arg), source="codes:ennead")
            return parse_ennead(d) if d is not None else None
        if kind == "humbao":
            t = await self._once(f"humbao:{arg}", lambda: self._github_raw(HUMBAO, arg, source="codes:humbao"))
            return parse_humbao(t) if t is not None else None
        if kind == "fandom":
            wiki, _, page = arg.partition("/")
            d = await f.get_json(FANDOM.format(wiki=wiki, page=page or "Redemption_Code"),
                                 source="codes:fandom", headers={"User-Agent": "Game-Express (code monitor)"})
            return parse_fandom(d) if d is not None else None
        if kind == "codehub":
            d = await self._once("codehub", lambda: self._github_raw(CODEHUB, source="codes:codehub", as_json=True))
            return parse_codehub(d, arg) if d is not None else None
        if kind == "wuthering.gg":
            t = await f.get_text(WUTHERING_GG, source="codes:wuthering.gg", retries=1,
                                 headers={"Accept": "text/html,application/xhtml+xml"})
            return parse_wuthering_gg(t) if t is not None else None
        log.warning("unknown code source spec %r", spec)
        return None
