"""Build the per-game Chinese -> English glossary from official EN and CN records.

Source: the nanoka static records (`static.nanoka.cc/<ns>/<version>/<locale>/<kind>/<id>.json`).
The EN and CN records of one item share a structure, so their strings line up by position.
Free and key-less; no translation service is called.

Rules (all must hold for a term to be written):
* A term only comes from a string whose CN side has CJK and whose EN side has none. Items whose EN
  text still lags behind (still Chinese) contribute nothing.
* Two alignments are trusted, as in `beta._zh_en_glossary`: colon-split title parts with the same
  count on both sides, and bracketed terms in the same order with the same count. A short whole
  string (no brackets or colon, EN <= 40 chars) also counts.
* A CN term is kept only when at least MIN_SUPPORT distinct items agree on the same EN reading
  and that reading covers at least MIN_SHARE of the items that contain the CN term. Any other
  spread is dropped, so a wrong alignment cannot leak into a card.

The output is reviewed before it is committed; it is not written to a live path automatically.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .beta import MANIFEST_URL, NANOKA_KINDS, STATIC_BASE, _detail, _listing

# Game key -> nanoka namespace. HNA and Ananta are excluded until they ship.
GLOSSARY_GAMES: dict[str, str] = {"zzz": "zzz", "starrail": "hsr", "genshin": "gi", "wuwa": "ww"}

MIN_SUPPORT = 2
MIN_SHARE = 0.9
SHORT_WHOLE_MAX_CN = 12
SHORT_WHOLE_MAX_EN = 40
MAX_EXAMPLE_ITEMS = 5

OUT_DIR = Path(__file__).parent / "data" / "glossary"

_CJK = re.compile(r"[\u4e00-\u9fff]")
_BRACKET = re.compile(r"\[([^\[\]\n]{1,40})\]")
_COLON = re.compile(r"[:：]\s*")
_DIGIT = re.compile(r"\d")
_BAD_TERM_CHARS = re.compile(r"[:：「」\"“”{}]")
_MARKUP = re.compile(r"[<>\\_]|\\n|\n")
_SENTENCE_PUNCT = re.compile(r"[。！？!?…，,；;]")
_SENTENCE_END = re.compile(r"[.!?…]$")
MIN_CJK_CHARS = 2
MAX_TERM_CHARS = 20
MAX_TERM_WORDS = 8


def _clean_term(zh: str, en: str) -> tuple[str, str] | None:
    """A CN -> EN pair is a glossary term only if it is a short, clean name or phrase.

    Rejected: single CJK characters (substring replacement would corrupt longer words), digits
    (values change per item), markup or tag text, sentence punctuation or sentence endings, and
    long English that reads as a sentence.
    """
    zh, en = zh.strip(), en.strip()
    if not zh or not en or not _CJK.search(zh) or _CJK.search(en):
        return None
    if len(_CJK.findall(zh)) < MIN_CJK_CHARS or len(zh) > MAX_TERM_CHARS:
        return None
    if _DIGIT.search(zh) or _DIGIT.search(en) or _MARKUP.search(zh) or _MARKUP.search(en):
        return None
    if _SENTENCE_PUNCT.search(zh) or _SENTENCE_END.search(en) or _BAD_TERM_CHARS.search(zh):
        return None
    if _BAD_TERM_CHARS.search(en) or len(en) > SHORT_WHOLE_MAX_EN or len(en.split()) > MAX_TERM_WORDS:
        return None
    return zh, en


def string_pairs(zh_s: str, en_s: str) -> list[tuple[str, str]]:
    """Term pairs from one CN string and its EN counterpart (only trusted alignments)."""
    out: list[tuple[str, str]] = []

    def add(z: str, e: str) -> None:
        c = _clean_term(z, e)
        if c:
            out.append(c)

    zh_parts, en_parts = _COLON.split(zh_s), _COLON.split(en_s)
    if len(zh_parts) > 1 and len(zh_parts) == len(en_parts):
        for z, e in zip(zh_parts, en_parts):
            add(z, e)
    zb, eb = _BRACKET.findall(zh_s), _BRACKET.findall(en_s)
    if zb and len(zb) == len(eb):
        for z, e in zip(zb, eb):
            add(z, e)
    if (not zb and len(zh_s.strip()) <= SHORT_WHOLE_MAX_CN and ":" not in en_s and "：" not in zh_s
            and not _DIGIT.search(zh_s) and not _DIGIT.search(en_s)):
        add(zh_s, en_s)
    return out


def walk_pairs(en_node: Any, zh_node: Any, out: list[tuple[str, str]]) -> None:
    """Walk two parallel JSON trees and collect term pairs from matching string leaves."""
    if isinstance(en_node, dict) and isinstance(zh_node, dict):
        if zh_node and set(zh_node) <= set(en_node):
            for key, zh_val in zh_node.items():
                walk_pairs(en_node[key], zh_val, out)
        elif len(zh_node) == len(en_node) and len(zh_node) > 0:
            # Keys that are themselves names differ by language (encore fetter groups): pair them
            # by position. The key pair is a term; the values are walked as usual.
            for (zh_key, zh_val), (en_key, en_val) in zip(zh_node.items(), en_node.items()):
                if _CJK.search(str(zh_key)) and not _CJK.search(str(en_key)):
                    out.extend(string_pairs(str(zh_key), str(en_key)))
                walk_pairs(en_val, zh_val, out)
    elif isinstance(en_node, list) and isinstance(zh_node, list) and len(en_node) == len(zh_node):
        for en_val, zh_val in zip(en_node, zh_node):
            walk_pairs(en_val, zh_val, out)
    elif (isinstance(en_node, str) and isinstance(zh_node, str)
          and _CJK.search(zh_node) and not _CJK.search(en_node) and en_node.strip()):
        out.extend(string_pairs(zh_node, en_node))


def item_pairs(en_record: dict | None, zh_record: dict | None) -> list[tuple[str, str]]:
    """Distinct term pairs from one item's EN and CN records."""
    if not isinstance(en_record, dict) or not isinstance(zh_record, dict):
        return []
    raw: list[tuple[str, str]] = []
    walk_pairs(en_record, zh_record, raw)
    return sorted(set(raw))


def identity_pair(en_record: dict | None, zh_record: dict | None) -> tuple[str, str] | None:
    """The item's own display name, CN -> EN (`薇薇安·班希` -> `Vivian Banshee`), if it is clean.

    A name belongs to one item, so the vote below would always drop it (support 1). It is kept
    instead on its own authority: the item's EN and CN records name it in the same field.
    """
    if not isinstance(en_record, dict) or not isinstance(zh_record, dict):
        return None
    en_name = str(en_record.get("name") or "").strip()
    zh_name = str(zh_record.get("name") or "").strip()
    if not en_name or not zh_name:
        return None
    return _clean_term(zh_name, en_name)


def aggregate(per_item: dict[str, list[tuple[str, str]]],
              identities: dict[str, tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """Vote across items: keep a CN term only when one EN reading has enough support and share.

    `identities` (item key -> its own name pair) add item names. A name is kept only when every
    item that carries that CN name gives the same EN name, so a conflict drops it.
    """
    votes: dict[str, Counter] = {}
    seen_in: dict[str, set[str]] = {}
    for item_id, pairs in per_item.items():
        for zh, en in set(pairs):
            votes.setdefault(zh, Counter())[en] += 1
            seen_in.setdefault(zh, set()).add(item_id)
    terms: list[dict[str, Any]] = []
    for zh, counter in votes.items():
        en, support = counter.most_common(1)[0]
        total = len(seen_in[zh])
        if support < MIN_SUPPORT or support / total < MIN_SHARE:
            continue
        examples = sorted(i for i in seen_in[zh] if any(p == (zh, en) for p in per_item[i]))
        terms.append({"zh": zh, "en": en, "support": support, "items_with_term": total,
                      "example_items": examples[:MAX_EXAMPLE_ITEMS]})
    names: dict[str, Counter] = {}
    name_items: dict[str, list[str]] = {}
    for item_id, (zh, en) in (identities or {}).items():
        names.setdefault(zh, Counter())[en] += 1
        name_items.setdefault(zh, []).append(item_id)
    for zh, counter in names.items():
        if zh in votes or len(counter) != 1:
            continue  # a term already voted, or two EN names for one CN name: never guess
        en = next(iter(counter))
        count = counter[en]
        terms.append({"zh": zh, "en": en, "support": count, "items_with_term": count,
                      "example_items": sorted(name_items[zh])[:MAX_EXAMPLE_ITEMS]})
    terms.sort(key=lambda t: (-len(t["zh"]), t["zh"]))
    return terms


def _item_ids(listing: dict) -> list[str]:
    return [str(k) for k in listing] if isinstance(listing, dict) else []


# Encore (api-v2.encore.moe) is a second WuWa source: the same EN / zh-Hans structure per record.
ENCORE_API = "https://api-v2.encore.moe/api"
# kind -> (list key in the EN list response, detail path segment)
ENCORE_KINDS: dict[str, tuple[str, str]] = {
    "character": ("roleList", "character"),
    "weapon": ("weapons", "weapon"),
    "echo": ("Echo", "echo"),
}


async def _encore_pairs(fetcher, *, max_items: int) -> dict[str, list[tuple[str, str]]]:
    """Per-record term pairs from Encore's EN and zh-Hans records (WuWa only)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for kind, (list_key, path) in ENCORE_KINDS.items():
        listing = await fetcher.get_json(f"{ENCORE_API}/en/{path}", source="glossary-encore", retries=1)
        rows = listing.get(list_key) if isinstance(listing, dict) else None
        ids = [str(r["Id"]) for r in (rows or []) if isinstance(r, dict) and "Id" in r][:max_items]

        async def one(item_id: str, kind: str = kind, path: str = path) -> tuple[str, list]:
            en = await fetcher.get_json(f"{ENCORE_API}/en/{path}/{item_id}", source="glossary-encore", retries=1)
            zh = await fetcher.get_json(f"{ENCORE_API}/zh-Hans/{path}/{item_id}", source="glossary-encore",
                                        retries=1)
            return f"encore/{kind}/{item_id}", item_pairs(en, zh)

        for key, pairs in await asyncio.gather(*(one(i) for i in ids)):
            if pairs:
                out[key] = pairs
    return out


async def build_game(fetcher, game: str, *, max_items: int = 400) -> dict[str, Any]:
    """Fetch EN and CN records for every listed item of one game and vote the glossary."""
    if game not in GLOSSARY_GAMES:
        raise ValueError(f"no glossary source for {game!r} (HNA and Ananta are excluded)")
    ns = GLOSSARY_GAMES[game]
    manifest = await fetcher.get_json(MANIFEST_URL, source="glossary-manifest", retries=1)
    version = str(((manifest or {}).get(ns) or {}).get("latest") or "")
    if not version:
        raise RuntimeError(f"manifest has no latest version for {ns}")

    kinds = NANOKA_KINDS.get(ns, {})
    detail_kinds = ["character"]
    for slot in ("weapon", "relic", "bangboo"):
        if isinstance(kinds.get(slot), dict):
            detail_kinds.append(kinds[slot]["detail"])

    targets: list[tuple[str, str]] = []
    for kind in dict.fromkeys(detail_kinds):
        listing_kind = kind
        for slot in ("weapon", "relic", "bangboo"):
            if isinstance(kinds.get(slot), dict) and kinds[slot]["detail"] == kind:
                listing_kind = kinds[slot]["listing"]
        ids = _item_ids(await _listing(fetcher, ns, version, listing_kind))
        targets.extend((kind, i) for i in ids[:max_items])

    async def one(kind: str, item_id: str) -> tuple[str, list[tuple[str, str]], tuple[str, str] | None]:
        en = await _detail(fetcher, ns, version, kind, item_id, locale="en")
        zh = await _detail(fetcher, ns, version, kind, item_id, locale="zh")
        return f"{kind}/{item_id}", item_pairs(en, zh), identity_pair(en, zh)

    results = await asyncio.gather(*(one(k, i) for k, i in targets))
    per_item = {key: pairs for key, pairs, _ident in results if pairs}
    identities = {key: ident for key, _pairs, ident in results if ident}
    encore_items = 0
    if game == "wuwa":
        encore = await _encore_pairs(fetcher, max_items=max_items)
        encore_items = len(encore)
        per_item.update(encore)
    terms = aggregate(per_item, identities)
    return {
        "game": game,
        "namespace": ns,
        "version": version,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": f"{STATIC_BASE}/{ns}/{version}/{{en,zh}}/<kind>/<id>.json"
        + (f" + {ENCORE_API}/{{en,zh-Hans}}/<kind>/<id>" if game == "wuwa" else ""),
        "items_listed": len(targets),
        "items_with_pairs": len(per_item),
        "encore_items_with_pairs": encore_items,
        "rules": {"min_support": MIN_SUPPORT, "min_share": MIN_SHARE},
        "terms": terms,
    }


def write_glossary(result: dict[str, Any], out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result['game']}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


async def run_cli(game: str, *, max_items: int, out_dir: Path) -> int:
    import aiohttp

    from .http import BOT_UA, Fetcher

    async with aiohttp.ClientSession(headers={"User-Agent": BOT_UA}) as session:
        try:
            result = await build_game(Fetcher(session, timeout=20), game, max_items=max_items)
        except (RuntimeError, ValueError) as exc:
            print(f"{game}: glossary not built: {exc}")
            return 1
    path = write_glossary(result, out_dir)
    print(f"{game}: {result['items_with_pairs']}/{result['items_listed']} items with pairs, "
          f"{len(result['terms'])} terms -> {path}")
    return 0
