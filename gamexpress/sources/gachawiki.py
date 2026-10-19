"""Banner line-ups read straight from each game's own Fandom wiki.

Why this exists: a banner block prints TBA until an official notice lands, and for a version
whose livestream has not aired yet that can be weeks. Every one of these games has a community
wiki that documents the line-up the moment the beta/livestream shows it, through the same
MediaWiki API — no key, pure JSON, two GET requests per game and version.

Rules (the whole point of the module):
  * it fills ONLY blanks, and it may confirm a name the notice already gave (a confirmed name is
    locked, see schedule._confirm). An official notice or config/overrides.json always wins
    (schedule.PRIORITY['gachawiki'] = 6, above the community banner feed, below everything
    official);
  * nothing is fetched while the banner block is complete AND confirmed (`banner_block_settled()`
    runs before any request, so a fully-known, locked version costs zero traffic). A complete block
    with an unconfirmed name is still read, because the wiki may be the second source that confirms it;
  * a 4★ list whose length is not exactly the game's `four_star_count` is dropped, never
    published half-right;
  * re-runs are a SET DIFFERENCE against the version's debut roster. A repeated banner title
    means nothing: HSR reused "Indelible Coterie" 14 times with disjoint casts.

Dialects (verified live 2026-10-05/06):

| game     | host                          | infobox                 | pool template        | 5★ field      | 4★ field      |
|----------|-------------------------------|-------------------------|----------------------|---------------|---------------|
| genshin  | genshin-impact.fandom.com     | Wish                    | Wish Pool            | character_5_F | character_4_F |
| starrail | honkai-star-rail.fandom.com   | Warp                    | Warp Pool            | character_5_F | character_4_F |
| zzz      | zenless-zone-zero.fandom.com  | Signal Search Infobox   | Signal Search Pool   | agent_S_F     | agent_A_F     |
| wuwa     | wutheringwaves.fandom.com     | Convene                 | Convene/Pool         | resonator_5_F | resonator_4_F |

Games with no gacha data (ANANTA, Honkai: Nexus Anima) are deliberately absent from WIKIS.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

log = logging.getLogger("gamexpress.sources.gachawiki")

# A bare product token, no version number — same rule as http.BOT_UA.
WIKI_UA = "Game-Express (banner monitor)"

MAX_TITLES_PER_CALL = 50          # MediaWiki's anonymous limit for prop=revisions


@dataclass(frozen=True)
class WikiDialect:
    host: str
    pool_template: str
    five_field: str
    four_field: str
    # Version/<X.Y> page
    debut_section: str            # heading / definition-list marker that holds the debut roster
    debut_template: str           # '{{<template>|Name}}' the roster uses ('' = wiki links)
    banner_section: str           # marker line that opens the character-banner list
    section_stop: str             # regex: a line that ends the banner list


WIKIS: dict[str, WikiDialect] = {
    "genshin": WikiDialect(
        host="genshin-impact.fandom.com",
        pool_template="Wish Pool",
        five_field="character_5_F",
        four_field="character_4_F",
        debut_section=";New Characters",
        debut_template="",
        banner_section=";Event Wishes",
        section_stop=r"^[;=]",
    ),
    "starrail": WikiDialect(
        host="honkai-star-rail.fandom.com",
        pool_template="Warp Pool",
        five_field="character_5_F",
        four_field="character_4_F",
        debut_section="===Characters===",
        debut_template="Character Intro",
        banner_section="* Character Event Warps:",
        section_stop=r"^(?:=|\*(?!\*))",
    ),
    "zzz": WikiDialect(
        host="zenless-zone-zero.fandom.com",
        pool_template="Signal Search Pool",
        five_field="agent_S_F",
        four_field="agent_A_F",
        debut_section="===Playable Agents===",
        debut_template="Intro/Agent/Full",
        banner_section="====Exclusive Channels====",
        section_stop=r"^=",
    ),
    "wuwa": WikiDialect(
        host="wutheringwaves.fandom.com",
        pool_template="Convene/Pool",
        five_field="resonator_5_F",
        four_field="resonator_4_F",
        debut_section="===Resonators===",
        debut_template="Intro/Resonator/Full",
        banner_section="===Character Event Convenes===",
        section_stop=r"^=",
    ),
}

_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_PAREN = re.compile(r"\(([^()]*)\)")
# Unannounced slots are padded on the wiki itself (Genshin writes character_4_F = Unknown
# Character x3). A placeholder is not a name and must never reach a card.
_PLACEHOLDER = re.compile(r"^(?:unknown\b.*|tba|tbd|\?+)$", re.I)
# A version page can name a SLOT instead of a character while the line-up is still unannounced:
# ZZZ writes "(Agent)", Genshin "(Character)", WuWa "(Resonator)". That is the wiki saying it
# does not know yet -- publishing one puts the literal word "Agent" on the card where a name
# belongs (ZZZ 3.3, 2026-10-06). A slot word is never a name, in any of the four dialects.
_STRUCTURAL = {
    "agent", "agents", "character", "characters", "resonator", "resonators",
    "unit", "units", "banner", "banners", "phase", "rerun", "reruns", "re-run",
    "new agent", "new character", "new resonator", "exclusive channel", "signal search",
}


def publishable_name(name: str) -> bool:
    """False for a placeholder or a slot word -- anything that is not a character's name."""
    n = (name or "").strip()
    return bool(n) and not _PLACEHOLDER.match(n) and n.casefold() not in _STRUCTURAL
_DATED = re.compile(r"/\d{4}-\d{2}-\d{2}$")
_PHASE_MARK = re.compile(r"^\*\s*Phase\s+(I{1,3}|\d)\s*:?\s*$", re.I)
_WHOLE_VERSION = re.compile(r"^\*\s*Lasting the whole version\s*:?\s*$", re.I)
_TRAIL_PHASE = re.compile(r"-\s*Phase\s*(\d)\s*$", re.I)
_ROMAN = {"I": 1, "II": 2, "III": 3}


def template_block(text: str, name: str) -> str | None:
    """The body of `{{<name>\\n …\\n}}`, anchored so `{{Wish` cannot match `{{Wish Pool`."""
    rx = re.compile(r"\{\{" + re.escape(name) + r"[ \t]*\n(.*?)\n\}\}", re.S)
    m = rx.search(text or "")
    return m.group(1) if m else None


def template_fields(text: str, name: str) -> dict[str, str]:
    """`{{Wish Pool|character_5_F = A|character_4_F = B;C}}` -> {'character_5_F': 'A', …}."""
    body = template_block(text, name)
    if body is None:
        return {}
    out: dict[str, str] = {}
    for line in body.split("\n"):
        line = line.strip()
        if not line.startswith("|") or "=" not in line:
            continue
        key, _, value = line[1:].partition("=")
        out[key.strip()] = value.strip()
    return out


def split_names(value: str) -> list[str]:
    """A pool field is never single-valued: 'A;B; C' -> ['A', 'B', 'C']."""
    parts = [p.strip() for p in re.split(r"[;\n]", value or "")]
    names = [clean_name(p) for p in parts if p.strip()]
    return [n for n in names if publishable_name(n)]


def clean_name(value: str) -> str:
    """'[[Iuno]]' / '[[Pearl|Pearl]]' / '  Hsin ' -> a bare display name."""
    value = (value or "").strip()
    m = _LINK.fullmatch(value)
    if m:
        value = (m.group(2) or m.group(1)).strip()
    return value.strip().strip("'\"").strip()


@dataclass
class BannerRef:
    """One banner found on a Version page."""
    page: str | None = None               # wiki page title, e.g. 'Bloodmoon Rising/2026-09-09'
    names: list[str] = field(default_factory=list)   # the names the Version page brackets
    phase: int = 0


@dataclass
class VersionPage:
    debuts: list[str] = field(default_factory=list)
    banners: list[BannerRef] = field(default_factory=list)


def _section_lines(wikitext: str, marker: str, stop: str) -> list[str]:
    """Lines that belong to `marker`, stopping at this wiki's own section boundary.

    Sibling sections share the list layout (HSR 'Light Cone Event Warps:', ZZZ
    '====W-Engine Channels====', WuWa 'Weapon Event Convenes'), so stopping too late silently
    mixes weapon banners into the character line-up.
    """
    lines = (wikitext or "").split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == marker)
    except StopIteration:
        return []
    stop_rx = re.compile(stop)
    out: list[str] = []
    for ln in lines[start + 1:]:
        if ln.strip() and stop_rx.match(ln.strip()):
            break
        out.append(ln)
    return out


def parse_debuts(game_key: str, wikitext: str) -> list[str]:
    """The characters that DEBUT in this version — the set re-runs are measured against."""
    d = WIKIS[game_key]
    names: list[str] = []
    for ln in _section_lines(wikitext, d.debut_section, r"^[;=]"):
        ln = ln.strip()
        if not ln.startswith("*"):
            continue
        if d.debut_template:
            m = re.search(r"\{\{" + re.escape(d.debut_template) + r"\|([^}|]+)", ln)
            if m:
                names.append(clean_name(m.group(1)))
            continue
        # Genshin writes the roster as wiki links; the icon File: link comes first.
        links = [clean_name(f"[[{t}{'|' + lbl if lbl else ''}]]")
                 for t, lbl in _LINK.findall(ln) if not t.lower().startswith("file:")]
        if links:
            names.append(links[-1])
    return [n for n in dict.fromkeys(names) if publishable_name(n)]


def _ref_from_line(line: str) -> BannerRef | None:
    links = _LINK.findall(line)
    if not links:
        return None
    page = links[0][0].strip()
    # HSR appends '- Phase 1' AFTER the name, so the bracket is not at the end of the line.
    rest = line[line.index("]]") + 2:]
    m = _PAREN.search(rest)
    # One annotation can hold SEVERAL characters: ZZZ 3.1's 'Exclusive Rescreening' is
    # ([[Dialyn]], [[Ukinami Yuzuha]], [[Asaba Harumasa]]) — one banner, three agents.
    names: list[str] = []
    if m:
        for part in m.group(1).split(","):
            name = clean_name(part)
            if publishable_name(name):
                names.append(name)
    # No bracket at all (some WuWa lines) -> the banner page resolves the names instead.
    return BannerRef(page=page, names=names)


def parse_version_page(game_key: str, wikitext: str) -> VersionPage:
    """Debut roster + the character banners of each phase, as the Version page states them."""
    d = WIKIS[game_key]
    page = VersionPage(debuts=parse_debuts(game_key, wikitext))
    phase = 0
    for raw in _section_lines(wikitext, d.banner_section, d.section_stop):
        line = raw.strip()
        if not line:
            continue
        m = _PHASE_MARK.match(line)
        if m:
            token = m.group(1).upper()
            phase = _ROMAN.get(token) or (int(token) if token.isdigit() else 0)
            continue
        if _WHOLE_VERSION.match(line):
            # ZZZ 3.1: Remielle ran the whole version, which makes it the phase-1 banner.
            phase = 1
            continue
        if not line.startswith("*"):
            continue
        ref = _ref_from_line(line)
        if ref is None:
            continue
        trailing = _TRAIL_PHASE.search(line)
        if trailing:                                   # HSR marks the phase per line
            ref.phase = int(trailing.group(1))
        elif game_key == "wuwa":
            # WuWa marks no phases at all: a dated page is phase 1, an undated one phase 2.
            ref.phase = 1 if ref.page and _DATED.search(ref.page) else 2
        else:
            ref.phase = phase
        if ref.phase in (1, 2):
            page.banners.append(ref)
    return page


def parse_banner_page(game_key: str, wikitext: str) -> dict[str, list[str]]:
    """A banner page -> {'five': [...], 'four': [...]} from its pool template."""
    d = WIKIS[game_key]
    fields = template_fields(wikitext, d.pool_template)
    if not fields:
        return {}
    return {"five": split_names(fields.get(d.five_field, "")),
            "four": split_names(fields.get(d.four_field, ""))}


def _is_debut(name: str, debuts: list[str]) -> bool:
    """ZZZ nicknames its own agents on Version pages ('Claret' for 'Claret Flint')."""
    if name in debuts:
        return True
    return any(d == name or d.startswith(name + " ") or name.startswith(d + " ") for d in debuts)


def build_lineup(game_key: str, version_wikitext: str, pages: dict[str, str],
                 four_star_count: int | None = None,
                 four_star_summary: bool = False) -> dict[str, list[str]]:
    """Everything the banner block can learn from this wiki, as schedule-ready lists.

    `pages` maps a banner page title to its wikitext (may be empty: the Version page alone is
    enough for the 5★ names and the early tier).
    """
    vp = parse_version_page(game_key, version_wikitext)
    if not vp.banners:
        # The early tier: a debut roster exists, no phase data yet.
        return {"confirmed": vp.debuts} if vp.debuts else {}

    out: dict[str, list[str]] = {}
    featured: list[str] = []
    four_by_phase: dict[int, list[str]] = {1: [], 2: []}
    for phase in (1, 2):
        five: list[str] = []
        for ref in [b for b in vp.banners if b.phase == phase]:
            pool = parse_banner_page(game_key, pages.get(ref.page or "", "")) if ref.page else {}
            names = pool.get("five") or ref.names
            for n in names:
                if n and n not in five:
                    five.append(n)
            for n in pool.get("four") or []:
                if n and n not in four_by_phase[phase]:
                    four_by_phase[phase].append(n)
        if five:
            out[f"phase{phase}"] = five
            featured.extend(five)
        four = four_by_phase[phase]
        if four and (not four_star_count or len(four) == four_star_count):
            out[f"phase{phase}_4"] = four
        elif four:
            log.info("%s phase %d: %d 4★ name(s) found, %s expected — dropped",
                     game_key, phase, len(four), four_star_count)

    if not featured:
        # The banner section exists but named no CHARACTER -- a version stub whose channels are
        # still slot words. That is the early tier with extra markup, not phase data.
        return {"confirmed": vp.debuts} if vp.debuts else {}

    reruns = [n for n in dict.fromkeys(featured) if not _is_debut(n, vp.debuts)]
    if reruns:
        out["reruns"] = reruns
    # Every name this wiki read states the version's characters in, both forms it uses them:
    # the debut roster plus the featured names. schedule._display_map matches the card's names
    # against these to render the full form ('Phoenix' -> 'Phoenix Reffaella') without ever
    # touching the stored banner data. Not a banner key: apply_gacha_wiki's fill loop never
    # reads it, and it reaches the card only as data["banners_display"].
    out["candidates"] = list(dict.fromkeys(list(vp.debuts) + featured))
    if four_star_summary:
        lists = [v for v in (out.get("phase1_4"), out.get("phase2_4")) if v]
        if lists and all(v == lists[0] for v in lists):
            out["four_star"] = lists[0]
    return out


# --------------------------------------------------------------------------- network
def version_page_url(game_key: str, version: str) -> str:
    host = WIKIS[game_key].host
    return (f"https://{host}/api.php?action=parse&page=Version%2F{quote(version)}"
            "&prop=wikitext&format=json&formatversion=2")


def revisions_url(game_key: str, titles: list[str]) -> str:
    host = WIKIS[game_key].host
    joined = quote("|".join(titles[:MAX_TITLES_PER_CALL]), safe="")
    return (f"https://{host}/api.php?action=query&prop=revisions&rvprop=content"
            f"&rvslots=main&format=json&formatversion=2&titles={joined}")


def _wikitext_of(payload: Any) -> str:
    if isinstance(payload, dict):
        parse = payload.get("parse")
        if isinstance(parse, dict):
            return str(parse.get("wikitext") or "")
    return ""


def _revisions_of(payload: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    pages = ((payload or {}).get("query") or {}).get("pages") or []
    for p in pages if isinstance(pages, list) else []:
        if not isinstance(p, dict) or p.get("ns") != 0:
            continue
        revs = p.get("revisions") or []
        if not revs:
            continue
        content = ((revs[0].get("slots") or {}).get("main") or {}).get("content")
        if content:
            out[str(p.get("title"))] = str(content)
    return out


async def fetch_lineup(fetcher, game_key: str, version: str,
                       four_star_count: int | None = None,
                       four_star_summary: bool = False) -> dict[str, list[str]] | None:
    """Two requests, and only ever for a version that still has a blank to fill.

    None means "could not read the wiki" (disabled, request failed, page missing). A dict --
    including an empty one -- means the page WAS read, and is therefore allowed to retract an
    earlier reading. Collapsing the two would let one failed request wipe a good line-up.
    """
    if fetcher is None or game_key not in WIKIS:
        return None
    headers = {"User-Agent": WIKI_UA}
    try:
        payload = await fetcher.get_json(version_page_url(game_key, version),
                                         source=f"gachawiki:{game_key}", headers=headers,
                                         retries=1)
    except Exception as e:                                      # noqa: BLE001 — one bad wiki
        log.warning("%s wiki: version page request failed: %s", game_key, e)
        return None
    wikitext = _wikitext_of(payload)
    if not wikitext:
        return None
    vp = parse_version_page(game_key, wikitext)
    titles = [b.page for b in vp.banners if b.page and _DATED.search(b.page)]
    pages: dict[str, str] = {}
    if titles:
        try:
            rev = await fetcher.get_json(revisions_url(game_key, titles),
                                         source=f"gachawiki:{game_key}", headers=headers,
                                         retries=1)
            pages = _revisions_of(rev)
        except Exception as e:                                  # noqa: BLE001
            log.warning("%s wiki: banner pages request failed: %s", game_key, e)
    return build_lineup(game_key, wikitext, pages, four_star_count, four_star_summary)
