"""Wuthering Waves (Kuro Games) official website news + global launcher index.

Endpoints (from RSSHub's kurogames/wutheringwaves route + WW downloader configs;
verified live 2026-09-25). NOTE: the English article menus are homepage SUBSETS
(they carry the official "Featured Resonator/Weapon Convene" banner notices but
not every announcement) — X stays the primary WW schedule source.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from ..config import Game
from ..http import Fetcher
from ..models import Item
from ..textutil import html_to_text, short_version
from ..timeparse import parse_kuro_time

log = logging.getLogger("gamexpress.kuro")

BASE = "https://hw-media-cdn-mingchao.kurogame.com/akiwebsite/website2.0/json/G152/en/"
MENUS = ("ArticleMenu.json", "MainMenu.json")
DETAIL = BASE + "article/{}.json"
LINK = "https://wutheringwaves.kurogames.com/en/main/news/detail/{}"
LAUNCHER_INDEX = ("https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G153/"
                  "50004_obOHXFrFanqsaIEOmuKroCcbZkQRBC7c/index.json")


async def menu_articles(fetcher: Fetcher) -> dict[int, dict]:
    """Every article in the English JSON menus, keyed by articleId (newest menu entry wins)."""
    articles: dict[int, dict] = {}
    menus = await asyncio.gather(*(fetcher.get_json(BASE + m, source="kuro", retries=1) for m in MENUS))
    for data in menus:
        rows = data.get("article") if isinstance(data, dict) else data
        for a in rows or []:
            if isinstance(a, dict) and a.get("articleId"):
                articles.setdefault(int(a["articleId"]), a)
    return articles


async def fetch_items(fetcher: Fetcher, game: Game, want: Callable[[str], bool],
                      since_ts: int) -> list[Item]:
    if not game.kuro_news:
        return []
    articles = await menu_articles(fetcher)
    picked = []
    for aid, a in sorted(articles.items(), reverse=True):
        ts = parse_kuro_time(a.get("createTime") or "") or 0
        title = (a.get("articleTitle") or "").strip()
        if ts >= since_ts and want(f"{title}\n{a.get('articleDesc') or ''}"):
            picked.append((aid, a, ts, title))
    details = await asyncio.gather(*(fetcher.get_json(DETAIL.format(aid), source="kuro", retries=1)
                                     for aid, *_ in picked))
    out: list[Item] = []
    for (aid, a, ts, title), detail in zip(picked, details):
        body = (detail or {}).get("articleContent") or a.get("articleContent") or ""
        text, links, imgs = html_to_text(body)
        cover = a.get("suggestCover") or ""
        out.append(Item(source="kuro", game=game.key, id=str(aid), url=LINK.format(aid), title=title,
                        text=text, published_ts=ts, images=([cover] if cover else []) + imgs, links=links))
    return out


def parse_launcher_index(data: dict) -> dict:
    """{'live': '3.6', 'pre': '3.7' | None} from the Kuro launcher index.json."""
    if not isinstance(data, dict):
        return {}
    default = data.get("default") or {}
    live = short_version(default.get("version") or (default.get("config") or {}).get("version"))
    pre_obj = data.get("predownload") or {}
    pre = None
    if isinstance(pre_obj, dict) and pre_obj:
        pre = short_version(pre_obj.get("version") or (pre_obj.get("config") or {}).get("version"))
    return {"live": live, "pre": pre}


async def launcher_versions(fetcher: Fetcher) -> dict:
    data = await fetcher.get_json(LAUNCHER_INDEX, source="kuro-launcher", retries=1)
    return parse_launcher_index(data) if data else {}


async def discover_news(fetcher: Fetcher, game: Game, since: int) -> list[dict] | None:
    """Kuro menu discovery; valid empty menus differ from unavailable/malformed responses."""
    if not game.kuro_news:
        return None
    picked = {}
    # English MainMenu.article is the current official index (also used by RSSHub).
    # ArticleMenu is not guaranteed to exist in English; do not require it for a valid baseline.
    for menu in ("MainMenu.json",):
        data = await fetcher.get_json(BASE + menu, source="news-kuro", retries=1)
        rows = data.get("article") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return None
        for article in rows:
            if not isinstance(article, dict):
                continue
            if article.get("articleType") == 0:
                continue
            aid = str(article.get("articleId") or "")
            ts = parse_kuro_time(article.get("createTime") or "") or 0
            if aid.isdigit() and ts >= since:
                picked[aid] = {"id": aid, "ts": ts, "title": str(article.get("articleTitle") or "")}
    return sorted(picked.values(), key=lambda row: (row["ts"], row["id"]))


async def news_article(fetcher: Fetcher, game: Game, row: dict) -> Item | None:
    from ..textutil import news_blocks

    detail = await fetcher.get_json(DETAIL.format(row["id"]), source="news-kuro", retries=1)
    if not isinstance(detail, dict) or not detail.get("articleContent"):
        return None
    if detail.get("articleId") is not None and str(detail["articleId"]) != row["id"]:
        return None
    blocks = news_blocks(detail["articleContent"])
    title = detail.get("articleTitle") or row.get("title")
    if not blocks or not title:
        return None
    return Item(source="kuro", game=game.key, id=row["id"], url=LINK.format(row["id"]),
                title=title, text="", published_ts=row["ts"], article_blocks=blocks, author="Kuro Games")
