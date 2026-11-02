"""Read an agent's kit from a Gachabase agent page (the beta view, as saved HTML).

Gachabase is an independent database. It is one of the update sources for beta cards, so its
kit text and stat tables are read the same way for every game it lists. Each stat table is one
skill's values at the level the page is showing; the table title is the nearest `... Stats`
heading above it. Pure stdlib: no network, no third-party HTML parser.
"""
from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_HEADING_RE = re.compile(r">\s*([^<>]+?)\s+Stats\s*<", re.S)
_TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)


class _Cells(HTMLParser):
    """Collect each table row as a list of cell texts (nested markup flattened)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _agent_name(page: str) -> str:
    match = _TITLE_RE.search(page)
    if not match:
        return ""
    title = _html.unescape(match.group(1))
    name = re.split(r"\s*\|", title, maxsplit=1)[0].strip()
    return re.sub(r"\s*\([^()]*\)\s*$", "", name).strip()     # `Mitya (Characters)` -> `Mitya`


def _label_and_id(cell: str) -> tuple[str, str]:
    """`1st-Hit DMG Multiplier 1391001` -> ('1st-Hit DMG Multiplier', '1391001')."""
    match = re.match(r"^(.*?)\s+(\d{5,})$", cell)
    return (match.group(1), match.group(2)) if match else (cell, "")


def parse_agent_page(page: str) -> dict:
    """Return the agent's name and its stat tables, in page order.

    `tables` is a list of {"title", "rows": [{"label", "id", "value"}]}. Tables with no rows, or
    with no skill title above them, are skipped rather than guessed.
    """
    headings = [(m.start(), m.group(1)) for m in _HEADING_RE.finditer(page)]
    tables: list[dict] = []
    for match in _TABLE_RE.finditer(page):
        parser = _Cells()
        parser.feed(match.group(0))
        rows = []
        for cells in parser.rows:
            if len(cells) < 2 or not cells[0] or not cells[1]:
                continue
            label, item_id = _label_and_id(cells[0])
            if label.isdigit():
                # The page's hidden raw-value copy of a table: its "label" is the item id.
                continue
            rows.append({"label": label, "id": item_id, "value": cells[1]})
        title = ""
        for position, text in headings:
            if position < match.start():
                title = text
        if rows and title:
            tables.append({"title": title, "rows": rows})
    return {"name": _agent_name(page), "tables": tables}


_IMG_RE = re.compile(r"<img\b[^>]*>", re.S | re.I)
_ATTR_RE = re.compile(r"\b(alt|src)=\"([^\"]*)\"")


def portrait_url(page: str, name: str) -> str:
    """The agent's own portrait: the Gachabase `<img alt="<name>">` served from its asset store.

    Only the signed `i.gachabase.net` asset URL is taken (the unsigned CDN path answers 500). Any
    other image, or an image whose alt is not the agent's name, is not a portrait.
    """
    if not name:
        return ""
    for tag in _IMG_RE.findall(page or ""):
        attrs = {k.lower(): _html.unescape(v) for k, v in _ATTR_RE.findall(tag)}
        src = attrs.get("src", "")
        if attrs.get("alt", "").strip() == name and src.startswith("https://i.gachabase.net/") \
                and "/assets/" in src:
            return src
    return ""


_HEADINGS = {"Talents": "Talents", "Passives": "Passives", "Constellations": "Constellations",
             "Eidolons": "Constellations", "Traces": "Passives", "Skills": "Talents"}

# The page's navigation under the kit: the first of these after a kit heading ends the kit text.
_KIT_END = {"Materials Calculator", "In-Game Build Guide", "Voicelines", "Voice Lines"}

# The kit a card can post must have these groups with text, per game. A kit missing one is
# incomplete and is not posted (Gachabase is only used when nanoka is silent).
REQUIRED_GROUPS = {"gi": ("Talents", "Passives", "Constellations"),
                   "hsr": ("Talents", "Passives", "Constellations"),
                   "zzz": ("Talents",)}


def kit_complete(built: dict | None, ns: str) -> bool:
    """True when a Gachabase-built kit has its stat block and every required group with text."""
    if not built or ns not in REQUIRED_GROUPS or not built.get("stats_lines"):
        return False
    with_text = {s.category for s in built.get("sections") or [] if s.lines}
    return all(group in with_text for group in REQUIRED_GROUPS[ns])


def kit_built(page: str) -> dict | None:
    """The card shape (as a nanoka character builder returns it), or None without kit text."""
    from .models import BetaSectionEntry

    kit = kit_from_page(page)
    sections = []
    for category, paragraphs in kit["blocks"]:
        if not paragraphs:
            continue
        sections.append(BetaSectionEntry(
            title=paragraphs[0], category=category, tag="[NEW]", meta="",
            lines=paragraphs[1:],
        ))
    if not sections:
        return None
    return {"rarity": 5, "element": "", "role": "", "camp": "", "subtext": "",
            "stats_lines": kit["stats"], "sections": sections}


def kit_from_page(page: str) -> dict:
    """Read a Gachabase agent page into the card's kit shape: sections of skill text.

    Uses the page's rendered text (the same text the site shows). Each section is a skill, passive
    or constellation: a short title line, then its paragraphs. The stat block is kept as lines.
    Returns {} when the page has no kit text. Only the values the page shows (one level) are read.
    """
    from .textutil import html_to_text

    text = html_to_text(page or "")[0] if page else ""
    parsed = parse_agent_page(page or "")
    name = parsed.get("name") or ""
    lines = [ln.strip() for ln in text.splitlines()]
    blocks: list[tuple[str, list[str]]] = []      # (category, paragraphs)
    stats: list[str] = []
    category = ""
    stat_mode = False
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph and category:
            blocks.append((category, list(paragraph)))
        paragraph.clear()

    for raw in lines:
        if category and raw in _KIT_END:
            flush()
            break                                 # navigation below the kit: nothing more is kit text
        if raw in ("Stats",):
            stat_mode = True
            continue
        if raw in _HEADINGS and raw != "Stats":
            flush()
            stat_mode = False
            category = _HEADINGS[raw]
            continue
        if stat_mode:
            if raw and not raw.startswith("Lv"):
                stats.append(raw)
            continue
        if raw in ("Skill Attributes", "Attributes", "Tooltips", "Raw Params", "Related Items",
                   "Dressing Room", "Effects", "Ingredients"):
            flush()
            category = ""                         # not card text: stop until the next heading
            continue
        if not raw:
            flush()
            continue
        if raw.startswith("Lv.") or raw.isdigit() or raw.startswith("Lv "):
            continue
        if category:
            paragraph.append(raw)
    flush()
    return {"name": name, "stats": stats, "blocks": blocks}
