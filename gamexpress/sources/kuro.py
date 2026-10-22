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


def _extract_media_entry(section: dict, keys: tuple[str, ...]) -> tuple[str, dict]:
    """Return `(raw_url, asset_meta)` preserving query strings and binding metadata."""
    cfg = section.get("config") if isinstance(section.get("config"), dict) else {}
    section_hash = str(
        cfg.get("indexFileMd5")
        or section.get("indexFileMd5")
        or cfg.get("md5")
        or section.get("md5")
        or section.get("hash")
        or section.get("sha256")
        or section.get("etag")
        or ""
    ).strip()
    section_size = str(cfg.get("size") or section.get("size") or section.get("fileSize") or "").strip()
    full_ver = str(section.get("version") or cfg.get("version") or "").strip()

    for key in keys:
        val = section.get(key)
        if isinstance(val, str) and val.strip():
            meta = {
                "full_version": full_ver,
                "hash": str(section.get(f"{key}Md5") or section.get(f"{key}_md5") or section_hash).strip(),
                "size": str(section.get(f"{key}Size") or section.get(f"{key}_size") or section_size).strip(),
                "asset_id": str(section.get(f"{key}Id") or section.get(f"{key}_id") or section.get("id") or "").strip(),
            }
            return val.strip(), {k: v for k, v in meta.items() if v}
        if isinstance(val, dict) and isinstance(val.get("url"), str) and val["url"].strip():
            meta = {
                "full_version": str(val.get("version") or full_ver).strip(),
                "hash": str(val.get("md5") or val.get("hash") or val.get("sha256") or val.get("etag") or section_hash).strip(),
                "size": str(val.get("size") or val.get("fileSize") or section_size).strip(),
                "asset_id": str(val.get("id") or section.get("id") or "").strip(),
            }
            return val["url"].strip(), {k: v for k, v in meta.items() if v}
    return "", {}


def _launcher_media_fields(section: dict | None) -> tuple[str, dict, str, dict]:
    """Extract optional structured video/artwork URLs + metadata without altering signed queries."""
    if not isinstance(section, dict):
        return "", {}, "", {}
    video, video_meta = _extract_media_entry(section, ("bgVideo", "videoUrl", "video_url", "video"))
    artwork, artwork_meta = _extract_media_entry(
        section, ("background", "bgImage", "imageUrl", "image_url", "poster", "cover", "image")
    )
    return video, video_meta, artwork, artwork_meta


def parse_launcher_index(data: dict) -> dict:
    """{'live': '3.6', 'pre': '3.7' | None} (+ optional structured launch media) from index.json."""
    if not isinstance(data, dict):
        return {}
    default = data.get("default") if isinstance(data.get("default"), dict) else {}
    def_cfg = default.get("config") if isinstance(default.get("config"), dict) else {}
    live = short_version(default.get("version") or def_cfg.get("version"))
    pre_obj = data.get("predownload") if isinstance(data.get("predownload"), dict) else {}
    pre = None
    if pre_obj:
        pre_cfg = pre_obj.get("config") if isinstance(pre_obj.get("config"), dict) else {}
        pre = short_version(pre_obj.get("version") or pre_cfg.get("version"))
    out: dict = {"live": live, "pre": pre}
    pre_video, pre_vmeta, pre_art, pre_ameta = _launcher_media_fields(pre_obj if isinstance(pre_obj, dict) else None)
    live_video, live_vmeta, live_art, live_ameta = _launcher_media_fields(default if isinstance(default, dict) else None)
    top_video, top_vmeta, top_art, top_ameta = _launcher_media_fields(data)
    if pre and (pre_video or pre_art):
        out["pre_media"] = {
            "version": pre,
            "video": pre_video,
            "video_meta": {**pre_vmeta, "version": pre, "slot": "predownload"} if pre_video else {},
            "artwork": pre_art,
            "artwork_meta": {**pre_ameta, "version": pre, "slot": "predownload"} if pre_art else {},
        }
    if live and (live_video or live_art or top_video or top_art):
        chosen_video = live_video or top_video
        chosen_vmeta = live_vmeta if live_video else top_vmeta
        chosen_art = live_art or top_art
        chosen_ameta = live_ameta if live_art else top_ameta
        out["live_media"] = {
            "version": live,
            "video": chosen_video,
            "video_meta": {**chosen_vmeta, "version": live, "slot": "default"} if chosen_video else {},
            "artwork": chosen_art,
            "artwork_meta": {**chosen_ameta, "version": live, "slot": "default"} if chosen_art else {},
        }
    return out


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
            # Kuro can create drafts days before their public startTime.
            # Use publication time for freshness; retain legacy menus without it.
            ts = parse_kuro_time(article.get("startTime") or article.get("createTime") or "") or 0
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
