"""Choose which independent database supplies a beta item's data.

The databases (nanoka, Gachabase, and Encore for WuWa) are independent teams and update at
different times. The rule is simple and does not depend on any one of them:

1. nanoka has data for the item -> nanoka supplies it.
2. nanoka has none, Gachabase has data -> Gachabase supplies it.
3. nanoka has none, Encore has data (WuWa only) -> Encore supplies it.
4. no source has data -> no card; the item waits for the next beta update.

When nanoka and Gachabase both have data, they are compared. A match is a clean post. A mismatch
still posts from nanoka (it is the first source) and is reported in the returned note, so the
disagreement is visible instead of silent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SOURCE_NANOKA = "nanoka"
SOURCE_GACHABASE = "gachabase"
SOURCE_ENCORE = "encore"


@dataclass(frozen=True)
class SourceChoice:
    source: str          # "" when no source has data (the item waits)
    data: Any = None
    note: str = ""       # non-empty only for a nanoka/Gachabase mismatch


def _has(data: Any) -> bool:
    return bool(data)


def choose_source(*, nanoka: Any = None, gachabase: Any = None, encore: Any = None,
                  game: str = "", encore_applies: bool = False,
                  same: Any = None) -> SourceChoice:
    """Pick the source for one item.

    `same(a, b)` decides whether two databases agree; it defaults to equality. Encore is only
    consulted for WuWa (`encore_applies`), and only when nanoka has nothing for the item.
    """
    compare = same or (lambda a, b: a == b)
    if _has(nanoka):
        if _has(gachabase) and not compare(nanoka, gachabase):
            return SourceChoice(SOURCE_NANOKA, nanoka,
                                note=f"{game}: nanoka and Gachabase differ; posted nanoka")
        return SourceChoice(SOURCE_NANOKA, nanoka)
    if _has(gachabase):
        return SourceChoice(SOURCE_GACHABASE, gachabase)
    if encore_applies and _has(encore):
        return SourceChoice(SOURCE_ENCORE, encore)
    return SourceChoice("")


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")


def _section_text(section: Any) -> str:
    if isinstance(section, dict):
        return " ".join(str(x) for x in (section.get("lines") or []))
    return " ".join(str(x) for x in (getattr(section, "lines", None) or []))


def kit_numbers(built: Any) -> set[str]:
    """Every number a built kit prints (`85.4%`, `1.5`), as the text shows it."""
    if isinstance(built, dict):
        sections = built.get("sections") or []
        stats = built.get("stats_lines") or []
    else:
        sections, stats = [], []
    text = " ".join([*(_section_text(s) for s in sections), *(str(x) for x in stats)])
    return set(_NUMBER_RE.findall(text.replace(",", "")))


def kit_agrees(nanoka: Any, gachabase: Any) -> bool:
    """True when every value Gachabase's skill tables print also appears in nanoka's kit.

    The two databases format a kit differently, so the comparison is on the numbers only: a
    Gachabase value that nanoka's kit does not contain is a disagreement.
    """
    values = {str(row.get("value") or "").replace(",", "")
              for table in (gachabase or {}).get("tables") or []
              for row in table.get("rows") or []}
    values = {v for v in values if v}
    return bool(values) and values <= kit_numbers(nanoka)
