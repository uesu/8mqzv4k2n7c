"""Fifth feature: beta changelog / revision-diff cards for every tracked game.

WHAT IT DOES
    While a version is in beta, community databases publish a new *revision* (v2,
    v3, …) every few days. The first observed beta build posts full Components V2
    kit cards for newly revealed characters and equipment to each game's own channel.
    Later builds compare against bounded readable-kit snapshots and post only changed
    blocks, with inline `~~old~~ → **new**` values and full-kit/revision-diff links.

    It never invents data: a first-seen existing character is a conservative full
    `[CHANGED]` snapshot, an unreadable item is deferred, and a direction tag only
    appears when before/after values and the mechanic wording make it unambiguous.
    Blank hotfix headings probe only known/new IDs; explicit upstream no-change
    headings advance silently without an empty Discord post. Missing current WuWa
    character/weapon details can use Encore's explicitly selected beta API after
    matching resource-version and stable-build checks within the same request budget.
    Its official legacy endpoint is tried only when the primary Encore read is unusable.

DESIGN GUARANTEES
    * Disabled by default: `BETA_ENABLED=1` is an explicit opt-in. Per-game opt-in
      via `BETA_GAMES` (default: the four released games — Honkai: Nexus Anima and
      ANANTA are excluded until they ship).
    * Dedicated destinations: `DISCORD_WEBHOOK_BETA_<GAME>` > `DISCORD_WEBHOOK_BETA`
      > `DISCORD_WEBHOOK_NEWS_<GAME>`. It never falls back to the schedule/codes
      catch-all, so enabling it can never leak into an unrelated channel.
    * Ping is its own switch: `PING_BETA_<GAME>` > `PING_BETA`. Nothing else can
      supply it — `PING_ROLE_ID` is deliberately NOT inherited (config.ping).
    * Bounded and polite: one shared manifest, at most one changelog per game, only
      new/snapshotted IDs on a blank hotfix, a **run-wide** request ledger, and a
      minimum re-poll interval. Deferred work resumes in a later bounded batch.
    * Image lock: a verified splash-art URL resolved for an item is stored and reused.
      A locked image is never re-resolved, so a finished card stops generating
      traffic — the "monitor forever" bug this feature must not have.
    * Idempotent: state records an exact source build and per-batch message cursor.
      A partial delivery resumes the same batch without re-posting, then advances only
      after all deferred items settle; ambiguous outcomes are held.
    * Multi-page delivery: after each successful page, waits exactly five seconds before
      the next page (never after the final page). Tests inject a recorder instead of idling.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from .cards import beta_payloads_with_map
from .config import Game
from .discord import webhook_fingerprint
from .models import BetaCharacter, BetaEquipment, BetaPatch, BetaSectionEntry
from .state import BETA_FORMAT_EPOCH, stable_hash
from .textutil import html_to_text

log = logging.getLogger("gamexpress.beta")

# --------------------------------------------------------------------------- sources
# One tiny JSON file that names, per game, the newest data revision and the ids that
# are new in it. It is the cheapest possible "did anything move?" probe: ~1 KiB.
MANIFEST_URL = "https://static.nanoka.cc/manifest.json"
STATIC_BASE = "https://static.nanoka.cc"
LOCALE = "en"

# Discord accepts multi-container Components V2 beta cards, but the source delivery guide
# requires a calm five-second hand-off after one page succeeds and before the next begins.
# Keep it fixed (rather than another production knob) so every beta destination behaves the
# same way; a Ctx may inject a sleeper for deterministic offline coverage.
BETA_PAGE_GAP_SECONDS = 5

# nanoka namespace -> how that game names its things.
NANOKA_KINDS: dict[str, dict[str, Any]] = {
    "zzz": {
        "character_label": "Agents",
        "weapon": {"detail": "weapon", "listing": "weapon", "label": "W-Engines"},
        "relic": {"detail": "equipment", "listing": "equipment", "label": "Drive Discs"},
        "new_keys": {"character": "character", "weapon": "weapon", "relic": "equipment"},
    },
    "hsr": {
        "character_label": "Characters",
        "weapon": {"detail": "lightcone", "listing": "lightcone", "label": "Light Cones"},
        "relic": {"detail": "relicset", "listing": "relicset", "label": "Relics & Ornaments"},
        "new_keys": {"character": "character", "weapon": "lightcone", "relic": "relicset"},
    },
    "gi": {
        "character_label": "Characters",
        "weapon": {"detail": "weapon", "listing": "weapon", "label": "Weapons"},
        "relic": {"detail": "artifact", "listing": "artifact", "label": "Artifacts"},
        "new_keys": {"character": "character", "weapon": "weapon", "relic": "artifact"},
    },
    "ww": {
        "character_label": "Resonators",
        "weapon": {"detail": "weapon", "listing": "weapon", "label": "Weapons"},
        "relic": {"detail": "echo", "listing": "echo", "label": "Echoes"},
        "new_keys": {"character": "character", "weapon": "weapon", "relic": "echo"},
    },
}

# Wuthering Waves stores element / weapon as bare ints.
WW_ELEMENTS = {1: "Glacio", 2: "Fusion", 3: "Electro", 4: "Aero", 5: "Spectro", 6: "Havoc"}
WW_WEAPONS = {1: "Broadblade", 2: "Sword", 3: "Pistols", 4: "Gauntlets", 5: "Rectifier"}

PLACEHOLDER_NAMES = {
    "",
    "?",
    "-",
    "—",
    "placeholder",
    "unknown",
    "stay tuned",
    "tba",
    "tbd",
    "n/a",
}

# Unresolved backend strings such as `Robin_Skill_Normal_Title` or
# `EquipmentSuit_34300_name` are the database saying it has no localisation yet.
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:_[A-Za-z0-9]+){2,}$")


def _is_key(value: str) -> bool:
    v = (value or "").strip()
    if not v or v.lower() in PLACEHOLDER_NAMES:
        return True
    if v.startswith("Item_") or v.startswith("EquipmentSuit_") or v.startswith("TestKey_"):
        return True
    if v.startswith("UI_") or v.startswith("Skill_") or v.startswith("SpriteOutput/"):
        return True
    return bool(_KEY_RE.match(v) and "_" in v and " " not in v)


def _clean(value: Any) -> str:
    """Collapse a scraped string into clean readable lines (or '' when it is backend noise)."""
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    raw_str = str(value)
    text = re.sub(r"<rt>.*?</rt>", "", raw_str, flags=re.I | re.S)
    text = re.sub(r"<size=\d+>\s*</size>", "", text, flags=re.I)
    text = re.sub(r"<(?:IconMap|Term):[^>]*>", "", text, flags=re.I)
    text = re.sub(
        r"</?(?:color(?:=[^>]*)?|unbreak|u|i|b|s|size(?:=[^>]*)?|te(?:\s+[^>]*)?|"
        r"SapTag(?:=[^>]*)?|align(?:=[^>]*)?|sprite(?:\s+[^>]*)?|ruby|highlight|span(?:\s+[^>]*)?)>",
        "",
        text,
        flags=re.I,
    )
    if _is_key(text):
        return ""
    text = text.replace("\\n", "\n").replace("\r", " ").replace("\xa0", " ")
    text = text.replace("{NON_BREAK_SPACE}", " ")
    text = re.sub(r"\{RUBY_(?:B#[^}]*|E#)\}", "", text, flags=re.I)
    text = re.sub(r"\{LINK#[^}]*\}([^\{]*)\{/LINK\}", r"\1", text)
    text = re.sub(r"\{(?:LINK|N|S)#\d+\}", "", text)
    text = re.sub(
        r"(?:\{LAYOUT_(?!FALLBACK)[A-Z0-9_]+#[^}]*\})+\{LAYOUT_FALLBACK#([^}]*)\}",
        r"\1",
        text,
    )
    text = re.sub(r"\{LAYOUT_[A-Z0-9_]+#([^}]*)\}", r"\1", text)
    text = re.sub(r"\{Cus:Ipt[^}]*\bPC=([A-Za-z]+)[^}]*\}", r"\1", text)
    text = re.sub(r"\{Cus:Ipt[^}]*\}", "", text)
    text = re.sub(r"\{Cus:Sap[^}]*\bP=([^,\s}]+)[^}]*\}", r"\1", text)
    text = re.sub(r"\{Cus:[^}]*\}", "", text)
    text = re.sub(r"\{CAL:[^}]*\}(%?)", lambda m: f"[TBD]{m.group(1)}", text)
    text = re.sub(r"\{param(\d+)(?::[A-Z]\d?[PI]?)?\}", "", text)
    text = re.sub(r"\[\s*\n\s*", "[", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    lines = [ln.lstrip("·• ").rstrip() if ln.strip().startswith("·") else ln.rstrip()
             for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln.strip()).strip()


def _parse_rarity(ns: str, raw: Any, fallback: int | None = 5) -> int | None:
    """Normalize GI/HSR/ZZZ/WW rarity enums and integers into 1..5 stars."""
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, (list, tuple)):
        vals = [r for item in raw if (r := _parse_rarity(ns, item, None)) is not None]
        return max(vals) if vals else fallback
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
        val = int(float(raw))
        if val <= 0:
            return fallback
        if ns == "zzz" and val in (2, 3, 4):
            return val + 1
        return max(1, min(5, val))
    s = str(raw).strip()
    up = s.upper()
    if "ORANGE" in up or "VERYRARE" in up or up.endswith("5"):
        return 5
    if "PURPLE" in up or ("RARE" in up and "SUPER" not in up) or up.endswith("4"):
        return 4
    if "BLUE" in up or up.endswith("3"):
        return 3
    m = re.search(r"(\d)\s*$", s)
    if m:
        return max(1, min(5, int(m.group(1))))
    return fallback


def _lines(value: Any) -> list[str]:
    return [ln.strip() for ln in _clean(value).split("\n") if ln.strip()]


def _display_version(version: str) -> str:
    """`3.4.1+19836465` -> `3.4.1`. The build number is noise on a card header."""
    return str(version or "").split("+", 1)[0].strip() or str(version or "")


def _pct(value: Any, digits: int = 1) -> str:
    try:
        return f"{round(float(value) * 100, digits):g}%"
    except (TypeError, ValueError):
        return ""


def _num(value: Any, digits: int = 4) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    if f == int(f):
        return str(int(f))
    return f"{round(f, digits):g}"


# --------------------------------------------------------------------------- placeholders
# The databases ship skill text with the numbers still in template form:
#   HSR  "Deals #1[i]% Quantum Elation DMG"        -> "Deals 50% Quantum Elation DMG"
#   HSR  "lasts #6[i] turn(s)"                     -> "lasts 3 turn(s)"
# A trailing `%` means the stored value is a ratio (0.5 -> 50); with no `%` the stored
# value is already the number the game shows (5 -> 5 instances).
_UNRESOLVED_RE = re.compile(r"#\{[^{}]*\}")
_PARAM_RE = re.compile(r"#(\d+)\[([A-Za-z0-9]+)\](%?)")

# Base (E0 / C0 / M0 / S0) maximum playable skill levels verified against raw game config tables:
#   * ZZZ: M0 = Lv. 12 combat skills + Core Skill F (tier 7); M3 = +2 (Lv. 14), M5 = +2 (Lv. 16 max), M6 = +0.
#   * GI:  C0 crowned = Lv. 10 (1 for 1-level sprints); C3/C5 = +3 (Lv. 13), raw promote tables run 1..15.
#   * WW:  In-game max = Lv. 10 on the 5 leveled skills (S1–S6 add +0 levels); raw param[0] runs 1..20.
#   * HSR: E0 = Lv. 6 (Basic ATK / Memosprite), Lv. 10 (Skill / Ult / Talent / Elation), Lv. 1 (Technique);
#          E3/E5 = +1 (Lv. 7) / +2 (Lv. 12), while raw skill tables run 1..10 and 1..15.
SKILL_LEVEL = {"zzz": 12, "gi": 10, "ww": 10, "hsr": 10}
BETA_NORM_REV = 2


def _fmt_value(value: Any, kind: str = "i") -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    k = (kind or "i").lower()
    if k == "i":
        if abs(f - round(f)) < 1e-6:
            return str(int(round(f)))
        return f"{round(f, 2):.2f}".rstrip("0").rstrip(".")
    if len(k) == 2 and k[0] == "f" and k[1].isdigit():
        digits = int(k[1])
        if digits == 0 or abs(f - round(f)) < 1e-9:
            return str(int(round(f)))
        return f"{round(f, digits):.{digits}f}".rstrip("0").rstrip(".")
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return f"{round(f, 4):.4f}".rstrip("0").rstrip(".")


def substitute_params(text: str, params: Any) -> tuple[str, bool]:
    """Fill `#N[i]` placeholders. Returns `(text, unresolved)`.

    `unresolved` is True when the database still has a backend string such as
    `#{series_ref_skill_desc:…}` in place of the real text — the card says so instead of
    printing the raw garbage.
    """
    raw = str(text or "")
    if not raw:
        return "", False
    unresolved = bool(_UNRESOLVED_RE.search(raw))
    if unresolved:
        raw = _UNRESOLVED_RE.sub("…", raw)
    if not isinstance(params, (list, tuple)) or not params:
        return re.sub(r"\s{2,}", " ", raw).strip(), unresolved

    def _repl(m: re.Match) -> str:
        idx = int(m.group(1)) - 1
        if idx < 0 or idx >= len(params):
            return m.group(0)
        try:
            value = float(params[idx])
        except (TypeError, ValueError):
            return m.group(0)
        if m.group(3) == "%":
            value *= 100
            return f"{_fmt_value(value, (m.group(2) or 'i').lower())}%"
        return _fmt_value(value, (m.group(2) or "i").lower())

    out = _PARAM_RE.sub(_repl, raw)
    return re.sub(r"\s{2,}", " ", out).strip(), unresolved


def _zzz_param_rows(items: Any, level: int = 12) -> list[dict]:
    """ZZZ skill stat tables (`main` + `growth` x level, `format: '%'` => /100)."""
    rows: list[dict] = []
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"))
        if not name:
            continue
        block = item.get("param")
        if not isinstance(block, dict):
            continue
        for entry in block.values():
            if not isinstance(entry, dict):
                continue
            main, growth = entry.get("main"), entry.get("growth")
            try:
                main_f = float(main)
            except (TypeError, ValueError):
                continue
            try:
                growth_f = float(growth or 0)
            except (TypeError, ValueError):
                growth_f = 0.0
            if not main_f and not growth_f:
                continue          # a table full of zeroes is noise, not data
            value = main_f + growth_f * (max(1, int(level)) - 1)
            fmt = str(entry.get("format") or "")
            text = f"{_fmt_value(value / 100.0, 'f')}%" if fmt == "%" else _fmt_value(value, "f")
            if text:
                rows.append({"name": name, "value": value, "text": text})
            break
    return rows


# --------------------------------------------------------------------------- text diff
_TOKEN_RE = re.compile(r"\w+|[^\s\w]")


def _spans(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(), m.start(), m.end()) for m in _TOKEN_RE.finditer(str(text or ""))]


def _diff_parts(old: str, new: str, keep: int = 4, max_blocks: int | None = None
                ) -> tuple[str, list[tuple[str, str]]]:
    """Word-level `old` -> `new`, returned as (rendered line, changed fragments)."""
    old_s, new_s = str(old or ""), str(new or "")
    if not old_s and not new_s:
        return "", []
    if old_s == new_s:
        return new_s, []
    o_toks, n_toks = _spans(old_s), _spans(new_s)
    matcher = difflib.SequenceMatcher(
        None, [t[0] for t in o_toks], [t[0] for t in n_toks], autojunk=False)
    if matcher.ratio() < 0.45:
        return new_s, []
    opcodes = list(matcher.get_opcodes())
    blocks: list[list[int]] = []
    for pos, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag != "equal":
            blocks.append([i1, i2, j1, j2])
            continue
        # A short run of unchanged words BETWEEN two edits belongs to the same readable
        # replacement. Leading/trailing text remains context outside the marked span.
        prev_change = pos > 0 and opcodes[pos - 1][0] != "equal"
        next_change = pos + 1 < len(opcodes) and opcodes[pos + 1][0] != "equal"
        if (i2 - i1) <= keep and blocks and prev_change and next_change:
            blocks[-1][1], blocks[-1][3] = i2, j2
    if not blocks:
        return new_s, []
    if max_blocks is not None:
        blocks = blocks[:max_blocks]

    def token_start(toks: list[tuple[str, int, int]], pos: int, src: str) -> int:
        return toks[pos][1] if pos < len(toks) else len(src)

    def token_end(toks: list[tuple[str, int, int]], pos: int, src: str) -> int:
        return toks[pos - 1][2] if pos > 0 else 0

    out: list[str] = []
    parts: list[tuple[str, str]] = []
    cursor = 0
    for i1, i2, j1, j2 in blocks:
        new_start = token_start(n_toks, j1, new_s)
        new_end = token_end(n_toks, j2, new_s)
        old_start = token_start(o_toks, i1, old_s)
        old_end = token_end(o_toks, i2, old_s)
        out.append(new_s[cursor:new_start])
        old_part = old_s[old_start:old_end]
        new_part = new_s[new_start:new_end]
        parts.append((old_part, new_part))
        if old_part and new_part:
            out.append(f"~~{old_part}~~ → **{new_part}**")
        elif new_part:
            out.append(f"**{new_part}**")
        elif old_part:
            out.append(f"~~{old_part}~~")
        cursor = new_end
    out.append(new_s[cursor:])
    return "".join(out).strip(), parts

def inline_diff(old: str, new: str, keep: int = 4, max_blocks: int = 6,
                max_len: int = 560) -> str:
    """`old` -> `new` rendered as one line with `~~gone~~ -> **now**` inline.

    The untouched parts of the line are kept (from the new text) so the result still reads
    as the skill it is; only what moved is marked. Falls back to the plain new line when
    the two versions are too different to align honestly.
    """
    text, _parts = _diff_parts(old, new, keep=keep, max_blocks=max_blocks)
    if len(text) <= max_len:
        return text
    # Keep the readable window around the first change instead of a wall of unchanged text.
    anchor = text.find("~~")
    if anchor < 0:
        return text[:max_len].rstrip() + "…"
    start = max(0, anchor - 120)
    head = "…" if start else ""
    return f"{head}{text[start:start + max_len].rstrip()}…"


# Direction rules. A number going DOWN is not automatically a nerf (a cheaper Ultimate or a
# shorter cooldown is a buff), and a number going UP is not automatically a buff. These two
# word lists are the only cases the card is willing to call a direction on; when a changed
# number belongs to neither it is reported as a plain [CHANGED], which is always honest.
_BAD_SUBJECT_RE = re.compile(
    r"\b(?:cost|costs|consume[sd]?|consumption|cooldown|cd|(?:damage|dmg) taken|taken|"
    r"penalt(?:y|ies)|loss|charge time|time|interval|delay|requirement|required|"
    r"max energy|ultimate cost|minimum|threshold)\b", re.I)
_GOOD_SUBJECT_RE = re.compile(
    r"\b(?:dmg|damage|multiplier|ratio|rate|crit|bonus|buff|heal|restor\w+|recover\w+|"
    r"shield|pen(?:etration)?|shred|ignore|resist|mastery|proficiency|amplif\w+|boost|"
    r"atk|attack|hp|def(?:en[cs]e)?|spd|speed|impact)\b", re.I)
# "reduced by 40%" measures the size of a cut, not the size of the thing being cut.
_REDUCTION_RE = re.compile(
    r"\b(?:reduc\w+|decreas\w+|lower\w+|less|down|drop\w*|mitigat\w+|sheds?)\s*"
    r"(?:by|to|of)?\s*$", re.I)


def _direction(label: str, old: float, new: float) -> str:
    """[BUFF] / [NERF] when the wording makes the direction obvious, else [CHANGED]."""
    text = str(label or "").strip()
    matches = list(re.finditer(r"-?\d+(?:\.\d+)?", text))
    # The changed number is normally the final number in this small replacement context;
    # `3rd/4th hit ... reduced by 35%` must not classify on the ordinal.
    number = matches[-1] if matches else None
    before = text[max(0, number.start() - 90):number.start()] if number else text[-90:]
    is_reduction = bool(_REDUCTION_RE.search(before))
    bad = bool(_BAD_SUBJECT_RE.search(text))
    # "DMG taken" is one bad thing, not the good word "DMG" plus the bad word "taken".
    good = bool(_GOOD_SUBJECT_RE.search(_BAD_SUBJECT_RE.sub(" ", text)))
    up = new > old
    if is_reduction and bad:                 # a bigger cut of something bad
        return "[BUFF]" if up else "[NERF]"
    if is_reduction and good and not bad:    # a bigger cut of something good
        return "[NERF]" if up else "[BUFF]"
    if bad:
        return "[NERF]" if up else "[BUFF]"
    if good:
        return "[BUFF]" if up else "[NERF]"
    return "[CHANGED]"


def _numbers(text: str) -> list[float]:
    out: list[float] = []
    for raw in re.findall(r"-?\d+(?:\.\d+)?", str(text or "")):
        try:
            out.append(float(raw))
        except ValueError:
            pass
    return out


def _number_window(text: str) -> str:
    """Words around the first changed number — what that number actually measures."""
    source = str(text or "")
    m = re.search(r"-?\d+(?:\.\d+)?", source)
    if not m:
        return source
    return source[max(0, m.start() - 70):min(len(source), m.end() + 80)]


def _changed_context(text: str, fragment: str) -> str:
    """Context around this particular replacement (not an earlier `3rd` / `E6`)."""
    source, needle = str(text or ""), str(fragment or "")
    pos = source.find(needle)
    if pos < 0:
        return _number_window(source)
    return source[max(0, pos - 90):min(len(source), pos + len(needle) + 90)]


def _change_line(old_frag: str, new_frag: str, label: str = "") -> tuple[str, str]:
    """(rendered change line, tag) for one old -> new fragment pair.

    The untouched words of the line are kept and only what moved is marked, so the card
    still reads as the skill it is: `DMG taken is reduced by ~~30~~ -> **40**%`.
    """
    body, parts = _diff_parts(old_frag, new_frag)
    if not parts:
        return f"**[CHANGED]** {body}", "[CHANGED]"
    tags: list[str] = []
    for old_part, new_part in parts:
        o_nums, n_nums = _numbers(old_part), _numbers(new_part)
        if len(o_nums) == 1 and len(n_nums) == 1 and o_nums[0] != n_nums[0]:
            tags.append(_direction(label or _changed_context(new_frag, new_part), o_nums[0], n_nums[0]))
        else:
            tags.append("[TEXT FIX]")
    # One line can carry several changes: the strongest statement wins, and a direction we
    # cannot reason about never outranks a plain change.
    for candidate in ("[BUFF]", "[NERF]", "[CHANGED]", "[NEW]", "[TEXT FIX]"):
        if candidate in tags:
            tag = candidate
            break
    else:
        tag = "[CHANGED]"
    if not body:
        body = str(new_frag or "")
    return f"**{tag}** {body}", tag


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def _numbered_slot(title: Any) -> tuple[str, str] | None:
    """Numbered progression titles are separate blocks, never fuzzy-renamed siblings."""
    raw = getattr(title, "title", title)
    normal = _norm_title(str(raw or ""))
    match = re.match(
        r"^(mindscape cinema|sequence node|constellation|eidolon|stat bonus|c|e)\s*(\d+)\b",
        normal,
    )
    return (match.group(1), match.group(2)) if match else None


_SINGLE_STAT_NUM_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*(?:%|s\b|%\s*(?:HP|DEF|ATK)\b|\s+(?:HP|DEF|ATK)\b)?\s*$",
    re.I,
)


def _single_stat_float(text: Any) -> float | None:
    """Extract the scalar float when `text` is a single numeric multiplier/cooldown (not `25%+25%*3`)."""
    m = _SINGLE_STAT_NUM_RE.match(str(text or ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _section_lines(entry: Any) -> list[str]:
    """The full prose of one kit block: description, its numbers, its mechanic glossary.

    Builders already render all three into `lines`; `params` / `defs` exist next to them so
    two revisions can be compared value by value. This helper is the one place that knows
    how to flatten a block that arrived with only its structured half.
    """
    lines = [str(ln) for ln in (getattr(entry, "lines", ()) or ()) if str(ln).strip()]
    if lines:
        return lines
    params = [p for p in (getattr(entry, "params", ()) or ()) if isinstance(p, dict)]
    if params:
        rows = " · ".join(f"{p.get('name')} {p.get('text')}" for p in params)
        label = str(getattr(entry, "values_label", "") or "max level")
        lines.append(f"**Values ({label}):** {rows}")
    for name, desc in (getattr(entry, "defs", ()) or ()):
        if name and desc:
            lines.append(f"**({name})** {desc}")
    return lines


def _diff_params(prev: Any, cur: Any) -> list[tuple[str, str]]:
    """(line, tag) for every added / changed skill value."""
    out: list[tuple[str, str]] = []
    prev_map: dict[str, dict] = {}
    for p in (prev or ()):
        if isinstance(p, dict) and p.get("name"):
            prev_map[str(p["name"])] = p
    for p in (cur or ()):
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        was = prev_map.get(name)
        if was is None:
            out.append((f"**[NEW]** {name}: **{p.get('text')}**.", "[NEW]"))
            continue
        if str(was.get("text")) == str(p.get("text")):
            continue
        o_nums, n_nums = _numbers(str(was.get("value"))), _numbers(str(p.get("value")))
        if len(o_nums) == 1 and len(n_nums) == 1 and o_nums[0] != n_nums[0]:
            tag = _direction(name, o_nums[0], n_nums[0])
            verb = "increased" if n_nums[0] > o_nums[0] else "reduced"
            out.append((f"**{tag}** {name} {verb} from "
                        f"~~**{was.get('text')}**~~ → **{p.get('text')}**.", tag))
        else:
            out.append((f"**[CHANGED]** {name}: ~~{was.get('text')}~~ → "
                        f"**{p.get('text')}**.", "[CHANGED]"))
    return out


def _prose_lines(section: Any) -> list[str]:
    """Description lines without the stat-table rows diffed separately by `_diff_params`."""
    params = [row for row in (getattr(section, "params", ()) or ()) if isinstance(row, dict)]
    out: list[str] = []
    for raw in getattr(section, "lines", ()) or ():
        line = str(raw or "").strip()
        if not line:
            continue
        if line.lower().startswith("**skill attributes"):
            continue
        is_param_line = any(
            str(row.get("name") or "").lower() in line.lower()
            and str(row.get("text") or "").lower() in line.lower()
            for row in params
        )
        if not is_param_line:
            out.append(line)
    return out


def _diff_section(prev: Any, cur: Any) -> tuple[list[str], list[str]]:
    """(rendered change lines, tags) for one kit block that exists in both revisions."""
    lines: list[tuple[str, str]] = []
    prev_lines = _prose_lines(prev)
    cur_lines = _prose_lines(cur)
    prose = "\n".join(prev_lines + cur_lines)
    # A numeric table sometimes mirrors the prose exactly.  Showing both would repeat one
    # change; keep the dedicated table only when the prose does not already show that value.
    prev_params = [row for row in (getattr(prev, "params", ()) or ()) if isinstance(row, dict)
                   and str(row.get("text") or "") not in prose]
    cur_params = [row for row in (getattr(cur, "params", ()) or ()) if isinstance(row, dict)
                  and str(row.get("text") or "") not in prose]
    lines.extend(_diff_params(prev_params, cur_params))
    prev_title = str(getattr(prev, "title", "")).strip()
    cur_title = str(getattr(cur, "title", "")).strip()
    if prev_title and cur_title and _norm_title(prev_title) != _norm_title(cur_title):
        lines.append((
            f"**[ADJUSTMENT]** Renamed from ~~{prev_title}~~ → **{cur_title}**.",
            "[ADJUSTMENT]",
        ))
    if str(getattr(prev, "meta", "")) != str(getattr(cur, "meta", "")):
        lines.append((f"**[ADJUSTMENT]** Targeting/type changed from "
                      f"~~{getattr(prev, 'meta', '')}~~ → **{getattr(cur, 'meta', '')}**.",
                      "[ADJUSTMENT]"))
    if str(getattr(prev, "category", "")) != str(getattr(cur, "category", "")):
        lines.append((f"**[ADJUSTMENT]** Reclassified from "
                      f"**{getattr(prev, 'category', '')}** → **{getattr(cur, 'category', '')}**.",
                      "[ADJUSTMENT]"))

    if prev_lines or cur_lines:
        matcher = difflib.SequenceMatcher(None, prev_lines, cur_lines, autojunk=False)
        for opcode, i1, i2, j1, j2 in matcher.get_opcodes():
            if opcode == "equal":
                continue
            if opcode == "replace":
                pairs = list(zip(prev_lines[i1:i2], cur_lines[j1:j2]))
                for old_ln, new_ln in pairs:
                    if difflib.SequenceMatcher(None, old_ln, new_ln).ratio() >= 0.45:
                        lines.append(_change_line(old_ln, new_ln))
                    elif len(old_ln) >= 28 and len(new_ln) >= 28:
                        lines.append((
                            f"**[REWORK]** *Old:* {old_ln} → *New:* {new_ln}",
                            "[REWORK]",
                        ))
                    else:
                        lines.append((f"**[CHANGED]** {new_ln}", "[CHANGED]"))
                for extra in cur_lines[j1 + len(pairs):j2]:
                    lines.append((f"**[NEW]** {extra}", "[NEW]"))
                lines.extend((f"**[CHANGED]** Removed: ~~{old_ln}~~", "[CHANGED]")
                             for old_ln in prev_lines[i1 + len(pairs):i2])
            elif opcode == "insert":
                lines.extend((f"**[NEW]** {new_ln}", "[NEW]") for new_ln in cur_lines[j1:j2])
            elif opcode == "delete":
                lines.extend((f"**[CHANGED]** Removed: ~~{old_ln}~~", "[CHANGED]")
                             for old_ln in prev_lines[i1:i2])

    if not lines:
        return [], []
    tags = [tag for _line, tag in lines]
    return [line for line, _tag in lines], tags

def _skill_slot_prefix(title: str) -> str:
    """Return normalized skill-slot prefix before ':' (e.g. 'special attack', 'ultimate')."""
    raw = str(title or "").strip()
    if ":" not in raw:
        return ""
    head = _norm_title(raw.split(":", 1)[0])
    known = {
        "normal attack", "charged attack", "plunging attack", "elemental skill",
        "elemental burst", "basic atk", "basic attack", "skill", "ultimate",
        "talent", "technique", "elation skill", "special attack", "ex special attack",
        "dodge", "dash attack", "dodge counter", "quick assist", "defensive assist",
        "evasive assist", "assist follow up", "chain attack", "entry skill",
        "core passive", "additional ability", "resonance skill", "resonance liberation",
        "forte circuit", "intro skill", "outro skill",
    }
    return head if head in known else ""


def diff_kits(prev_sections: list, cur_sections: list) -> tuple[list[Any], int]:
    """Compare two revisions of one kit without claiming a direction we cannot establish."""
    prev_list = list(prev_sections or ())
    cur_list = list(cur_sections or ())
    by_title: dict[str, Any] = {}
    for section in prev_list:
        by_title.setdefault(_norm_title(getattr(section, "title", "")), section)

    out: list[BetaSectionEntry] = []
    untouched = 0
    used: set[int] = set()
    for section in cur_list:
        key = _norm_title(getattr(section, "title", ""))
        previous = by_title.get(key)
        if previous is None:
            # A rename inside the same category is a true rename, not a brand-new skill.
            current_slot = _numbered_slot(getattr(section, "title", ""))
            pool = [candidate for candidate in prev_list
                    if id(candidate) not in used
                    and _norm_title(getattr(candidate, "category", ""))
                    == _norm_title(getattr(section, "category", ""))
                    and (not current_slot or _numbered_slot(getattr(candidate, "title", "")) == current_slot)]
            close = difflib.get_close_matches(
                key, [_norm_title(getattr(candidate, "title", "")) for candidate in pool],
                n=1, cutoff=0.75)
            if close:
                previous = next(candidate for candidate in pool
                                if _norm_title(getattr(candidate, "title", "")) == close[0])
            elif not current_slot:
                slot_pfx = _skill_slot_prefix(getattr(section, "title", ""))
                if slot_pfx:
                    same_pfx = [
                        c for c in pool
                        if _skill_slot_prefix(getattr(c, "title", "")) == slot_pfx
                    ]
                    if len(same_pfx) == 1:
                        previous = same_pfx[0]
        if previous is not None:
            used.add(id(previous))

        if previous is None:
            out.append(BetaSectionEntry(
                title=getattr(section, "title", ""), category=getattr(section, "category", ""),
                tag="[NEW]", meta=getattr(section, "meta", ""),
                lines=_section_lines(section), params=[dict(row) for row in getattr(section, "params", ()) or []],
                defs=list(getattr(section, "defs", ()) or []),
                values_label=getattr(section, "values_label", "")))
            continue

        change_lines, tags = _diff_section(previous, section)
        if not change_lines:
            untouched += 1
            continue
        if all(tag == "[TEXT FIX]" for tag in tags):
            marker = "[TEXT FIX]"
        elif all(tag in ("[NEW]", "[TEXT FIX]") for tag in tags) and "[NEW]" in tags:
            marker = "[NEW]"
        elif all(tag == "[ADJUSTMENT]" for tag in tags):
            marker = "[ADJUSTMENT]"
        elif "[REWORK]" in tags:
            marker = "[REWORK]"
        else:
            marker = "[CHANGED]"
        title = str(getattr(section, "title", ""))
        if _norm_title(getattr(previous, "title", "")) != key:
            change_lines.insert(0, f"**[CHANGED]** Renamed from ~~{getattr(previous, 'title', '')}~~ → **{title}**.")
        out.append(BetaSectionEntry(
            title=title, category=str(getattr(section, "category", "")), tag=marker,
            meta=str(getattr(section, "meta", "")), lines=change_lines,
            values_label=str(getattr(section, "values_label", "")),
        ))

    # A removed skill / node matters too.  Do not silently erase it just because the newer
    # export no longer contains that title.
    for previous in prev_list:
        if id(previous) in used:
            continue
        old = _section_lines(previous)
        if not old:
            continue
        out.append(BetaSectionEntry(
            title=str(getattr(previous, "title", "")),
            category=str(getattr(previous, "category", "")), tag="[CHANGED]",
            meta=str(getattr(previous, "meta", "")),
            lines=[f"**[CHANGED]** Removed from this revision: ~~{line}~~" for line in old],
        ))
    return out, untouched

# --------------------------------------------------------------------------- changelog
_ENTRY_RE = re.compile(
    r"\[!\[[^\]]*\]\((?P<img>[^)]*)\)"
    r"(?P<name>[^\]]*)\]\((?P<url>[^)]*)\)"
    r"(?:\s*\[(?:See\s+)?Changes\]\((?P<diff>[^)]*)\))?",
    re.I,
)
_MD_LINK_RE = re.compile(r"(?<!!)\[(?P<text>[^\]]+)\]\((?P<href>https?://[^)\s]+|/[^)\s]+)\)")
# A changelog entry wraps across three lines — the image markdown, a blank separator and
# the display name — so the entry regex has to run over the block, never line by line.
_REV_RE = re.compile(r"^##\s+(?P<label>v?[0-9][^\n|]*?)(?:\s*\|\s*(?P<date>[^\n]+))?\s*$", re.M)
_SECTION_RE = re.compile(r"^###[ \t]+(?P<name>[^\n]+?)[ \t]*$", re.M)
_ITEM_ID_RE = re.compile(r"/(?:agents|characters|resonators|w-engines|weapons|light-cones|"
                         r"lightcones|relics|relicsets|planar-ornaments|drive-discs|artifacts|"
                         r"echoes|echo)/(\d+)")
_GENERIC_CHANGELOG_LABELS = frozenset({
    "full", "full kit", "changes", "see changes", "diff",
    "chinese", "english", "cn", "chs", "cht", "en", "jp", "kr",
    "beta", "view", "details",
})
_GACHABASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}


def _is_generic_changelog_label(value: str) -> bool:
    cleaned = re.sub(r"[\s:()\[\]•·|-]+", " ", str(value or "")).strip().lower()
    return not cleaned or cleaned in _GENERIC_CHANGELOG_LABELS


def _url_has_chs_lang(url: str) -> bool:
    return bool(re.search(r"(?:[?&])lang=(?:chs|cht|zh|cn)\b", str(url or ""), re.I))


def _normalize_gachabase_lang(url: str) -> str:
    u = str(url or "").strip()
    if not u or "gachabase.net" not in u.lower():
        return u
    if re.search(r"([?&])lang=[^&#]*", u, re.I):
        return re.sub(r"([?&])lang=[^&#]*", r"\1lang=en", u, flags=re.I)
    return u


def _slug_display_name(item_path: str) -> str:
    slug = item_path.rstrip("/").rsplit("/", 1)[-1].split("?")[0].strip()
    if not slug or slug.isdigit() or re.fullmatch(r"id-\d+", slug, re.I):
        return ""
    return slug.replace("-", " ").title()


def _normalize_changelog(markdown: str) -> str:
    """Undo the page's escaped newlines so one entry becomes one contiguous match."""
    text = (markdown or "").replace("\r", "\n")
    # gachabase writes the entry's display name behind `\` + newline sequences; the exact
    # number of backslashes varies between page renders, so collapse every run of them.
    text = re.sub(r"\\+[ \t]*\n[ \t]*", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)


class _ChangelogHTML(HTMLParser):
    """Read server-rendered revision headings/cards without executing hydration scripts."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.revisions: list[dict] = []
        self.section = ""
        self.capture = ""
        self.parts: list[str] = []
        self.href = ""
        self.image = ""
        self.hidden = 0
        self.body: list[str] = []
        self.active = False
        self._entries_by_path: dict[str, dict] = {}
        self._pending_diffs: dict[str, str] = {}
        self._has_chs_link = False

    def _link(self, href: str) -> str:
        try:
            url = urljoin(self.base_url, href)
            parsed = urlsplit(url)
            origin = urlsplit(self.base_url)
        except ValueError:
            return ""
        if parsed.scheme not in {"https", "http"} or parsed.netloc != origin.netloc:
            return ""
        return url

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        attrs = dict(attrs)
        if tag in {"h2", "h3", "a"}:
            self.capture, self.parts = tag, []
            self.href = self._link(attrs.get("href", "")) if tag == "a" else ""
            self.image = ""
        elif tag == "img" and self.capture == "a":
            self.image = attrs.get("src", "")

    def handle_data(self, data):
        if self.hidden:
            return
        if self.capture:
            self.parts.append(data)
        elif self.active:
            self.body.append(data)

    def _finish_revision(self):
        if self.active:
            entries = self.revisions[-1]["entries"]
            if not entries and self._pending_diffs:
                for item_path, diff_url in self._pending_diffs.items():
                    q = urlsplit(diff_url).query
                    if "prev=" in q and "cur=" in q and _ITEM_ID_RE.match(item_path):
                        origin = urlsplit(self.base_url)
                        base_root = f"{origin.scheme}://{origin.netloc}" if origin.netloc else ""
                        beta_url = f"{base_root}{item_path}/beta?lang=en" if base_root else ""
                        entries.append({
                            "name": _slug_display_name(item_path),
                            "url": beta_url,
                            "diff": diff_url,
                            "image": "",
                            "section": self.section,
                        })
            joined = " ".join(self.body)
            self.revisions[-1]["no_changes"] = bool(re.search(
                r"\bno tracked changes detected\b", joined, re.I))
            self.revisions[-1]["zh_check_advised"] = (
                self._has_chs_link
                or bool(re.search(r"\b(?:double[- ]check|cross[- ]check)\s+with\s+chinese\b", joined, re.I))
            )
        self.body = []
        self._entries_by_path = {}
        self._pending_diffs = {}
        self._has_chs_link = False

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
            return
        if self.hidden or tag != self.capture:
            return
        text = re.sub(r"\s+", " ", "".join(self.parts)).strip()
        self.capture = ""
        if tag == "h2":
            self._finish_revision()
            match = _REV_RE.fullmatch("## " + text)
            self.section = ""
            self.active = bool(match)
            if match:
                self.revisions.append({"label": match.group("label").strip(),
                    "date": (match.group("date") or "").strip(), "entries": [], "no_changes": False})
        elif tag == "h3":
            self.section = text
        elif tag == "a" and self.active and self.href:
            if _url_has_chs_lang(self.href):
                self._has_chs_link = True
            norm_href = _normalize_gachabase_lang(self.href)
            path = urlsplit(norm_href).path
            entries = self.revisions[-1]["entries"]
            if path.startswith("/diff/"):
                item_path = path.removeprefix("/diff")
                if not _ITEM_ID_RE.match(item_path):
                    return
                existing = self._entries_by_path.get(item_path)
                if existing is not None:
                    if not existing["diff"] or not _url_has_chs_lang(self.href):
                        existing["diff"] = norm_href
                elif item_path not in self._pending_diffs or not _url_has_chs_lang(self.href):
                    self._pending_diffs[item_path] = norm_href
            elif _ITEM_ID_RE.match(path) and path.endswith("/beta"):
                item_path = path.removesuffix("/beta")
                clean_name = "" if _is_generic_changelog_label(text) else text
                existing = self._entries_by_path.get(item_path)
                if existing is not None:
                    if clean_name and (not existing["name"] or existing["name"] == _slug_display_name(item_path)):
                        existing["name"] = clean_name
                    if not _url_has_chs_lang(self.href):
                        existing["url"] = norm_href
                    if self.image and not existing["image"]:
                        existing["image"] = self.image
                    if item_path in self._pending_diffs and not existing["diff"]:
                        existing["diff"] = self._pending_diffs[item_path]
                elif clean_name or text:
                    entry = {
                        "name": clean_name or _slug_display_name(item_path),
                        "url": norm_href,
                        "diff": self._pending_diffs.get(item_path, ""),
                        "image": self.image,
                        "section": self.section,
                    }
                    entries.append(entry)
                    self._entries_by_path[item_path] = entry

    def close(self):
        super().close()
        self._finish_revision()


_SVELTE_ANCHOR = "data:{revisions:["
_SVELTE_REV_META_RE = re.compile(
    r'branch:"([^"]*)",version:"([^"]*)",design_revision:(\d+)(?:,design_timestamp:(\d+))?'
)
_SVELTE_HREF_ID_RE = re.compile(r'/([^/"?]+)/([^/"?]+)/(\d+)(?:[?]|$)')
_SVELTE_DIFF_ID_RE = re.compile(r'cur=([^_&"]+)_([^_&"]+)_(\d+)')
_SVELTE_STATUS_RE = re.compile(r'\bstatus:"(\w+)"')
_SVELTE_HREF_RE = re.compile(r'\bhref:"([^"]*)"')
_SVELTE_DIFF_RE = re.compile(r'\bdiff:"([^"]*)"')
_SVELTE_TEXT_RE = re.compile(r'\btext:"((?:[^"\\]|\\.)*)"')


def _match_balanced(text: str, pos: int, open_ch: str, close_ch: str) -> int:
    """Return index of matching `close_ch` starting one char after `open_ch`, skipping strings."""
    depth, n = 1, len(text)
    i = pos
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    break
                i += 1
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _svelte_top_objects(inner: str) -> list[str]:
    out: list[str] = []
    i, n = 0, len(inner)
    while i < n:
        if inner[i] == "{":
            end = _match_balanced(inner, i + 1, "{", "}")
            out.append(inner[i:end + 1])
            i = end + 1
        else:
            i += 1
    return out


def _parse_sveltekit_revisions(raw: str, base_url: str = "") -> list[dict]:
    """Extract beta revisions from SvelteKit `data:{revisions:[...]}` home/changelog hydration."""
    idx = (raw or "").find(_SVELTE_ANCHOR)
    if idx < 0:
        return []
    tail = raw[idx:]
    end_uses = tail.find(",uses:{")
    seg = tail[:end_uses] if end_uses > 0 else tail[:250_000]

    ts_by_rev: dict[int, int] = {}
    empty_beta_revs: dict[int, str] = {}
    for m in _SVELTE_REV_META_RE.finditer(seg):
        branch, ver, rev_s, ts_s = m.group(1), m.group(2), m.group(3), m.group(4)
        rev_n = int(rev_s)
        if ts_s:
            ts_by_rev[rev_n] = int(ts_s)
        if branch == "beta":
            empty_beta_revs.setdefault(rev_n, ver)

    origin = urlsplit(base_url) if base_url else None
    base_root = f"{origin.scheme}://{origin.netloc}" if (origin and origin.netloc) else ""

    grouped: dict[tuple[str, int], dict[str, dict]] = {}
    has_chs_by_rev: set[int] = set()
    for bm in re.finditer(r"(\w+):\[", seg):
        bucket = bm.group(1)
        if bucket == "revisions":
            continue
        arr_end = _match_balanced(seg, bm.end(), "[", "]")
        for obj in _svelte_top_objects(seg[bm.end():arr_end]):
            st_m = _SVELTE_STATUS_RE.search(obj)
            if st_m is None or st_m.group(1) == "removed":
                continue
            href_m = _SVELTE_HREF_RE.search(obj)
            diff_m = _SVELTE_DIFF_RE.search(obj)
            raw_href = href_m.group(1) if href_m else ""
            raw_diff = diff_m.group(1) if diff_m else ""
            ident: tuple[str, str, int] | None = None
            if raw_href:
                hm = _SVELTE_HREF_ID_RE.search(raw_href)
                if hm:
                    ident = (hm.group(1), hm.group(2), int(hm.group(3)))
            if ident is None and raw_diff:
                dm = _SVELTE_DIFF_ID_RE.search(raw_diff)
                if dm:
                    ident = (dm.group(1), dm.group(2), int(dm.group(3)))
            if ident is None or ident[0] != "beta":
                continue
            _, ver, rev_num = ident
            if _url_has_chs_lang(raw_href) or _url_has_chs_lang(raw_diff):
                has_chs_by_rev.add(rev_num)

            item_path = ""
            if raw_href:
                p_clean = re.sub(r"/beta/\d[^?#]*$", "", urlsplit(raw_href).path).removesuffix("/beta")
                if _ITEM_ID_RE.match(p_clean):
                    item_path = p_clean
            if not item_path and raw_diff:
                p_clean = urlsplit(raw_diff).path.removeprefix("/diff")
                if _ITEM_ID_RE.match(p_clean):
                    item_path = p_clean
            if not item_path:
                continue

            txt_m = _SVELTE_TEXT_RE.search(obj)
            raw_name = txt_m.group(1) if txt_m else ""
            if (
                not raw_name
                or raw_name in ("...", "…")
                or ("{" in raw_name and "}" in raw_name)
                or raw_name.startswith(("武器-", "Weapon-"))
                or _is_generic_changelog_label(raw_name)
                or _is_key(raw_name)
            ):
                raw_name = ""

            full_url = f"{base_root}{item_path}/beta?lang=en" if base_root else f"{item_path}/beta?lang=en"
            full_diff = ""
            if raw_diff:
                raw_d = urljoin(base_root + "/", raw_diff) if (base_root and raw_diff.startswith("/")) else raw_diff
                full_diff = _normalize_gachabase_lang(raw_d)
                if "lang=" not in full_diff and "?" in full_diff:
                    full_diff = f"{full_diff}&lang=en"

            rev_map = grouped.setdefault((ver, rev_num), {})
            existing = rev_map.get(item_path)
            if existing is None:
                rev_map[item_path] = {
                    "name": raw_name or _slug_display_name(item_path),
                    "url": full_url,
                    "diff": full_diff,
                    "image": "",
                    "section": bucket,
                }
            else:
                if raw_name and (not existing["name"] or existing["name"] == _slug_display_name(item_path)):
                    existing["name"] = raw_name
                if full_diff and not existing["diff"]:
                    existing["diff"] = full_diff

    for rev_n, ver in empty_beta_revs.items():
        grouped.setdefault((ver, rev_n), {})

    out: list[dict] = []
    for (ver, rev_num), by_path in sorted(grouped.items(), key=lambda kv: kv[0][1], reverse=True):
        ts = ts_by_rev.get(rev_num, 0)
        date_str = (
            datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
            if ts > 0 else ""
        )
        out.append({
            "label": f"v{ver} Rev. {rev_num}",
            "date": date_str,
            "entries": list(by_path.values()),
            "no_changes": False,
            "zh_check_advised": rev_num in has_chs_by_rev,
        })
    return out


def parse_changelog(markdown: str, *, base_url: str = "") -> list[dict]:
    """Parse a gachabase beta changelog or SvelteKit home page into revisions.

    -> [{'label', 'date', 'entries': [{'name', 'url', 'diff', 'image', 'section'}]}]
    Newest revision first, exactly as the page lists them.
    """
    if re.search(r"<(?:!doctype|html|h2)\b", markdown or "", re.I):
        parser = _ChangelogHTML(base_url)
        parser.feed(markdown)
        parser.close()
        if parser.revisions:
            return parser.revisions
        return _parse_sveltekit_revisions(markdown, base_url=base_url)
    if _SVELTE_ANCHOR in (markdown or ""):
        svelte_revs = _parse_sveltekit_revisions(markdown, base_url=base_url)
        if svelte_revs:
            return svelte_revs
    text = _normalize_changelog(markdown)
    if not text:
        return []
    headers = [m for m in _REV_RE.finditer(text)
               if not (m.group("label") or "").lower().startswith("site")]
    revs: list[dict] = []
    for idx, head in enumerate(headers):
        label = (head.group("label") or "").strip().rstrip("\\").strip()
        if not label:
            continue
        start = head.end()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(text)
        block = text[start:end]
        sections = [(m.start(), m.group("name").strip()) for m in _SECTION_RE.finditer(block)]
        entries: list[dict] = []
        entries_by_path: dict[str, dict] = {}
        pending_diffs: dict[str, str] = {}
        has_chs_link = False
        for m in _ENTRY_RE.finditer(block):
            raw_url = (m.group("url") or "").strip()
            raw_diff = (m.group("diff") or "").strip()
            if _url_has_chs_lang(raw_url) or _url_has_chs_lang(raw_diff):
                has_chs_link = True
            url = _normalize_gachabase_lang(raw_url)
            diff_u = _normalize_gachabase_lang(raw_diff)
            name = re.sub(r"\s+", " ", (m.group("name") or "")).strip(" \n\t\\")
            if _is_generic_changelog_label(name):
                name = ""
            if not name and url:
                name = _slug_display_name(urlsplit(url).path.removesuffix("/beta"))
            if not name and not url:
                continue
            section = ""
            for pos, sname in sections:
                if pos < m.start():
                    section = sname
                else:
                    break
            entry = {
                "name": name,
                "url": url,
                "diff": diff_u,
                "image": (m.group("img") or "").strip(),
                "section": section,
            }
            entries.append(entry)
            item_path = urlsplit(url).path.removesuffix("/beta")
            if item_path:
                entries_by_path[item_path] = entry

        # Also support bilingual/plain-link hotfix rows (e.g. `Chinese: [Changes](...) [Full](...)`
        # + `English: [Changes](...) [Full](...)` without inline image markdown).
        for m in _MD_LINK_RE.finditer(block):
            raw_href = (m.group("href") or "").strip()
            if base_url and raw_href.startswith("/"):
                raw_href = urljoin(base_url, raw_href)
            if _url_has_chs_lang(raw_href):
                has_chs_link = True
            norm_href = _normalize_gachabase_lang(raw_href)
            path = urlsplit(norm_href).path
            link_txt = re.sub(r"\s+", " ", (m.group("text") or "")).strip()
            section = ""
            for pos, sname in sections:
                if pos < m.start():
                    section = sname
                else:
                    break
            if path.startswith("/diff/"):
                item_path = path.removeprefix("/diff")
                if not _ITEM_ID_RE.match(item_path):
                    continue
                existing = entries_by_path.get(item_path)
                if existing is not None:
                    if not existing["diff"] or not _url_has_chs_lang(raw_href):
                        existing["diff"] = norm_href
                elif item_path not in pending_diffs or not _url_has_chs_lang(raw_href):
                    pending_diffs[item_path] = norm_href
            elif _ITEM_ID_RE.match(path) and path.endswith("/beta"):
                item_path = path.removesuffix("/beta")
                clean_name = "" if _is_generic_changelog_label(link_txt) else link_txt
                existing = entries_by_path.get(item_path)
                if existing is not None:
                    if clean_name and (not existing["name"] or existing["name"] == _slug_display_name(item_path)):
                        existing["name"] = clean_name
                    if not _url_has_chs_lang(raw_href):
                        existing["url"] = norm_href
                    if item_path in pending_diffs and not existing["diff"]:
                        existing["diff"] = pending_diffs[item_path]
                else:
                    entry = {
                        "name": clean_name or _slug_display_name(item_path),
                        "url": norm_href,
                        "diff": pending_diffs.get(item_path, ""),
                        "image": "",
                        "section": section,
                    }
                    entries.append(entry)
                    entries_by_path[item_path] = entry

        # The page escapes the `|` separator (`\|`), so the date arrives with a stray
        # backslash hanging off the end.
        date = (head.group("date") or "").strip().rstrip("\\").strip()
        revs.append({
            "label": label,
            "date": date,
            "entries": entries,
            # This is materially different from a temporarily empty / JS-truncated revision:
            # the source has explicitly said that there was nothing to post.
            "no_changes": bool(re.search(r"\bno tracked changes detected\b", block, re.I)),
            "zh_check_advised": (
                has_chs_link
                or bool(re.search(r"\b(?:double[- ]check|cross[- ]check)\s+with\s+chinese\b", block, re.I))
            ),
        })
    return revs


def _entry_id(entry: dict) -> str:
    """Read an item id from a source URL or an explicit internal fallback field.

    Changelog entries carry their id in a public detail URL. Manifest/snapshot fallback entries
    already have the exact id, however, and WuWa's public `character` / `weapon` singular paths
    are intentionally not coupled to the gachabase-style URL regex. Prefer that numeric fallback
    rather than silently dropping a valid manifest candidate.
    """
    explicit = str(entry.get("id") or "").strip()
    if explicit.isdigit():
        return explicit
    m = _ITEM_ID_RE.search(entry.get("url") or "")
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- manifest
async def fetch_manifest(fetcher) -> dict:
    data = await fetcher.get_json(MANIFEST_URL, source="beta-manifest", retries=1)
    return data if isinstance(data, dict) else {}


def manifest_game(manifest: dict, ns: str) -> dict:
    g = (manifest or {}).get(ns)
    return g if isinstance(g, dict) else {}


def manifest_new_ids(manifest: dict, ns: str, key: str) -> set[str]:
    new = manifest_game(manifest, ns).get("new")
    if not isinstance(new, dict):
        return set()
    raw = new.get(key)
    if not isinstance(raw, list):
        return set()
    return {str(i) for i in raw}


# --------------------------------------------------------------------------- detail JSON
async def _detail(
    fetcher,
    ns: str,
    version: str,
    kind: str,
    item_id: str,
    *,
    locale: str = LOCALE,
) -> dict | None:
    if not ns or not version or not item_id:
        return None
    url = f"{STATIC_BASE}/{ns}/{version}/{locale}/{kind}/{item_id}.json"
    data = await fetcher.get_json(url, source="beta-data", retries=1)
    return data if isinstance(data, dict) else None


async def _listing(fetcher, ns: str, version: str, kind: str) -> dict:
    if not ns or not version:
        return {}
    url = f"{STATIC_BASE}/{ns}/{version}/{kind}.json"
    data = await fetcher.get_json(url, source="beta-data", retries=1)
    return data if isinstance(data, dict) else {}


# Encore's frontend uses this beta selector (not included in its OpenAPI export).
ENCORE_API = "https://api-v2.encore.moe/api/en"
ENCORE_BACKUP_API = "https://api.encore.moe/en"


def _encore_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("Content")
    if not isinstance(value, str):
        return ""
    return _clean(html_to_text(_clean(value))[0])


def _dedup_skill_title(category: str, name: str) -> str:
    cat = (category or "").strip()
    nm = re.sub(r"\b([A-Za-z]{3,})\s+\1\b", r"\1", (name or "").strip())
    if not cat:
        return nm
    if nm.lower().startswith(f"{cat.lower()}:"):
        rest = nm[len(cat) + 1:].strip()
        return f"{cat}: {rest}" if rest else cat
    return f"{cat}: {nm}"


def _encore_metadata(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, list) or not value or not isinstance(value[0], dict):
        return None
    row = value[0]
    fields = tuple(str(row.get(key) or "") for key in ("GameVer", "ResVer", "Changelist"))
    if (not all(re.fullmatch(r"\d+(?:\.\d+){1,3}", part) for part in fields[:2])
            or not re.fullmatch(r"\d+", fields[2])):
        return None
    return fields


def _encore_rows(value: Any) -> list[dict]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _encore_properties(data: dict) -> list[str]:
    lines = []
    for prop in _encore_rows(data.get("Properties")):
        if not isinstance(prop, dict):
            continue
        name = _encore_text(prop.get("Name"))
        # Use only an explicitly published Lv.90 value; never relabel a Lv.1 fallback.
        for row in _encore_rows(prop.get("GrowthValues")):
            if not isinstance(row, dict) or str(row.get("Level", row.get("level"))) != "90":
                continue
            value = row.get("Value", row.get("value"))
            if name and value is not None:
                lines.append(f"{name} {_num(value) if isinstance(value, (int, float)) else _encore_text(value)}")
            break
    return lines


def _encore_character(data: dict) -> dict:
    sections = []
    for skill in _encore_rows(data.get("Skills")):
        if not isinstance(skill, dict):
            continue
        name = _encore_text(skill.get("SkillName"))
        if not name:
            continue
        category = _encore_text(skill.get("SkillType")) or "Skill"
        lines = _lines(_encore_text(skill.get("SkillDescribe")))
        attr_rows: list[str] = []
        params = []
        level = SKILL_LEVEL["ww"]
        for row in _encore_rows(skill.get("SkillAttributes")):
            if not isinstance(row, dict):
                continue
            label = _encore_text(row.get("attributeName"))
            values = row.get("values")
            if not label or not isinstance(values, list) or len(values) < level:
                continue
            shown = _encore_text(values[level - 1])
            if not shown:
                continue
            raw_unit = row.get("Description")
            unit_txt = _encore_text(raw_unit)
            if unit_txt and not shown.lower().endswith(unit_txt.lower()):
                sep = " " if (
                    (isinstance(raw_unit, str) and raw_unit[:1].isspace())
                    or (unit_txt[:1].isalpha() and unit_txt.lower() != "s")
                ) else ""
                shown = f"{shown}{sep}{unit_txt}"
            attr_rows.append(f"{label}: {shown}")
            scalar = _single_stat_float(shown)
            if scalar is not None:
                params.append({"name": label, "value": scalar, "text": shown})
        if attr_rows:
            lines.append(f"**Skill Attributes (Lv. {level})**")
            lines.extend(attr_rows)
        if lines:
            sections.append(BetaSectionEntry(title=_dedup_skill_title(category, name),
                                             category=category, lines=lines, params=params,
                                             values_label=f"Lv. {level}" if attr_rows else ""))
    stat_bonuses: dict[str, list[float]] = {}
    stat_bonus_units: dict[str, str] = {}
    for index, node in enumerate(_encore_rows(data.get("SkillTree")), 1):
        if not isinstance(node, dict):
            continue
        name = _encore_text(node.get("PropertyNodeTitle"))
        desc = _encore_text(node.get("PropertyNodeDescribe"))
        if not (name and desc):
            continue
        m_val = re.search(r"([+-]?\d+(?:\.\d+)?)(%?)", desc)
        if name.endswith("+") and m_val and "{" not in desc:
            stat_name = name.rstrip("+").strip()
            try:
                stat_bonuses.setdefault(stat_name, []).append(float(m_val.group(1)))
                stat_bonus_units[stat_name] = m_val.group(2)
                continue
            except ValueError:
                pass
        sections.append(BetaSectionEntry(title=f"Stat Bonus {index} — {name}", category="Stat Bonus", lines=_lines(desc)))
    for node in _encore_rows(data.get("ResonantChain")):
        if not isinstance(node, dict):
            continue
        name = _encore_text(node.get("NodeName"))
        index = node.get("GroupIndex")
        if not isinstance(index, int) or not 1 <= index <= 6:
            continue
        desc = _encore_text(_ww_fill_params(node.get("AttributesDescription"),
                                           node.get("AttributesDescriptionParams")))
        if name and desc:
            sections.append(BetaSectionEntry(title=f"Sequence Node {index} — {name}",
                                             category="Resonance Chain", lines=_lines(desc)))
    stats = _encore_properties(data)
    stats_lines = [f"Lv. 90 — {' · '.join(stats)}"] if stats else []
    if stat_bonuses:
        bonus_parts = [
            f"{s_name} +{_num(sum(vals), 2)}{stat_bonus_units.get(s_name, '')}"
            for s_name, vals in stat_bonuses.items()
        ]
        stats_lines.append("Stat Bonuses: " + " | ".join(bonus_parts))
    return {"element": _encore_text(data.get("ElementName")),
            "role": _encore_text(data.get("WeaponTypeName")), "sections": sections,
            "stats_lines": stats_lines}


def _encore_detail(data: Any, kind: str, item_id: str, revision: tuple[str, str, str]) -> dict | None:
    if not isinstance(data, dict):
        return None
    if str(data.get("Id") if kind == "character" else data.get("ItemId")) != item_id:
        return None
    name = _encore_text(data.get("Name") if kind == "character" else data.get("WeaponName"))
    if not name:
        return None
    try:
        rarity = int(data.get("QualityId"))
    except (TypeError, ValueError):
        return None
    if rarity not in range(1, 6):
        return None
    normalized = {"name": name, "rarity": rarity, "_encore": data,
                  "_encore_revision": list(revision)}
    if kind == "character" and not _encore_character(data)["sections"]:
        return None
    if kind == "weapon" and not _encore_text(data.get("Desc")):
        return None
    return normalized


# --------------------------------------------------------------------------- ZZZ kit
_ZZZ_CATEGORY_LABELS = {
    "basic": "Basic Attack",
    "dodge": "Dodge",
    "assist": "Assist",
    "special": "Special Attack & EX Special Attack",
    "chain": "Chain Attack & Ultimate",
    "core": "Core Passive & Additional Ability",
}


def _zzz_stats(data: dict) -> list[str]:
    stats = data.get("stats") or {}
    if not isinstance(stats, dict):
        return []
    level6 = (data.get("level") or {}).get("6") or {}
    if not isinstance(level6, dict):
        level6 = {}

    # Lv. 60 = Lv.1 base + per-level growth x 59 + the final promotion tier's flat bonus.
    # Cross-checked against the database's own rendered stat block (ZZZ Robin:
    # HP 8754 / ATK 750 / DEF 706; Sunbringer: HP 5736 / DEF 441).
    def at(base_key: str, grow_key: str, level_key: str, scale: float = 10000.0) -> str | None:
        base, grow = stats.get(base_key), stats.get(grow_key)
        extra = level6.get(level_key)
        if base is None or grow is None:
            return None
        try:
            value = float(base) + float(grow) * 59.0 / scale
            if extra is not None:
                value += float(extra)
            # Truncated, not rounded: that is how the database renders the same stat block
            # (Robin HP 8754 / ATK 750 / DEF 706 — rounding would print 8755 / 751).
            return str(int(value))
        except (TypeError, ValueError):
            return None

    # The core-skill track (`extra_level`) grants flat Base ATK (`prop=12101`) plus a
    # secondary bonus at its last node (`extra_level.6`); guard flat vs percent props
    # so `Percent ATK` (`prop=12102`, `%` format) never corrupts flat Base ATK.
    extra_lv = (data.get("extra_level") or {}).get("6") or {}
    if not isinstance(extra_lv, dict):
        extra_lv = {}
    flat_hp = 0.0
    flat_atk = 0.0
    flat_def = 0.0
    crit_bonus = 0.0
    cd_bonus = 0.0
    pen_bonus = 0.0
    impact_bonus = 0.0
    am_bonus = 0.0
    ap_bonus = 0.0
    sharp_bonus = 0.0
    for entry in (extra_lv.get("extra", {}).values()
                  if isinstance(extra_lv.get("extra"), dict) else ()):
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("name") or "").strip().lower()
        fmt_str = str(entry.get("format") or "")
        try:
            prop_id = int(entry.get("prop") or 0)
        except (TypeError, ValueError):
            prop_id = 0
        try:
            value = float(entry.get("value") or 0)
        except (TypeError, ValueError):
            continue
        is_pct = "%" in fmt_str or "percent" in label
        if prop_id == 20101 or label.startswith("crit rate"):
            crit_bonus += value
        elif prop_id == 21101 or label.startswith("crit dmg"):
            cd_bonus += value
        elif prop_id == 23101 or label.startswith("pen ratio"):
            pen_bonus += value
        elif prop_id == 12201 or label in ("impact", "base impact"):
            impact_bonus += value
        elif prop_id == 31401 or label == "anomaly mastery":
            am_bonus += value
        elif prop_id == 31201 or label == "anomaly proficiency":
            ap_bonus += value
        elif prop_id == 21301 or "laceration" in label:
            sharp_bonus += value
        elif prop_id == 12101 or ("atk" in label and not is_pct):
            flat_atk += value
        elif prop_id == 11101 or ("base hp" in label and not is_pct):
            flat_hp += value
        elif prop_id == 13101 or ("base def" in label and not is_pct):
            flat_def += value

    out: list[str] = []
    hp, atk, df = at("hp_max", "hp_growth", "hp_max"), at("attack", "attack_growth", "attack"), \
        at("defence", "defence_growth", "defence")
    if hp is not None and flat_hp:
        try:
            hp = str(int(float(hp) + flat_hp))
        except (TypeError, ValueError):
            pass
    if atk is not None and flat_atk:
        try:
            atk = str(int(float(atk) + flat_atk))
        except (TypeError, ValueError):
            pass
    if df is not None and flat_def:
        try:
            df = str(int(float(df) + flat_def))
        except (TypeError, ValueError):
            pass
    if hp and atk and df:
        out.append(f"Lv. 60 — HP {hp} · ATK {atk} · DEF {df}")
    crit = stats.get("crit")
    if crit is not None:
        try:
            out.append(f"CRIT Rate {round(float(crit) / 100 + crit_bonus / 100, 3):g}%")
        except (TypeError, ValueError):
            pass
    cd = stats.get("crit_damage")
    if cd is not None:
        try:
            p = _pct((float(cd) + cd_bonus) / 10000)
            if p:
                out.append(f"CRIT DMG {p}")
        except (TypeError, ValueError):
            pass
    for label, key, bonus_val in (
        ("Impact", "break_stun", impact_bonus),
        ("Anomaly Mastery", "element_abnormal_power", am_bonus),
        ("Anomaly Proficiency", "element_mystery", ap_bonus),
    ):
        val = stats.get(key)
        if val not in (None, ""):
            try:
                out.append(f"{label} {_num(float(val) + bonus_val)}")
            except (TypeError, ValueError):
                out.append(f"{label} {_num(val)}")
    pr = stats.get("pen_rate")
    if pr is not None:
        try:
            out.append(f"PEN Ratio {_num(float(pr) + pen_bonus / 100, 3)}%")
        except (TypeError, ValueError):
            out.append(f"PEN Ratio {_num(pr)}%")
    sharp = stats.get("sharp_critical_damage")
    if sharp or sharp_bonus:
        try:
            p = _pct((float(sharp or 0) + sharp_bonus) / 10000)
            if p:
                out.append(f"Laceration DMG {p}")
        except (TypeError, ValueError):
            pass
    return out


_ZZZ_ELEMENT_ENUM = {
    "200": "Physical",
    "201": "Fire",
    "202": "Ice",
    "203": "Electric",
    "204": "Wind",
    "205": "Ether",
}
_ZZZ_SPECIALTY_ENUM = {
    "1": "Attack",
    "2": "Stun",
    "3": "Anomaly",
    "4": "Support",
    "5": "Defense",
    "6": "Rupture",
    "7": "Armorer",
}
_ZZZ_SPECIAL_ELEMENT_ENUM = {
    "20001": "Honed Edge",
    "20101": "Genesis Flare",
    "20201": "Frost",
    "20501": "Auric Ink",
    "300": "Lumiflux",
}


def _zzz_character(data: dict) -> dict:
    element = ""
    elem_map = data.get("element_type") if isinstance(data.get("element_type"), dict) else {}
    for k, v in elem_map.items():
        if isinstance(v, str) and v.strip():
            element = v.strip()
            break
        if str(k) in _ZZZ_ELEMENT_ENUM:
            element = _ZZZ_ELEMENT_ENUM[str(k)]
            break
    role = ""
    role_map = data.get("weapon_type") if isinstance(data.get("weapon_type"), dict) else {}
    for k, v in role_map.items():
        if isinstance(v, str) and v.strip():
            role = v.strip()
            break
        if str(k) in _ZZZ_SPECIALTY_ENUM:
            role = _ZZZ_SPECIALTY_ENUM[str(k)]
            break
    camp = ""
    for v in (data.get("camp") or {}).values():
        if isinstance(v, str) and v:
            camp = v
            break

    sections: list[BetaSectionEntry] = []
    skill = data.get("skill") or {}
    if isinstance(skill, dict):
        for key, label in _ZZZ_CATEGORY_LABELS.items():
            block = skill.get(key)
            if not isinstance(block, dict):
                continue
            # The database ships a skill twice: once with its description and once carrying
            # only its stat table. Both halves are the same block, so they are merged by
            # name instead of printing a bare title for the half that has no prose.
            order: list[str] = []
            prose: dict[str, list[str]] = {}
            values: dict[str, list[dict]] = {}
            for item in block.get("description") or []:
                if not isinstance(item, dict):
                    continue
                name = _clean(item.get("name")).rstrip(": \t") or label
                desc = _clean(item.get("desc"))
                if name not in prose:
                    order.append(name)
                    prose[name] = []
                    values[name] = []
                if desc:
                    prose[name] = _lines(desc)
                rows = _zzz_param_rows(item.get("param"), SKILL_LEVEL["zzz"])
                if rows:
                    values[name] = rows
            for name in order:
                if not prose[name] and not values[name]:
                    continue
                sections.append(BetaSectionEntry(
                    title=name, category=label, tag="[NEW]", meta="",
                    lines=prose[name], params=values[name],
                    values_label=f"Lv. {SKILL_LEVEL['zzz']}" if values[name] else "",
                ))
    attack_type = ""
    for v in (data.get("hit_type") or {}).values():
        if isinstance(v, str) and v:
            attack_type = v
            break
    spec_elem = data.get("special_element_type") or {}
    spec_elem_name = ""
    if isinstance(spec_elem, dict):
        spec_elem_name = _clean(spec_elem.get("name"))
        if not spec_elem_name and spec_elem.get("id") is not None:
            spec_elem_name = _ZZZ_SPECIAL_ELEMENT_ENUM.get(str(spec_elem.get("id")), "")

    # Real static.nanoka.cc ZZZ detail JSON stores Core Passive & Additional Ability at
    # top-level `passive.level` (max `level` = Core Skill F), rather than `skill.core`.
    if not any(sec.category == "Core Passive & Additional Ability" for sec in sections):
        passive_obj = data.get("passive")
        passive_lvls = passive_obj.get("level") if isinstance(passive_obj, dict) else None
        if isinstance(passive_lvls, dict) and passive_lvls:
            best_passive = None
            best_rank = (-1, -1)
            for p_key, p_item in passive_lvls.items():
                if not isinstance(p_item, dict):
                    continue
                try:
                    lvl_n = int(p_item.get("level") or 0)
                except (TypeError, ValueError):
                    lvl_n = 0
                try:
                    id_n = int(p_key) if str(p_key).isdigit() else 0
                except (TypeError, ValueError):
                    id_n = 0
                if (lvl_n, id_n) > best_rank and p_item.get("desc"):
                    best_rank = (lvl_n, id_n)
                    best_passive = p_item
            if isinstance(best_passive, dict):
                p_names = best_passive.get("name")
                p_descs = best_passive.get("desc")
                if isinstance(p_descs, str):
                    p_descs = [p_descs]
                if isinstance(p_names, str):
                    p_names = [p_names]
                for p_idx, raw_ds in enumerate(p_descs if isinstance(p_descs, list) else ()):
                    ds = _clean(raw_ds)
                    if not ds:
                        continue
                    default_lbl = "Core Passive" if p_idx == 0 else "Additional Ability"
                    raw_nm = (
                        _clean(p_names[p_idx]).rstrip(": \t")
                        if isinstance(p_names, list) and p_idx < len(p_names)
                        else ""
                    )
                    sections.append(BetaSectionEntry(
                        title=raw_nm or default_lbl,
                        category="Core Passive & Additional Ability",
                        tag="[NEW]", meta="", lines=_lines(ds),
                    ))

    for sec in sections:
        if any("[TBD]" in ln for ln in sec.lines):
            sec.lines.append(
                "-# ⚠️ [TBD] marks an unresolved client formula placeholder in this beta build."
            )

    talent = data.get("talent") or {}
    if isinstance(talent, dict):
        for level in sorted(talent, key=lambda k: int(k) if str(k).isdigit() else 0):
            item = talent[level]
            if not isinstance(item, dict):
                continue
            desc = _clean(item.get("desc"))
            if not desc:
                continue
            t_name = _clean(item.get("name"))
            if t_name and (
                _is_key(t_name)
                or t_name.lower() == f"mindscape cinema {level}"
            ):
                t_name = ""
            t_lines = _lines(desc)
            if any("[TBD]" in ln for ln in t_lines):
                t_lines.append(
                    "-# ⚠️ [TBD] marks an unresolved client formula placeholder in this beta build."
                )
            sections.append(BetaSectionEntry(
                title=f"Mindscape Cinema {level}" + (f" — {t_name}" if t_name else ""),
                category="Mindscape Cinema",
                tag="[NEW]", meta="", lines=t_lines,
            ))

    pot_detail = data.get("potential_detail")
    if isinstance(pot_detail, dict) and pot_detail:
        best_pot_by_name: dict[str, tuple[int, dict]] = {}
        pot_order: list[str] = []
        for pot_id in sorted(pot_detail, key=lambda k: int(k) if str(k).isdigit() else 0):
            p_entry = pot_detail[pot_id]
            if not isinstance(p_entry, dict):
                continue
            p_desc = _clean(p_entry.get("desc"))
            if not p_desc:
                continue
            p_name = _clean(p_entry.get("name")) or "Potential"
            try:
                p_lvl = int(p_entry.get("level") or 0)
            except (TypeError, ValueError):
                p_lvl = 0
            if p_name not in best_pot_by_name:
                pot_order.append(p_name)
                best_pot_by_name[p_name] = (p_lvl, p_entry)
            elif p_lvl >= best_pot_by_name[p_name][0]:
                best_pot_by_name[p_name] = (p_lvl, p_entry)
        for p_name in pot_order:
            _, p_entry = best_pot_by_name[p_name]
            p_lines = _lines(_clean(p_entry.get("desc")))
            if any("[TBD]" in ln for ln in p_lines):
                p_lines.append(
                    "-# ⚠️ [TBD] marks an unresolved client formula placeholder in this beta build."
                )
            lvl_show = _clean(p_entry.get("level_show_name"))
            sections.append(BetaSectionEntry(
                title=f"Potential: {p_name}" if p_name != "Potential" else (lvl_show or "Potential"),
                category="Potential Awakening",
                tag="[NEW]", meta=lvl_show, lines=p_lines,
            ))
    return {
        "rarity": _parse_rarity("zzz", data.get("rarity"), 5),
        "element": element,
        "special_element": spec_elem_name,
        "role": role,
        "camp": camp,
        "attack_type": attack_type,
        "stats_lines": _zzz_stats(data),
        "sections": sections,
    }


# --------------------------------------------------------------------------- HSR kit
def _hsr_stats(data: dict) -> list[str]:
    stats = data.get("stats") or {}
    last = None
    if isinstance(stats, dict):
        for key in sorted(stats, key=lambda k: int(k) if str(k).isdigit() else -1):
            entry = stats[key]
            if isinstance(entry, dict) and "attack_base" in entry:
                last = entry
    if not last:
        return []

    def at(base_key: str, add_key: str, level: int = 80) -> str | None:
        base, add = last.get(base_key), last.get(add_key)
        if base is None or add is None:
            return None
        try:
            return str(int(round(float(base) + float(add) * (level - 1))))
        except (TypeError, ValueError):
            return None

    out: list[str] = []
    hp, atk, df = at("hp_base", "hp_add"), at("attack_base", "attack_add"), at("defence_base", "defence_add")
    if hp and atk and df:
        out.append(f"Lv. 80 — HP {hp} · ATK {atk} · DEF {df}")
    spd = last.get("speed_base")
    if spd is not None:
        out.append(f"SPD {_num(spd)}")
    energy = data.get("sp_need")
    if energy not in (None, "", 0):
        out.append(f"Max Energy {_num(energy)}")
    for label, key in (("CRIT Rate", "critical_chance"), ("CRIT DMG", "critical_damage")):
        val = last.get(key)
        if val is not None:
            p = _pct(val, 2)
            if p:
                out.append(f"{label} {p}")
    bonus: dict[str, float] = {}
    points = data.get("points") or {}
    for node in (points.values() if isinstance(points, dict) else ()):
        for tier in (node.values() if isinstance(node, dict) else ()):
            if not isinstance(tier, dict):
                continue
            for add in tier.get("status_add_list") or []:
                if not isinstance(add, dict):
                    continue
                name = str(add.get("name") or "")
                try:
                    bonus[name] = bonus.get(name, 0.0) + float(add.get("value") or 0)
                except (TypeError, ValueError):
                    pass
    if bonus:
        parts = []
        for name, total in bonus.items():
            if name in ("CRIT Rate", "CRIT DMG"):
                parts.append(f"{name} +{_pct(total, 3)}")
            else:
                parts.append(f"{name} +{_num(total, 3)}")
        out.append("Trace Stat Bonuses: " + " | ".join(parts))
    return out


_HSR_SKILL_TAGS = {
    "SingleAttack": "Single Target",
    "Blast": "Blast",
    "AoEAttack": "AoE",
    "AoE": "AoE",
    "Support": "Support",
    "Enhance": "Enhance",
    "Restore": "Restore",
    "Defence": "Defence",
    "SingleHeal": "Single Heal",
    "AoEHeal": "AoE Heal",
    "Maze": "Technique",
    "Passive": "Passive",
}


def _hsr_meta(tag_value: str) -> str:
    raw = str(tag_value or "").strip()
    return _HSR_SKILL_TAGS.get(raw, re.sub(r"(?<!^)(?=[A-Z])", " ", raw).title())


def _hsr_skill_cap(item: dict) -> int:
    """E0 playable skill-level cap per HSR skill category (both EN and CN labels)."""
    tname = str(item.get("type_name") or "").strip().lower()
    raw_type = str(item.get("type") or "").strip().lower()
    tag = str(item.get("tag") or "").strip().lower()
    if (
        "technique" in tname
        or "秘技" in tname
        or "大世界" in tname
        or raw_type.startswith("maze")
        or tag.startswith("maze")
    ):
        return 1
    if (
        "basic" in tname
        or "memosprite" in tname
        or "普攻" in tname
        or "忆灵" in tname
        or raw_type in ("normal", "memospriteskill", "memospritetalent")
    ):
        return 6
    return SKILL_LEVEL["hsr"]


def _hsr_skill_params_with_level(item: dict) -> tuple[Any, int | None]:
    """The skill's parameters capped at its playable E0 max level (never unobtainable Lv. 10/15)."""
    levels = item.get("level")
    if isinstance(levels, dict) and levels:
        cap = _hsr_skill_cap(item)
        num_keys = [(int(k), k) for k in levels if str(k).isdigit()]
        if num_keys:
            capped = [(n, k) for n, k in num_keys if n <= cap]
            chosen_num, chosen_key = max(capped) if capped else min(num_keys)
            top = levels.get(chosen_key)
            if isinstance(top, dict) and isinstance(top.get("param_list"), list):
                return top["param_list"], chosen_num
        top_key = sorted(levels, key=lambda k: int(k) if str(k).isdigit() else -1)[-1]
        top = levels[top_key]
        if isinstance(top, dict) and isinstance(top.get("param_list"), list):
            lvl_n = int(top_key) if str(top_key).isdigit() else None
            return top["param_list"], lvl_n
    return item.get("param_list"), None


def _hsr_skill_params(item: dict) -> Any:
    """The skill's own numbers, capped at its playable E0 max level."""
    params, _ = _hsr_skill_params_with_level(item)
    return params


def _hsr_character(data: dict) -> dict:
    sections: list[BetaSectionEntry] = []
    skills = data.get("skills") or {}
    seen_glossary: set[str] = set()

    def collect_extras(extra: Any) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for _eid, entry in (extra.items() if isinstance(extra, dict) else ()):
            if not isinstance(entry, dict):
                continue
            nm = _clean(entry.get("name"))
            ds = " ".join(_clean(entry.get("desc")).split())
            if not (nm and ds):
                continue
            norm = nm.lower().strip()
            if norm in seen_glossary:
                continue
            seen_glossary.add(norm)
            found.append((nm, ds))
        return found

    def render(text: str, params: Any) -> tuple[list[str], bool]:
        filled, unresolved = substitute_params(text, params)
        pending = bool(_PARAM_RE.search(filled))
        lines = [ln for ln in _lines(filled) if not (unresolved and ln == "…")]
        if pending:
            lines.append("-# ⚠️ This revision does not publish the numbers for this "
                         "effect yet — the raw database markers are shown as-is.")
        return lines, unresolved

    for skill_id in sorted((skills or {}), key=lambda k: str(k)):
        item = skills[skill_id]
        if not isinstance(item, dict):
            continue
        tag = str(item.get("type_name") or "").strip()
        name = _clean(item.get("name"))
        sk_params, sk_lvl = _hsr_skill_params_with_level(item)
        lines, unresolved = render(item.get("desc"), sk_params)
        glossary = collect_extras(item.get("extra"))
        if not name and not lines and not glossary:
            continue
        if unresolved:
            lines.append("-# ⚠️ This revision's data still holds an unresolved backend "
                         "placeholder here — the full effect is not readable yet.")
        lines.extend(f"**({nm})** {ds}" for nm, ds in glossary)
        sections.append(BetaSectionEntry(
            title=f"{tag}: {name}" if tag else name, category="Skills",
            tag="[NEW]", meta=_hsr_meta(item.get("tag")), lines=lines, defs=glossary,
            values_label=f"Lv. {sk_lvl}" if (sk_lvl and sk_lvl > 1 and sk_params) else "",
        ))

    points = data.get("points") or {}
    for node_key in sorted((points or {}), key=lambda k: (len(str(k)), str(k))):
        node = points[node_key]
        if not isinstance(node, dict):
            continue
        for tier in node.values():
            if not isinstance(tier, dict):
                continue
            name = _clean(tier.get("point_name"))
            lines, unresolved = render(tier.get("point_desc"), tier.get("param_list"))
            if not lines:
                break
            glossary = collect_extras(tier.get("extra"))
            if unresolved:
                lines.append("-# ⚠️ Unresolved backend placeholder in this revision's data.")
            lines.extend(f"**({nm})** {ds}" for nm, ds in glossary)
            sections.append(BetaSectionEntry(
                title=name or "Trace", category="Traces", tag="[NEW]", meta="",
                lines=lines, defs=glossary,
            ))
            break

    unique = data.get("unique") or {}
    for ukey in sorted((unique or {}), key=str):
        item = unique[ukey]
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"))
        lines, unresolved = render(item.get("desc"), _hsr_skill_params(item))
        glossary = collect_extras(item.get("extra"))
        if not (name or lines or glossary):
            continue
        if unresolved:
            lines.append("-# ⚠️ Unresolved backend placeholder in this revision's data.")
        lines.extend(f"**({nm})** {ds}" for nm, ds in glossary)
        sections.append(BetaSectionEntry(
            title=name or "Exclusive Effect", category="Exclusive Effect", tag="[NEW]",
            meta=_hsr_meta(item.get("tag")), lines=lines, defs=glossary,
        ))

    ranks = data.get("ranks") or {}
    for level in sorted(ranks, key=lambda k: int(k) if str(k).isdigit() else 0):
        item = ranks[level]
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"))
        lines, unresolved = render(item.get("desc"), item.get("param_list"))
        glossary = collect_extras(item.get("extra"))
        if not (name or lines or glossary):
            continue
        if unresolved:
            lines.append("-# ⚠️ Unresolved backend placeholder in this revision's data — "
                         "the enhanced effect is not readable yet.")
        lines.extend(f"**({nm})** {ds}" for nm, ds in glossary)
        sections.append(BetaSectionEntry(
            title=f"E{level}" + (f" — {name}" if name else ""), category="Eidolons",
            tag="[NEW]", meta="", lines=lines, defs=glossary,
        ))

    return {
        "rarity": _parse_rarity("hsr", data.get("rarity"), 5),
        "element": str(data.get("damage_type") or "").strip(),
        "role": str(data.get("base_type") or "").strip(),
        "camp": "",
        "stats_lines": _hsr_stats(data),
        "sections": sections,
        "mechanic_defs": [],
    }

# --------------------------------------------------------------------------- GI kit
_GI_WEAPON_LABELS = {
    "WEAPON_SWORD_ONE_HAND": "Sword",
    "WEAPON_CLAYMORE": "Claymore",
    "WEAPON_POLE": "Polearm",
    "WEAPON_CATALYST": "Catalyst",
    "WEAPON_BOW": "Bow",
}


def _gi_character(data: dict) -> dict:
    sections: list[BetaSectionEntry] = []
    info = data.get("chara_info") or {}
    for skill in data.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        name = _clean(skill.get("name"))
        desc = _clean(skill.get("desc"))
        if not (name or desc):
            continue
        promote = skill.get("promote") or {}
        rows: list[str] = []
        param_rows: list[dict] = []
        top_key = "1"
        shown_level = SKILL_LEVEL["gi"]
        if isinstance(promote, dict) and promote:
            lvl_entries = [
                (int(v["level"]), k)
                for k, v in promote.items()
                if isinstance(v, dict) and str(v.get("level") or "").isdigit()
            ]
            if lvl_entries:
                capped_lv = [(lv, k) for lv, k in lvl_entries if lv <= SKILL_LEVEL["gi"]]
                if capped_lv:
                    shown_level, top_key = max(capped_lv)
                else:
                    shown_level, top_key = min(lvl_entries)
                    if len(lvl_entries) == 1 and top_key == str(SKILL_LEVEL["gi"]):
                        shown_level = SKILL_LEVEL["gi"]
            else:
                num_keys = [(int(k), k) for k in promote if str(k).isdigit()]
                if num_keys:
                    capped_k = [(n, k) for n, k in num_keys if n <= SKILL_LEVEL["gi"]]
                    shown_level, top_key = max(capped_k) if capped_k else min(num_keys)
        top = promote.get(top_key) if isinstance(promote, dict) else None
        if isinstance(top, dict):
            params = top.get("param") or []
            for spec in top.get("desc") or []:
                label, separator, template = str(spec).partition("|")
                label = label.strip()
                if not label or not separator:
                    continue
                values: list[tuple[int, float, str]] = []

                def fill(match: re.Match, params=params, values=values) -> str:
                    idx = int(match.group(1)) - 1
                    if idx < 0 or idx >= len(params):
                        return match.group(0)
                    try:
                        raw = float(params[idx])
                    except (TypeError, ValueError):
                        return match.group(0)
                    is_percent = "P" in str(match.group(2) or "").upper()
                    shown = _pct(raw) if is_percent else _num(raw)
                    values.append((idx, raw, shown))
                    return shown

                rendered = re.sub(r"\{param(\d+):([^}]*)\}", fill, template)
                rendered = re.sub(r"\s*\+\s*", " + ", rendered).strip()
                if not values:
                    continue
                rows.append(f"{label}: {rendered}")
                # The first value keeps a stable, human-readable key for a compact numeric
                # diff; the complete rendered row still carries every value/text fragment.
                idx, raw, shown = values[0]
                param_rows.append({"name": label, "value": raw, "text": shown})
        sections.append(BetaSectionEntry(
            title=name or "Talent", category="Talents", tag="[NEW]", meta="",
            lines=_lines(desc) + rows, params=param_rows,
            values_label=f"Lv. {shown_level}" if (param_rows and shown_level > 1) else "",
        ))

    for passive in data.get("passives") or []:
        if not isinstance(passive, dict):
            continue
        name = _clean(passive.get("name"))
        if not name:
            continue
        sections.append(BetaSectionEntry(
            title=name, category="Passives", tag="[NEW]", meta="",
            lines=_lines(passive.get("desc")),
        ))

    for idx, con in enumerate(data.get("constellations") or [], start=1):
        if not isinstance(con, dict):
            continue
        name = _clean(con.get("name"))
        if not name:
            continue
        sections.append(BetaSectionEntry(
            title=f"C{idx} — {name}", category="Constellations", tag="[NEW]", meta="",
            lines=_lines(con.get("desc")),
        ))

    stats: list[str] = []
    for label, key in (("CRIT Rate", "crit_rate"), ("CRIT DMG", "crit_dmg")):
        val = data.get(key)
        if val is not None:
            p = _pct(val, 2)
            if p:
                stats.append(f"{label} {p}")
    em = data.get("elemental_mastery")
    if em:
        stats.append(f"Elemental Mastery {_num(em)}")
    title = _clean(info.get("title"))
    constel = _clean(info.get("constellation"))
    sub = " · ".join(x for x in (title, (f"Constellation: {constel}" if constel else "")) if x)

    return {
        "rarity": _parse_rarity("gi", data.get("rarity"), 5),
        "element": str(data.get("element") or "").strip(),
        "role": _GI_WEAPON_LABELS.get(str(data.get("weapon") or ""), ""),
        "camp": "",
        "subtext": f"-# {sub}" if sub else "",
        "stats_lines": stats,
        "sections": sections,
    }


# --------------------------------------------------------------------------- WW kit
def _ww_fill_params(text: Any, params: Any) -> str:
    """Wuthering Waves uses `{0}` placeholders (zero-based) in chain text."""
    values = list(params or []) if isinstance(params, (list, tuple)) else []

    def replace(match: re.Match) -> str:
        try:
            idx = int(match.group(1))
        except (TypeError, ValueError):
            return match.group(0)
        return str(values[idx]) if 0 <= idx < len(values) else match.group(0)

    return re.sub(r"\{(\d+)\}", replace, str(text or ""))


def _ww_character(data: dict) -> dict:
    if "_encore" in data:
        built_enc = _encore_character(data["_encore"])
        built_enc.setdefault("rarity", _parse_rarity("ww", data.get("rarity"), 5))
        return built_enc
    sections: list[BetaSectionEntry] = []
    stat_bonuses: dict[str, list[float]] = {}
    stat_bonus_units: dict[str, str] = {}
    stat_bonus_counts: dict[str, int] = {}

    trees = data.get("skill_trees") or {}
    for node_key in sorted((trees or {}), key=lambda k: int(k) if str(k).isdigit() else 0):
        node = trees[node_key]
        if not isinstance(node, dict):
            continue
        skill = node.get("skill") or {}
        if not isinstance(skill, dict):
            continue
        name = _clean(skill.get("name"))
        if not name:
            continue
        node_type = node.get("node_type")
        stype = str(skill.get("type") or "").strip() or "Skill"
        filled_desc = _ww_fill_params(skill.get("desc"), skill.get("param"))
        desc = _clean(filled_desc)
        levels = skill.get("level") or {}

        if node_type == 4 or (name.endswith("+") and not levels and stype in ("Skill", "Stat Bonus")):
            stat_name = name.rstrip("+").strip()
            m_val = re.search(r"([+-]?\d+(?:\.\d+)?)(%?)", desc)
            if m_val and "{" not in desc:
                try:
                    stat_bonuses.setdefault(stat_name, []).append(float(m_val.group(1)))
                    stat_bonus_units[stat_name] = m_val.group(2)
                except ValueError:
                    pass
            elif stat_name:
                stat_bonus_counts[stat_name] = stat_bonus_counts.get(stat_name, 0) + 1
            continue

        desc = re.sub(r"\{\d+\}", "[TBD]", desc)
        rows: list[str] = []
        param_rows: list[dict] = []
        for spec_key in sorted((levels or {}), key=lambda k: int(k) if str(k).isdigit() else 0):
            spec = levels[spec_key]
            if not isinstance(spec, dict):
                continue
            label = _clean(spec.get("name"))
            params = spec.get("param") or []
            if not params or not isinstance(params[0], list):
                continue
            col = params[0]
            if not col:
                continue
            # Kuro's raw param[0] carries 20 entries on some skills, while the in-game max on
            # all five leveled skills is strictly Lv. 10 (index 9; S1–S6 add +0 levels).
            idx = min(SKILL_LEVEL["ww"], len(col)) - 1
            raw_top = col[idx] if idx >= 0 else ""
            if not label or raw_top == "" or raw_top is None:
                continue
            fmt = spec.get("format")
            if isinstance(fmt, str) and fmt.strip():
                top = (
                    _clean(fmt.replace("{0}", str(raw_top)))
                    if "{0}" in fmt
                    else f"{raw_top}{fmt}"
                )
            else:
                top = str(raw_top)
            if not top:
                continue
            rows.append(f"{label}: {top}")
            scalar = _single_stat_float(top)
            if scalar is not None:
                param_rows.append({"name": label, "value": scalar, "text": str(top)})
        if rows:
            rows = [f"**Skill Attributes (Lv. {SKILL_LEVEL['ww']})**"] + rows
        s_lines = _lines(desc) + rows
        if any("[TBD]" in ln for ln in s_lines):
            s_lines.append("-# ⚠️ [TBD] marks an unresolved parameter in this beta build.")
        sections.append(BetaSectionEntry(
            title=_dedup_skill_title(stype, name), category=stype, tag="[NEW]", meta="",
            lines=s_lines, params=param_rows,
            values_label=f"Lv. {SKILL_LEVEL['ww']}" if rows else "",
        ))

    chains = data.get("chains") or {}
    for level in sorted(chains, key=lambda k: int(k) if str(k).isdigit() else 0):
        item = chains[level]
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"))
        desc = _clean(re.sub(r"\{\d+\}", "[TBD]", _ww_fill_params(item.get("desc"), item.get("param"))))
        if not (name or desc):
            continue
        c_lines = _lines(desc)
        if any("[TBD]" in ln for ln in c_lines):
            c_lines.append("-# ⚠️ [TBD] marks an unresolved parameter in this beta build.")
        sections.append(BetaSectionEntry(
            title=f"Sequence Node {level}" + (f" — {name}" if name else ""),
            category="Resonance Chain", tag="[NEW]", meta="", lines=c_lines,
        ))

    stats: list[str] = []
    stat_table = data.get("stats") or {}
    if isinstance(stat_table, dict):
        max_block = None
        for key in sorted(stat_table, key=lambda k: int(k) if str(k).isdigit() else -1):
            if isinstance(stat_table[key], dict):
                max_block = stat_table[key]
        if isinstance(max_block, dict):
            lvl = max_block.get("90") or max_block.get("1")
            if isinstance(lvl, dict):
                life, atk, df = lvl.get("life"), lvl.get("atk"), lvl.get("def")
                if life is not None and atk is not None and df is not None:
                    stats.append(f"Lv. 90 — HP {_num(life)} · ATK {_num(atk)} · DEF {_num(df)}")
    bonus_parts: list[str] = []
    for s_name, vals in stat_bonuses.items():
        unit = stat_bonus_units.get(s_name, "")
        bonus_parts.append(f"{s_name} +{_num(sum(vals), 2)}{unit}")
    for s_name, count in stat_bonus_counts.items():
        if s_name not in stat_bonuses:
            bonus_parts.append(f"{s_name} (×{count} nodes)")
    if bonus_parts:
        stats.append("Stat Bonuses: " + " | ".join(bonus_parts))

    element = data.get("element")
    try:
        element_label = WW_ELEMENTS.get(int(element), "")
    except (TypeError, ValueError):
        element_label = ""
    weapon = data.get("weapon")
    try:
        weapon_label = WW_WEAPONS.get(int(weapon), "")
    except (TypeError, ValueError):
        weapon_label = ""

    return {
        "rarity": _parse_rarity("ww", data.get("rarity"), 5),
        "element": element_label,
        "role": weapon_label,
        "camp": "",
        "stats_lines": stats,
        "sections": sections,
    }

_CHARACTER_BUILDERS = {
    "zzz": _zzz_character,
    "hsr": _hsr_character,
    "gi": _gi_character,
    "ww": _ww_character,
}


# --------------------------------------------------------------------------- equipment
_GI_FIGHT_PROP_LABELS = {
    "FIGHT_PROP_CRITICAL": "CRIT Rate",
    "FIGHT_PROP_CRITICAL_HURT": "CRIT DMG",
    "FIGHT_PROP_CHARGE_EFFICIENCY": "Energy Recharge",
    "FIGHT_PROP_ELEMENT_MASTERY": "Elemental Mastery",
    "FIGHT_PROP_ATTACK_PERCENT": "ATK%",
    "FIGHT_PROP_HP_PERCENT": "HP%",
    "FIGHT_PROP_DEFENSE_PERCENT": "DEF%",
    "FIGHT_PROP_PHYSICAL_ADD_HURT": "Physical DMG Bonus",
    "FIGHT_PROP_FIRE_ADD_HURT": "Pyro DMG Bonus",
    "FIGHT_PROP_WATER_ADD_HURT": "Hydro DMG Bonus",
    "FIGHT_PROP_GRASS_ADD_HURT": "Dendro DMG Bonus",
    "FIGHT_PROP_ELEC_ADD_HURT": "Electro DMG Bonus",
    "FIGHT_PROP_ICE_ADD_HURT": "Cryo DMG Bonus",
    "FIGHT_PROP_WIND_ADD_HURT": "Anemo DMG Bonus",
    "FIGHT_PROP_ROCK_ADD_HURT": "Geo DMG Bonus",
    "FIGHT_PROP_HEAL_ADD": "Healing Bonus",
}
# Verified against Genshin WeaponCurveExcelConfigData.json: all GROW_CURVE_CRITICAL_{101,201,301}
# weapon substats multiply init_value by 4.594 at Lv. 90 (e.g. 0.192 -> 88.2% CRIT DMG).
_GI_WEAPON_SUBSTAT_LV90_MULT = 4.594


def _equip_from_detail(ns: str, kind: str, data: dict, listing: dict) -> dict:
    """Pull the display fields of one weapon / relic out of its detail JSON."""
    stats_line = ""
    effect_title = ""
    lines: list[str] = []
    sub_type = ""
    weapon_class = ""
    set_kind = ""
    set_parts = ""
    glossary: list[tuple[str, str]] = []
    seen_gloss: set[str] = set()
    rarity = _parse_rarity(
        ns,
        data.get("rarity") if data.get("rarity") not in (None, "") else listing.get("rank"),
        None,
    )

    def add_gloss(extra: Any) -> None:
        for _eid, entry in (extra.items() if isinstance(extra, dict) else ()):
            if not isinstance(entry, dict):
                continue
            nm = _clean(entry.get("name"))
            ds = " ".join(_clean(entry.get("desc")).split())
            if nm and ds and nm.lower() not in seen_gloss:
                seen_gloss.add(nm.lower())
                glossary.append((nm, ds))

    if ns == "ww" and kind == "weapon" and "_encore" in data:
        raw = data["_encore"]
        stats_line = " · ".join(_encore_properties(raw))
        if stats_line:
            stats_line = f"Lv. 90 — {stats_line}"
        effect_title = _encore_text(raw.get("ResonName"))
        raw_desc = _encore_text(raw.get("Desc"))
        clean_desc = re.sub(r"\b(\d+(?:\.\d+)?%?)(?:/\1){2,}", r"\1", raw_desc)
        lines = ["-# Effect values shown for refinements 1–5."] + _lines(clean_desc)
        sub_type = _encore_text(raw.get("WeaponTypeName"))
        weapon_class = sub_type
        rarity = rarity or _parse_rarity("ww", raw.get("QualityId"), None)
    elif ns == "zzz" and kind == "weapon":
        rp = data.get("rand_property") or {}
        bp = data.get("base_property") or {}
        stars_map = data.get("stars") if isinstance(data.get("stars"), dict) else {}
        lvl_map = data.get("level") if isinstance(data.get("level"), dict) else {}
        max_star = None
        if stars_map:
            for sk in sorted(stars_map, key=lambda k: int(k) if str(k).isdigit() else -1):
                if isinstance(stars_map[sk], dict):
                    max_star = stars_map[sk]
        atk = listing.get("atk")
        if not atk and isinstance(bp, dict) and bp.get("value") is not None and max_star and lvl_map:
            max_lv = None
            for lk in sorted(lvl_map, key=lambda k: int(k) if str(k).isdigit() else -1):
                if isinstance(lvl_map[lk], dict):
                    max_lv = lvl_map[lk]
            if isinstance(max_lv, dict):
                try:
                    atk = int(
                        float(bp["value"])
                        * (
                            1.0
                            + float(max_lv.get("rate") or 0) / 10000.0
                            + float(max_star.get("star_rate") or 0) / 10000.0
                        )
                    )
                except (TypeError, ValueError):
                    atk = None
        if atk:
            stats_line = f"Base ATK {_num(atk)}"
        sub_name = _clean(rp.get("name2") or rp.get("name"))
        if sub_name:
            sub_disp_map = {"percent hp": "HP%", "percent atk": "ATK%", "percent def": "DEF%"}
            disp_lbl = sub_disp_map.get(sub_name.lower(), sub_name)
            sub_val_txt = ""
            if isinstance(max_star, dict) and max_star.get("rand_rate") is not None and rp.get("value") is not None:
                try:
                    scaled = float(rp["value"]) * (1.0 + float(max_star["rand_rate"]) / 10000.0)
                    fmt_s = str(rp.get("format") or "")
                    sub_val_txt = f"{_fmt_value(scaled / 100.0, 'f')}%" if "%" in fmt_s else _fmt_value(scaled, "f")
                except (TypeError, ValueError):
                    sub_val_txt = ""
            sub_type = f"{disp_lbl} {sub_val_txt}" if sub_val_txt else sub_name
        wt = data.get("weapon_type") or {}
        if isinstance(wt, dict):
            for v in wt.values():
                if isinstance(v, str) and v:
                    weapon_class = v
                    break
        talents = data.get("talents") or {}
        first = None
        for key in sorted(talents, key=lambda k: int(k) if str(k).isdigit() else 0):
            if isinstance(talents[key], dict):
                first = talents[key]
                break
        if isinstance(first, dict):
            effect_title = _clean(first.get("name"))
            lines = _lines(first.get("desc"))
    elif ns == "zzz" and kind == "equipment":
        set_kind = "Drive Disc Set"
        d2, d4 = _clean(data.get("desc2")), _clean(data.get("desc4"))
        if d2:
            lines.append(f"2-Piece: {d2}")
        if d4:
            lines.append(f"4-Piece: {d4}")
        suit = data.get("suit") or {}
        if isinstance(suit, dict):
            p_names = [
                _clean(suit[sk].get("name"))
                for sk in sorted(suit, key=lambda k: int(k) if str(k).isdigit() else 0)
                if isinstance(suit[sk], dict) and _clean(suit[sk].get("name"))
            ]
            if p_names:
                set_parts = " · ".join(p_names)
    elif ns == "hsr" and kind == "lightcone":
        weapon_class = str(data.get("base_type") or listing.get("baseType") or "").strip()
        stats_table = data.get("stats") or {}
        if isinstance(stats_table, list):
            rows = [row for row in stats_table if isinstance(row, dict)]
            last = max(rows, key=lambda row: int(row.get("max_level") or 0), default={})
            level = int(last.get("max_level") or 0)
            parts = []
            for label, key in (("HP", "hp"), ("ATK", "attack"), ("DEF", "defence")):
                base, growth = last.get(f"base_{key}"), last.get(f"base_{key}_add")
                if base is not None and growth is not None and level:
                    value = int(round(float(base) + float(growth) * (level - 1)))
                    parts.append(f"{label} {value}")
            if parts:
                stats_line = f"Lv. {level} — " + " · ".join(parts)
        else:
            last = None
            for key in sorted(stats_table, key=lambda k: int(k) if str(k).isdigit() else -1):
                if isinstance(stats_table[key], dict):
                    last = stats_table[key]
            if isinstance(last, dict):
                hp, atk, df = last.get("base_hp"), last.get("base_attack"), last.get("base_defence")
                if hp is not None and atk is not None:
                    stats_line = f"Lv. 80 — HP {_num(hp)} · ATK {_num(atk)}"
                    if df is not None:
                        stats_line += f" · DEF {_num(df)}"
        ref = data.get("refinements") or data.get("refimements") or {}
        if isinstance(ref, dict):
            effect_title = _clean(ref.get("name"))
            params = ((ref.get("level") or {}).get("1") or {}).get("param_list")
            desc, _ = substitute_params(_clean(ref.get("desc")), params)
            lines = _lines(desc)
            add_gloss(ref.get("extra"))
    elif ns == "hsr" and kind == "relicset":
        parts_map = data.get("parts") or {}
        if isinstance(parts_map, dict) and parts_map:
            set_kind = "Planar Ornament" if len(parts_map) == 2 else "Relic Set"
            p_names = [
                _clean(parts_map[pk].get("name"))
                for pk in sorted(parts_map, key=str)
                if isinstance(parts_map[pk], dict) and _clean(parts_map[pk].get("name"))
            ]
            if p_names:
                set_parts = " · ".join(p_names)
        sets = data.get("require_num") or data.get("set") or {}
        for num in sorted(sets, key=lambda k: int(k) if str(k).isdigit() else 0):
            entry = sets[num]
            desc = _clean((entry or {}).get("desc") if isinstance(entry, dict) else entry)
            if isinstance(entry, dict):
                desc, _ = substitute_params(desc, entry.get("param_list"))
                add_gloss(entry.get("extra"))
            if desc:
                lines.append(f"{num}-Piece: {desc}")
    elif ns == "gi" and kind == "weapon":
        weapon_class = _GI_WEAPON_LABELS.get(str(data.get("type") or listing.get("type") or ""), "")
        sub_raw = str(listing.get("sub") or "").strip()
        props = data.get("weapon_prop") or []
        main = None
        sub_prop = None
        for prop in props:
            if not isinstance(prop, dict):
                continue
            ptype = str(prop.get("prop_type") or "").strip()
            if "BASE_ATTACK" in ptype and main is None:
                main = prop
            elif ptype and "BASE_ATTACK" not in ptype and sub_prop is None:
                sub_prop = prop
        if not sub_raw and isinstance(sub_prop, dict):
            sub_raw = str(sub_prop.get("prop_type") or "").strip()
        if sub_raw:
            sub_lbl = _GI_FIGHT_PROP_LABELS.get(sub_raw, "")
            sub_val_str = ""
            if (
                sub_lbl
                and isinstance(sub_prop, dict)
                and str(sub_prop.get("type") or "").startswith("GROW_CURVE_CRITICAL_")
                and sub_prop.get("init_value") is not None
            ):
                try:
                    scaled_sub = float(sub_prop["init_value"]) * _GI_WEAPON_SUBSTAT_LV90_MULT
                    if sub_raw == "FIGHT_PROP_ELEMENT_MASTERY":
                        sub_val_str = str(int(round(scaled_sub)))
                    else:
                        sub_val_str = f"{round(scaled_sub * 100, 1):g}%"
                except (TypeError, ValueError):
                    sub_val_str = ""
            if sub_lbl and sub_val_str:
                disp_lbl = sub_lbl.rstrip("%") if sub_lbl in ("ATK%", "HP%", "DEF%") else sub_lbl
                sub_type = f"{disp_lbl} {sub_val_str}"
            else:
                sub_type = sub_lbl
        if listing.get("atk") is not None:
            stats_line = f"Base ATK {_num(listing['atk'])}"
        elif isinstance(main, dict):
            stats_line = f"Lv. 1 Base ATK {_num(main.get('init_value'))}"
        ref = (data.get("refinement") or {}).get("1") or {}
        if isinstance(ref, dict) and (ref.get("name") or ref.get("desc")):
            effect_title = _clean(ref.get("name"))
            lines = _lines(ref.get("desc"))
        else:
            affix = data.get("affix") or {}
            for spec in (affix.values() if isinstance(affix, dict) else ()):
                if isinstance(spec, dict):
                    effect_title = _clean(spec.get("name"))
                    up = spec.get("upgrade") or {}
                    lines = _lines(up.get("0") if isinstance(up, dict) else "")
                    break
    elif ns == "gi" and kind == "artifact":
        set_kind = "Artifact Set"
        parts_map = data.get("parts") or {}
        if isinstance(parts_map, dict) and parts_map:
            p_names = [
                _clean(parts_map[pk].get("name"))
                for pk in sorted(parts_map, key=str)
                if isinstance(parts_map[pk], dict) and _clean(parts_map[pk].get("name"))
            ]
            if p_names:
                set_parts = " · ".join(p_names)
        sets = data.get("set") or {}
        if isinstance(sets, dict) and sets:
            for sid in sorted(sets, key=str):
                entry = sets[sid] or {}
                name = _clean(
                    (entry.get("name") or {}).get("en")
                    if isinstance(entry.get("name"), dict)
                    else entry.get("name")
                )
                desc = _clean(
                    (entry.get("desc") or {}).get("en")
                    if isinstance(entry.get("desc"), dict)
                    else entry.get("desc")
                )
                if desc:
                    lines.append(f"{name}: {desc}" if name else desc)
        elif isinstance(data.get("affix"), list):
            for idx, raw_affix in enumerate(data["affix"][:2]):
                desc = _clean(raw_affix)
                if desc:
                    lines.append(f"{2 if idx == 0 else 4}-Piece: {desc}")
    elif ns == "ww" and kind == "weapon":
        try:
            weapon_class = WW_WEAPONS.get(int(data.get("type") or listing.get("type")), "")
        except (TypeError, ValueError):
            weapon_class = ""
        if listing.get("sub") and isinstance(listing.get("sub"), str):
            sub_type = str(listing["sub"]).strip()
        effect_title = _clean(data.get("effect_name"))
        params = [row[0] if isinstance(row, list) and row else row
                  for row in data.get("param") or []]
        lines = _lines(_ww_fill_params(data.get("effect"), params))
        stats_table = data.get("stats") or {}
        block = None
        for key in sorted(stats_table, key=lambda k: int(k) if str(k).isdigit() else -1):
            if isinstance(stats_table[key], dict):
                block = stats_table[key]
        if isinstance(block, dict):
            top = block.get("90") or block.get("1")
            if isinstance(top, list):
                parts = []
                for stat in top:
                    if isinstance(stat, dict):
                        value = stat.get("value")
                        if stat.get("is_percent"):
                            shown = _num(float(value) / 100, 2) + "%"
                        else:
                            shown = _num(value, 2)
                        parts.append(f"{stat.get('name')} {shown}")
                if parts:
                    stats_line = " · ".join(parts)
    elif ns == "ww" and kind == "echo":
        set_kind = "Sonata Effect"
        skill = data.get("skill") or {}
        if isinstance(skill, dict):
            sk_raw_params = skill.get("param") or []
            if (
                isinstance(sk_raw_params, list)
                and sk_raw_params
                and isinstance(sk_raw_params[-1], list)
            ):
                echo_params = sk_raw_params[-1]
            else:
                echo_params = sk_raw_params
            lines = _lines(_ww_fill_params(skill.get("desc"), echo_params))
        groups = data.get("group") or {}
        for gid in sorted(groups, key=str):
            group = groups[gid] or {}
            gname = _clean(group.get("name"))
            for num in sorted((group.get("set") or {}), key=lambda k: int(k) if str(k).isdigit() else 0):
                entry = (group.get("set") or {})[num] or {}
                desc = _clean(
                    _ww_fill_params(entry.get("desc"), entry.get("param"))
                    if isinstance(entry, dict) else entry
                )
                if desc:
                    lines.append(f"{gname} {num}-Piece: {desc}" if gname else f"{num}-Piece: {desc}")

    unresolved = re.compile(r"#\d+\[[A-Za-z0-9]+\]%?|\{\d+\}")
    if any(unresolved.search(line) for line in lines):
        lines = [unresolved.sub("[value unavailable]", line) for line in lines]
        lines.append("-# ⚠️ Some values are unavailable in this revision's source data.")
    return {
        "stats_line": stats_line,
        "effect_title": effect_title,
        "lines": lines,
        "sub_type": sub_type,
        "rarity": rarity,
        "weapon_class": weapon_class,
        "set_kind": set_kind,
        "set_parts": set_parts,
        "glossary": glossary,
    }


# --------------------------------------------------------------------------- images
_MISSING_IMAGE_RE = re.compile(r"(?:^|/)missing\.webp$", re.I)


def _usable_image(url: str) -> str:
    """A gallery URL, or '' when it is a placeholder the database itself uses."""
    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return ""
    if _MISSING_IMAGE_RE.search(u):
        return ""
    return u


def _ww_asset_matches_id(item_id: str, raw_path: str) -> bool:
    """Reject placeholder WuWa assets whose embedded numeric ID belongs to another item."""
    if not raw_path or not item_id or not str(item_id).isdigit():
        return True
    stem = Path(str(raw_path)).stem
    nums = [n for n in re.findall(r"\d{4,}", stem)]
    if not nums:
        return True
    return any(n == str(item_id) or str(item_id).endswith(n) or n.endswith(str(item_id)) for n in nums)


def _image_preflight_cost(entry: dict, locked: dict, cache: dict) -> int:
    """Maximum media GETs `_resolve_image` may issue for this item (zero for a lock/cache)."""
    key = entry.get("id") or entry.get("name") or ""
    if not key or key in cache:
        return 0
    cached = locked.get(key)
    if isinstance(cached, dict) and cached.get("url"):
        return 0
    urls = [entry.get("image")] + list(entry.get("candidates") or [])
    usable = {_usable_image(str(raw or "")) for raw in urls}
    return len(usable - {""})


async def _resolve_image(fetcher, entry: dict, locked: dict, cache: dict) -> str:
    """Resolve one verified art URL once, then reuse the lock without another request."""
    key = entry.get("id") or entry.get("name") or ""
    if not key:
        return ""
    if key in cache:
        return cache[key]
    usable_urls: list[str] = []
    for raw in [entry.get("image"), *(entry.get("candidates") or [])]:
        u = _usable_image(str(raw or ""))
        if u and u not in usable_urls:
            usable_urls.append(u)
    cached = locked.get(key)
    if isinstance(cached, dict) and cached.get("url"):
        cached_url = str(cached["url"])
        if len(usable_urls) >= 2 and cached_url in usable_urls[1:]:
            try:
                ok_top = await fetcher.verify_media(usable_urls[0])
            except Exception:  # noqa: BLE001
                ok_top = False
            if ok_top:
                cache[key] = usable_urls[0]
                return usable_urls[0]
        cache[key] = cached_url
        return cache[key]
    for url in usable_urls:
        try:
            ok = await fetcher.verify_media(url)
        except Exception:  # noqa: BLE001 - a preflight failure must never break a card
            ok = False
        if ok:
            cache[key] = url
            return url
    cache[key] = ""
    return ""


async def _resolve_image_within_budget(
    fetcher,
    entry: dict,
    locked: dict,
    cache: dict,
    budget: dict,
    requests: int,
) -> tuple[str, int]:
    """Probe candidate URLs one at a time, charging only actual media requests issued."""
    key = entry.get("id") or entry.get("name") or ""
    if not key:
        return "", requests
    if key in cache:
        return cache[key], requests
    usable_urls: list[str] = []
    for raw in [entry.get("image"), *(entry.get("candidates") or [])]:
        u = _usable_image(str(raw or ""))
        if u and u not in usable_urls:
            usable_urls.append(u)
    cached = locked.get(key)
    if isinstance(cached, dict) and cached.get("url"):
        cached_url = str(cached["url"])
        # If an earlier beta build only had the small shop/item icon (`usable_urls[1:]`),
        # check once whether the full splash/gacha art (`usable_urls[0]`) is now live.
        if (
            len(usable_urls) >= 2
            and cached_url in usable_urls[1:]
            and requests + 1 <= budget["max_requests"]
            and _take_shared_request(budget, 1)
        ):
            requests += 1
            try:
                ok_top = await fetcher.verify_media(usable_urls[0])
            except Exception:  # noqa: BLE001
                ok_top = False
            if ok_top:
                cache[key] = usable_urls[0]
                return usable_urls[0], requests
        cache[key] = cached_url
        return cache[key], requests
    for url in usable_urls:
        if requests + 1 > budget["max_requests"] or not _take_shared_request(budget, 1):
            return "", requests
        requests += 1
        try:
            ok = await fetcher.verify_media(url)
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            cache[key] = url
            return url, requests
    cache[key] = ""
    return "", requests


async def _resolve_gallery_within_budget(
    fetcher,
    key: str,
    candidates: list[str],
    locked: dict,
    gallery_cache: dict[str, list[str]],
    budget: dict,
    requests: int,
) -> tuple[list[str], int]:
    """Verify multi-icon gallery assets (e.g. 6 Eidolon icons or Planar/Relic/Artifact pieces)."""
    if not key or not candidates:
        return [], requests
    gkey = f"{key}:gallery"
    if gkey in gallery_cache:
        return list(gallery_cache[gkey]), requests
    usable: list[str] = []
    for raw in candidates:
        u = _usable_image(raw)
        if u and u not in usable:
            usable.append(u)
    if not usable:
        return [], requests
    cached = locked.get(gkey)
    cached_urls = (
        [str(u) for u in cached["urls"] if u]
        if isinstance(cached, dict) and isinstance(cached.get("urls"), list)
        else []
    )
    if cached_urls and len(cached_urls) >= len(usable):
        gallery_cache[gkey] = list(cached_urls)
        return list(cached_urls), requests
    missing = [u for u in usable if u not in cached_urls]
    if requests + len(missing) > budget["max_requests"]:
        gallery_cache[gkey] = list(cached_urls)
        return list(cached_urls), requests
    shared_left = budget.get("_shared_requests_left")
    if shared_left is not None and int(shared_left) < len(missing):
        gallery_cache[gkey] = list(cached_urls)
        return list(cached_urls), requests
    verified: list[str] = list(cached_urls)
    for url in missing:
        if not _take_shared_request(budget, 1):
            break
        requests += 1
        try:
            ok = await fetcher.verify_media(url)
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            if not verified:
                gallery_cache[gkey] = []
                return [], requests
            continue
        verified.append(url)
    ordered = [u for u in usable if u in verified]
    gallery_cache[gkey] = ordered
    return list(ordered), requests


# --------------------------------------------------------------------------- run
def _game_ns(game: Game) -> str:
    beta_cfg = getattr(game, "beta", None) or {}
    return str(beta_cfg.get("nanoka") or "").strip()


def _changelog_url(game: Game) -> str:
    beta_cfg = getattr(game, "beta", None) or {}
    return str(beta_cfg.get("changelog") or "").strip()


def _budget(ctx) -> dict:
    s = ctx.settings
    env = s.env

    def _int(name: str, default: int, low: int, high: int) -> int:
        raw = str(env.get(name, "") or "").strip()
        if not raw:
            return default
        try:
            return max(low, min(high, int(raw)))
        except ValueError:
            return default

    return {
        "min_poll_min": _int("BETA_MIN_POLL_MINUTES", 30, 0, 1440),
        "max_chars": _int("BETA_MAX_CHARACTERS", 4, 1, 8),
        "max_equip": _int("BETA_MAX_EQUIPMENT", 12, 1, 24),
        "max_changed": _int("BETA_MAX_CHANGES", 8, 0, 16),
        "max_requests": _int("BETA_MAX_REQUESTS", 48, 4, 120),
    }


def _take_shared_request(budget: dict, count: int = 1) -> bool:
    """Reserve from the run-wide budget when present (unit callers keep local semantics)."""
    left = budget.get("_shared_requests_left")
    if left is None:
        return True
    try:
        left = int(left)
    except (TypeError, ValueError):
        return False
    if count < 0 or left < count:
        return False
    budget["_shared_requests_left"] = left - count
    return True


async def _fetch_changelog_revisions(
    ctx, game: Game, budget: dict | None = None, current_requests: int = 2
) -> tuple[list[dict], int]:
    """Read all available beta revisions once; an unavailable/403 changelog falls back cleanly."""
    ch_url = _changelog_url(game)
    if not ch_url:
        return [], 0
    hdrs = dict(_GACHABASE_HEADERS) if "gachabase.net" in ch_url.lower() else None
    text = await ctx.fetcher.get_text(ch_url, source="beta-changelog", headers=hdrs, retries=1)
    extra = 0
    if text is None and "?" in ch_url:
        alt_url = ch_url.split("?", 1)[0]
        can_try_alt = budget is None or (
            current_requests + 1 <= budget["max_requests"] and _take_shared_request(budget, 1)
        )
        if alt_url and alt_url != ch_url and can_try_alt:
            extra += 1
            text = await ctx.fetcher.get_text(
                alt_url, source="beta-changelog", headers=hdrs, retries=0
            )
    if text is None and "gachabase.net" in ch_url.lower():
        parsed_ch = urlsplit(ch_url)
        home_url = f"{parsed_ch.scheme}://{parsed_ch.netloc}/?lang=en" if parsed_ch.netloc else ""
        can_try_home = bool(home_url and home_url not in (ch_url, ch_url.split("?", 1)[0])) and (
            budget is None
            or (
                budget["max_requests"] >= 8
                and current_requests + extra + 1 <= budget["max_requests"]
                and _take_shared_request(budget, 1)
            )
        )
        if can_try_home:
            extra += 1
            text = await ctx.fetcher.get_text(
                home_url, source="beta-changelog", headers=hdrs, retries=0
            )
    if not text:
        log.info("%s beta: changelog unavailable (%s); using static manifest fallback", game.short, ch_url)
        return [], extra
    revisions = parse_changelog(text, base_url=ch_url)
    if not revisions:
        log.info("%s beta: changelog unreadable/blocked (%s); using static manifest fallback", game.short, ch_url)
    return revisions, extra


async def _changelog_revisions(ctx, game: Game) -> list[dict]:
    revisions, _ = await _fetch_changelog_revisions(ctx, game)
    return revisions


def _revision_build(value: str) -> int | None:
    """The numeric build from `3.4.1+19850203` or `v3.4.1 Rev. 19850203`."""
    match = re.search(r"(?:\+|\bRev\.\s*)(\d{5,})\b", str(value or ""), re.I)
    try:
        return int(match.group(1)) if match else None
    except (TypeError, ValueError):
        return None


def _major_minor(version: str) -> tuple[int, int]:
    nums = [int(n) for n in re.findall(r"\d+", _display_version(version))[:2]]
    if len(nums) >= 2:
        return (nums[0], nums[1])
    return (nums[0], 0) if nums else (0, 0)


def _is_beta_version_string(version: str) -> bool:
    """True when `version` is a revisioned beta build (e.g. '3.4.1', '4.7.51', '3.8.0.2')."""
    base = _display_version(version)
    nums = [n for n in re.split(r"[^0-9]+", base) if n != ""]
    return len(nums) >= 3


def _live_cycle(ctx, game: Game, m_game: dict) -> tuple[int, int] | None:
    """Determine the game's current live (major, minor) cycle to gate out finished/past betas."""
    candidates: list[tuple[int, int]] = []
    for m_key in ("live", "hotfix"):
        raw = str((m_game or {}).get(m_key) or "").strip()
        if raw and _major_minor(raw) != (0, 0):
            candidates.append(_major_minor(raw))
    versions = getattr(ctx, "versions", None)
    if isinstance(versions, dict):
        vinfo = versions.get(game.key)
        if isinstance(vinfo, dict):
            for k in ("live", "current"):
                raw = str(vinfo.get(k) or "").strip()
                if raw and _major_minor(raw) != (0, 0):
                    candidates.append(_major_minor(raw))
    state = getattr(ctx, "state", None)
    if state is not None:
        sched = state.schedule_records(game.key)
        for ver_str, rec in (sched.items() if isinstance(sched, dict) else ()):
            if not isinstance(rec, dict):
                continue
            maint_ts = int(rec.get("maint_start_ts") or 0)
            if maint_ts and maint_ts <= int(getattr(ctx, "now", 0) or 0):
                mm = _major_minor(str(ver_str))
                if mm != (0, 0):
                    candidates.append(mm)
    return max(candidates) if candidates else None


def _available_beta_versions(
    m_game: dict,
    *,
    live_cycle: tuple[int, int] | None = None,
) -> list[str]:
    """Ordered, de-duplicated static data builds strictly newer than `live_cycle` when known."""
    versions: list[str] = []
    for raw in list(m_game.get("available") or []) + [m_game.get("latest")]:
        version = str(raw or "").strip()
        if not version or version in versions:
            continue
        if live_cycle is not None and (
            not _is_beta_version_string(version) or _major_minor(version) <= live_cycle
        ):
            continue
        versions.append(version)
    return versions


def _next_data_version(
    record: dict,
    m_game: dict,
    *,
    live_cycle: tuple[int, int] | None = None,
) -> str:
    """Walk a beta revision at a time; never publish a beta cycle that is already live."""
    for revision in (record.get("revisions") or {}).values():
        if (isinstance(revision, dict) and revision.get("status") in ("partial", "ambiguous")
                and revision.get("source_version")):
            in_flight = str(revision["source_version"])
            if live_cycle is None or _major_minor(in_flight) > live_cycle:
                return in_flight
    versions = _available_beta_versions(m_game, live_cycle=live_cycle)
    if not versions:
        return ""
    raw_latest = str(m_game.get("latest") or "").strip()
    latest = raw_latest if raw_latest in versions else versions[-1]
    last = str(record.get("source_version") or "").strip()
    if not last:
        return latest
    if last in versions:
        pos = versions.index(last)
        return versions[pos + 1] if pos + 1 < len(versions) else ""
    previous_build = _revision_build(last)
    if previous_build is not None:
        newer = [(idx, _revision_build(version), version) for idx, version in enumerate(versions)
                 if _revision_build(version) is not None and _revision_build(version) > previous_build]
        if newer:
            return min(newer, key=lambda row: (row[1], row[0]))[2]
        if _display_version(last) != _display_version(latest):
            return latest
        return ""
    return latest if last != latest else ""


def _changelog_for_version(revisions: list[dict], version: str) -> dict | None:
    build = _revision_build(version)
    if build is not None:
        for revision in revisions:
            if _revision_build(str(revision.get("label") or "")) == build:
                return revision
    display = _display_version(version).lstrip("v")
    candidates = [r for r in revisions if display and display in str(r.get("label") or "")]
    return candidates[0] if len(candidates) == 1 else None


def _revision_label(revision: dict | None, version: str) -> tuple[str, str]:
    if revision:
        key = str(revision.get("label") or "").strip()
        label = key
        date = str(revision.get("date") or "").strip()
        if date:
            label = f"{label} — {date}"
        return label or f"v{_display_version(version)}", key or version
    build = _revision_build(version)
    suffix = f" Rev. {build}" if build is not None else ""
    return f"v{_display_version(version)}{suffix}", version


def _entry_role(ns: str, url: str) -> str:
    low = (url or "").lower()
    if "/agents/" in low or "/characters/" in low or "/resonators/" in low:
        return "character"
    if "/w-engines/" in low or "/light-cones/" in low or "/lightcones/" in low or "/weapons/" in low:
        return "weapon"
    if ("/drive-discs/" in low or "/relics/" in low or "/relicsets/" in low
            or "/planar-ornaments/" in low or "/artifacts/" in low or "/echoes/" in low
            or "/echo/" in low):
        return "relic"
    return ""


def _public_path(ns: str, role: str) -> str:
    paths = {
        "zzz": {"character": "agents", "weapon": "w-engines", "relic": "drive-discs"},
        "hsr": {"character": "characters", "weapon": "light-cones", "relic": "relicsets"},
        "gi": {"character": "characters", "weapon": "weapons", "relic": "artifacts"},
        "ww": {"character": "resonators", "weapon": "weapons", "relic": "echoes"},
    }
    return paths.get(ns, {}).get(role, role)


def _fallback_entry(ns: str, role: str, item_id: str) -> dict:
    """A manifest/snapshot candidate with deterministic source URLs (no guessed content)."""
    path = _public_path(ns, role)
    if ns == "ww":
        url = f"https://ww.nanoka.cc/{role}/{item_id}/"
    else:
        url = f"https://{ns}.gachabase.net/{path}/{item_id}/{item_id}/beta?lang=en"
    return {"id": str(item_id), "name": "", "url": url, "diff": "", "image": "", "section": "",
            "role": role, "synthetic": True}


def _entry_key(ns: str, role: str, item_id: str) -> str:
    return f"{role}:{ns}:{item_id}"


def _snapshot_key(ns: str, item_id: str) -> str:
    return f"{ns}:{item_id}"


def _typed_snapshot_key(ns: str, role: str, item_id: str) -> str:
    """Collision-safe state key for an item whose legacy numeric key is occupied."""
    return f"{ns}:{role}:{item_id}"


def _snapshot_key_for(snapshots: dict, ns: str, role: str, item_id: str) -> str:
    """Choose a compatible key without letting cross-kind numeric ids alias each other."""
    legacy = _snapshot_key(ns, item_id)
    typed = _typed_snapshot_key(ns, role, item_id)
    if typed in snapshots:
        return typed
    existing = snapshots.get(legacy)
    if isinstance(existing, dict) and str(existing.get("kind") or "") not in ("", role):
        return typed
    return legacy


def _snapshot_item_id(key: object, snapshot: dict) -> str:
    """Recover an id from old ``ns:id`` and collision-safe ``ns:role:id`` snapshots."""
    data = snapshot.get("data") if isinstance(snapshot, dict) else None
    candidate = str((data or {}).get("id") or key) if isinstance(data, dict) else str(key)
    return candidate.rsplit(":", 1)[-1]


def _candidate_entries(ns: str, kinds: dict, entries: list[dict], snapshots: dict,
                       new_char_ids: set[str], new_weapon_ids: set[str],
                       new_relic_ids: set[str]) -> tuple[list[dict], list[dict]]:
    """Changelog entries first; if it is empty, probe only tracked/new ids, never every unit."""
    chars: dict[str, dict] = {}
    equipment: dict[tuple[str, str], dict] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        item_id = _entry_id(raw)
        role = _entry_role(ns, str(raw.get("url") or ""))
        if not item_id or role not in ("character", "weapon", "relic"):
            continue
        copy = dict(raw)
        copy["role"] = role
        if role == "character":
            chars.setdefault(item_id, copy)
        else:
            equipment.setdefault((role, item_id), copy)

    for role, ids in (
        ("character", new_char_ids),
        ("weapon", new_weapon_ids),
        ("relic", new_relic_ids),
    ):
        for item_id in sorted(ids):
            fallback = _fallback_entry(ns, role, item_id)
            if role == "character":
                chars.setdefault(item_id, fallback)
            else:
                equipment.setdefault((role, item_id), fallback)
    if not entries:
        for key, snapshot in snapshots.items():
            if not isinstance(snapshot, dict) or not str(key).startswith(f"{ns}:"):
                continue
            role = str(snapshot.get("kind") or "")
            item_id = _snapshot_item_id(key, snapshot)
            if not item_id:
                continue
            if role == "character":
                chars.setdefault(item_id, _fallback_entry(ns, role, item_id))
            elif role in ("weapon", "relic"):
                equipment.setdefault((role, item_id), _fallback_entry(ns, role, item_id))
    return list(chars.values()), list(equipment.values())


def _clip(value: Any, limit: int = 1400) -> str:
    text = str(value or "").strip()
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def _section_snapshot(section: BetaSectionEntry) -> dict:
    return {
        "title": _clip(section.title, 240),
        "category": _clip(section.category, 120),
        "meta": _clip(section.meta, 160),
        "lines": [_clip(line) for line in list(section.lines or [])[:32] if str(line).strip()],
        "params": [
            {"name": _clip(row.get("name"), 160), "value": row.get("value"),
             "text": _clip(row.get("text"), 100)}
            for row in list(section.params or [])[:20] if isinstance(row, dict)
        ],
        "defs": [(_clip(name, 160), _clip(desc)) for name, desc in list(section.defs or [])[:12]
                 if str(name).strip() and str(desc).strip()],
        "values_label": _clip(section.values_label, 80),
    }


def _section_from_snapshot(value: Any) -> BetaSectionEntry:
    raw = value if isinstance(value, dict) else {}
    defs = []
    for pair in raw.get("defs") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            defs.append((str(pair[0]), str(pair[1])))
    return BetaSectionEntry(
        title=str(raw.get("title") or ""), category=str(raw.get("category") or ""),
        tag=str(raw.get("tag") or "[CHANGED]"), meta=str(raw.get("meta") or ""),
        lines=[str(line) for line in raw.get("lines") or [] if str(line).strip()],
        params=[row for row in raw.get("params") or [] if isinstance(row, dict)], defs=defs,
        values_label=str(raw.get("values_label") or ""),
    )


def _snapshot_record(item, data: dict, kind: str, revision: str, now: int) -> dict:
    """Keep bounded text plus a digest of full content, excluding mutable media/links."""
    excluded = ("image_url", "image_urls", "page_url", "weapon_class", "set_kind", "set_parts")
    full = {key: getattr(item, key) for key in data if key not in excluded}
    if kind == "character":
        full["sections"] = [
            {key: getattr(section, key) for key in _section_snapshot(section)}
            for section in item.sections
        ]
    content_signature = stable_hash(full)
    retained = {key: value for key, value in data.items() if key in full}
    return {"kind": kind, "revision": revision, "ts": int(now), "data": data,
            "signature": stable_hash(data), "content_signature": content_signature,
            "truncated": stable_hash(retained) != content_signature}


def _snapshot_comparison(previous: dict, current: dict) -> str:
    if previous.get("provider", "nanoka") != current.get("provider", "nanoka"):
        return "snapshot"
    # Pre-BETA_NORM_REV WuWa nanoka character snapshots stored Lv. 20 param[-1] rows under
    # a Lv. 10 label; never diff them directly without re-normalizing the historical build.
    if (
        previous.get("kind") == "character"
        and previous.get("provider", "nanoka") == "nanoka"
        and str((previous.get("data") or {}).get("id") or "").startswith("ww:")
        and int(previous.get("norm_rev") or 0) < BETA_NORM_REV
    ):
        return "snapshot"
    if previous.get("content_signature") == current["content_signature"]:
        return "equal"
    # Old snapshots did not record what was clipped. Rebaseline once without claiming
    # a before/after comparison; a clipped suffix could contain the only real change.
    if not previous.get("content_signature") or previous.get("truncated") or current["truncated"]:
        return "snapshot"
    return "diff"


def _character_snapshot(character: BetaCharacter, revision: str, now: int) -> dict:
    data = {
        "id": character.id, "name": character.name, "rarity": character.rarity,
        "element": character.element, "role": character.role, "subtext": character.subtext,
        "stats_header": character.stats_header,
        "stats_lines": [_clip(line, 300) for line in character.stats_lines[:16]],
        "sections": [_section_snapshot(section) for section in character.sections[:64]],
        "mechanic_defs": [(str(name), _clip(desc)) for name, desc in character.mechanic_defs[:24]],
        "image_url": character.image_url, "page_url": character.page_url,
    }
    snap = _snapshot_record(character, data, "character", revision, now)
    snap["norm_rev"] = BETA_NORM_REV
    return snap


def _character_from_snapshot(snapshot: Any) -> BetaCharacter | None:
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "character":
        return None
    data = snapshot.get("data")
    if not isinstance(data, dict) or not data.get("sections"):
        return None                         # legacy signature-only record: no invented diff
    defs = []
    for pair in data.get("mechanic_defs") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            defs.append((str(pair[0]), str(pair[1])))
    try:
        rarity = int(data.get("rarity") or 5)
    except (TypeError, ValueError):
        rarity = 5
    return BetaCharacter(
        id=str(data.get("id") or ""), name=str(data.get("name") or ""), rarity=rarity,
        element=str(data.get("element") or ""), role=str(data.get("role") or ""),
        subtext=str(data.get("subtext") or ""), stats_header=str(data.get("stats_header") or "Base Stats"),
        stats_lines=[str(line) for line in data.get("stats_lines") or []],
        sections=[_section_from_snapshot(section) for section in data.get("sections") or []],
        mechanic_defs=defs, image_url=str(data.get("image_url") or ""),
        page_url=str(data.get("page_url") or ""),
    )


def _equipment_snapshot(equipment: BetaEquipment, revision: str, now: int, kind: str) -> dict:
    data = {
        "id": equipment.id, "name": equipment.name, "category_label": equipment.category_label,
        "rarity": equipment.rarity, "sub_type": equipment.sub_type,
        "weapon_class": equipment.weapon_class, "set_kind": equipment.set_kind,
        "set_parts": equipment.set_parts,
        "stats_line": _clip(equipment.stats_line, 400), "effect_title": _clip(equipment.effect_title, 200),
        "lines": [_clip(line) for line in equipment.lines[:32]], "image_url": equipment.image_url,
        "image_urls": [str(u) for u in (equipment.image_urls or [])[:10] if u],
        "page_url": equipment.page_url,
    }
    return _snapshot_record(equipment, data, kind, revision, now)


def _equipment_from_snapshot(snapshot: Any) -> BetaEquipment | None:
    if not isinstance(snapshot, dict) or snapshot.get("kind") not in ("weapon", "relic"):
        return None
    data = snapshot.get("data")
    if not isinstance(data, dict):
        return None
    rarity = data.get("rarity")
    try:
        rarity = int(rarity) if rarity not in (None, "") else None
    except (TypeError, ValueError):
        rarity = None
    return BetaEquipment(
        id=str(data.get("id") or ""), name=str(data.get("name") or ""),
        category_label=str(data.get("category_label") or "Equipment"), rarity=rarity,
        sub_type=str(data.get("sub_type") or ""),
        weapon_class=str(data.get("weapon_class") or ""),
        set_kind=str(data.get("set_kind") or ""),
        set_parts=str(data.get("set_parts") or ""),
        stats_line=str(data.get("stats_line") or ""),
        effect_title=str(data.get("effect_title") or ""),
        lines=[str(line) for line in data.get("lines") or []],
        image_url=str(data.get("image_url") or ""),
        image_urls=[str(u) for u in (data.get("image_urls") or snapshot.get("gallery_urls") or []) if u],
        page_url=str(data.get("page_url") or ""),
    )


def _clone_sections(sections: list[BetaSectionEntry], tag: str) -> list[BetaSectionEntry]:
    return [BetaSectionEntry(
        title=section.title, category=section.category, tag=tag, meta=section.meta,
        lines=list(section.lines), params=[dict(row) for row in section.params],
        defs=list(section.defs), values_label=section.values_label,
    ) for section in sections]


def _stat_diff(prev: BetaCharacter, current: BetaCharacter) -> list[BetaSectionEntry]:
    if prev.stats_lines == current.stats_lines:
        return []
    old_lines = list(prev.stats_lines)
    new_lines = list(current.stats_lines)
    lines: list[str] = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for opcode, i1, i2, j1, j2 in matcher.get_opcodes():
        if opcode == "equal":
            continue
        if opcode == "replace":
            pairs = list(zip(old_lines[i1:i2], new_lines[j1:j2]))
            for old, new in pairs:
                lines.append(_change_line(old, new)[0])
            for extra in new_lines[j1 + len(pairs):j2]:
                lines.append(f"**[NEW]** {extra}")
            lines.extend(f"**[CHANGED]** Removed: ~~{line}~~"
                         for line in old_lines[i1 + len(pairs):i2])
        elif opcode == "insert":
            lines.extend(f"**[NEW]** {line}" for line in new_lines[j1:j2])
        elif opcode == "delete":
            lines.extend(f"**[CHANGED]** Removed: ~~{line}~~" for line in old_lines[i1:i2])
    return [BetaSectionEntry(title="Profile & Base Stats", category="Profile Changes",
                             tag="[CHANGED]", lines=lines)]


def _diff_equipment(previous: BetaEquipment, current: BetaEquipment) -> BetaEquipment | None:
    changes: list[str] = []
    if previous.stats_line != current.stats_line:
        changes.append(_change_line(previous.stats_line, current.stats_line)[0])
    if previous.effect_title != current.effect_title:
        changes.append(f"**[CHANGED]** Effect renamed from ~~{previous.effect_title}~~ → "
                       f"**{current.effect_title}**.")
    old_lines, new_lines = previous.lines, current.lines
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for opcode, i1, i2, j1, j2 in matcher.get_opcodes():
        if opcode == "equal":
            continue
        if opcode == "replace":
            pairs = list(zip(old_lines[i1:i2], new_lines[j1:j2]))
            changes.extend(_change_line(old, new)[0] for old, new in pairs)
            changes.extend(f"**[NEW]** {line}" for line in new_lines[j1 + len(pairs):j2])
            changes.extend(f"**[CHANGED]** Removed: ~~{line}~~"
                           for line in old_lines[i1 + len(pairs):i2])
        elif opcode == "insert":
            changes.extend(f"**[NEW]** {line}" for line in new_lines[j1:j2])
        elif opcode == "delete":
            changes.extend(f"**[CHANGED]** Removed: ~~{line}~~" for line in old_lines[i1:i2])
    if not changes:
        return None
    return BetaEquipment(
        id=current.id, name=current.name, category_label=current.category_label,
        rarity=current.rarity, sub_type=current.sub_type,
        weapon_class=current.weapon_class, set_kind=current.set_kind, set_parts=current.set_parts,
        tag="[CHANGED]", stats_line="", effect_title="", lines=changes,
        glossary=list(current.glossary), image_url=current.image_url,
        image_urls=list(current.image_urls), diff_url=current.diff_url, page_url=current.page_url,
    )


def _data_image_candidates(
    ns: str,
    role: str,
    item_id: str,
    data: dict,
    listing: dict | None = None,
) -> list[str]:
    """Known static.nanoka.cc asset conventions, preflighted before use and locked after."""
    if not item_id or "_encore" in data:
        return []
    candidates: list[str] = []

    def add(url: str) -> None:
        if url and url not in candidates:
            candidates.append(url)

    raw_icon = str(data.get("icon") or (listing or {}).get("icon") or "").strip()
    if ns == "gi" and role == "character":
        if raw_icon:
            suffix = raw_icon.removeprefix("UI_AvatarIcon_")
            if suffix and suffix != raw_icon:
                add(f"{STATIC_BASE}/assets/gi/UI_Gacha_AvatarImg_{suffix}.webp")
            add(f"{STATIC_BASE}/assets/gi/{raw_icon}.webp")
    elif ns == "gi" and role == "weapon":
        if raw_icon:
            suffix = raw_icon.removeprefix("UI_EquipIcon_")
            if suffix and suffix != raw_icon:
                add(f"{STATIC_BASE}/assets/gi/UI_Gacha_EquipIcon_{suffix}.webp")
            add(f"{STATIC_BASE}/assets/gi/{raw_icon}.webp")
    elif ns == "gi" and role == "relic":
        parts = data.get("parts") or {}
        if isinstance(parts, dict):
            for pk in sorted(parts, key=str):
                pv = parts[pk]
                if isinstance(pv, dict) and pv.get("icon"):
                    add(f"{STATIC_BASE}/assets/gi/{str(pv['icon']).strip()}.webp")
                    break
        if raw_icon:
            add(f"{STATIC_BASE}/assets/gi/{Path(raw_icon).stem}.webp")
    elif ns == "hsr" and role == "character":
        add(f"{STATIC_BASE}/assets/hsr/avatardrawcard/{item_id}.webp")
        add(f"{STATIC_BASE}/assets/hsr/avatarshopicon/{item_id}.webp")
    elif ns == "hsr" and role == "weapon":
        add(f"{STATIC_BASE}/assets/hsr/lightconemaxfigures/{item_id}.webp")
        add(f"{STATIC_BASE}/assets/hsr/lightconemediumicon/{item_id}.webp")
    elif ns == "hsr" and role == "relic":
        if raw_icon:
            add(f"{STATIC_BASE}/assets/hsr/itemfigures/{Path(raw_icon).stem}.webp")
    elif ns == "zzz" and role in ("character", "weapon", "relic"):
        for z_raw in (
            data.get("icon"),
            data.get("icon2"),
            (listing or {}).get("icon") if isinstance(listing, dict) else "",
        ):
            z_str = str(z_raw or "").strip()
            if z_str:
                stem = Path(z_str).stem
                if re.fullmatch(r"[A-Za-z0-9_]+", stem):
                    add(f"{STATIC_BASE}/assets/zzz/{stem}.webp")
    elif ns == "ww" and role in ("character", "weapon", "relic"):
        for field in ("figure", "background", "icon"):
            val = str(data.get(field) or "").strip()
            if not val or not _ww_asset_matches_id(item_id, val):
                continue
            rel = val.removeprefix("/Game/Aki/").lstrip("/")
            if "." in Path(rel).name and not rel.endswith((".webp", ".png")):
                rel = str(Path(rel).with_suffix(".webp"))
            elif not rel.endswith((".webp", ".png")):
                rel = f"{rel}.webp"
            add(f"{STATIC_BASE}/assets/ww/{rel}")
    return candidates


def _data_gallery_candidates(
    ns: str,
    role: str,
    item_id: str,
    data: dict,
    listing: dict | None = None,
) -> list[str]:
    """Multi-icon gallery URLs (6 HSR Eidolon icons, 2 Planar Ornaments, 4 Relic/Artifact pieces)."""
    if not item_id or "_encore" in data:
        return []
    urls: list[str] = []
    if ns == "hsr" and role == "character":
        ranks = data.get("ranks") or {}
        if isinstance(ranks, dict) and len(ranks) >= 6:
            for idx in range(1, 7):
                urls.append(
                    f"{STATIC_BASE}/assets/hsr/rank/_dependencies/textures/{item_id}/{item_id}_Rank_{idx}.webp"
                )
    elif ns == "hsr" and role == "relic":
        parts = data.get("parts") or {}
        if isinstance(parts, dict) and parts:
            slots: list[str] = []
            for pk in sorted(parts, key=str):
                s = str(pk).strip()
                if s.isdigit() and s[-1] in "123456" and s[-1] not in slots:
                    slots.append(s[-1])
            if not slots:
                slots = [str(i) for i in range(1, len(parts) + 1)]
            for slot in slots:
                urls.append(f"{STATIC_BASE}/assets/hsr/relicfigures/IconRelic_{item_id}_{slot}.webp")
    elif ns == "gi" and role == "relic":
        parts = data.get("parts") or {}
        if isinstance(parts, dict) and len(parts) >= 2:
            for pk in sorted(parts, key=str):
                pv = parts[pk]
                if isinstance(pv, dict) and pv.get("icon"):
                    urls.append(f"{STATIC_BASE}/assets/gi/{str(pv['icon']).strip()}.webp")
    return urls


def _safe_entry_name(
    entry: dict,
    data: dict,
    listing: dict | None,
    item_id: str,
    zh_data: dict | None = None,
) -> str:
    if "_encore" in data:
        return _clean(data.get("name")) or f"ID {item_id}"
    entry_name = str(entry.get("name") or "").strip()
    if (
        entry_name.lower().startswith("id:")
        or _is_key(entry_name)
        or _is_generic_changelog_label(entry_name)
    ):
        entry_name = ""
    localized = (listing or {}).get("en") if isinstance(listing, dict) else ""
    listing_name = _clean(localized.get("name") if isinstance(localized, dict) else localized)
    if _is_key(listing_name):
        listing_name = ""
    data_name = _clean(data.get("name")) if isinstance(data, dict) else ""
    if _is_key(data_name):
        data_name = ""
    resolved = entry_name or listing_name or data_name
    if resolved:
        return resolved
    zh_localized = (listing or {}).get("zh") if isinstance(listing, dict) else ""
    zh_listing_name = _clean(
        zh_localized.get("name") if isinstance(zh_localized, dict) else zh_localized
    )
    zh_detail_name = _clean(zh_data.get("name")) if isinstance(zh_data, dict) else ""
    for zh_cand in (zh_listing_name, zh_detail_name):
        if zh_cand and not _is_key(zh_cand):
            return f"{zh_cand} (ID {item_id})"
    return f"ID {item_id}"


def _slug(value: str, fallback_id: str = "") -> str:
    """Gachabase's stable URL fallback; source-provided URLs always win."""
    raw = re.sub(r"\s*\(ID\s*\d+\)\s*$", "", str(value or "").strip(), flags=re.I)
    m_id = re.fullmatch(r"id\s*:?\s*(\d+)", raw, re.I)
    if m_id:
        return m_id.group(1)
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug or str(fallback_id or "").strip() or "item"


def _item_page_url(ns: str, role: str, item_id: str, name: str) -> str:
    if ns == "ww":
        return f"https://ww.nanoka.cc/{role}/{item_id}/"
    return (
        f"https://{ns}.gachabase.net/{_public_path(ns, role)}/"
        f"{item_id}/{_slug(name, item_id)}/beta?lang=en"
    )


def _generated_diff_url(ns: str, role: str, item_id: str, name: str,
                        previous_version: str, current_version: str) -> str:
    """A deterministic diff link for a blank hotfix revision (e.g. 19850203)."""
    if ns == "ww" or not previous_version or not current_version:
        return ""
    before = str(previous_version).replace("+", "_")
    after = str(current_version).replace("+", "_")
    return (
        f"https://{ns}.gachabase.net/diff/{_public_path(ns, role)}/"
        f"{item_id}/{_slug(name, item_id)}"
        f"?prev=beta_{before}&cur=beta_{after}&lang=en"
    )


def _upgrade_legacy_beta_record(record: dict) -> bool:
    """Reset pre-Prime-Formatting beta records once so deleted old posts can be reposted cleanly."""
    if int(record.get("format_epoch") or 0) >= BETA_FORMAT_EPOCH:
        return False
    kept_revisions: dict[str, dict] = {}
    revs = record.get("revisions")
    if isinstance(revs, dict):
        for rev_id, rev_obj in revs.items():
            if not isinstance(rev_obj, dict):
                continue
            has_ambig = rev_obj.get("status") == "ambiguous" or any(
                isinstance(b, dict) and b.get("ambiguous")
                for b in (rev_obj.get("batches") or {}).values()
            )
            if has_ambig:
                kept_revisions[str(rev_id)] = rev_obj
    record["revisions"] = kept_revisions
    record["snapshots"] = {}
    record["locked_images"] = {}
    record["latest_rev"] = ""
    record["source_version"] = ""
    record["source_revision"] = ""
    record.pop("paused", None)
    record["last_poll"] = 0
    record["format_epoch"] = BETA_FORMAT_EPOCH
    return True


def _kit_needs_zh_check(built: dict | None) -> bool:
    """True when an EN kit shows placeholders (`???`, `[TBD]`, `/-`) or empty sections."""
    if not built or not built.get("sections"):
        return True
    for sec in built["sections"]:
        if "???" in (sec.title or "") or _is_key(sec.title):
            return True
        for ln in sec.lines:
            if "???" in ln or "[TBD]" in ln or re.search(r"\d+(?:\.\d+)?%?/-(\s|$|[.,;)])", ln):
                return True
    return False


def _extract_kit_percents(lines: list[str]) -> list[str]:
    """Extract combat percentage and explicit duration values (ordinal-immune across CN/EN)."""
    tokens: list[str] = []
    for ln in lines:
        if ln.startswith("-#"):
            continue
        tokens.extend(re.findall(r"\d+(?:\.\d+)?%", ln))
        for dur in re.findall(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*(?:s\b|sec(?:ond)?s?\b|秒)", ln, re.I):
            tokens.append(f"{dur}s")
    return tokens


def _extract_numeric_tokens_clean(text: str) -> list[str]:
    """Extract numeric tokens (`1000`, `24%`, `12.5%`), normalizing thousands commas (`1,000`)."""
    norm = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text or "")
    return re.findall(r"\d+(?:\.\d+)?%?", norm)


def _patch_placeholder_line_from_zh(en_ln: str, zh_ln: str, placeholder_re: str) -> tuple[str, bool]:
    """Fill `[TBD]`, `???`, or `[value unavailable]` tokens from aligned CN line."""
    if not zh_ln or any(tok in zh_ln for tok in ("[TBD]", "???", "[value unavailable]")):
        return en_ln, False
    zh_nums = _extract_numeric_tokens_clean(zh_ln)
    if not zh_nums:
        return en_ln, False
    en_without = re.sub(placeholder_re, " ", en_ln)
    existing_counts = Counter(_extract_numeric_tokens_clean(en_without))
    unmatched: list[str] = []
    for n in zh_nums:
        if existing_counts.get(n, 0) > 0:
            existing_counts[n] -= 1
        else:
            unmatched.append(n)
    pool = unmatched if unmatched else zh_nums
    pct_pool = [n for n in pool if n.endswith("%")]
    changed = False
    out = en_ln
    while re.search(placeholder_re, out) and pool:
        m = re.search(placeholder_re, out)
        if m is None:
            break
        wants_pct = m.group(0).endswith("%")
        if wants_pct and pct_pool:
            pick = pct_pool.pop(0)
            if pick in pool:
                pool.remove(pick)
        else:
            pick = pool.pop(0)
            if pick in pct_pool:
                pct_pool.remove(pick)
        out = out[:m.start()] + pick + out[m.end():]
        changed = True
    return out, changed


def _reconcile_en_with_zh_kit(en_built: dict, zh_built: dict) -> bool:
    """Patch stale/broken EN numbers or missing sections using authoritative CN/CHS beta data."""
    zh_secs: list[BetaSectionEntry] = list(zh_built.get("sections") or [])
    if not zh_secs:
        return False
    en_secs: list[BetaSectionEntry] = list(en_built.get("sections") or [])
    if not en_secs:
        copied = _clone_sections(zh_secs, "[NEW]")
        copied[0].lines.append("-# 🌐 Sourced from CN/CHS beta data — EN localization pending.")
        en_built["sections"] = copied
        return True

    # Align EN and CN sections by (category_occurrence, slot/positional_index) so missing
    # intermediate sections in EN never misalign later skills or constellations/mindscapes.
    def _group_by_cat(secs: list[BetaSectionEntry]) -> tuple[list[str], dict[str, list[BetaSectionEntry]]]:
        order: list[str] = []
        by_cat: dict[str, list[BetaSectionEntry]] = {}
        for s in secs:
            ck = (s.category or "").strip()
            if ck not in by_cat:
                order.append(ck)
                by_cat[ck] = []
            by_cat[ck].append(s)
        return order, by_cat

    en_cats, en_by_cat = _group_by_cat(en_secs)
    zh_cats, zh_by_cat = _group_by_cat(zh_secs)
    matched_zh_ids: set[int] = set()
    pairs_to_sync: list[tuple[BetaSectionEntry, BetaSectionEntry]] = []

    if en_cats == zh_cats or (set(en_cats) & set(zh_cats)):
        for ck in en_cats:
            en_list = en_by_cat[ck]
            zh_list = zh_by_cat.get(ck) or []
            if not zh_list:
                continue
            zh_slots = {_numbered_slot(zs): zs for zs in zh_list if _numbered_slot(zs) is not None}
            for pos, es in enumerate(en_list):
                es_slot = _numbered_slot(es)
                zs = zh_slots.get(es_slot) if es_slot is not None else None
                if zs is None and pos < len(zh_list) and id(zh_list[pos]) not in matched_zh_ids:
                    zs = zh_list[pos]
                if zs is not None:
                    matched_zh_ids.add(id(zs))
                    pairs_to_sync.append((es, zs))
    else:
        for idx, es in enumerate(en_secs):
            if idx >= len(zh_secs):
                break
            matched_zh_ids.add(id(zh_secs[idx]))
            pairs_to_sync.append((es, zh_secs[idx]))

    changed_any = False
    for en_sec, zh_sec in pairs_to_sync:
        sec_changed = False

        if en_sec.params and zh_sec.params:
            pair_len = min(len(en_sec.params), len(zh_sec.params))
            for p_idx in range(pair_len):
                en_p = en_sec.params[p_idx]
                zh_p = zh_sec.params[p_idx]
                if en_p.get("value") != zh_p.get("value") and zh_p.get("text"):
                    old_txt = str(en_p.get("text") or "")
                    new_txt = str(zh_p["text"])
                    en_p["value"] = zh_p.get("value")
                    en_p["text"] = new_txt
                    label_prefix = f"{en_p.get('name')}:"
                    en_sec.lines = [
                        f"{en_p.get('name')}: {new_txt}"
                        if ln.startswith(label_prefix) and old_txt in ln
                        else ln
                        for ln in en_sec.lines
                    ]
                    sec_changed = True
            if len(zh_sec.params) > len(en_sec.params):
                for zh_extra in zh_sec.params[len(en_sec.params):]:
                    if zh_extra.get("name") and zh_extra.get("text"):
                        en_sec.params.append(dict(zh_extra))
                        en_sec.lines.append(f"{zh_extra['name']}: {zh_extra['text']}")
                        sec_changed = True

        updated_lines: list[str] = []
        for l_idx, ln in enumerate(en_sec.lines):
            if l_idx < len(zh_sec.lines):
                zh_ln = zh_sec.lines[l_idx]
                zh_nums = _extract_numeric_tokens_clean(zh_ln)
                zh_pcts = [n for n in zh_nums if n.endswith("%")]
                if "[TBD]" in ln:
                    ln, did = _patch_placeholder_line_from_zh(ln, zh_ln, r"\[TBD\]%?")
                    sec_changed = sec_changed or did
                if "???" in ln:
                    ln, did = _patch_placeholder_line_from_zh(ln, zh_ln, r"\?\?\?%?")
                    sec_changed = sec_changed or did
                if re.search(r"\d+(?:\.\d+)?%?/-(\s|$|[.,;)])", ln) and zh_nums:
                    pick_tail = zh_pcts[-1] if zh_pcts else zh_nums[-1]
                    patched = re.sub(
                        r"(\d+(?:\.\d+)?%?)/-(?=\s|$|[.,;)])", f"\\1/{pick_tail}", ln, count=1
                    )
                    if patched != ln:
                        ln = patched
                        sec_changed = True
            updated_lines.append(ln)
        if sec_changed and not any("[TBD]" in x for x in updated_lines if not x.startswith("-#")):
            updated_lines = [
                x for x in updated_lines
                if not x.startswith("-# ⚠️ [TBD] marks an unresolved")
            ]

        en_tokens = _extract_kit_percents(updated_lines)
        zh_tokens = _extract_kit_percents(zh_sec.lines)
        if sec_changed:
            if not any("CN/CHS" in x for x in updated_lines):
                updated_lines.append("-# 🌐 Values synced with authoritative CN/CHS beta data.")
            en_sec.lines = updated_lines
            changed_any = True
        elif (
            zh_tokens
            and Counter(en_tokens) != Counter(zh_tokens)
            and not any("CN/CHS" in x for x in updated_lines)
        ):
            preview = ", ".join(zh_tokens[:8])
            if preview:
                updated_lines.append(
                    f"-# 🌐 CN/CHS client values updated ({preview}); EN text lags in this build."
                )
                en_sec.lines = updated_lines
                changed_any = True

    # Recover any non-empty CN sections whose category/slot was completely missing in EN.
    if set(en_cats) & set(zh_cats) or len(zh_secs) > len(en_secs):
        for zs in zh_secs:
            if id(zs) in matched_zh_ids:
                continue
            z_cat = (zs.category or "").strip()
            if z_cat and z_cat in en_by_cat:
                es_slots = {_numbered_slot(s) for s in en_by_cat[z_cat] if _numbered_slot(s) is not None}
                zs_slot = _numbered_slot(zs)
                if zs_slot is None and len(en_by_cat[z_cat]) >= len(zh_by_cat.get(z_cat) or ()):
                    continue
                if zs_slot is not None and zs_slot in es_slots:
                    continue
            cloned = _clone_sections([zs], "[NEW]")[0]
            if not any("CN/CHS" in x for x in cloned.lines):
                cloned.lines.append("-# 🌐 Sourced from CN/CHS beta data — EN localization pending.")
            en_secs.append(cloned)
            changed_any = True
        if changed_any:
            en_built["sections"] = en_secs

    return changed_any


def _reconcile_en_with_zh_equipment(current: BetaEquipment, zh_fields: dict) -> bool:
    """Resolve `[value unavailable]` / `[TBD]` / `???` or numeric lag on equipment via `/zh/`."""
    zh_lines = list(zh_fields.get("lines") or [])
    if not zh_lines:
        return False
    changed = False
    updated: list[str] = []
    zh_content = [ln for ln in zh_lines if not ln.startswith("-#")]
    en_content_idx = 0
    for ln in current.lines:
        if ln.startswith("-#"):
            updated.append(ln)
            continue
        zh_ln = zh_content[en_content_idx] if en_content_idx < len(zh_content) else ""
        en_content_idx += 1
        if zh_ln:
            for ph_re, needle in (
                (r"\[value unavailable\]%?", "[value unavailable]"),
                (r"\[TBD\]%?", "[TBD]"),
                (r"\?\?\?%?", "???"),
            ):
                if needle in ln:
                    ln, did = _patch_placeholder_line_from_zh(ln, zh_ln, ph_re)
                    changed = changed or did
        updated.append(ln)
    if changed and not any("[value unavailable]" in x for x in updated if not x.startswith("-#")):
        updated = [
            x for x in updated
            if "Some values are unavailable in this revision's source data" not in x
        ]
    en_eq_toks = _extract_kit_percents(updated)
    zh_eq_toks = _extract_kit_percents(zh_lines)
    if changed:
        if not any("CN/CHS" in x for x in updated):
            updated.append("-# 🌐 Values synced with authoritative CN/CHS beta data.")
        current.lines = updated
        return True
    if (
        zh_eq_toks
        and Counter(en_eq_toks) != Counter(zh_eq_toks)
        and not any("CN/CHS" in x for x in updated)
    ):
        preview = ", ".join(zh_eq_toks[:8])
        if preview:
            updated.append(
                f"-# 🌐 CN/CHS client values updated ({preview}); EN text lags in this build."
            )
            current.lines = updated
            return True
    return False


def _synthesize_kit_overview(
    game_key: str,
    name: str,
    built: dict,
    sections: list[BetaSectionEntry],
    *,
    is_new: bool,
) -> str:
    """Generate a concise Prime Formatting kit overview or revision-change summary."""
    if not sections:
        return ""
    if not is_new:
        counts: dict[str, int] = {}
        touched: list[str] = []
        for sec in sections:
            for ln in sec.lines:
                m_t = re.match(r"^\*\*(\[[A-Z ]+\])\*\*", ln.strip())
                t = m_t.group(1) if m_t else (sec.tag or "[CHANGED]")
                counts[t] = counts.get(t, 0) + 1
            short = sec.title.split("—")[0].split(":")[0].strip()
            if short and short not in touched:
                touched.append(short)
        badge_parts: list[str] = []
        for tag, label in (
            ("[BUFF]", "buff"),
            ("[NERF]", "nerf"),
            ("[REWORK]", "rework"),
            ("[ADJUSTMENT]", "adjustment"),
            ("[CHANGED]", "change"),
            ("[NEW]", "addition"),
            ("[TEXT FIX]", "wording fix"),
        ):
            n = counts.get(tag, 0)
            if n:
                plural = "es" if label.endswith("x") and n != 1 else ("s" if n != 1 else "")
                badge_parts.append(f"**{n}** {label}{plural}")
        digest = ", ".join(badge_parts) if badge_parts else f"**{len(sections)}** updated block(s)"
        scope = ", ".join(f"[{s}]" for s in touched[:5])
        if len(touched) > 5:
            scope += f", +{len(touched) - 5} more"
        return f"**Revision Changes:** Touching {scope} ({digest})."

    rarity = built.get("rarity")
    element = str(built.get("element") or "").strip()
    spec_elem = str(built.get("special_element") or "").strip()
    elem_disp = f"{element} ({spec_elem})" if spec_elem and spec_elem != element else element
    role = str(built.get("role") or "").strip()
    camp = str(built.get("camp") or "").strip()

    if game_key == "zzz":
        rank_txt = "S-Rank" if rarity == 5 else ("A-Rank" if rarity == 4 else "")
    else:
        rank_txt = f"{rarity}-Star" if rarity else ""

    lead_bits = [b for b in (rank_txt, elem_disp, role) if b]
    lead = " ".join(lead_bits) if lead_bits else "Playable"
    use_an = lead.startswith(("A-", "S-", "8")) or (
        lead[:1].upper() in ("A", "E", "I", "O")
        and not lead.upper().startswith(("ONE", "ONCE"))
    )
    article = "an" if use_an else "a"
    noun = {
        "genshin": "character",
        "starrail": "unit",
        "zzz": "Agent",
        "wuwa": "Resonator",
    }.get(game_key, "character")
    camp_clause = f" affiliated with **{camp}**" if camp else ""

    all_text = " ".join(
        ln for sec in sections for ln in sec.lines if not ln.startswith("-#") and not ln.startswith("**(")
    )
    states: list[str] = []
    for m in re.finditer(r"\[([A-Za-z][A-Za-z0-9' -]{2,28})\]", all_text):
        tok = m.group(1).strip()
        if tok in ("TBD", "NEW", "BUFF", "NERF", "CHANGED", "ADJUSTMENT", "REWORK", "TEXT FIX"):
            continue
        if tok not in states:
            states.append(tok)
        if len(states) >= 3:
            break
    for sec in sections:
        for nm, _ds in (sec.defs or ()):
            if nm not in states and len(nm) <= 28:
                states.append(nm)
            if len(states) >= 3:
                break

    focus: list[str] = []
    low = all_text.lower()
    for needle, label in (
        ("follow-up", "follow-up attacks"),
        ("super break", "Super Break"),
        ("break dmg", "Break DMG"),
        ("shield", "shielding"),
        ("restore hp", "healing"),
        ("heals", "healing"),
        ("crit dmg", "CRIT scaling"),
        ("laceration", "Laceration DMG"),
        ("sharp dmg", "Sharp DMG"),
        ("sheer", "Sheer DMG"),
        ("miasma", "Miasma interaction"),
        ("anomaly", "Anomaly buildup"),
        ("daze", "Daze / Stun"),
        ("aftershock", "Aftershock attacks"),
        ("elation", "Elation DMG"),
        ("memosprite", "Memosprite summons"),
        ("coordinated", "coordinated attacks"),
        ("nightsoul", "Nightsoul mechanics"),
        ("lunar", "Lunar reactions"),
        ("tune break", "Tune Break"),
        ("unison", "Unison mechanics"),
        ("outro skill", "Outro support"),
    ):
        if needle in low and label not in focus:
            focus.append(label)
        if len(focus) >= 3:
            break

    mech_bits: list[str] = []
    if states:
        mech_bits.append("revolving around " + ", ".join(f"[{s}]" for s in states[:3]))
    if focus:
        mech_bits.append("with " + ", ".join(focus[:3]))
    mech_clause = (" " + " ".join(mech_bits)) if mech_bits else ""
    return f"**{name}** is {article} {lead} {noun}{camp_clause}{mech_clause}."


async def build_patch(ctx, game: Game, manifest: dict, budget: dict) -> BetaPatch | None:
    """Build the next bounded beta card batch without guessing unavailable source data."""
    ns = _game_ns(game)
    kinds = NANOKA_KINDS.get(ns)
    if not ns or kinds is None:
        return None
    m_game = manifest_game(manifest, ns)
    if not m_game:
        return None
    record = ctx.state.beta_record(game.key)
    _upgrade_legacy_beta_record(record)
    live_cycle = _live_cycle(ctx, game, m_game)
    version = _next_data_version(record, m_game, live_cycle=live_cycle)
    if not version:
        return None

    if _changelog_url(game) and not _take_shared_request(budget):
        ctx.warnings.append(f"{game.short} beta: run-wide request budget reached before changelog; deferred")
        return None
    revisions, extra_ch_reqs = await _fetch_changelog_revisions(ctx, game, budget, 2)
    source_revision = _changelog_for_version(revisions, version)
    rev_label, rev_key = _revision_label(source_revision, version)
    rev_id = stable_hash([game.key, version])
    rev_records = record.get("revisions") if isinstance(record.get("revisions"), dict) else {}
    saved = rev_records.get(rev_id) if isinstance(rev_records, dict) else None
    if str(record.get("latest_rev") or "") == rev_id and (
            str(record.get("source_version") or "") == version or not record.get("source_version")):
        return None
    if isinstance(saved, dict) and saved.get("status") in ("posted", "silent") and not saved.get("more"):
        return None

    snapshots = record.get("snapshots") if isinstance(record.get("snapshots"), dict) else {}
    known_snapshots = dict(snapshots)
    patch = BetaPatch(
        game_key=game.key, beta_version=_display_version(version), rev_label=rev_label,
        rev_id=rev_id, source_url=_changelog_url(game) or f"{STATIC_BASE}/{ns}/{version}",
        source_version=version, source_revision=rev_key,
    )
    if source_revision and source_revision.get("no_changes") and (
            record.get("source_version") or snapshots):
        patch.no_changes = True
        return patch

    zh_advised = bool(source_revision and source_revision.get("zh_check_advised"))
    new_char_ids = manifest_new_ids(manifest, ns, kinds["new_keys"]["character"])
    new_weapon_ids = manifest_new_ids(manifest, ns, kinds["new_keys"]["weapon"])
    new_relic_ids = manifest_new_ids(manifest, ns, kinds["new_keys"]["relic"])
    source_entries = list(source_revision.get("entries") or []) if source_revision else []
    requests = (2 + extra_ch_reqs) if _changelog_url(game) else 1
    previous_ver = str(record.get("source_version") or "").strip()
    # When GachaBase changelog failed (`extra_ch_reqs >= 1`) during an intra-cycle beta hotfix,
    # diff static.nanoka.cc's `character.json` across the two builds so non-new buffed/reworked
    # characters (e.g. Potential Awakening updates) are discovered without GachaBase HTML.
    if (
        not source_entries
        and extra_ch_reqs >= 1
        and previous_ver
        and previous_ver != version
        and _major_minor(previous_ver) == _major_minor(version)
        and budget["max_requests"] >= 12
        and requests + 2 <= budget["max_requests"]
        and _take_shared_request(budget, 2)
    ):
        requests += 2
        char_list_kind = kinds["new_keys"]["character"]
        prev_c_idx = await _listing(ctx.fetcher, ns, previous_ver, char_list_kind)
        cur_c_idx = await _listing(ctx.fetcher, ns, version, char_list_kind)
        if prev_c_idx and cur_c_idx:
            for cid, cval in cur_c_idx.items():
                if str(cid).isdigit() and prev_c_idx.get(cid) != cval:
                    source_entries.append(_fallback_entry(ns, "character", str(cid)))

    char_entries, equip_entries = _candidate_entries(
        ns, kinds, source_entries, snapshots, new_char_ids, new_weapon_ids, new_relic_ids)

    processed = set(saved.get("processed") or []) if isinstance(saved, dict) else set()
    char_entries = [entry for entry in char_entries
                    if _entry_key(ns, "character", _entry_id(entry)) not in processed]
    equip_entries = [entry for entry in equip_entries
                     if _entry_key(ns, str(entry.get("role") or ""), _entry_id(entry)) not in processed]
    if not char_entries and not equip_entries:
        patch.no_changes = True
        return patch

    locked = record.get("locked_images") if isinstance(record.get("locked_images"), dict) else {}
    image_cache: dict[str, str] = {}
    gallery_cache: dict[str, list[str]] = {}
    char_new, char_changed, equip_count = 0, 0, 0
    listing_cache: dict[str, dict] = {}
    encore_revision = None

    async def encore_fallback(kind: str, item_id: str) -> dict | None:
        nonlocal requests, encore_revision
        if ns != "ww" or kind not in ("character", "weapon") or version != m_game.get("latest"):
            return None
        deferred_reason = ""
        for api in (ENCORE_API, ENCORE_BACKUP_API):
            if requests + 3 > budget["max_requests"] or not _take_shared_request(budget, 3):
                break
            requests += 3
            metadata_url = f"{api}/new?v=Beta"
            before = _encore_metadata(await ctx.fetcher.get_json(
                metadata_url, source="beta-encore", retries=1))
            if before is None or before[1] != version:
                continue
            if encore_revision is not None and before != encore_revision:
                deferred_reason = "between items"
                continue
            raw = await ctx.fetcher.get_json(
                f"{api}/{kind}/{item_id}?ver=latest&v=Beta", source="beta-encore", retries=1)
            after = _encore_metadata(await ctx.fetcher.get_json(
                metadata_url, source="beta-encore", retries=1))
            if after != before:
                deferred_reason = "while reading"
                continue
            normalized = _encore_detail(raw, kind, item_id, before)
            if normalized:
                encore_revision = before
                return normalized
        if deferred_reason:
            ctx.warnings.append(f"{game.short} beta: Encore build changed {deferred_reason}; deferred")
        return None

    for entry in char_entries:
        item_id = _entry_id(entry)
        if not item_id:
            continue
        snap_key = _snapshot_key_for(known_snapshots, ns, "character", item_id)
        previous_snapshot = known_snapshots.get(snap_key)
        previous = _character_from_snapshot(previous_snapshot)
        is_new = item_id in new_char_ids and previous is None
        cap = budget["max_chars"] if is_new else budget["max_changed"]
        seen = char_new if is_new else char_changed
        if (not is_new and cap == 0):
            patch.item_keys.append(_entry_key(ns, "character", item_id))
            continue
        if seen >= cap or requests + 1 > budget["max_requests"]:
            patch.more = True
            continue
        if not _take_shared_request(budget):
            patch.more = True
            continue
        char_detail_kind = kinds["new_keys"]["character"]
        data = await _detail(ctx.fetcher, ns, version, char_detail_kind, item_id)
        requests += 1
        builder = _CHARACTER_BUILDERS.get(ns)
        built = builder(data) if data and builder else None
        if not built or not built.get("sections"):
            enc_data = await encore_fallback("character", item_id)
            if enc_data:
                data = enc_data
                built = builder(data) if builder else None
        zh_data: dict | None = None
        if (
            data
            and "_encore" not in data
            and builder
            and (zh_advised or _kit_needs_zh_check(built))
            and requests + 1 <= budget["max_requests"]
            and _take_shared_request(budget, 1)
        ):
            requests += 1
            zh_data = await _detail(
                ctx.fetcher, ns, version, char_detail_kind, item_id, locale="zh"
            )
            if zh_data:
                if not built:
                    built = {"sections": []}
                _reconcile_en_with_zh_kit(built, builder(zh_data))
        if not data:
            ctx.warnings.append(f"{game.short} beta: no kit data for id {item_id}; deferred")
            patch.more = True
            continue
        if not built or not built.get("sections"):
            ctx.warnings.append(f"{game.short} beta: kit data for id {item_id} is not readable yet; deferred")
            patch.more = True
            continue

        rarity = _parse_rarity(ns, data.get("rarity"), built.get("rarity") or 5) or 5
        name = _safe_entry_name(entry, data, None, item_id, zh_data=zh_data)
        previous_revision = str((previous_snapshot or {}).get("revision") or "")
        # Self-heal pre-BETA_NORM_REV WuWa nanoka snapshots (which stored Lv. 20 param[-1]
        # multipliers) by re-building the historical revision at Lv. 10 before diffing.
        if (
            ns == "ww"
            and previous_snapshot is not None
            and previous_snapshot.get("provider", "nanoka") == "nanoka"
            and "_encore" not in data
            and int(previous_snapshot.get("norm_rev") or 0) < BETA_NORM_REV
            and previous_revision
            and previous_revision != version
            and builder is not None
            and requests + 1 <= budget["max_requests"]
            and _take_shared_request(budget, 1)
        ):
            requests += 1
            hist_data = await _detail(
                ctx.fetcher, ns, previous_revision, char_detail_kind, item_id
            )
            hist_built = builder(hist_data) if hist_data else None
            if hist_built and hist_built.get("sections"):
                hist_rar = _parse_rarity(
                    ns, (hist_data or {}).get("rarity"), hist_built.get("rarity") or rarity
                ) or rarity
                hist_char = BetaCharacter(
                    id=snap_key,
                    name=str(((previous_snapshot.get("data") or {}).get("name")) or name),
                    rarity=hist_rar,
                    element=str(hist_built.get("element") or ""),
                    role=str(hist_built.get("role") or ""),
                    subtext=str(hist_built.get("subtext") or ""),
                    stats_lines=list(hist_built.get("stats_lines") or []),
                    sections=_clone_sections(list(hist_built.get("sections") or []), "[NEW]"),
                    mechanic_defs=[],
                    image_url=str(((previous_snapshot.get("data") or {}).get("image_url")) or ""),
                    page_url=str(((previous_snapshot.get("data") or {}).get("page_url")) or ""),
                )
                rebuilt_prev = _character_snapshot(hist_char, previous_revision, ctx.now)
                if previous_snapshot.get("gallery_urls"):
                    rebuilt_prev["gallery_urls"] = list(previous_snapshot["gallery_urls"])
                previous_snapshot = rebuilt_prev
                previous = _character_from_snapshot(previous_snapshot)
        page_url = ("" if entry.get("synthetic") else str(entry.get("url") or "")) or \
            _item_page_url(ns, "character", item_id, name)
        diff_url = (str(entry.get("diff") or "") or
                    _generated_diff_url(ns, "character", item_id, name, previous_revision, version))
        if "_encore" in data:
            page_url = f"https://encore.moe/character/{item_id}?lang=en&v=Beta"
            diff_url = ""

        candidates = _data_image_candidates(ns, "character", item_id, data)
        image_entry = {"id": snap_key, "image": entry.get("image"), "candidates": candidates}
        image, requests = await _resolve_image_within_budget(
            ctx.fetcher, image_entry, locked, image_cache, budget, requests
        )
        gal_candidates = _data_gallery_candidates(ns, "character", item_id, data)
        eidolon_imgs, requests = await _resolve_gallery_within_budget(
            ctx.fetcher, snap_key, gal_candidates, locked, gallery_cache, budget, requests
        )

        elem_label = str(built.get("element") or "").strip()
        spec_elem = str(built.get("special_element") or "").strip()
        if spec_elem and spec_elem != elem_label:
            elem_label = f"{elem_label} ({spec_elem})" if elem_label else spec_elem

        full = BetaCharacter(
            id=snap_key, name=name, rarity=rarity, element=elem_label,
            role=str(built.get("role") or ""),
            faction=str(built.get("camp") or ""),
            attack_type=str(built.get("attack_type") or ""),
            subtext=str(built.get("subtext") or ""),
            stats_lines=list(built.get("stats_lines") or []),
            sections=_clone_sections(list(built.get("sections") or []), "[NEW]"),
            mechanic_defs=[], image_url=image, eidolon_images=eidolon_imgs,
            diff_url=diff_url, page_url=page_url, is_new=is_new,
        )
        cur_snap = _character_snapshot(full, version, ctx.now)
        if "_encore" in data:
            cur_snap.update(provider="encore", provider_revision=data["_encore_revision"])

        if (
            previous_snapshot is not None
            and "_encore" not in data
            and not zh_advised
            and not _kit_needs_zh_check(built)
            and _snapshot_comparison(previous_snapshot, cur_snap) == "equal"
            and builder is not None
            and requests + 1 <= budget["max_requests"]
            and _take_shared_request(budget, 1)
        ):
            requests += 1
            zh_data = await _detail(
                ctx.fetcher, ns, version, char_detail_kind, item_id, locale="zh"
            )
            if zh_data and _reconcile_en_with_zh_kit(built, builder(zh_data)):
                full.sections = _clone_sections(list(built.get("sections") or []), "[NEW]")
                cur_snap = _character_snapshot(full, version, ctx.now)

        prev_char_img = str(((previous_snapshot or {}).get("data") or {}).get("image_url") or "")
        prev_char_gal = [
            str(u) for u in (
                (previous_snapshot or {}).get("gallery_urls")
                or (((previous_snapshot or {}).get("data") or {}).get("eidolon_images"))
                or []
            )
            if u
        ]
        new_char_img_added = bool(previous_snapshot is not None and image and image != prev_char_img)
        new_char_gal_added = bool(
            previous_snapshot is not None and eidolon_imgs and len(eidolon_imgs) > len(prev_char_gal)
        )

        patch.snapshots[snap_key] = cur_snap
        if eidolon_imgs:
            patch.snapshots[snap_key]["gallery_urls"] = list(eidolon_imgs)
        known_snapshots[snap_key] = patch.snapshots[snap_key]
        patch.item_keys.append(_entry_key(ns, "character", item_id))
        cmp_mode = (
            _snapshot_comparison(previous_snapshot, patch.snapshots[snap_key])
            if previous_snapshot is not None
            else "new"
        )
        if previous is None:
            full.sections = _clone_sections(full.sections, "[NEW]" if is_new else "[CHANGED]")
            if not is_new:
                full.subtext = "-# Full readable kit snapshot — a prior baseline was not retained, so no direction is inferred."
            full.overview = _synthesize_kit_overview(
                game.key, name, built, full.sections, is_new=is_new
            )
            patch.characters.append(full)
        elif cmp_mode == "snapshot":
            full.sections = _clone_sections(full.sections, "[CHANGED]")
            full.is_new = False
            full.subtext = ("-# Full current kit — complete before/after text is unavailable; "
                            "no direction is inferred.")
            full.overview = _synthesize_kit_overview(
                game.key, name, built, full.sections, is_new=False
            )
            patch.characters.append(full)
        elif cmp_mode == "diff":
            diff_sections, unchanged = diff_kits(previous.sections, full.sections)
            diff_sections = _stat_diff(previous, full) + diff_sections
            if new_char_gal_added and not any(s.category == "Eidolons" for s in diff_sections):
                diff_sections.append(BetaSectionEntry(
                    title="Eidolon Icons", category="Eidolons", tag="[NEW]",
                    lines=["**[NEW]** Official Eidolon icons added in this beta revision."],
                ))
            if diff_sections:
                full.sections = diff_sections
                full.is_new = False
                old_rev = str((previous_snapshot or {}).get("revision") or "previous revision")
                full.subtext = (f"-# Revision diff against {old_rev}"
                                + (f" • {unchanged} unchanged block(s) omitted" if unchanged else ""))
                full.overview = _synthesize_kit_overview(
                    game.key, name, built, full.sections, is_new=False
                )
                patch.characters.append(full)
            elif new_char_img_added or new_char_gal_added:
                vis_secs: list[BetaSectionEntry] = []
                if new_char_img_added:
                    vis_secs.append(BetaSectionEntry(
                        title="Splash & Character Art", category="Visual Assets", tag="[NEW]",
                        lines=["**[NEW]** Official beta character artwork added in this revision."],
                    ))
                if new_char_gal_added:
                    vis_secs.append(BetaSectionEntry(
                        title="Eidolon Icons", category="Eidolons", tag="[NEW]",
                        lines=["**[NEW]** Official Eidolon icons added in this beta revision."],
                    ))
                full.sections = vis_secs
                full.is_new = False
                old_rev = str((previous_snapshot or {}).get("revision") or "previous revision")
                full.subtext = f"-# Visual assets added against {old_rev} • kit text unchanged"
                full.overview = _synthesize_kit_overview(
                    game.key, name, built, full.sections, is_new=False
                )
                patch.characters.append(full)
        elif cmp_mode == "equal" and (new_char_img_added or new_char_gal_added):
            vis_secs = []
            if new_char_img_added:
                vis_secs.append(BetaSectionEntry(
                    title="Splash & Character Art", category="Visual Assets", tag="[NEW]",
                    lines=["**[NEW]** Official beta character artwork added in this revision."],
                ))
            if new_char_gal_added:
                vis_secs.append(BetaSectionEntry(
                    title="Eidolon Icons", category="Eidolons", tag="[NEW]",
                    lines=["**[NEW]** Official Eidolon icons added in this beta revision."],
                ))
            full.sections = vis_secs
            full.is_new = False
            old_rev = str((previous_snapshot or {}).get("revision") or "previous revision")
            full.subtext = f"-# Visual assets added against {old_rev} • kit text unchanged"
            full.overview = _synthesize_kit_overview(
                game.key, name, built, full.sections, is_new=False
            )
            patch.characters.append(full)
        if is_new:
            char_new += 1
        else:
            char_changed += 1

    for entry in equip_entries:
        item_id = _entry_id(entry)
        role = str(entry.get("role") or _entry_role(ns, str(entry.get("url") or "")))
        if not item_id or role not in ("weapon", "relic"):
            continue
        if equip_count >= budget["max_equip"]:
            patch.more = True
            continue
        detail_kind, listing_kind = kinds[role]["detail"], kinds[role]["listing"]
        listing_cost = 0 if listing_kind in listing_cache else 1
        if requests + listing_cost + 1 > budget["max_requests"]:
            patch.more = True
            continue
        if listing_kind not in listing_cache:
            if not _take_shared_request(budget):
                patch.more = True
                continue
            listing_cache[listing_kind] = await _listing(ctx.fetcher, ns, version, listing_kind)
            requests += 1
        listing = listing_cache.get(listing_kind) or {}
        listing_entry = listing.get(item_id) if isinstance(listing, dict) else None
        if not _take_shared_request(budget):
            patch.more = True
            continue
        data = await _detail(ctx.fetcher, ns, version, detail_kind, item_id)
        requests += 1
        if not data:
            data = await encore_fallback(detail_kind, item_id)
        if not data:
            ctx.warnings.append(f"{game.short} beta: no {role} data for id {item_id}; deferred")
            patch.more = True
            continue
        fields = _equip_from_detail(ns, detail_kind, data,
                                    listing_entry if isinstance(listing_entry, dict) else {})
        if not fields.get("lines") and not fields.get("stats_line"):
            if "_encore" not in data:
                fallback = await encore_fallback(detail_kind, item_id)
                if fallback:
                    data = fallback
                    fields = _equip_from_detail(ns, detail_kind, data, {})
            if not fields.get("lines") and not fields.get("stats_line"):
                if ns == "ww" and role == "weapon":
                    patch.more = True
                    ctx.warnings.append(f"{game.short} beta: unreadable {role} id {item_id}; deferred")
                else:
                    patch.item_keys.append(_entry_key(ns, role, item_id))
                continue
        rarity = fields.get("rarity")
        if rarity is None and isinstance(listing_entry, dict):
            rarity = _parse_rarity(ns, listing_entry.get("rank"), None)
        snap_key = _snapshot_key_for(known_snapshots, ns, role, item_id)
        previous_snapshot = known_snapshots.get(snap_key)
        l_dict = listing_entry if isinstance(listing_entry, dict) else None
        candidates = _data_image_candidates(ns, role, item_id, data, l_dict)
        image_entry = {"id": snap_key, "image": entry.get("image"), "candidates": candidates}
        image, requests = await _resolve_image_within_budget(
            ctx.fetcher, image_entry, locked, image_cache, budget, requests
        )
        gal_candidates = _data_gallery_candidates(ns, role, item_id, data, l_dict)
        eq_gallery, requests = await _resolve_gallery_within_budget(
            ctx.fetcher, snap_key, gal_candidates, locked, gallery_cache, budget, requests
        )
        previous = (_equipment_from_snapshot(previous_snapshot)
                    if str((previous_snapshot or {}).get("kind") or "") == role else None)
        is_new = item_id in (new_weapon_ids if role == "weapon" else new_relic_ids) and previous is None
        name = _safe_entry_name(entry, data, listing_entry, item_id)
        previous_revision = str((previous_snapshot or {}).get("revision") or "")
        page_url = ("" if entry.get("synthetic") else str(entry.get("url") or "")) or \
            _item_page_url(ns, role, item_id, name)
        diff_url = (str(entry.get("diff") or "") or
                    _generated_diff_url(ns, role, item_id, name, previous_revision, version))
        if "_encore" in data:
            page_url = f"https://encore.moe/weapon/{item_id}?lang=en&v=Beta"
            diff_url = ""
            rarity = data.get("rarity")
        current = BetaEquipment(
            id=snap_key, name=name, category_label=str(kinds[role]["label"]), rarity=rarity,
            sub_type=str(fields.get("sub_type") or ""),
            weapon_class=str(fields.get("weapon_class") or ""),
            set_kind=str(fields.get("set_kind") or ""),
            set_parts=str(fields.get("set_parts") or ""),
            tag="[NEW]" if is_new else "[CHANGED]",
            stats_line=str(fields.get("stats_line") or ""), effect_title=str(fields.get("effect_title") or ""),
            lines=list(fields.get("lines") or []),
            glossary=list(fields.get("glossary") or []),
            image_url=image, image_urls=eq_gallery,
            diff_url=diff_url, page_url=page_url,
        )
        eq_snap = _equipment_snapshot(current, version, ctx.now, role)
        eq_needs_zh = any(
            tok in ln
            for ln in current.lines
            for tok in ("[value unavailable]", "[TBD]", "???")
        ) or name.startswith("ID ")
        if (
            "_encore" not in data
            and (
                zh_advised
                or eq_needs_zh
                or (
                    previous_snapshot is not None
                    and _snapshot_comparison(previous_snapshot, eq_snap) == "equal"
                )
            )
            and requests + 1 <= budget["max_requests"]
            and _take_shared_request(budget, 1)
        ):
            requests += 1
            zh_eq_data = await _detail(
                ctx.fetcher, ns, version, detail_kind, item_id, locale="zh"
            )
            if zh_eq_data:
                if name.startswith("ID "):
                    zh_resolved_name = _safe_entry_name(
                        entry, data, listing_entry, item_id, zh_data=zh_eq_data
                    )
                    if zh_resolved_name != name:
                        name = zh_resolved_name
                        current.name = name
                zh_fields = _equip_from_detail(
                    ns, detail_kind, zh_eq_data,
                    listing_entry if isinstance(listing_entry, dict) else {},
                )
                did_zh_eq = _reconcile_en_with_zh_equipment(current, zh_fields)
                if did_zh_eq or eq_snap["data"].get("name") != current.name:
                    eq_snap = _equipment_snapshot(current, version, ctx.now, role)

        prev_eq_img = str(((previous_snapshot or {}).get("data") or {}).get("image_url") or "")
        prev_eq_gal = [
            str(u) for u in (
                (previous_snapshot or {}).get("gallery_urls")
                or (((previous_snapshot or {}).get("data") or {}).get("image_urls"))
                or []
            )
            if u
        ]
        eq_media_added = bool(
            previous_snapshot is not None
            and ((image and image != prev_eq_img) or (eq_gallery and len(eq_gallery) > len(prev_eq_gal)))
        )

        patch.snapshots[snap_key] = eq_snap
        if eq_gallery:
            patch.snapshots[snap_key]["gallery_urls"] = list(eq_gallery)
        if "_encore" in data:
            patch.snapshots[snap_key].update(provider="encore", provider_revision=data["_encore_revision"])
        known_snapshots[snap_key] = patch.snapshots[snap_key]
        patch.item_keys.append(_entry_key(ns, role, item_id))
        eq_cmp = (
            _snapshot_comparison(previous_snapshot, patch.snapshots[snap_key])
            if previous_snapshot is not None
            else "new"
        )
        if previous is None:
            patch.equipment.append(current)
        elif eq_cmp == "snapshot":
            current.tag = "[CHANGED]"
            current.lines = [
                "-# Full current snapshot — complete before/after text is unavailable; "
                "no direction is inferred.", *current.lines,
            ]
            patch.equipment.append(current)
        elif eq_cmp == "diff":
            changed = _diff_equipment(previous, current)
            if changed:
                patch.equipment.append(changed)
            elif eq_media_added:
                patch.equipment.append(BetaEquipment(
                    id=current.id, name=current.name, category_label=current.category_label,
                    rarity=current.rarity, sub_type=current.sub_type,
                    weapon_class=current.weapon_class, set_kind=current.set_kind,
                    set_parts=current.set_parts, tag="[NEW]",
                    stats_line=current.stats_line, effect_title="",
                    lines=["**[NEW]** Official beta visual assets added in this revision."],
                    glossary=[], image_url=current.image_url, image_urls=list(current.image_urls),
                    diff_url=current.diff_url, page_url=current.page_url,
                ))
        elif eq_cmp == "equal" and eq_media_added:
            patch.equipment.append(BetaEquipment(
                id=current.id, name=current.name, category_label=current.category_label,
                rarity=current.rarity, sub_type=current.sub_type,
                weapon_class=current.weapon_class, set_kind=current.set_kind,
                set_parts=current.set_parts, tag="[NEW]",
                stats_line=current.stats_line, effect_title="",
                lines=["**[NEW]** Official beta visual assets added in this revision."],
                glossary=[], image_url=current.image_url, image_urls=list(current.image_urls),
                diff_url=current.diff_url, page_url=current.page_url,
            ))
        equip_count += 1

    patch.batch_id = stable_hash([patch.rev_id, sorted(patch.item_keys)]) if patch.item_keys else ""
    if not patch.characters and not patch.equipment:
        patch.no_changes = True
    return patch


async def run(ctx) -> None:
    """Post one bounded batch per game; a successful batch resumes the next one safely."""
    if "beta" not in ctx.settings.features or not ctx.active:
        return
    enabled = {game.key for game in ctx.games} & set(ctx.settings.beta_games)
    if not enabled:
        return
    budget = _budget(ctx)
    shared_budget = dict(budget)
    shared_budget["_shared_requests_left"] = max(0, budget["max_requests"] - 1)
    manifest = await fetch_manifest(ctx.fetcher)
    if not manifest:
        ctx.warnings.append("beta: manifest unavailable; no beta cards this run")
        return

    ephemeral = ctx.settings.dry_run or ctx.settings.test_mode
    for game in ctx.games:
        if game.key not in enabled:
            continue
        record = ctx.state.beta_record(game.key)
        migrated = _upgrade_legacy_beta_record(record)
        was_paused = bool(record.pop("paused", None))
        if (migrated or was_paused) and not ephemeral:
            if migrated:
                log.info("[%s] beta: migrated legacy state to format_epoch=%d", game.key, BETA_FORMAT_EPOCH)
            ctx.state.save()
        last_poll = int(record.get("last_poll") or 0)
        if budget["min_poll_min"] and ctx.now - last_poll < budget["min_poll_min"] * 60:
            continue
        before_requests = int(shared_budget.get("_shared_requests_left", 0))
        starved_before = len(ctx.warnings)
        try:
            patch = await build_patch(ctx, game, manifest, shared_budget)
        except Exception as exc:  # noqa: BLE001 - one game must never sink the run
            log.exception("beta: %s failed to build its patch", game.key)
            ctx.warnings.append(f"{game.short} beta: {type(exc).__name__}: {exc}")
            continue
        starved = any(
            "run-wide request budget" in w for w in ctx.warnings[starved_before:]
        )
        should_stamp_poll = (
            patch is not None
            or int(shared_budget.get("_shared_requests_left", 0)) < before_requests
            or not starved
        )
        if patch is None:
            if should_stamp_poll:
                record["last_poll"] = ctx.now
                if not ephemeral:
                    ctx.state.save()
            continue
        if patch.no_changes and not patch.characters and not patch.equipment:
            pending = (record.get("revisions") or {}).get(patch.rev_id) or {}
            if pending.get("status") == "ambiguous" or any(
                batch.get("status") != "posted" and batch.get("messages")
                for batch in (pending.get("batches") or {}).values()
            ):
                ctx.warnings.append(f"{game.short} beta: unfinished delivery became quiet; held for reconciliation")
                record["last_poll"] = ctx.now
                if not ephemeral:
                    ctx.state.save()
                continue
            record["last_poll"] = ctx.now
            if not ephemeral:
                _settle_beta_patch(ctx, record, patch, status="silent")
                ctx.state.save()
            ctx.report.append(
                f"🧪 {game.short} {patch.beta_version} beta ({patch.rev_label}): no readable changes"
                + ("; next bounded batch pending" if patch.more else "")
            )
            continue
        warn_before_deliver = len(ctx.warnings)
        await _deliver(ctx, game, patch, record, ephemeral)
        will_retry = any(
            "; will retry" in w for w in ctx.warnings[warn_before_deliver:]
        )
        if not will_retry and should_stamp_poll:
            record["last_poll"] = ctx.now
            if not ephemeral:
                ctx.state.save()


def _settle_beta_patch(ctx, record: dict, patch: BetaPatch, *, status: str) -> None:
    """Atomically retain baselines/locks after a settled delivery (or intentional no-op)."""
    record["format_epoch"] = BETA_FORMAT_EPOCH
    revisions = record.setdefault("revisions", {})
    entry = revisions.setdefault(patch.rev_id, {"ts": ctx.now, "messages": []})
    entry["ts"] = int(entry.get("ts") or ctx.now)
    entry["label"] = patch.rev_label
    entry["source_version"] = patch.source_version
    entry["source_revision"] = patch.source_revision
    processed = set(entry.get("processed") or [])
    processed.update(patch.item_keys)
    entry["processed"] = sorted(processed)
    entry["more"] = bool(patch.more)
    entry["status"] = "partial" if patch.more else status

    snapshots = record.setdefault("snapshots", {})
    for key, snapshot in patch.snapshots.items():
        if isinstance(snapshot, dict):
            snapshots[key] = snapshot
    locked = record.setdefault("locked_images", {})
    for key, snapshot in patch.snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        data = snapshot.get("data")
        image = str((data or {}).get("image_url") or "") if isinstance(data, dict) else ""
        if image:
            locked[key] = {"url": image, "locked": True, "ts": ctx.now}
        gal_urls = snapshot.get("gallery_urls")
        if isinstance(gal_urls, list) and gal_urls:
            locked[f"{key}:gallery"] = {"urls": list(gal_urls), "locked": True, "ts": ctx.now}

    if not patch.more:
        record["latest_rev"] = patch.rev_id
        record["source_version"] = patch.source_version
        record["source_revision"] = patch.source_revision


def _batch_record(record: dict, patch: BetaPatch, now: int) -> tuple[dict, dict]:
    """Get the per-batch resume record; older one-list state remains harmless."""
    revisions = record.setdefault("revisions", {})
    revision = revisions.setdefault(patch.rev_id, {"status": "partial", "ts": now, "messages": []})
    revision.setdefault("source_version", patch.source_version)
    batches = revision.setdefault("batches", {})
    batch_key = patch.batch_id or stable_hash([patch.rev_id, patch.item_keys])
    batch = batches.setdefault(batch_key, {"status": "partial", "ts": now, "messages": []})
    return revision, batch


async def _wait_between_beta_pages(ctx, *, ephemeral: bool) -> None:
    """Apply the required page hand-off without making normal dry-run tests sleep."""
    sleeper = getattr(ctx, "sleep", None)
    if sleeper is None:
        if ephemeral:
            return
        sleeper = asyncio.sleep
    await sleeper(BETA_PAGE_GAP_SECONDS)


async def _deliver(ctx, game: Game, patch: BetaPatch, record: dict, ephemeral: bool) -> None:
    hook = ctx.settings.webhook("beta", game.key)
    if not hook:
        ctx.warnings.append(
            f"{game.short} beta: missing dedicated webhook; add "
            f"{ctx.settings.expected_webhook_names('beta', game.key)}")
        return
    fp = webhook_fingerprint(hook)
    fp = stable_hash([fp, hook.partition("?")[2]])
    if record.get("route") and record.get("route") != fp:
        ctx.warnings.append(f"{game.short} beta: destination changed; review beta state before posting")
        return
    record.setdefault("route", fp)

    ping = ctx.settings.ping("beta", game.key)
    try:
        payloads, char_map = beta_payloads_with_map(game, patch, ctx.settings, ping)
    except Exception as exc:  # noqa: BLE001 - a render bug must not post a half card
        log.exception("beta: %s failed to render", game.key)
        ctx.warnings.append(f"{game.short} beta: render failed ({type(exc).__name__}: {exc})")
        return
    if ctx.settings.test_mode:
        from .cards import mark_test
        payloads = [mark_test(payload) for payload in payloads]

    batch_key = patch.batch_id or stable_hash([patch.rev_id, patch.item_keys])
    for old_revision in (record.get("revisions") or {}).values():
        for old_key, old_batch in (old_revision.get("batches") or {}).items():
            if (old_batch.get("status") != "posted" and
                    (old_batch.get("messages") or old_batch.get("ambiguous")) and
                    (old_revision is not (record.get("revisions") or {}).get(patch.rev_id)
                     or old_key != batch_key)):
                ctx.warnings.append(f"{game.short} beta: unfinished batch changed; held for reconciliation")
                return
    revision, batch = _batch_record(record, patch, ctx.now)
    if batch.get("ambiguous") or revision.get("status") == "ambiguous":
        ctx.warnings.append(
            f"{game.short} beta: prior ambiguous Discord outcome is held; reconcile the batch before retrying")
        return
    messages = batch.setdefault("messages", [])
    if len(messages) > len(payloads):
        ctx.warnings.append(f"{game.short} beta: stored batch state is inconsistent; held to prevent duplicates")
        return
    signature = stable_hash(payloads)
    if messages and batch.get("signature") != signature:
        ctx.warnings.append(f"{game.short} beta: partial payload changed or has no signature; held for reconciliation")
        return
    batch["signature"] = signature
    sent = 0
    for idx in range(len(messages), len(payloads)):
        result = await ctx.webhook.send(hook, payloads[idx], stop_on_ambiguous=True)
        if result.ambiguous or (result.ok and not result.message_id):
            batch["ambiguous"] = True
            revision["status"] = "ambiguous"
            if not ephemeral:
                ctx.state.save()
            ctx.warnings.append(
                f"{game.short} beta: ambiguous Discord outcome on page {idx + 1}; held to prevent a duplicate post")
            return
        if not result.ok:
            ctx.warnings.append(f"{game.short} beta: page {idx + 1} failed ({result.status}); will retry")
            return
        messages.append(result.message_id)
        if not ephemeral:
            ctx.state.save()
        sent += 1
        if idx + 1 < len(payloads):
            await _wait_between_beta_pages(ctx, ephemeral=ephemeral)

    if not ephemeral:
        batch["status"] = "posted"
        revision.setdefault("messages", [])
        revision["messages"].extend(mid for mid in messages if mid not in revision["messages"])
        revision["characters"] = char_map
        _settle_beta_patch(ctx, record, patch, status="posted")
        ctx.state.save()

    names = ", ".join(character.name for character in patch.characters) or "equipment changes"
    ctx.report.append(
        f"🧪 {game.short} {patch.beta_version} beta ({patch.rev_label}): "
        f"{len(payloads)} page(s) — {names}"
        + (f", {len(patch.equipment)} equipment entr(ies)" if patch.equipment else "")
        + ("; next bounded batch pending" if patch.more else "")
        + (" [DRY RUN]" if ephemeral and not ctx.settings.test_mode else "")
    )
    if sent:
        log.info("[%s] beta: delivered %d page(s) for %s", game.key, sent, patch.rev_label)
