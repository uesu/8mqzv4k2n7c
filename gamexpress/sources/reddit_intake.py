"""Devvit relay intake reader — the primary Reddit listing (round GX-intake, 2026-11-02).

The *Gaming Express* Devvit app (News-Express `devvit/gaming-express`, installed under the
operator's own Reddit account) polls the public `/new` listings of the tracked subreddits and
publisher accounts with Reddit's own API every 5 minutes and writes **one fenced-JSON record per
new post** into a private Discord channel (the *intake channel*). This module reads that channel
back with a bot token (REST only, no gateway) and serves the records to `RedditClient` in the
exact listing shape `parse_reddit_json()` / `parse_redlib_html()` produce, so `items()`,
`program_post()` and `community_codes()` need no new branch.

Contract (identical to the News-Express reader, round 101):

* `INTAKE_READER=primary|shadow|off` (default `primary`), secret `DISCORD_BOT_TOKEN`, Variable
  `INTAKE_CHANNEL_ID`, `INTAKE_STALE_SECONDS=1800`, `INTAKE_LOOKBACK_SECONDS=21600`.
  GitHub Actions passes every secret/variable through `GE_SECRETS_JSON` / `GE_VARS_JSON`, so
  the workflow needs no edit.
* `ok` is the ONLY status that serves a listing, and only for a target that has at least one
  record in the window. Every other state — unset, HTTP error, stale, empty, a target without
  records — returns `None` and the pre-existing redlib / reddit.com chain runs exactly as before.
* Freshness is measured on RELAY RECORDS, never on arbitrary messages: the operator shares the
  channel with other bots, and webhook messages returned with an empty `content` (Message
  Content Intent missing) must never pass as a healthy relay with zero posts.
* Paging is newest-first (`before=`), so a busy shared channel can only shorten the look-back
  window, never hide the fresh records.
* The bot token travels in the `Authorization` header only; nothing is written through the bot;
  messages that are not relay records are skipped and never stored.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from urllib.parse import urlsplit

from ..config import Settings
from ..http import Fetcher
from ..textutil import clean_text, dedupe, find_urls
from .reddit import (
    _REDDIT_NAV_LINK_RE,
    INLINE_REDDIT_VIDEO_RE,
    POST_LINK_RE,
    VREDDIT_ID_RE,
    canonical_post_url,
)

log = logging.getLogger("gamexpress.reddit.intake")

DISCORD_EPOCH_MS = 1_420_070_400_000
INTAKE_API = "https://discord.com/api/v10"
INTAKE_ATTACHMENT_FETCHES = 10  # `record.json` completions per run (relay 0.0.7)
INTAKE_MAX_PAGES = 8
INTAKE_TIMEOUT = 12
_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
_REQUIRED = ("id", "sub", "title", "permalink", "created")
_REDDIT_IMAGE_RE = re.compile(r"https?://(?:i|preview|external-preview)\.redd\.it/[^\s<>()\"']+", re.I)


def snowflake_from_epoch(epoch_s: float) -> int:
    return (int(epoch_s * 1000) - DISCORD_EPOCH_MS) << 22


def epoch_from_snowflake(snowflake: object) -> float | None:
    try:
        return ((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000.0
    except (TypeError, ValueError):
        return None


def parse_intake_record(content: str | None) -> dict | None:
    """The fenced ```json block the relay writes, v1 only; anything else -> None."""
    m = _FENCE_RE.search(content or "")
    if not m:
        return None
    try:
        rec = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(rec, dict) or rec.get("v") != 1:
        return None
    if any(not rec.get(k) for k in _REQUIRED):
        return None
    if not re.fullmatch(r"[a-z0-9]{4,12}", str(rec["id"]).lower()):
        return None
    return rec


def parse_intake_update(content: str | None) -> dict | None:
    """Relay 0.0.7 re-check message — ```json {"v":2,"kind":"update","at":…,"posts":[{id,sub,age,stage,
    removed,deleted,edited,…},…]}```; `None` for anything else (v1 records included)."""
    m = _FENCE_RE.search(content or "")
    if not m:
        return None
    try:
        msg = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("v") != 2 or msg.get("kind") != "update":
        return None
    posts = []
    for item in msg.get("posts") or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9]{4,12}", pid):
            continue
        posts.append({**item, "id": pid, "removed": str(item["removed"]) if item.get("removed") else None,
                      "deleted": bool(item.get("deleted"))})
    return {"at": msg.get("at"), "posts": posts}


def attachment_url(message: dict) -> str | None:
    """The `record.json` attachment of a relay message (0.0.7) — the complete record."""
    for a in message.get("attachments") or []:
        if isinstance(a, dict) and str(a.get("filename") or "").lower() == "record.json":
            url = str(a.get("url") or "")
            host = urlsplit(url).hostname or ""
            if host in ("cdn.discordapp.com", "media.discordapp.net"):
                return url
    return None


def needs_full_record(rec: dict) -> bool:
    """Only cut records and crossposts without a parent permalink are worth the CDN request."""
    return bool(rec.get("bodyTruncated") or rec.get("mediaTruncated")
                or (rec.get("xpost") and not rec.get("xpostPermalink")))


def apply_full_record(rec: dict, full: object) -> bool:
    """Merge the complete record into the inline one (body, media, crosspost parent)."""
    if not isinstance(full, dict) or str(full.get("id") or "").strip().lower() != str(rec["id"]).lower():
        return False
    changed = False
    if rec.get("bodyTruncated") and isinstance(full.get("body"), str):
        rec["body"], rec["bodyTruncated"], changed = full["body"], bool(full.get("bodyTruncated")), True
    media = [str(u) for u in (full.get("media") or []) if isinstance(u, str) and u]
    if media and len(media) > len(rec.get("media") or []):
        rec["media"], rec["mediaTruncated"], changed = media, False, True
    parent = full.get("parent")
    if isinstance(parent, dict) and str(parent.get("permalink") or "").startswith("/r/"):
        rec["parent"], changed = parent, True
        rec["xpostPermalink"] = rec.get("xpostPermalink") or parent.get("permalink")
    elif isinstance(full.get("xpostPermalink"), str) and not rec.get("xpostPermalink"):
        rec["xpostPermalink"], changed = full["xpostPermalink"], True
    return changed


def record_to_entry(rec: dict) -> dict:
    """Relay record -> the listing dict `RedditClient` consumers already understand."""
    post_id = str(rec["id"]).strip().lower()
    subreddit = str(rec["sub"]).strip()
    body_raw = str(rec.get("body") or "")
    # Reddit's API escapes titles and bodies once (`&amp;`) and the relay forwards them as-is;
    # the markdown body also carries `\_` style escapes inside bare URLs (live 1xlhem9).
    body = clean_text(html.unescape(_unescape_markdown_in_urls(body_raw)))
    images: list[str] = []
    videos: list[str] = []
    dest = str(rec.get("url") or "").strip()
    if _REDDIT_IMAGE_RE.fullmatch(dest):
        images.append(dest)  # like parse_reddit_json: an i.redd.it / preview destination is the image
    media = list(rec.get("media") or [])
    parent = rec.get("parent") if isinstance(rec.get("parent"), dict) else None
    if not media and parent:
        media = list(parent.get("media") or [])  # 0.0.7: a crosspost carries its parent's media
    for u in media + _REDDIT_IMAGE_RE.findall(body_raw.replace("\\_", "_")):
        u = str(u or "").strip()
        if not u.startswith(("http://", "https://")):
            continue
        vm = VREDDIT_ID_RE.match(u) or INLINE_REDDIT_VIDEO_RE.match(u)
        if vm:
            videos.append(f"https://v.redd.it/{vm.group(1)}/CMAF_720.mp4")
        else:
            images.append(u)
    for vm in VREDDIT_ID_RE.finditer(" ".join([dest, body_raw])):
        videos.append(f"https://v.redd.it/{vm.group(1)}/CMAF_720.mp4")
    author = str(rec.get("author") or "").strip().lstrip("/").removeprefix("u/").strip()
    m_link = POST_LINK_RE.search(str(rec.get("permalink") or ""))
    slug_part = (m_link.group(3) or "") if m_link else ""
    links = [
        u for u in ([dest] + find_urls(body))
        if u
        and u.startswith(("http://", "https://"))
        and not POST_LINK_RE.search(u)
        and not _REDDIT_NAV_LINK_RE.match(u)
        and not INLINE_REDDIT_VIDEO_RE.match(u)
        and not VREDDIT_ID_RE.match(u)
        and u not in images
        and "reddit.com/gallery/" not in u.lower()
    ]
    return {
        "id": post_id,
        "subreddit": subreddit,
        "author": author,
        "distinguished": str(rec.get("distinguished") or ""),
        "flair": clean_text(html.unescape(str(rec.get("flair") or ""))),
        "title": clean_text(html.unescape(str(rec.get("title") or ""))),
        "text": body,
        "url": canonical_post_url(subreddit, post_id, slug_part),
        "ts": int(rec.get("created") or 0),
        "images": dedupe(images),
        "videos": dedupe(videos),
        "links": dedupe(links),
        "stickied": False,
        "comments": [],
        "relay": True,
        "body_truncated": bool(rec.get("bodyTruncated")),
        # Relay 0.0.8 / round 104: nothing cut, parent named -> the entry IS the post;
        # `resolve_post()` (redlib / reddit.com post pages) is not needed for it.
        "relay_complete": not needs_full_record(rec),
        "edited": bool(rec.get("edited")),
        "crosspost_url": (f"https://www.reddit.com{rec['xpostPermalink']}"
                          if str(rec.get("xpostPermalink") or "").startswith("/r/") else ""),
    }


_BARE_URL_RE = re.compile(r"(?<!\()https?://[^\s<>()]+")
_MD_ESCAPE_RE = re.compile(r"\\([_*~#\-\[\]])")


def _unescape_markdown_in_urls(text: str) -> str:
    return _BARE_URL_RE.sub(lambda m: _MD_ESCAPE_RE.sub(r"\1", m.group(0)), text or "")


class IntakeReader:
    """One channel read per run (cached), then in-memory lookups per listing target."""

    def __init__(self, fetcher: Fetcher, settings: Settings) -> None:
        self.fetcher = fetcher
        self.settings = settings
        self._result: dict | None = None
        self.served: list[str] = []
        self.now: float | None = None  # tests pin the clock; production uses time.time()

    @property
    def mode(self) -> str:
        return self.settings.intake_reader

    def configured(self) -> bool:
        return bool(self.settings.discord_bot_token and self.settings.intake_channel_id.isdigit())

    async def fetch(self, now: float | None = None) -> dict:
        """{"status", "records", "seen", "newest_age", "pages", "detail"} — never raises."""
        if self._result is not None:
            return self._result
        now = (self.now if self.now is not None else time.time()) if now is None else now
        out: dict = {"status": "unconfigured", "records": [], "seen": 0, "newest_age": None, "pages": 0,
                     "detail": ""}
        if self.mode == "off":
            out["status"] = "off"
        elif not self.configured():
            out["detail"] = "DISCORD_BOT_TOKEN secret and/or INTAKE_CHANNEL_ID variable missing"
        else:
            out = await self._read(now)
        self._result = out
        return out

    async def _read(self, now: float) -> dict:
        out: dict = {"status": "error", "records": [], "seen": 0, "newest_age": None, "pages": 0, "detail": "",
                     "update_msgs": 0, "updates": 0, "completed": 0}
        url = f"{INTAKE_API}/channels/{self.settings.intake_channel_id}/messages"
        headers = {"Authorization": f"Bot {self.settings.discord_bot_token}",
                   "User-Agent": "Game-Express intake reader (+https://github.com)"}
        cutoff = snowflake_from_epoch(now - self.settings.intake_lookback_seconds)
        before: int | None = None
        messages: list[dict] = []
        for _ in range(INTAKE_MAX_PAGES):
            params = {"limit": "100"}
            if before is not None:
                params["before"] = str(before)
            out["pages"] += 1
            page = await self.fetcher.get_json(url, source="intake", headers=headers, params=params,
                                               retries=0, timeout=INTAKE_TIMEOUT)
            if page is None:
                last = (self.fetcher.health.get("intake").last_error or "") if self.fetcher.health.get("intake") else ""
                out["detail"] = {
                    "HTTP 401": "bot token rejected",
                    "HTTP 403": "bot lacks View Channel / Read Message History on the channel",
                    "HTTP 404": "channel id unknown to this bot (wrong id, or bot not in the server)",
                    "HTTP 429": "rate limited",
                }.get(last, last or "request failed")
                out["status"] = last.lower().replace(" ", "_") if last.startswith("HTTP ") else "error"
                return out
            if not isinstance(page, list):
                out["detail"] = "non-list response"
                return out
            page = [m for m in page if isinstance(m, dict) and str(m.get("id") or "").isdigit()]
            in_window = [m for m in page if int(m["id"]) >= cutoff]
            messages.extend(in_window)
            if len(page) < 100 or len(in_window) < len(page):
                break
            before = min(int(m["id"]) for m in page)
        messages.sort(key=lambda m: int(m["id"]), reverse=True)
        raw: list[tuple[dict, dict]] = []
        updates: dict[str, dict] = {}
        by_id: set[str] = set()
        for m in messages:
            rec = parse_intake_record(m.get("content"))
            upd = None if rec else parse_intake_update(m.get("content"))
            if not rec and not upd:
                continue
            # Freshness from relay messages only (records and 0.0.7 re-checks) — never from other bots.
            if out["newest_age"] is None:
                created = epoch_from_snowflake(m["id"])
                if created is not None:
                    out["newest_age"] = max(0, int(now - created))
            if upd:
                out["update_msgs"] += 1
                for item in upd["posts"]:
                    updates.setdefault(item["id"], item)  # newest message first -> latest look wins
                continue
            out["seen"] += 1
            pid = str(rec["id"]).lower()
            if pid in by_id:
                continue  # a re-sent record after an edit (revision >= 2) is the newer message
            by_id.add(pid)
            raw.append((rec, m))
        out["updates"] = len(updates)
        # 0.0.7: complete cut records / crossposts from `record.json` (CDN, no token, bounded).
        fetched = 0
        for rec, m in raw:
            if fetched >= INTAKE_ATTACHMENT_FETCHES:
                break
            url = attachment_url(m)
            if not url or not needs_full_record(rec):
                continue
            fetched += 1
            full = await self.fetcher.get_json(url, source="intake-cdn", retries=0, timeout=INTAKE_TIMEOUT,
                                               headers={"User-Agent": headers["User-Agent"]})
            if apply_full_record(rec, full):
                out["completed"] += 1
        records: list[dict] = []
        for rec, _m in raw:
            latest = updates.get(str(rec["id"]).lower())
            if latest is not None:
                if latest.get("removed") or latest.get("deleted"):
                    continue  # the relay looked again: gone
            elif rec.get("removed"):
                continue  # the relay saw it removed: alive relay, no listing entry
            records.append(record_to_entry(rec))
        out["records"] = records
        minutes = self.settings.intake_lookback_seconds // 60
        if not messages:
            out["status"], out["detail"] = "empty", f"no messages in the last {minutes} min"
        elif not out["seen"] and out["update_msgs"]:
            out["status"] = "empty"
            out["detail"] = (f"relay alive ({out['update_msgs']} re-check message(s)) but no new post "
                             f"in the last {minutes} min")
        elif not out["seen"]:
            out["status"] = "empty"
            out["detail"] = (f"{len(messages)} message(s) in the last {minutes} min but none is a relay record "
                             f"(Message Content Intent off, wrong channel id, or the relay webhook points elsewhere)")
        elif out["newest_age"] is not None and out["newest_age"] > self.settings.intake_stale_seconds:
            out["status"] = "stale"
            out["detail"] = (f"newest relay record is {out['newest_age']} s old "
                             f"(limit {self.settings.intake_stale_seconds} s)")
        else:
            out["status"] = "ok"
        return out

    async def listing(self, kind: str, target: str) -> list[dict] | None:
        """Relay listing for `r/<target>` (kind `sub`) or `u/<target>` (kind `user`), newest first;
        `None` whenever the old chain must run (off / shadow / not ok / no record for the target)."""
        if self.mode == "off":
            return None
        res = await self.fetch()
        if res["status"] != "ok":
            return None
        key = target.strip().lower()
        if kind == "user":
            hits = [e for e in res["records"] if e["author"].lower() == key]
        else:
            hits = [e for e in res["records"] if e["subreddit"].lower() == key]
        if not hits:
            return None
        hits = sorted(hits, key=lambda e: e.get("ts") or 0, reverse=True)
        label = f"{'u' if kind == 'user' else 'r'}/{target}"
        if self.mode == "shadow":
            log.info("[reddit:intake] SHADOW %s: relay has %d record(s); old chain decides", label, len(hits))
            return None
        self.served.append(label)
        log.info("[reddit:intake] %s served from the relay: %d record(s)", label, len(hits))
        return hits

    def summary_line(self) -> str:
        """One step-summary line; empty when the reader never ran (reddit never consulted)."""
        res = self._result
        if res is None or res["status"] == "off":
            return ""
        if res["status"] == "ok":
            served = ", ".join(dedupe(self.served)) or "no target needed Reddit"
            shadow = " (shadow — old chain decided)" if self.mode == "shadow" else ""
            extra = ""
            if res.get("updates"):
                extra += f", {res['updates']} re-checked"
            if res.get("completed"):
                extra += f", {res['completed']} completed from record.json"
            return (f"📡 reddit intake: relay ok — {len(res['records'])} record(s) in window, newest "
                    f"{res['newest_age']} s{extra}; served {served}{shadow}")
        detail = f" ({res['detail']})" if res.get("detail") else ""
        return f"📡 reddit intake: relay {res['status']}{detail} — redlib/reddit.com chain used"
