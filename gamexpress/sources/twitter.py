"""Official X (Twitter) accounts: nitter RSS fleet for the timeline, FxTwitter → vxTwitter
for full tweet data (text with expanded links, photos, exact timestamp).

Same approach as News-Express twitter_v3 (round 13/14 fleet); the first TWO working
instances are merged so one stale-but-200 mirror can't hide a fresh tweet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from calendar import timegm
from collections.abc import Callable
from urllib.parse import quote

import feedparser

from .. import schedule
from ..config import CONFIG_DIR, Game, Settings
from ..http import Fetcher
from ..media import nitter_pic_to_twimg, sane_aspect
from ..models import Item
from ..textutil import find_urls, html_to_text

log = logging.getLogger("gamexpress.x")

TOKEN_GATED = {"https://nitter.miningtcup.me"}

# --------------------------------------------------------------------------- announcement seed
SEED_PATH = CONFIG_DIR / "program_announcements.json"
_seeds: dict | None = None


def program_seed(game_key: str, version: str) -> dict:
    """config/program_announcements.json -> the recorded X post for game+version ({} if none).

    Committed to the repo and rewritten by the monitor workflow, so the knowledge survives the
    throwaway state of a test run. Keys starting with '_' are comments and are ignored."""
    global _seeds
    if _seeds is None:
        try:
            _seeds = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _seeds = {}
    per_game = (_seeds or {}).get(game_key)
    if not isinstance(per_game, dict):
        return {}
    hit = per_game.get(version)
    return hit if isinstance(hit, dict) else {}


def save_program_seed(game_key: str, version: str, entry: dict) -> bool:
    """Record a newly discovered announcement. Returns True when the file changed, which is what
    tells the workflow there is something to commit back."""
    global _seeds
    data = json.loads(SEED_PATH.read_text(encoding="utf-8")) if SEED_PATH.exists() else {}
    per_game = data.setdefault(game_key, {})
    old = per_game.get(version)
    if isinstance(old, dict) and old.get("id") == entry.get("id"):
        return False
    import time as _t
    per_game[version] = {**entry, "found_at": int(_t.time())}
    SEED_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                         encoding="utf-8")
    _seeds = None                                  # next read picks the new entry up
    log.info("[x:%s] recorded the %s program announcement (%s) in %s",
             game_key, version, entry.get("id"), SEED_PATH.name)
    return True


FX_SOURCES = (("fxtwitter", "https://api.fxtwitter.com/status/{id}"),
              ("fixupx", "https://api.fixupx.com/status/{id}"))
VXTWITTER_URL = "https://api.vxtwitter.com/Twitter/status/{id}"
# Single-tweet lookups ask nitter.cf first. xitter.cf is the same backend behind a second domain, so it
# is only the second try. Both serve a per-status RSS feed (…/<account>/status/<id>/rss) that reaches
# back weeks, not days: nitter.cf returned the ZZZ 3.2 announcement from 2026-08-24 on 2026-10-09.
STATUS_MIRRORS = ("https://nitter.cf", "https://xitter.cf")
# After this many failed requests in one run a status mirror is skipped for the rest of the run. Without
# this a dead nitter.cf costs a full NITTER_TIMEOUT on EVERY tweet before FxTwitter is asked.
STATUS_FAIL_LIMIT = 2
TCO_WRAPPED = re.compile(r"/t\.co/", re.I)   # nitter leaves t.co links unexpanded in its RSS
X_UA = "Game-Express"


def _from_fx(t: dict, tweet_id: str) -> dict:
    links = [f.get("replacement") for f in ((t.get("raw_text") or {}).get("facets") or [])
             if f.get("type") == "url" and f.get("replacement")]
    return {"id": str(t.get("id") or tweet_id), "text": t.get("text") or "",
            "ts": int(t.get("created_timestamp") or 0),
            "photos": [p.get("url") for p in ((t.get("media") or {}).get("photos") or [])
                       if p.get("url") and sane_aspect(p.get("width"), p.get("height"))],
            "links": links + [u for u in find_urls(t.get("text") or "") if u not in links],
            "author": (t.get("author") or {}).get("screen_name") or "",
            "avatar": (t.get("author") or {}).get("avatar_url") or "",
            "url": t.get("url") or ""}


def _from_vx(vx: dict, tweet_id: str) -> dict:
    return {"id": str(vx.get("tweetID") or tweet_id), "text": vx.get("text") or "",
            "ts": int(vx.get("date_epoch") or 0),
            "photos": [u for u in (vx.get("mediaURLs") or []) if "video" not in u],
            "links": find_urls(vx.get("text") or ""),
            "author": vx.get("user_screen_name") or "",
            "avatar": vx.get("user_profile_image_url") or "",
            "url": vx.get("tweetURL") or ""}

NITTER_BATCH = 4          # instances probed in parallel per round
NITTER_TIMEOUT = 12       # seconds — a dead mirror must not stall the run
NITTER_WORKING = 2        # merge this many instances, then stop waiting for the rest
NITTER_GRACE = 1.5        # seconds a still-silent HIGHER-ranked mirror gets once that many answered
STATUS_RE = re.compile(r"/status(?:es)?/(\d{8,25})")


class XClient:
    """Per-run cache so schedule + codes share one timeline fetch per account."""

    def __init__(self, fetcher: Fetcher, settings: Settings) -> None:
        self.fetcher = fetcher
        self.settings = settings
        self._timelines: dict[str, list[dict] | None] = {}
        self._tweets: dict[str, dict | None] = {}
        self.avatars: dict[str, str] = {}     # account (lower) -> current avatar url
        self.source_used: dict[str, int] = {}  # tweet-data service that answered, per run
        self.status_fails: dict[str, int] = {}  # failed status-feed requests per mirror, this run
        self.reachable: set[str] = set()      # accounts with >= 1 working instance this run

    async def _probe(self, inst: str, account: str) -> list | None:
        """One nitter instance -> parsed feed entries, or None (dead / bot-check / stale)."""
        url = f"{inst}/{account}/rss"
        headers = {"User-Agent": "Mozilla/5.0"}
        if inst in TOKEN_GATED:
            token = self.settings.nitter_token
            if not token:
                return None
            headers = {"User-Agent": f"Mozilla/5.0 {token}", "Authorization": f"Bearer {token}"}
            url += f"?token={quote(token, safe='')}"
        body = await self.fetcher.get_text(url, source="nitter", headers=headers, retries=0,
                                           timeout=NITTER_TIMEOUT)
        if not body:
            return None
        feed = await asyncio.to_thread(feedparser.parse, body)
        if not feed.entries:
            log.info("[x:%s] %s: HTTP 200 but 0 entries (bot check / stale) — next", account, inst)
            return None
        return list(feed.entries)

    async def _probe_batch(self, batch: list[str], account: str, needed: int) -> list[list]:
        """Probe a whole batch at once and return the first `needed` answers, in fleet order.

        The batch stops waiting as soon as the winners are decided instead of awaiting the
        slowest member: asyncio.gather() used to sit out a dead mirror's whole 12 s
        NITTER_TIMEOUT even when the mirrors the run needs had already replied — live run #217
        (nitter.cf -> TimeoutError) took 16.2 s where #215/#216/#218 took 4.9-6 s. Cancelling
        the stragglers also hands the Fetcher's global request slot straight back to the
        HoYoLAB / Kuro / code requests queued behind it.

        The *selection* is unchanged, so the merged timeline is the same as before: a mirror is
        only dropped once `needed` HIGHER-ranked mirrors have answered, never just because it
        was slower. The one exception is the NITTER_GRACE cap — a higher-ranked mirror that is
        still silent `NITTER_GRACE` seconds after the batch already has enough answers is
        treated as dead (a healthy mirror answers in well under that), which turns the worst
        case from a full 12 s timeout into 1.5 s. Every instance in the batch is still
        requested, in parallel, in fleet order.
        """
        tasks = {asyncio.ensure_future(self._probe(inst, account)): idx for idx, inst in enumerate(batch)}
        answers: dict[int, list] = {}
        pending = set(tasks)
        grace_until: float | None = None
        try:
            while pending:
                best = sorted(answers)[:needed]
                if len(best) == needed and best[-1] < min(tasks[t] for t in pending):
                    break                       # no pending mirror can outrank the winners
                timeout = None
                if grace_until is not None:
                    timeout = grace_until - time.monotonic()
                    if timeout <= 0:
                        log.info("[x:%s] %s still silent after %.1fs — treated as dead", account,
                                 ", ".join(batch[tasks[t]] for t in pending), NITTER_GRACE)
                        break
                done, pending = await asyncio.wait(pending, timeout=timeout,
                                                   return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    continue                    # grace expired — handled at the top of the loop
                for task in done:
                    try:
                        feed_entries = task.result()
                    except Exception as e:      # a parser blow-up must never kill the run
                        log.warning("[x:%s] %s probe failed: %s", account, batch[tasks[task]], e)
                        continue
                    if feed_entries:
                        answers[tasks[task]] = feed_entries
                if len(answers) >= needed and grace_until is None:
                    grace_until = time.monotonic() + NITTER_GRACE
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return [answers[i] for i in sorted(answers)][:needed]

    async def timeline(self, account: str) -> list[dict]:
        """Instances are probed in parallel batches and the first TWO working ones are merged.

        A dead mirror costs one short timeout for the whole batch instead of one per instance —
        and nothing at all once two mirrors in the same batch have answered, because the batch
        stops waiting there (see _probe_batch)."""
        if account in self._timelines:
            return self._timelines[account] or []
        entries: dict[str, dict] = {}
        working = 0
        fleet = list(self.settings.nitter_instances)
        for i in range(0, len(fleet), NITTER_BATCH):
            if working >= NITTER_WORKING:
                break
            batch = fleet[i:i + NITTER_BATCH]
            for feed_entries in await self._probe_batch(batch, account, NITTER_WORKING - working):
                working += 1
                for e in feed_entries:
                    m = STATUS_RE.search(e.get("link", ""))
                    if not m:
                        continue
                    creator = (e.get("author") or e.get("dc_creator") or "").lstrip("@").lower()
                    if creator and creator != account.lower():
                        continue  # retweet of another account
                    tid = m.group(1)
                    ts = timegm(e.published_parsed) if e.get("published_parsed") else 0
                    html_body = e.get("summary") or e.get("description") or ""
                    text, links, imgs = html_to_text(html_body)
                    entries.setdefault(tid, {
                        "id": tid, "ts": ts, "title": e.get("title") or "", "text": text or e.get("title") or "",
                        "links": links, "images": [nitter_pic_to_twimg(i) for i in imgs],
                    })
        if not working:
            log.warning("[x:%s] no working nitter instance this run", account)
        else:
            self.reachable.add(account.lower())
        self._timelines[account] = sorted(entries.values(), key=lambda x: x["ts"], reverse=True) if working else None
        return self._timelines[account] or []

    async def _tweet_from_nitter(self, tweet_id: str, account: str) -> dict | None:
        """One tweet from a nitter status feed, in the same shape as _from_fx. nitter.cf first."""
        for inst in STATUS_MIRRORS:
            if self.status_fails.get(inst, 0) >= STATUS_FAIL_LIMIT:
                continue                              # down for this run: do not wait on it again
            body = await self.fetcher.get_text(f"{inst}/{account}/status/{tweet_id}/rss", source="nitter",
                                               headers={"User-Agent": "Mozilla/5.0"}, retries=0,
                                               timeout=NITTER_TIMEOUT)
            if not body:
                self.status_fails[inst] = self.status_fails.get(inst, 0) + 1
                continue
            feed = await asyncio.to_thread(feedparser.parse, body)
            for e in feed.entries:
                m = STATUS_RE.search(e.get("link", ""))
                if not m or m.group(1) != str(tweet_id):
                    continue
                text, links, imgs = html_to_text(e.get("summary") or e.get("description") or "")
                text = text or e.get("title") or ""
                if not text:
                    continue
                return {"id": str(tweet_id), "text": text,
                        "ts": timegm(e.published_parsed) if e.get("published_parsed") else 0,
                        "photos": [nitter_pic_to_twimg(i) for i in imgs], "links": links,
                        "author": account, "avatar": "",
                        "url": f"https://x.com/{account}/status/{tweet_id}"}
        return None

    async def tweet(self, tweet_id: str, account: str | None = None) -> dict | None:
        """One tweet's text, time, photos and links.

        With the account known, nitter.cf (then xitter.cf) answers first. FxTwitter and vxTwitter are
        still consulted when nitter cannot answer, and also when the post carries a t.co link: nitter
        leaves those unexpanded, and the expanded URL (a YouTube stream, say) is what the card reads.
        """
        if tweet_id in self._tweets:
            return self._tweets[tweet_id]
        nitter_hit = await self._tweet_from_nitter(str(tweet_id), account) if account else None
        if nitter_hit and not any(TCO_WRAPPED.search(u) for u in nitter_hit["links"]):
            self.source_used["nitter"] = self.source_used.get("nitter", 0) + 1
            self._tweets[tweet_id] = nitter_hit
            return nitter_hit
        result = None
        for name, url in FX_SOURCES:
            data = await self.fetcher.get_json(url.format(id=tweet_id), source=name,
                                               headers={"User-Agent": X_UA},
                                               retries=1 if name == "fxtwitter" else 0)
            t = (data or {}).get("tweet") if isinstance(data, dict) else None
            if t:
                result = _from_fx(t, tweet_id)
                self.source_used[name] = self.source_used.get(name, 0) + 1
                if name != "fxtwitter":
                    log.info("[x] %s answered for %s (fxtwitter could not)", name, tweet_id)
                break
        if not result:
            vx = await self.fetcher.get_json(VXTWITTER_URL.format(id=tweet_id),
                                             source="vxtwitter", retries=1)
            if isinstance(vx, dict) and vx.get("text") is not None:
                result = _from_vx(vx, tweet_id)
                self.source_used["vxtwitter"] = self.source_used.get("vxtwitter", 0) + 1
                log.info("[x] vxtwitter answered for %s (FxEmbed could not)", tweet_id)
        if not result and nitter_hit:
            result = nitter_hit                  # nothing expanded the links, but the text is real
            self.source_used["nitter"] = self.source_used.get("nitter", 0) + 1
        self._tweets[tweet_id] = result
        if result and result.get("author") and result.get("avatar"):
            self.avatars[result["author"].lower()] = result["avatar"]
        return result

    async def program_tweet(self, game: Game, version: str) -> Item | None:
        """THE schedule lookup, X first. Two stages, because the two halves of X age differently:

        1. SEED — config/program_announcements.json maps game+version to a tweet id that a
           previous run discovered. fxtwitter still serves a five-week-old tweet by id (verified:
           the ZZZ 3.2 announcement from 2026-08-24 resolves fine on 2026-09-27), so this is the
           stage that makes a `mode=test` run — whose state is always empty — show the real link
           and the real key art.
        2. TIMELINE — a nitter RSS feed only reaches back a few days (nitter.cf on 2026-09-27
           stopped at 2026-09-23), so it can only ever find an announcement that is still new.
           That is exactly when one first appears, which is when it gets discovered and seeded.
        """
        seed = program_seed(game.key, version)
        if seed and seed.get("id"):
            t = await self.tweet(str(seed["id"]), seed.get("account") or None)
            if t and t.get("text"):
                log.info("[x:%s] %s program announcement from the seed file: %s",
                         game.key, version, t.get("url") or seed["id"])
                return self._program_item(game, str(seed["id"]), seed.get("account") or "", t)
            log.info("[x:%s] seed tweet %s did not resolve — falling back to the timeline",
                     game.key, seed.get("id"))
        # The ANNOUNCEMENT is the OLDEST matching post, not the newest. The timeline is newest-first,
        # and a giveaway or a same-day reminder that repeats the air time matches the same test:
        # taking the first match seeded ZZZ 3.3's 2026-10-09 "Share to Win" post as its
        # announcement, and that seed is what the card then locked onto.
        found: tuple[str, dict] | None = None
        for account in game.x_accounts:
            for e in await self.timeline(account):
                if not schedule.is_program_announcement(game, e["text"]):
                    continue
                if version and version not in e["text"]:
                    continue                      # an announcement for some other version
                if found is None or e["ts"] < found[1]["ts"]:
                    found = (account, e)
        if found is None:
            return None
        account, e = found
        t = await self.tweet(e["id"], account) or e
        log.info("[x:%s] %s program announcement found on the timeline: %s",
                 game.key, version or "?", t.get("url") or e["id"])
        item = self._program_item(game, e["id"], account, t)
        save_program_seed(game.key, version or "?", {
            "id": e["id"], "account": account, "url": item.url,
            "posted_ts": item.published_ts, "image": (item.images or [""])[0],
        })
        return item

    def _program_item(self, game: Game, tid: str, account: str, t: dict) -> Item:
        return Item(source="x", game=game.key, id=tid,
                    url=t.get("url") or f"https://x.com/{account}/status/{tid}",
                    title="", text=t.get("text") or "", published_ts=t.get("ts") or 0,
                    images=t.get("photos") or [], links=t.get("links") or [])

    async def items(self, game: Game, want: Callable[[str], bool], since_ts: int) -> list[Item]:
        if not self.settings.x_enabled:
            return []
        timelines = await asyncio.gather(*(self.timeline(a) for a in game.x_accounts))
        picked = [(account, e) for account, tl in zip(game.x_accounts, timelines) for e in tl
                  if not (e["ts"] and e["ts"] < since_ts) and want(e["text"])]
        details = await asyncio.gather(*(self.tweet(e["id"], account) for account, e in picked))
        out: list[Item] = []
        for (account, e), t in zip(picked, details):
            text = (t or {}).get("text") or e["text"]
            out.append(Item(
                source="x", game=game.key, id=e["id"],
                url=(t or {}).get("url") or f"https://x.com/{account}/status/{e['id']}",
                title="", text=text, published_ts=(t or {}).get("ts") or e["ts"],
                images=(t or {}).get("photos") or e["images"],
                links=(t or {}).get("links") or e["links"]))
        return out


def item_from_fx_json(game_key: str, data: dict) -> Item:
    """Build an Item from a raw FxTwitter /status response (tests + TEST tools)."""
    t = data.get("tweet") or data
    links = [f.get("replacement") for f in ((t.get("raw_text") or {}).get("facets") or [])
             if f.get("type") == "url" and f.get("replacement")]
    return Item(source="x", game=game_key, id=str(t.get("id")), url=t.get("url") or "", title="",
                text=t.get("text") or "", published_ts=int(t.get("created_timestamp") or 0),
                images=[p["url"] for p in ((t.get("media") or {}).get("photos") or []) if p.get("url")],
                links=links + find_urls(t.get("text") or ""))
