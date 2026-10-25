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
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from .cards import beta_payloads_with_map
from .config import Game
from .discord import webhook_fingerprint
from .models import BetaCharacter, BetaEquipment, BetaPatch, BetaSectionEntry
from .state import stable_hash
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
    """Collapse a scraped string into one clean line (or '' when it is noise)."""
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    text = re.sub(r"</?(?:color(?:=[^>]*)?|unbreak)>", "", str(value), flags=re.I)
    if _is_key(text):
        return ""
    text = text.replace("\\n", "\n").replace("\r", " ")
    text = re.sub(r"\{LINK#[^}]*\}([^\{]*)\{/LINK\}", r"\1", text)
    text = re.sub(r"\{(?:LINK|N|S)#\d+\}", "", text)
    text = re.sub(r"\{Cus:Ipt[^}]*\}", "", text)
    text = re.sub(r"\{Cus:Sap,S=[^,]*\sP=[^}\s]*\sSapTag=\d+\}", "", text)
    text = re.sub(r"\{CAL:[^}]*\}", "", text)
    text = re.sub(r"\{param(\d+)(?::[A-Z]\d?[PI]?)?\}", r"", text)
    text = re.sub(r"\[\s*\n\s*", "[", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    # An inline input marker ("Press {Cus:Ipt…} to activate") leaves a doubled space.
    text = re.sub(r"[ \t]{2,}", " ", text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln.strip()).strip()


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
_PARAM_RE = re.compile(r"#(\d+)\[([A-Za-z])\](%?)")

# Skill values are rendered at the level the databases themselves default to.
SKILL_LEVEL = {"zzz": 12, "gi": 10, "ww": 10, "hsr": 0}


def _fmt_value(value: Any, kind: str = "i") -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    if kind == "i":
        if abs(f - round(f)) < 1e-6:
            return str(int(round(f)))
        return f"{round(f, 2):g}"
    return f"{round(f, 4):g}"


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


def _numbered_slot(title: str) -> tuple[str, str] | None:
    """Numbered progression titles are separate blocks, never fuzzy-renamed siblings."""
    normal = _norm_title(title)
    match = re.match(r"^(mindscape cinema|sequence node|constellation|eidolon|c|e)\s*(\d+)\b", normal)
    return (match.group(1), match.group(2)) if match else None


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
    r"(?:\s*\[See Changes\]\((?P<diff>[^)]*)\))?"
)
# A changelog entry wraps across three lines — the image markdown, a blank separator and
# the display name — so the entry regex has to run over the block, never line by line.
_REV_RE = re.compile(r"^##\s+(?P<label>v?[0-9][^\n|]*?)(?:\s*\|\s*(?P<date>[^\n]+))?\s*$", re.M)
_SECTION_RE = re.compile(r"^###[ \t]+(?P<name>[^\n]+?)[ \t]*$", re.M)
_ITEM_ID_RE = re.compile(r"/(?:agents|characters|resonators|w-engines|weapons|light-cones|"
                         r"lightcones|relics|relicsets|planar-ornaments|drive-discs|artifacts|"
                         r"echoes|echo)/(\d+)")


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
            self.revisions[-1]["no_changes"] = bool(re.search(
                r"\bno tracked changes detected\b", " ".join(self.body), re.I))
        self.body = []

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
            path = urlsplit(self.href).path
            entries = self.revisions[-1]["entries"]
            if path.startswith("/diff/"):
                # Pair only the same item/category; an unrelated link must not poison a card.
                if entries and path.removeprefix("/diff") == urlsplit(entries[-1]["url"]).path.removesuffix("/beta"):
                    entries[-1]["diff"] = self.href
            elif _ITEM_ID_RE.match(path) and path.endswith("/beta") and text:
                entries.append({"name": text, "url": self.href, "diff": "",
                                "image": self.image, "section": self.section})

    def close(self):
        super().close()
        self._finish_revision()


def parse_changelog(markdown: str, *, base_url: str = "") -> list[dict]:
    """Parse a gachabase beta changelog page into revisions.

    -> [{'label', 'date', 'entries': [{'name', 'url', 'diff', 'image', 'section'}]}]
    Newest revision first, exactly as the page lists them.
    """
    if re.search(r"<(?:!doctype|html|h2)\b", markdown or "", re.I):
        parser = _ChangelogHTML(base_url)
        parser.feed(markdown)
        parser.close()
        return parser.revisions
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
        for m in _ENTRY_RE.finditer(block):
            url = (m.group("url") or "").strip()
            name = re.sub(r"\s+", " ", (m.group("name") or "")).strip(" \n\t\\")
            if not name and url:
                slug = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0].replace("-", " ")
                name = slug.title()
            if not name and not url:
                continue
            section = ""
            for pos, sname in sections:
                if pos < m.start():
                    section = sname
                else:
                    break
            entries.append({
                "name": name,
                "url": url,
                "diff": (m.group("diff") or "").strip(),
                "image": (m.group("img") or "").strip(),
                "section": section,
            })
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
async def _detail(fetcher, ns: str, version: str, kind: str, item_id: str) -> dict | None:
    if not ns or not version or not item_id:
        return None
    url = f"{STATIC_BASE}/{ns}/{version}/{LOCALE}/{kind}/{item_id}.json"
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
    return _clean(html_to_text(value)[0])


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
        params = []
        for row in _encore_rows(skill.get("SkillAttributes")):
            if not isinstance(row, dict):
                continue
            label = _encore_text(row.get("attributeName"))
            values = row.get("values")
            level = SKILL_LEVEL["ww"]
            if not label or not isinstance(values, list) or len(values) < level:
                continue
            shown = _encore_text(values[level - 1])
            if not shown:
                continue
            lines.append(f"{label} (Lv. {level}): {shown}")
            if re.fullmatch(r"-?\d+(?:\.\d+)?%?", shown):
                params.append({"name": label, "value": float(shown.rstrip("%")), "text": shown})
        if lines:
            sections.append(BetaSectionEntry(title=f"{category}: {name}", category=category,
                                             lines=lines, params=params,
                                             values_label=f"Lv. {SKILL_LEVEL['ww']}" if params else ""))
    for index, node in enumerate(_encore_rows(data.get("SkillTree")), 1):
        if not isinstance(node, dict):
            continue
        name = _encore_text(node.get("PropertyNodeTitle"))
        desc = _encore_text(node.get("PropertyNodeDescribe"))
        if name and desc:
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
    return {"element": _encore_text(data.get("ElementName")),
            "role": _encore_text(data.get("WeaponTypeName")), "sections": sections,
            "stats_lines": [f"Lv. 90 — {' · '.join(stats)}"] if stats else []}


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

    # The core-skill track (`extra_level`) grants a FLAT ATK bonus and a % CRIT Rate bonus
    # at its last node; both have to be added or ATK comes out ~10% low (Robin 676 vs 750).
    extra_lv = (data.get("extra_level") or {}).get("6") or {}
    if not isinstance(extra_lv, dict):
        extra_lv = {}
    flat_atk = 0.0
    crit_bonus = 0.0
    for entry in (extra_lv.get("extra", {}).values()
                  if isinstance(extra_lv.get("extra"), dict) else ()):
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("name") or "").strip().lower()
        try:
            value = float(entry.get("value") or 0)
        except (TypeError, ValueError):
            continue
        if label.startswith("crit rate"):
            crit_bonus += value
        elif "atk" in label:
            flat_atk += value

    out: list[str] = []
    hp, atk, df = at("hp_max", "hp_growth", "hp_max"), at("attack", "attack_growth", "attack"), \
        at("defence", "defence_growth", "defence")
    if atk is not None and flat_atk:
        try:
            atk = str(int(float(atk) + flat_atk))
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
        p = _pct(float(cd) / 10000)
        if p:
            out.append(f"CRIT DMG {p}")
    for label, key in (("Impact", "break_stun"), ("Anomaly Mastery", "element_abnormal_power"),
                       ("Anomaly Proficiency", "element_mystery")):
        val = stats.get(key)
        if val not in (None, ""):
            out.append(f"{label} {_num(val)}")
    pr = stats.get("pen_rate")
    if pr is not None:
        out.append(f"PEN Ratio {_num(pr)}%")
    sharp = stats.get("sharp_critical_damage")
    if sharp:
        p = _pct(float(sharp) / 10000)
        if p:
            out.append(f"Laceration DMG {p}")
    return out


def _zzz_character(data: dict) -> dict:
    element = ""
    for v in (data.get("element_type") or {}).values():
        if isinstance(v, str) and v:
            element = v
            break
    role = ""
    for v in (data.get("weapon_type") or {}).values():
        if isinstance(v, str) and v:
            role = v
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
                name = _clean(item.get("name")) or label
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
    talent = data.get("talent") or {}
    if isinstance(talent, dict):
        for level in sorted(talent, key=lambda k: int(k) if str(k).isdigit() else 0):
            item = talent[level]
            if not isinstance(item, dict):
                continue
            desc = _clean(item.get("desc"))
            if not desc:
                continue
            sections.append(BetaSectionEntry(
                title=f"Mindscape Cinema {level}", category="Mindscape Cinema",
                tag="[NEW]", meta="", lines=_lines(desc),
            ))
    return {
        "element": element, "role": role, "camp": camp,
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


def _hsr_skill_params(item: dict) -> Any:
    """The skill's own numbers, at its highest published level."""
    levels = item.get("level")
    if isinstance(levels, dict) and levels:
        top_key = sorted(levels, key=lambda k: int(k) if str(k).isdigit() else -1)[-1]
        top = levels[top_key]
        if isinstance(top, dict) and isinstance(top.get("param_list"), list):
            return top["param_list"]
    return item.get("param_list")


def _hsr_character(data: dict) -> dict:
    sections: list[BetaSectionEntry] = []
    skills = data.get("skills") or {}
    defs: dict[str, tuple[str, str]] = {}

    def collect_extras(extra: Any) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for eid, entry in (extra.items() if isinstance(extra, dict) else ()):
            if not isinstance(entry, dict):
                continue
            nm = _clean(entry.get("name"))
            ds = _clean(entry.get("desc"))
            if nm and ds:
                defs[str(eid)] = (nm, ds)
                found.append((nm, ds))
        return found

    def render(text: str, params: Any) -> tuple[list[str], bool]:
        """One description, with its `#N[i]` numbers filled in.

        `pending` is True when a number is still in template form because this revision
        does not publish the skill's parameter list yet. The marker is kept (never
        replaced with a guess) and the block says so once.
        """
        filled, unresolved = substitute_params(text, params)
        pending = bool(_PARAM_RE.search(filled))
        lines = _lines(filled)
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
        lines, unresolved = render(item.get("desc"), _hsr_skill_params(item))
        glossary = collect_extras(item.get("extra"))
        if not name and not lines and not glossary:
            continue
        if unresolved:
            lines.append("-# ⚠️ This revision's data still holds an unresolved backend "
                         "placeholder here — the full effect is not readable yet.")
        lines.extend(f"**({nm})** {ds}" for nm, ds in glossary)
        sections.append(BetaSectionEntry(
            title=f"{tag}: {name}" if tag else name, category=str(tag or "Skills"),
            tag="[NEW]", meta=_hsr_meta(item.get("tag")), lines=lines, defs=glossary,
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
            rows: list[dict] = []
            # A stat node has no prose at all — its value IS the node.
            for add in tier.get("status_add_list") or []:
                if not isinstance(add, dict):
                    continue
                label = _clean(add.get("name"))
                value = add.get("value")
                try:
                    f_value = float(value)
                except (TypeError, ValueError):
                    continue
                if label:
                    rows.append({"name": label, "value": f_value,
                                 "text": _pct(f_value, 3) if abs(f_value) < 1 else _num(f_value, 3)})
            glossary = collect_extras(tier.get("extra"))
            if not (name or lines or rows or glossary):
                continue
            if unresolved:
                lines.append("-# ⚠️ Unresolved backend placeholder in this revision's data.")
            lines.extend(f"**({nm})** {ds}" for nm, ds in glossary)
            sections.append(BetaSectionEntry(
                title=name or "Trace", category="Traces", tag="[NEW]", meta="",
                lines=lines + [f"{r['name']} +{r['text']}" for r in rows] if rows else lines,
                params=rows, values_label="Trace" if rows else "", defs=glossary,
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
        top_key = "10" if isinstance(promote, dict) and "10" in promote else "1"
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
            values_label=f"Lv. {top_key}" if param_rows else "",
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
        return _encore_character(data["_encore"])
    sections: list[BetaSectionEntry] = []
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
        stype = str(skill.get("type") or "").strip() or "Skill"
        desc = _clean(skill.get("desc"))
        rows: list[str] = []
        param_rows: list[dict] = []
        levels = skill.get("level") or {}
        for spec_key in sorted((levels or {}), key=lambda k: int(k) if str(k).isdigit() else 0):
            spec = levels[spec_key]
            if not isinstance(spec, dict):
                continue
            label = _clean(spec.get("name"))
            params = spec.get("param") or []
            if not params or not isinstance(params[0], list):
                continue
            col = params[0]
            top = col[-1] if col else ""
            if label and top != "" and top is not None:
                rows.append(f"{label}: {top}")
                try:
                    param_rows.append({"name": label, "value": float(str(top).rstrip("%")),
                                       "text": str(top)})
                except (TypeError, ValueError):
                    pass
        if rows:
            rows = [f"**Skill Attributes (Lv. {SKILL_LEVEL['ww']})**"] + rows
        sections.append(BetaSectionEntry(
            title=f"{stype}: {name}", category=stype, tag="[NEW]", meta="",
            lines=_lines(desc) + rows, params=param_rows,
            values_label=f"Lv. {SKILL_LEVEL['ww']}" if param_rows else "",
        ))

    chains = data.get("chains") or {}
    for level in sorted(chains, key=lambda k: int(k) if str(k).isdigit() else 0):
        item = chains[level]
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"))
        desc = _clean(_ww_fill_params(item.get("desc"), item.get("param")))
        if not (name or desc):
            continue
        sections.append(BetaSectionEntry(
            title=f"Sequence Node {level}" + (f" — {name}" if name else ""),
            category="Resonance Chain", tag="[NEW]", meta="", lines=_lines(desc),
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
def _equip_from_detail(ns: str, kind: str, data: dict, listing: dict) -> dict:
    """Pull the display fields of one weapon / relic out of its detail JSON."""
    stats_line = ""
    effect_title = ""
    lines: list[str] = []
    sub_type = ""

    if ns == "ww" and kind == "weapon" and "_encore" in data:
        raw = data["_encore"]
        stats_line = " · ".join(_encore_properties(raw))
        if stats_line:
            stats_line = f"Lv. 90 — {stats_line}"
        effect_title = _encore_text(raw.get("ResonName"))
        lines = ["-# Effect values shown for refinements 1–5."] + _lines(_encore_text(raw.get("Desc")))
        sub_type = _encore_text(raw.get("WeaponTypeName"))
    elif ns == "zzz" and kind == "weapon":
        rp = data.get("rand_property") or {}
        atk = listing.get("atk")
        if atk:
            stats_line = f"Base ATK {_num(atk)}"
        sub_name = _clean(rp.get("name2") or rp.get("name"))
        if sub_name:
            sub_type = sub_name
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
        d2, d4 = _clean(data.get("desc2")), _clean(data.get("desc4"))
        if d2:
            lines.append(f"2-Piece: {d2}")
        if d4:
            lines.append(f"4-Piece: {d4}")
    elif ns == "hsr" and kind == "lightcone":
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
            # Older fixture/export shape contains already evaluated level stats.
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
        ref = data.get("refinements") or {}
        if isinstance(ref, dict):
            effect_title = _clean(ref.get("name"))
            params = ((ref.get("level") or {}).get("1") or {}).get("param_list")
            desc, _ = substitute_params(_clean(ref.get("desc")), params)
            lines = _lines(desc)
    elif ns == "hsr" and kind == "relicset":
        sets = data.get("require_num") or data.get("set") or {}
        for num in sorted(sets, key=lambda k: int(k) if str(k).isdigit() else 0):
            entry = sets[num]
            desc = _clean((entry or {}).get("desc") if isinstance(entry, dict) else entry)
            if isinstance(entry, dict):
                desc, _ = substitute_params(desc, entry.get("param_list"))
            if desc:
                lines.append(f"{num}-Piece: {desc}")
    elif ns == "gi" and kind == "weapon":
        props = data.get("weapon_prop") or []
        main = None
        for prop in props:
            if isinstance(prop, dict) and "BASE_ATTACK" in str(prop.get("prop_type") or ""):
                main = prop
                break
        if listing.get("atk") is not None:
            stats_line = f"Base ATK {_num(listing['atk'])}"
        elif isinstance(main, dict):
            stats_line = f"Lv. 1 Base ATK {_num(main.get('init_value'))}"
        ref = (data.get("refinement") or {}).get("1") or {}
        if isinstance(ref, dict):
            effect_title = _clean(ref.get("name"))
            lines = _lines(ref.get("desc"))
    elif ns == "gi" and kind == "artifact":
        sets = data.get("set") or {}
        for sid in sorted(sets, key=str):
            entry = sets[sid] or {}
            name = _clean((entry.get("name") or {}).get("en") if isinstance(entry.get("name"), dict) else entry.get("name"))
            desc = _clean((entry.get("desc") or {}).get("en") if isinstance(entry.get("desc"), dict) else entry.get("desc"))
            if desc:
                lines.append(f"{name}: {desc}" if name else desc)
    elif ns == "ww" and kind == "weapon":
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
        skill = data.get("skill") or {}
        if isinstance(skill, dict):
            lines = _lines(skill.get("desc"))
        groups = data.get("group") or {}
        for gid in sorted(groups, key=str):
            group = groups[gid] or {}
            gname = _clean(group.get("name"))
            for num in sorted((group.get("set") or {}), key=lambda k: int(k) if str(k).isdigit() else 0):
                entry = (group.get("set") or {})[num] or {}
                desc = _clean(entry.get("desc"))
                if desc:
                    lines.append(f"{gname} {num}-Piece: {desc}" if gname else f"{num}-Piece: {desc}")

    # An incomplete upstream parameter table must not publish raw backend tokens.
    unresolved = re.compile(r"#\d+\[[A-Za-z]\]%?|\{\d+\}")
    if any(unresolved.search(line) for line in lines):
        lines = [unresolved.sub("[value unavailable]", line) for line in lines]
        lines.append("-# Some values are unavailable in this revision's source data.")
    return {"stats_line": stats_line, "effect_title": effect_title, "lines": lines,
            "sub_type": sub_type}


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
    """Resolve one verified art URL once, then reuse the lock without another request.

    The changelog's thumbnail is preferred.  Structured exports sometimes omit it, in which
    case a small, known static-asset candidate list is tried.  An unavailable candidate is
    never posted and never turns a card build into an error.
    """
    key = entry.get("id") or entry.get("name") or ""
    if not key:
        return ""
    if key in cache:
        return cache[key]
    cached = locked.get(key)
    if isinstance(cached, dict) and cached.get("url"):
        cache[key] = str(cached["url"])
        return cache[key]
    urls = [entry.get("image")]
    urls.extend(entry.get("candidates") or [])
    seen: set[str] = set()
    for raw in urls:
        url = _usable_image(str(raw or ""))
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            ok = await fetcher.verify_media(url)
        except Exception:  # noqa: BLE001 - a preflight failure must never break a card
            ok = False
        if ok:
            cache[key] = url
            return url
    cache[key] = ""
    return ""


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


async def _changelog_revisions(ctx, game: Game) -> list[dict]:
    """Read all available beta revisions once; an unavailable changelog is non-fatal."""
    ch_url = _changelog_url(game)
    if not ch_url:
        return []
    text = await ctx.fetcher.get_text(ch_url, source="beta-changelog", retries=1)
    if not text:
        ctx.warnings.append(f"{game.short} beta: changelog unavailable; using the manifest safely")
        return []
    revisions = parse_changelog(text, base_url=ch_url)
    if not revisions:
        ctx.warnings.append(f"{game.short} beta: changelog unreadable; using the manifest safely")
    return revisions


def _revision_build(value: str) -> int | None:
    """The numeric build from `3.4.1+19850203` or `v3.4.1 Rev. 19850203`."""
    match = re.search(r"(?:\+|\bRev\.\s*)(\d{5,})\b", str(value or ""), re.I)
    try:
        return int(match.group(1)) if match else None
    except (TypeError, ValueError):
        return None


def _available_beta_versions(m_game: dict) -> list[str]:
    """Ordered, de-duplicated static data builds. `latest` is always the final fallback."""
    versions: list[str] = []
    for raw in list(m_game.get("available") or []) + [m_game.get("latest")]:
        version = str(raw or "").strip()
        if version and version not in versions:
            versions.append(version)
    return versions


def _next_data_version(record: dict, m_game: dict) -> str:
    """Walk a beta revision at a time; a first run intentionally starts at the current build."""
    # Finish the durable in-flight build even if the provider publishes another one.
    for revision in (record.get("revisions") or {}).values():
        if (isinstance(revision, dict) and revision.get("status") in ("partial", "ambiguous")
                and revision.get("source_version")):
            return str(revision["source_version"])
    versions = _available_beta_versions(m_game)
    latest = str(m_game.get("latest") or "").strip()
    if not versions or not latest:
        return ""
    last = str(record.get("source_version") or "").strip()
    if not last:
        return latest
    if last in versions:
        pos = versions.index(last)
        return versions[pos + 1] if pos + 1 < len(versions) else ""
    # Providers prune old builds.  The build number lets us still choose the first newer
    # retained one instead of jumping blindly to the end.
    previous_build = _revision_build(last)
    if previous_build is not None:
        newer = [(idx, _revision_build(version), version) for idx, version in enumerate(versions)
                 if _revision_build(version) is not None and _revision_build(version) > previous_build]
        if newer:
            return min(newer, key=lambda row: (row[1], row[0]))[2]
        # Build counters can restart on a new beta series. A changed display version is a real
        # new source generation even when its raw build number is numerically lower.
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
    # Do not recover this id by reparsing the human-facing URL: WuWa intentionally uses
    # singular `character` / `weapon` paths, whereas gachabase uses plural paths. Keeping the
    # manifest id explicit makes every namespace's bounded fallback equally deliverable.
    return {"id": str(item_id), "name": "", "url": url, "diff": "", "image": "", "section": "",
            "role": role, "synthetic": True}


def _entry_key(ns: str, role: str, item_id: str) -> str:
    return f"{role}:{ns}:{item_id}"


def _snapshot_key(ns: str, item_id: str) -> str:
    # Kept compatible with the first beta implementation's state layout.
    return f"{ns}:{item_id}"


def _typed_snapshot_key(ns: str, role: str, item_id: str) -> str:
    """Collision-safe state key for an item whose legacy numeric key is occupied.

    Most upstream ids are globally distinct, so preserving the original ``ns:id`` key keeps
    existing state compact and backwards-compatible.  The APIs do not contractually guarantee
    that a character, weapon, and relic can never share an integer, though.  Only the second
    colliding role needs this typed form; it prevents one item's snapshot or locked art from
    being mistaken for another's.
    """
    return f"{ns}:{role}:{item_id}"


def _snapshot_key_for(snapshots: dict, ns: str, role: str, item_id: str) -> str:
    """Choose a compatible key without letting cross-kind numeric ids alias each other."""
    legacy = _snapshot_key(ns, item_id)
    typed = _typed_snapshot_key(ns, role, item_id)
    # Once a collision was written with a typed key, keep using it even if the old key remains.
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
    # Weapon/relic ids are not promised to share a global namespace.  Keeping their role in
    # this local key means a changelog or manifest with both ``weapon:42`` and ``relic:42``
    # gets two bounded probes rather than silently dropping one.
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

    # A newly revealed item is the only thing we must still discover if a changelog rendering
    # is partial.  A snapshot is the other bounded probe: it lets an empty hotfix page still
    # detect a change to a character that has already been posted.
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
    full = {key: getattr(item, key) for key in data if key not in ("image_url", "page_url")}
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
    return _snapshot_record(character, data, "character", revision, now)


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
        "stats_line": _clip(equipment.stats_line, 400), "effect_title": _clip(equipment.effect_title, 200),
        "lines": [_clip(line) for line in equipment.lines[:32]], "image_url": equipment.image_url,
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
        sub_type=str(data.get("sub_type") or ""), stats_line=str(data.get("stats_line") or ""),
        effect_title=str(data.get("effect_title") or ""),
        lines=[str(line) for line in data.get("lines") or []],
        image_url=str(data.get("image_url") or ""), page_url=str(data.get("page_url") or ""),
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
        rarity=current.rarity, sub_type=current.sub_type, tag="[CHANGED]",
        stats_line="", effect_title="", lines=changes,
        image_url=current.image_url, diff_url=current.diff_url, page_url=current.page_url,
    )


def _data_image_candidates(ns: str, role: str, item_id: str, data: dict, listing: dict | None = None) -> list[str]:
    """Known static asset conventions, all preflighted before they are used or locked."""
    candidates: list[str] = []
    icon = str(data.get("icon") or "").strip()
    if ns == "zzz" and role == "character" and re.fullmatch(r"[A-Za-z0-9_]+", icon):
        candidates.append(f"{STATIC_BASE}/assets/zzz/{icon}.webp")
    if ns == "hsr" and role == "character":
        candidates.append(f"{STATIC_BASE}/assets/hsr/avatardrawcard/{item_id}.webp")
    if ns == "gi" and role == "character":
        candidates.append(f"{STATIC_BASE}/assets/gi/avatardrawcard/{item_id}.webp")
    if ns == "zzz" and role == "weapon":
        listing_icon = str((listing or {}).get("icon") or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_]+", listing_icon):
            candidates.append(f"{STATIC_BASE}/assets/zzz/{listing_icon}.webp")
    return candidates


def _safe_entry_name(entry: dict, data: dict, listing: dict | None, item_id: str) -> str:
    if "_encore" in data:
        return _clean(data.get("name")) or f"ID {item_id}"
    entry_name = str(entry.get("name") or "").strip()
    if entry_name.lower().startswith("id:") or _is_key(entry_name):
        entry_name = ""
    localized = (listing or {}).get("en") if isinstance(listing, dict) else ""
    listing_name = _clean(localized.get("name") if isinstance(localized, dict) else localized)
    data_name = _clean(data.get("name")) if isinstance(data, dict) else ""
    return entry_name or listing_name or data_name or f"ID {item_id}"


def _slug(value: str) -> str:
    """Gachabase's stable URL fallback; source-provided URLs always win."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug or "item"


def _item_page_url(ns: str, role: str, item_id: str, name: str) -> str:
    if ns == "ww":
        return f"https://ww.nanoka.cc/{role}/{item_id}/"
    return f"https://{ns}.gachabase.net/{_public_path(ns, role)}/{item_id}/{_slug(name)}/beta?lang=en"


def _generated_diff_url(ns: str, role: str, item_id: str, name: str,
                        previous_version: str, current_version: str) -> str:
    """A deterministic diff link for a blank hotfix revision (e.g. 19850203)."""
    if ns == "ww" or not previous_version or not current_version:
        return ""
    before = str(previous_version).replace("+", "_")
    after = str(current_version).replace("+", "_")
    return (f"https://{ns}.gachabase.net/diff/{_public_path(ns, role)}/{item_id}/{_slug(name)}"
            f"?prev=beta_{before}&cur=beta_{after}&lang=en")


async def build_patch(ctx, game: Game, manifest: dict, budget: dict) -> BetaPatch | None:
    """Build the next bounded beta card batch without guessing unavailable source data.

    A manifest tells us the immutable/static-data revisions in order.  A gachabase changelog
    adds source links and changed ids; when a revision is blank (the 19850203 hotfix case),
    only new ids and already-snapshotted ids are probed.  That preserves site friendliness and
    still detects a real kit change with an exact old baseline.
    """
    ns = _game_ns(game)
    kinds = NANOKA_KINDS.get(ns)
    if not ns or kinds is None:
        return None
    m_game = manifest_game(manifest, ns)
    if not m_game:
        return None
    record = ctx.state.beta_record(game.key)
    version = _next_data_version(record, m_game)
    if not version:
        return None

    if _changelog_url(game) and not _take_shared_request(budget):
        ctx.warnings.append(f"{game.short} beta: run-wide request budget reached before changelog; deferred")
        return None
    revisions = await _changelog_revisions(ctx, game)
    source_revision = _changelog_for_version(revisions, version)
    rev_label, rev_key = _revision_label(source_revision, version)
    rev_id = stable_hash([game.key, version])
    rev_records = record.get("revisions") if isinstance(record.get("revisions"), dict) else {}
    saved = rev_records.get(rev_id) if isinstance(rev_records, dict) else None
    if str(record.get("latest_rev") or "") == rev_id and (
            str(record.get("source_version") or "") == version or not record.get("source_version")):
        return None
    if isinstance(saved, dict) and saved.get("status") == "posted" and not saved.get("more"):
        return None

    snapshots = record.get("snapshots") if isinstance(record.get("snapshots"), dict) else {}
    # Include candidates staged into this patch as we build it.  That makes an uncommon
    # character/weapon/relic numeric-id collision safe even before `_settle_beta_patch` writes
    # the first item's baseline into the durable record.
    known_snapshots = dict(snapshots)
    patch = BetaPatch(
        game_key=game.key, beta_version=_display_version(version), rev_label=rev_label,
        rev_id=rev_id, source_url=_changelog_url(game) or f"{STATIC_BASE}/{ns}/{version}",
        source_version=version, source_revision=rev_key,
    )
    # Source says this exact build is quiet: once a baseline exists, advance the cursor without
    # empty-card spam. On a cold opt-in we still publish the currently-new characters' first
    # full-kit overview — otherwise enabling the feature after a no-change micro-hotfix would
    # permanently skip the beta reveal the operator explicitly opted into.
    if source_revision and source_revision.get("no_changes") and (
            record.get("source_version") or snapshots):
        patch.no_changes = True
        return patch

    new_char_ids = manifest_new_ids(manifest, ns, kinds["new_keys"]["character"])
    new_weapon_ids = manifest_new_ids(manifest, ns, kinds["new_keys"]["weapon"])
    new_relic_ids = manifest_new_ids(manifest, ns, kinds["new_keys"]["relic"])
    source_entries = list(source_revision.get("entries") or []) if source_revision else []
    char_entries, equip_entries = _candidate_entries(
        ns, kinds, source_entries, snapshots, new_char_ids, new_weapon_ids, new_relic_ids)

    processed = set(saved.get("processed") or []) if isinstance(saved, dict) else set()
    char_entries = [entry for entry in char_entries
                    if _entry_key(ns, "character", _entry_id(entry)) not in processed]
    equip_entries = [entry for entry in equip_entries
                     if _entry_key(ns, str(entry.get("role") or ""), _entry_id(entry)) not in processed]
    if not char_entries and not equip_entries:
        # All bounded batches for this revision were delivered.  A no-card settlement writes
        # the final cursor, which prevents a future scheduler run from retrying them.
        patch.no_changes = True
        return patch

    locked = record.get("locked_images") if isinstance(record.get("locked_images"), dict) else {}
    image_cache: dict[str, str] = {}
    requests = 2 if _changelog_url(game) else 1
    char_new, char_changed, equip_count = 0, 0, 0
    listing_cache: dict[str, dict] = {}
    encore_revision = None

    async def encore_fallback(kind: str, item_id: str) -> dict | None:
        nonlocal requests, encore_revision
        # Encore exposes a mutable current beta view, not the ordered nanoka archive.
        # Never use it to reconstruct an older build or issue an unbudgeted request.
        if ns != "ww" or kind not in ("character", "weapon") or version != m_game.get("latest"):
            return None
        deferred_reason = ""
        # The published legacy server uses /en, not /api/en. Each attempt must bracket
        # its own detail read: never borrow metadata from the other server. Reserve the
        # complete attempt even when its first request fails, keeping both budgets bounded.
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
            # Explicit operator choice: do not publish existing-character changes.  Keep the
            # revision cursor moving rather than creating an unbounded retry loop.
            patch.item_keys.append(_entry_key(ns, "character", item_id))
            continue
        if seen >= cap or requests + 1 > budget["max_requests"]:
            patch.more = True
            continue
        if not _take_shared_request(budget):
            patch.more = True
            continue
        data = await _detail(ctx.fetcher, ns, version, kinds["new_keys"]["character"], item_id)
        requests += 1
        builder = _CHARACTER_BUILDERS.get(ns)
        built = builder(data) if data and builder else None
        if not built or not built.get("sections"):
            data = await encore_fallback("character", item_id)
            built = builder(data) if data and builder else None
        if not data:
            ctx.warnings.append(f"{game.short} beta: no kit data for id {item_id}; deferred")
            patch.more = True
            continue
        if not built or not built.get("sections"):
            ctx.warnings.append(f"{game.short} beta: kit data for id {item_id} is not readable yet; deferred")
            patch.more = True
            continue
        try:
            raw_rank = int(data.get("rarity"))
        except (TypeError, ValueError):
            raw_rank = 4
        rarity = raw_rank + 1 if ns == "zzz" and raw_rank in (3, 4) else max(1, min(5, raw_rank))
        name = _safe_entry_name(entry, data, None, item_id)
        previous_revision = str((previous_snapshot or {}).get("revision") or "")
        page_url = ("" if entry.get("synthetic") else str(entry.get("url") or "")) or \
            _item_page_url(ns, "character", item_id, name)
        diff_url = (str(entry.get("diff") or "") or
                    _generated_diff_url(ns, "character", item_id, name, previous_revision, version))
        if "_encore" in data:
            page_url = f"https://encore.moe/character/{item_id}?lang=en&v=Beta"
            diff_url = ""
        candidates = _data_image_candidates(ns, "character", item_id, data)
        image_entry = {"id": snap_key, "image": entry.get("image"), "candidates": candidates}
        image_cost = _image_preflight_cost(image_entry, locked, image_cache)
        image = ""
        if (requests + image_cost <= budget["max_requests"]
                and _take_shared_request(budget, image_cost)):
            image = await _resolve_image(ctx.fetcher, image_entry, locked, image_cache)
            requests += image_cost
        full = BetaCharacter(
            id=snap_key, name=name, rarity=rarity, element=str(built.get("element") or ""),
            role=str(built.get("role") or ""), subtext=str(built.get("subtext") or ""),
            stats_lines=list(built.get("stats_lines") or []),
            sections=_clone_sections(list(built.get("sections") or []), "[NEW]"),
            # Per-skill glossary entries sit directly under their skill; do not duplicate the
            # same definition as a separate page-end appendix.
            mechanic_defs=[], image_url=image, diff_url=diff_url,
            page_url=page_url, is_new=is_new,
        )
        patch.snapshots[snap_key] = _character_snapshot(full, version, ctx.now)
        if "_encore" in data:
            patch.snapshots[snap_key].update(provider="encore", provider_revision=data["_encore_revision"])
        known_snapshots[snap_key] = patch.snapshots[snap_key]
        patch.item_keys.append(_entry_key(ns, "character", item_id))
        if previous is None:
            full.sections = _clone_sections(full.sections, "[NEW]" if is_new else "[CHANGED]")
            if not is_new:
                full.subtext = "-# Full readable kit snapshot — a prior baseline was not retained, so no direction is inferred."
            patch.characters.append(full)
        elif _snapshot_comparison(previous_snapshot, patch.snapshots[snap_key]) == "snapshot":
            full.sections = _clone_sections(full.sections, "[CHANGED]")
            full.is_new = False
            full.subtext = ("-# Full current kit — complete before/after text is unavailable; "
                            "no direction is inferred.")
            patch.characters.append(full)
        elif _snapshot_comparison(previous_snapshot, patch.snapshots[snap_key]) == "diff":
            diff_sections, unchanged = diff_kits(previous.sections, full.sections)
            diff_sections = _stat_diff(previous, full) + diff_sections
            if diff_sections:
                full.sections = diff_sections
                full.is_new = False
                old_rev = str((previous_snapshot or {}).get("revision") or "previous revision")
                full.subtext = (f"-# Revision diff against {old_rev}"
                                + (f" • {unchanged} unchanged block(s) omitted" if unchanged else ""))
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
        rarity = None
        if isinstance(listing_entry, dict):
            try:
                rarity = int(listing_entry.get("rank") or 0) or None
            except (TypeError, ValueError):
                pass
        snap_key = _snapshot_key_for(known_snapshots, ns, role, item_id)
        previous_snapshot = known_snapshots.get(snap_key)
        candidates = _data_image_candidates(ns, role, item_id, data,
                                             listing_entry if isinstance(listing_entry, dict) else None)
        image_entry = {"id": snap_key, "image": entry.get("image"), "candidates": candidates}
        image_cost = _image_preflight_cost(image_entry, locked, image_cache)
        image = ""
        if (requests + image_cost <= budget["max_requests"]
                and _take_shared_request(budget, image_cost)):
            image = await _resolve_image(ctx.fetcher, image_entry, locked, image_cache)
            requests += image_cost
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
            sub_type=str(fields.get("sub_type") or ""), tag="[NEW]" if is_new else "[CHANGED]",
            stats_line=str(fields.get("stats_line") or ""), effect_title=str(fields.get("effect_title") or ""),
            lines=list(fields.get("lines") or []), image_url=image,
            diff_url=diff_url, page_url=page_url,
        )
        patch.snapshots[snap_key] = _equipment_snapshot(current, version, ctx.now, role)
        if "_encore" in data:
            patch.snapshots[snap_key].update(provider="encore", provider_revision=data["_encore_revision"])
        known_snapshots[snap_key] = patch.snapshots[snap_key]
        patch.item_keys.append(_entry_key(ns, role, item_id))
        if previous is None:
            patch.equipment.append(current)
        elif _snapshot_comparison(previous_snapshot, patch.snapshots[snap_key]) == "snapshot":
            current.tag = "[CHANGED]"
            current.lines = [
                "-# Full current snapshot — complete before/after text is unavailable; "
                "no direction is inferred.", *current.lines,
            ]
            patch.equipment.append(current)
        elif _snapshot_comparison(previous_snapshot, patch.snapshots[snap_key]) == "diff":
            changed = _diff_equipment(previous, current)
            if changed:
                patch.equipment.append(changed)
        equip_count += 1

    patch.batch_id = stable_hash([patch.rev_id, sorted(patch.item_keys)]) if patch.item_keys else ""
    # A fully inspected quiet revision should advance its cursor, but it should never emit an
    # empty Discord card.  A partially inspected one remains pending for the next bounded run.
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
    # One manifest is shared by every game. The rest of the ceiling is a mutable ledger passed
    # through every changelog/detail/listing/media call below — `BETA_MAX_REQUESTS` is truly
    # feature-wide, not 48 requests once per game.
    shared_budget = dict(budget)
    shared_budget["_shared_requests_left"] = max(0, budget["max_requests"] - 1)
    manifest = await fetch_manifest(ctx.fetcher)
    if not manifest:
        ctx.warnings.append("beta: manifest unavailable; no beta cards this run")
        return

    for game in ctx.games:
        if game.key not in enabled:
            continue
        record = ctx.state.beta_record(game.key)
        last_poll = int(record.get("last_poll") or 0)
        if budget["min_poll_min"] and ctx.now - last_poll < budget["min_poll_min"] * 60:
            continue
        ephemeral = ctx.settings.dry_run or ctx.settings.test_mode
        before_requests = int(shared_budget.get("_shared_requests_left", 0))
        try:
            patch = await build_patch(ctx, game, manifest, shared_budget)
        except Exception as exc:  # noqa: BLE001 - one game must never sink the run
            log.exception("beta: %s failed to build its patch", game.key)
            ctx.warnings.append(f"{game.short} beta: {type(exc).__name__}: {exc}")
            continue
        # Do not mark a game polled when the global ledger denied even its changelog. That
        # prevents a low cap from starving later games for a full polling interval.
        if patch is not None or int(shared_budget.get("_shared_requests_left", 0)) < before_requests:
            record["last_poll"] = ctx.now
            if not ephemeral:
                ctx.state.save()
        if patch is None:
            continue
        if patch.no_changes and not patch.characters and not patch.equipment:
            pending = (record.get("revisions") or {}).get(patch.rev_id) or {}
            if pending.get("status") == "ambiguous" or any(
                batch.get("status") != "posted" and batch.get("messages")
                for batch in (pending.get("batches") or {}).values()
            ):
                ctx.warnings.append(f"{game.short} beta: unfinished delivery became quiet; held for reconciliation")
                continue
            if not ephemeral:
                _settle_beta_patch(ctx, record, patch, status="silent")
                ctx.state.save()
            ctx.report.append(
                f"🧪 {game.short} {patch.beta_version} beta ({patch.rev_label}): no readable changes"
                + ("; next bounded batch pending" if patch.more else "")
            )
            continue
        await _deliver(ctx, game, patch, record, ephemeral)


def _settle_beta_patch(ctx, record: dict, patch: BetaPatch, *, status: str) -> None:
    """Atomically retain baselines/locks after a settled delivery (or intentional no-op)."""
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
    """Apply the required page hand-off without making normal dry-run tests sleep.

    A real enabled monitor has no injected collaborator and therefore awaits asyncio.sleep(5).
    Dry-run/test contexts intentionally skip a real wall-clock pause unless they supply `ctx.sleep`;
    the deterministic feature harness supplies one and records every requested five-second gap.
    """
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
    # A timeout after Discord accepted the POST cannot be distinguished from a true failed POST.
    # Persisting it as ambiguous is deliberately a circuit breaker, not just a label: a later
    # scheduler invocation must not resend page one and turn uncertainty into a duplicate. An
    # operator can reconcile the message and clear/repair this tiny batch record explicitly.
    if batch.get("ambiguous") or revision.get("status") == "ambiguous":
        ctx.warnings.append(
            f"{game.short} beta: prior ambiguous Discord outcome is held; reconcile the batch before retrying")
        return
    messages = batch.setdefault("messages", [])
    # A corrupt/outdated state must never be allowed to skip a fresh payload by accident.
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
        # The successful message id is durable before waiting. If a runner dies in this gap,
        # the next pass resumes at the next page rather than duplicating this one; it also must
        # not replay a five-second gap that already belongs to a prior invocation.
        if idx + 1 < len(payloads):
            await _wait_between_beta_pages(ctx, ephemeral=ephemeral)

    if not ephemeral:
        batch["status"] = "posted"
        # Preserve the historical top-level messages list for diagnostics/readability while
        # per-batch lists carry the actual retry cursor.
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
