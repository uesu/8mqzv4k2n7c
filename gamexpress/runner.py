"""One monitor run: fail-over check → gather official items once → schedule → codes → save."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import aiohttp

from . import beta, build_id, codeposter, maintenance, schedule
from .config import Game, Settings, active_games, load_games, load_overrides
from .discord import WebhookClient, webhook_fingerprint
from .http import BOT_UA, Fetcher, Probe
from .models import Item
from .sources import bannerfeed, countdown, hoyolab, kuro, launcher, newspage
from .sources.codes import CodeSources
from .sources.reddit import RedditClient
from .sources.twitter import XClient
from .state import State
from .textutil import version_key

log = logging.getLogger("gamexpress")


@dataclass
class Ctx:
    settings: Settings
    games: list[Game]
    state: State
    fetcher: Fetcher
    webhook: WebhookClient
    x: XClient
    code_sources: CodeSources
    overrides: dict
    now: int
    versions: dict = field(default_factory=dict)
    items: dict[str, list[Item]] = field(default_factory=dict)
    reachable: dict[str, bool] = field(default_factory=dict)   # did ANY source answer for this game?
    report: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    active: bool = True
    elapsed: float = 0.0
    estimates: dict = field(default_factory=dict)   # game key -> countdown-site estimate
    media: dict = field(default_factory=dict)       # game key -> version -> program announcement
    banners: dict = field(default_factory=dict)     # game key -> list of 5★ rate-up banners
    reddit: RedditClient | None = None
    # Optional async collaborator for deterministic feature-delivery tests.  Production leaves
    # it unset so feature code uses asyncio.sleep; dry-run harnesses can record the intended
    # wait without idling a test suite.
    sleep: Callable[[float], Awaitable[object]] | None = None


async def gather_versions(ctx: Ctx) -> None:
    need_hyp = any(g.launcher.get("type") == "hoyoplay" for g in ctx.games)
    need_kuro = any(g.launcher.get("type") == "kuro" for g in ctx.games)

    async def none() -> dict:
        return {}
    hyp, kr = await asyncio.gather(launcher.hoyoplay_versions(ctx.fetcher) if need_hyp else none(),
                                   kuro.launcher_versions(ctx.fetcher) if need_kuro else none())
    for g in ctx.games:
        t = g.launcher.get("type")
        if t == "hoyoplay" and g.launcher.get("game_id") in hyp:
            ctx.versions[g.key] = hyp[g.launcher["game_id"]]
        elif t == "kuro" and kr:
            ctx.versions[g.key] = kr


async def _gather_game(ctx: Ctx, g: Game, since: int) -> None:
    want = schedule.wants(g)
    probe = Probe(ctx.fetcher)              # counts THIS game's answers while games run in parallel
    parts = await asyncio.gather(hoyolab.fetch_items(probe, g, want, since),
                                 kuro.fetch_items(probe, g, want, since),
                                 ctx.x.items(g, want, since))
    items: list[Item] = [it for part in parts for it in part]
    x_reachable = getattr(ctx.x, "reachable", ())
    x_ok = any(a.lower() in x_reachable for a in g.x_accounts)
    has_primary_api = bool(g.hoyolab_gid or g.kuro_news)
    primary_ok = (not has_primary_api) or probe.ok > 0
    # True failover backup: when primary APIs + X both answered, skip `/user/<official>` requests
    # (`0` extra schedule HTTP requests on healthy runs); fail over automatically in the same run
    # whenever X or a primary official feed could not be reached.
    if getattr(ctx, "reddit", None) is not None and ctx.settings.reddit_enabled and (not x_ok or not primary_ok):
        items.extend(await ctx.reddit.items(g, want, since))
    ctx.items[g.key] = items
    reddit_ok = getattr(ctx, "reddit", None) is not None and (
        any(f"u/{a.lower()}" in ctx.reddit.reachable for a in g.reddit_official)
        or any(f"r/{s.lower()}" in ctx.reddit.reachable for s in g.subreddits)
    )
    ctx.reachable[g.key] = probe.ok > 0 or x_ok or reddit_ok
    icons = ctx.state.data.setdefault("icons", {})
    fresh = next((ctx.x.avatars[a.lower()] for a in g.x_accounts if a.lower() in ctx.x.avatars), None)
    if fresh and icons.get(g.key) != fresh:
        icons[g.key] = fresh
    g.icon = icons.get(g.key) or g.icon
    log.info("[%s] %d official item(s) matched in the last %dh (live %s, pre-install %s)", g.key,
             len(items), ctx.settings.lookback_hours, ctx.versions.get(g.key, {}).get("live") or "?",
             ctx.versions.get(g.key, {}).get("pre") or "—")


async def gather_estimates(ctx: Ctx) -> None:
    """Ask the countdown sites — only for games that still miss a program / maintenance time,
    and only when the schedule feature is on (COUNTDOWN_ESTIMATES=0 switches it off)."""
    if "schedule" not in ctx.settings.features or not ctx.settings.countdown_estimates:
        return
    keys = [g.key for g in ctx.games if schedule.needs_estimate(ctx.state, g.key, ctx.now)]
    if not keys:
        return
    got = await asyncio.gather(*(countdown.fetch_game(ctx.fetcher, k, ctx.now) for k in keys))
    for key, est in zip(keys, got):
        if est:
            ctx.estimates[key] = est
            log.info("[%s] countdown estimate: %s", key, est)


async def gather_banners(ctx: Ctx) -> None:
    """Fetch the community banner feed (ertezy.github.io/Kitsudock-data/hub.json) — one request
    per run for all games. Refused when older than 14 days, and BANNER_FEED=0 switches it off."""
    if "schedule" not in ctx.settings.features or not ctx.settings.banner_feed:
        return
    try:
        ctx.banners = await bannerfeed.fetch_banners(ctx.fetcher, ctx.now)
    except Exception as e:
        log.warning("gather_banners failed: %s", e)


async def find_program(ctx, g: Game, ver: str) -> dict | None:
    """THE program lookup, in one place, X first.

    X is the primary source because it is the only one that reliably carries BOTH halves of what
    the card needs — the announcement's own URL and its key art at `?name=orig`. Every source
    below it is a backup, in the order it proved itself:

      1. X           config/program_announcements.json -> fxtwitter by id (a seeded id resolves
                     weeks later), else the nitter timeline (nitter.cf first). This is what fixed
                     the 2026-09-27 run, where all three HoYoverse games got "no program
                     announcement found" and WW linked a lore article instead.
      2. HoYoLAB     the official news list, paged back past the lookback window. Good text and
                     timestamps; its image list is often a small article cover.
      3. news page   the official site's news index, which archives every announcement.
      4. feed mirror its RSS/Atom mirror (Kuro's site is a JS build the scraper cannot read).

    -> {'url','title','images','youtube','text','ts','program_ts','source'} or None."""
    now = ctx.now
    source_url = ""
    item: Item | None = None
    if getattr(ctx, "x", None) is not None and ctx.settings.x_enabled:
        item = await ctx.x.program_tweet(g, ver)
    hit: dict | None = None
    if item:
        source_url = item.url
        hit = {"url": item.url, "title": "", "images": list(item.images or []),
               "youtube": "", "text": item.text, "ts": item.published_ts or now, "source": "x"}
    if not hit:
        hit = await hoyolab.find_program(ctx.fetcher, g, ver)
    if not hit:
        hit = await newspage.fetch_program(ctx.fetcher, g, ver, now)
    if not hit and getattr(g, "program_feeds", None):
        hit = await newspage.fetch_program_feed(ctx.fetcher, g, ver, now)
    if not hit and getattr(ctx, "reddit", None) is not None and ctx.settings.reddit_enabled:
        r_item = await ctx.reddit.program_post(g, ver)
        if r_item:
            source_url = r_item.url
            hit = {
                "url": r_item.url,
                "title": r_item.title,
                "images": list(r_item.images or []),
                "youtube": "",
                "text": r_item.text,
                "ts": r_item.published_ts or now,
                "source": "reddit",
            }
    if not hit:
        return None
    # the air time comes from the article's own text, through the same extractor that
    # every other official post goes through — no separate date parsing here
    found = Item(source=hit.get("source") or "hoyolab", game=g.key, id=ver,
                 url=source_url or hit["url"], title=hit.get("title") or "",
                 text=hit.get("text") or "", published_ts=hit.get("ts") or now,
                 images=list(hit.get("images") or []))
    fields = schedule.extract_program(g, found)
    hit["program_ts"] = fields.get("program_ts")
    hit["youtube"] = hit.get("youtube") or fields.get("youtube_video")
    hit["images"] = hit.get("images") or fields.get("images") or []
    return hit


async def gather_program_media(ctx: Ctx) -> None:
    """Find the program announcement for every current version whose card would otherwise show
    somebody else's post. Versions come from this run as well as from state, so a first run and
    every test run (whose state starts empty) still performs the lookup."""
    if "schedule" not in ctx.settings.features or not ctx.settings.program_media:
        return
    jobs: list[tuple[Game, str]] = []
    for g in ctx.games:
        records = ctx.state.schedule_records(g.key) or {}
        by_version = schedule.version_extracts(ctx, g)
        todo = [v for v in by_version
                if schedule.needs_program_lookup(by_version[v], records.get(v, {}), ctx.now, g.key)]
        for ver, rec in records.items():
            if ver not in by_version and schedule.needs_program_lookup([], rec, ctx.now, g.key):
                todo.append(ver)
        jobs += [(g, v) for v in sorted(set(todo), key=version_key)]
        # Cards the lookup cannot help, said once. A version whose announcement never arrived
        # and whose tweet id is not cached is otherwise unreachable: it keeps the notice's
        # cover and link for ever, the lookup no longer runs, and the summary shows a card
        # that looks merely stale. That is the Genshin 7.1 state minus the cache — the one
        # thing the operator can fix, and only if somebody tells them.
        for ver, rec in records.items():
            reason = schedule.announcement_gap(rec, g.key, ctx.now)
            if not reason:
                rec.pop("announce_gap", None)      # repaired, or never needed one
            elif rec.get("announce_gap") != reason:
                rec["announce_gap"] = reason
                ctx.report.append(f"🩹 {g.short} {ver}: {reason}")
    if not jobs:
        return
    hits = await asyncio.gather(*(find_program(ctx, g, ver) for g, ver in jobs))
    for (g, ver), hit in zip(jobs, hits):
        if hit:
            ctx.media.setdefault(g.key, {})[ver] = hit
    wanted: dict[str, list[str]] = {}
    shorts = {}
    for g, ver in jobs:
        wanted.setdefault(g.key, []).append(ver)
        shorts[g.key] = g.short
    for key, vers in wanted.items():
        found = ctx.media.get(key) or {}
        if found:
            log.info("[%s] program announcement found for %s (source: %s)", key,
                     ", ".join(sorted(found)),
                     ", ".join(sorted({(found[v].get("source") or "?") for v in found})))
        else:
            log.info("[%s] no program announcement found on X, HoYoLAB, the official news page "
                     "or its feed mirror — the card keeps its current link", key)
        missed = [v for v in vers if v not in found]
        if missed:
            # A lookup only runs for a version that needs one, so this is never chatter: it is
            # the other half of the "announcement found" line. Without it, a card with no air
            # time and no link looks like a rendering bug, when what happened is that every
            # source was asked and none answered. It stops by itself -- the moment one answers,
            # media_from is set and needs_program_lookup() returns False for good.
            ctx.report.append(f"🔍 {shorts[key]} {', '.join(sorted(missed))}: no Special Program "
                              f"announcement found (X, HoYoLAB, official news page) — the card "
                              f"keeps its current link; the lookup runs again next run")


async def gather_items(ctx: Ctx) -> None:
    """Every game is gathered concurrently (HoYoLAB lists + full posts, Kuro, X timelines),
    capped by the Fetcher's global request limit."""
    since = ctx.now - ctx.settings.lookback_hours * 3600
    await asyncio.gather(*(_gather_game(ctx, g, since) for g in ctx.games))


async def failover_check(ctx: Ctx) -> None:
    s = ctx.settings
    if not s.peer_state_url:
        if s.instance_role == "standby":
            ctx.active = False
            ctx.report.append("💤 standby without PEER_STATE_URL — passive (set ENABLED_FEATURES / role to activate)")
        return
    peer = await ctx.fetcher.get_json(s.peer_state_url, source="peer-state",
                                      headers={"Cache-Control": "no-cache"}, retries=1)
    if isinstance(peer, dict):
        n = ctx.state.merge_peer(
            peer,
            include_news="news" in s.features,
            include_maintenance="maintenance" in s.features,
            include_beta="beta" in s.features,
        )
        if n:
            ctx.report.append(f"🔄 imported {n} posted key(s) from the peer instance")
    if s.instance_role != "standby":
        return
    if not isinstance(peer, dict):
        ctx.active = False
        ctx.report.append("💤 standby: peer state unreachable — staying passive (never risks a double post)")
        return
    hb = peer.get("heartbeat") or {}
    last = int(hb.get("last_success") or 0)
    age_min = (ctx.now - last) / 60 if last else 10 ** 9
    # never fail over faster than 2.5x the primary's own heartbeat interval
    threshold = max(s.failover_after_min, int(2.5 * int(hb.get("every_min") or 0)))
    if age_min < threshold:
        ctx.active = False
        ctx.report.append(f"💤 standby: primary healthy (heartbeat {age_min:.0f} min ago) — passive")
    else:
        ctx.report.append(f"🚨 FAIL-OVER: primary heartbeat is {age_min:.0f} min old (> {threshold}) "
                          f"— this standby is now posting")


async def _safe_step(ctx: Ctx, label: str, coro) -> None:
    """Run one top-level feature pass so an unhandled exception logs its traceback and
    records a summary error without skipping sibling features, state persistence, or summary."""
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 — isolate top-level feature stage
        log.exception("feature step '%s' raised an unexpected exception", label)
        ctx.errors.append(f"{label} failed: {_summary_text(exc)}")


async def run_once(settings: Settings, games_all: dict[str, Game] | None = None,
                   overrides: dict | None = None, session: aiohttp.ClientSession | None = None) -> Ctx:
    games_all = games_all if games_all is not None else load_games()
    overrides = overrides if overrides is not None else load_overrides()
    state = State.load(settings.state_path)
    own_session = session is None
    session = session or aiohttp.ClientSession(headers={"User-Agent": BOT_UA})
    try:
        started = time.monotonic()
        fetcher = Fetcher(session, timeout=settings.http_timeout)
        reddit = RedditClient(fetcher, settings)
        ctx = Ctx(settings=settings, games=active_games(games_all, settings), state=state, fetcher=fetcher,
                  webhook=WebhookClient(session, dry_run=settings.dry_run), x=XClient(fetcher, settings),
                  code_sources=CodeSources(fetcher, reddit=reddit), overrides=overrides,
                  now=int(time.time()), reddit=reddit)
        ephemeral = settings.dry_run or settings.test_mode
        features: set[str] = set()
        try:
            await failover_check(ctx)
            features = settings.features if ctx.active else set()
            if features & {"schedule", "codes"} and ctx.games:
                await _safe_step(ctx, "gather_versions", gather_versions(ctx))
                await _safe_step(ctx, "gather_items", gather_items(ctx))
                await asyncio.gather(
                    _safe_step(ctx, "gather_program_media", gather_program_media(ctx)),
                    _safe_step(ctx, "gather_estimates", gather_estimates(ctx)),
                    _safe_step(ctx, "gather_banners", gather_banners(ctx)),
                )
                for g in ctx.games:
                    if not ctx.reachable.get(g.key, True):
                        ctx.warnings.append(f"{g.short}: no announcement source answered this run — the next run "
                                            f"re-reads the last {settings.lookback_hours}h, so nothing is missed")
                if "schedule" in features:
                    await _safe_step(ctx, "schedule", schedule.run(ctx))
                if "codes" in features:
                    await _safe_step(ctx, "codes", codeposter.run(ctx))
            if "maintenance" in features:
                await _safe_step(ctx, "maintenance", maintenance.run(ctx))
            elif ctx.active and not settings.maintenance_enabled:
                maint_map = state.data.get("maintenance")
                for record in (maint_map.values() if isinstance(maint_map, dict) else ()):
                    if isinstance(record, dict):
                        record["paused"] = True
            if "news" in features:
                await _safe_step(ctx, "news", run_news(ctx))
            elif ctx.active and not settings.news_enabled:
                # One control-marker write on disabling, no discovery or per-tick news churn.
                news_map = state.data.get("news")
                for record in (news_map.values() if isinstance(news_map, dict) else ()):
                    if isinstance(record, dict):
                        record["paused"] = True
            if "beta" in features:
                await _safe_step(ctx, "beta", beta.run(ctx))
            elif ctx.active and not settings.beta_enabled:
                # Same one-marker rule: disabling parks the records, it never discovers.
                beta_map = state.data.get("beta")
                for record in (beta_map.values() if isinstance(beta_map, dict) else ()):
                    if isinstance(record, dict):
                        record["paused"] = True
            if not features:
                ctx.report.append("⏸️ no active features this run (ENABLED_FEATURES=none or passive standby)")
        except Exception as exc:  # noqa: BLE001 — guarantee persistence & step summary on unexpected abort
            log.exception("unexpected failure during run_once orchestration")
            ctx.errors.append(f"orchestration failed: {_summary_text(exc)}")
        finally:
            if ctx.active and features and not ctx.errors and not ephemeral:
                state.heartbeat(settings.instance_name, settings.heartbeat_min, ctx.now)
            state.prune(ctx.now)
            state.prune_beta()
            if not ephemeral:
                if state.save():
                    log.info("state saved -> %s", settings.state_path)
            elif settings.test_mode:
                ctx.report.insert(0, "🧪 TEST MODE — every card below is a NEW message marked TEST, built "
                                     "from this run's live data. No live card was edited, no copy was "
                                     "touched and the state file was NOT saved. Delete the test cards "
                                     "when you are done")
            log.info("sources: %s", _summary_text(fetcher.summary()))
            ctx.elapsed = time.monotonic() - started
            write_summary(ctx)
        return ctx
    finally:
        if own_session:
            await session.close()


def _summary_text(value):
    text = str(value)
    for name, secret in os.environ.items():
        if len(secret) >= 8 and re.search(r"TOKEN|SECRET|PASSWORD|WEBHOOK|API_KEY", name, re.I):
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"https?://[^\s/]*(?:discord(?:app)?\.com)/api/webhooks/[^\s<>]+",
                  "[REDACTED_WEBHOOK]", text, flags=re.I)
    text = re.sub(r"([?&](?:token|feed|user|key|api_key|access_token|auth_key)=)[^&\s<>]+",
                  r"\1[REDACTED]", text, flags=re.I)
    text = re.sub(r"(Bearer\s+)[^\s<>]+", r"\1[REDACTED]", text, flags=re.I)
    return html.escape(text.replace("\r", " ").replace("\n", " "), quote=False).replace("`", "'")[:1200]


def _annotation_text(value):
    return _summary_text(value).replace("%", "%25")


def write_summary(ctx: Ctx) -> None:
    lines = [f"### Game-Express {build_id()} — {ctx.settings.instance_name} ({ctx.settings.instance_role})"]
    if ctx.settings.dry_run:
        lines.append("> **DRY RUN** — nothing was posted or saved; \"posted\" below means *would post*. "
                     "The full card JSON is in the job log (paste it into discohook.app to see it).")
    elif ctx.settings.test_mode:
        lines.append("> **TEST MODE** — real data, throwaway cards. Every card is a NEW message "
                     "labelled 🧪 TEST, posted from the sources this run actually fetched; live cards "
                     "and their copies are never edited and the state file is not saved. "
                     "Delete the test cards afterwards.")
    lines += [f"- {r}" for r in ctx.report] or ["- No report messages recorded; this alone does not establish source health or absence of eligible items."]
    for w in ctx.warnings:
        lines.append(f"- ⚠️ {w}")
        print(f"::warning::{_annotation_text(w)}")
    for e in ctx.errors:
        lines.append(f"- ❌ {e}")
        print(f"::error::{_annotation_text(e)}")
    lines.append("- repository commit/push: not observed here; see the subsequent workflow persistence step.")
    lines.append(f"- sources: {ctx.fetcher.summary()}")
    if ctx.elapsed:
        lines.append(f"- ⏱️ run took {ctx.elapsed:.1f}s")
    text = "\n".join(_summary_text(line) for line in lines)
    log.info("\n%s", text)
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError as exc:
            log.warning("Could not append workflow summary (%s)", type(exc).__name__)


async def run_news(ctx: Ctx) -> None:
    """Independent opt-in official news. No schedule/code mirror or catch-all routing.

    Persist progress per part using the existing atomic state writer. Cross-process failures
    after Discord accepts but before state is saved remain an inherently ambiguous outcome.
    """
    from .cards import news_payloads
    from .state import stable_hash

    if "news" not in ctx.settings.features or not ctx.active:
        return
    if ctx.settings.test_mode:
        ctx.warnings.append("News test-mode posting is disabled; use preview --kind news or run --only news --dry-run")
        return
    ctx.state.prune_news(ctx.now)
    for key, record in (ctx.state.data.get("news") or {}).items():
        if key not in ctx.settings.news_games:
            record["paused"] = True
    ephemeral = ctx.settings.dry_run or ctx.settings.test_mode
    for game in ctx.games:
        if game.key not in ctx.settings.news_games:
            continue
        source = hoyolab if game.hoyolab_gid else kuro if game.kuro_news else None
        if source is None:
            ctx.report.append(f"ℹ️ {game.short} news: no verified source (inactive)")
            continue
        hook = ctx.settings.webhook("news", game.key)
        if not hook:
            ctx.warnings.append(f"{game.short} news: missing dedicated webhook; no request or bootstrap")
            continue
        record = ctx.state.data.get("news", {}).get(game.key, {})
        fp = webhook_fingerprint(hook)
        # Include thread routing in the non-secret fingerprint (same webhook can target threads).
        fp = stable_hash([fp, hook.partition("?")[2]])
        if record.get("route") and record["route"] != fp:
            ctx.warnings.append(f"{game.short} news: destination changed; review news state before resuming")
            continue
        since = max(ctx.now - 48 * 3600, record.get("floor", 0) + 1)
        try:
            rows = await source.discover_news(ctx.fetcher, game, since)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            ctx.warnings.append(f"{game.short} news: invalid discovery ({type(exc).__name__}); retry")
            continue
        if rows is None:
            ctx.warnings.append(f"{game.short} news: discovery unavailable; no baseline advanced")
            continue
        record = ctx.state.news_record(game.key)
        if "floor" not in record or record.get("paused"):
            record.update(floor=ctx.now, route=fp, paused=False)
            record.setdefault("records", {})
            if not ephemeral:
                ctx.state.save()
            ctx.report.append(f"🌱 {game.short} news: silent baseline; historical articles not posted")
            continue
        record.setdefault("route", fp)
        records = record.setdefault("records", {})
        eligible = [row for row in rows if record["floor"] < row["ts"] <= ctx.now
                    and row["ts"] >= ctx.now - 48 * 3600]
        failures = attempted = delivered = 0
        # Finish known partial deliveries even if the discovery page/window has moved on.
        for key, rec in records.items():
            if not isinstance(rec, dict) or rec.get("status") != "partial":
                continue
            saved_row = rec.get("row")
            if not isinstance(saved_row, dict) or "id" not in saved_row or "ts" not in saved_row:
                failures += 1
                ctx.warnings.append(
                    f"{game.short} news {key}: incomplete partial delivery state; manual review required"
                )
                continue
            if not any(row["id"] == key for row in eligible):
                eligible.append(saved_row)
        eligible.sort(key=lambda value: (value["ts"], value["id"]))
        # Process every discovered eligible row in publication order. The stored
        # cursor remains diagnostic only, never a floor or delivery checkpoint.
        for row in eligible:
            rec = records.get(row["id"])
            if rec and rec.get("status") == "posted":
                continue
            attempted += 1
            if rec is None and len(records) >= 500:
                ctx.warnings.append(f"{game.short} news: state capacity reached; safe cleanup deferred admission")
                break
            record["cursor"] = [row["ts"], row["id"]]
            try:
                item = await source.news_article(ctx.fetcher, game, row)
                if item is None:
                    raise ValueError("full article unavailable/mismatched")
                # Official news delivery is strictly independent of schedule, schedule mirrors,
                # codes, and preload/maintenance started alerts (Monitor #456 / GI 47151000).
                # Schedule classification or mirror routing never suppresses an eligible official
                # article. Previously persisted legacy entries carrying {"status": "posted",
                # "excluded": "schedule"} stay marked "posted" above so historical articles are
                # never mass-reposted on rollout without separate operator authorization.
                payloads = news_payloads(
                    game,
                    item,
                    timestamps=ctx.settings.news_timestamps,
                    expand_full=ctx.settings.news_expand_full,
                    max_pages=ctx.settings.news_expand_max_pages,
                    max_chars=ctx.settings.news_expand_max_chars,
                    max_media=ctx.settings.news_expand_max_media,
                )
                from .media import group_news_media, news_media_available

                media_cap = ctx.settings.news_expand_max_media if ctx.settings.news_expand_full else 40
                if not await news_media_available(ctx.fetcher, payloads, max_urls=media_cap):
                    # One fresh official full response can supply a corrected/renewed URL.
                    fresh = await source.news_article(ctx.fetcher, game, row)
                    if fresh is None or fresh.title != item.title:
                        raise ValueError("media unavailable; fresh official article mismatched")
                    item = fresh
                    payloads = news_payloads(
                        game,
                        item,
                        timestamps=ctx.settings.news_timestamps,
                        expand_full=ctx.settings.news_expand_full,
                        max_pages=ctx.settings.news_expand_max_pages,
                        max_chars=ctx.settings.news_expand_max_chars,
                        max_media=ctx.settings.news_expand_max_media,
                    )
                    if not await news_media_available(ctx.fetcher, payloads, max_urls=media_cap):
                        raise ValueError("media unavailable after official refresh; held for retry, not posted")
                payloads = await group_news_media(
                    ctx.fetcher,
                    payloads,
                    enabled=ctx.settings.news_group_long_strips,
                    max_urls=media_cap,
                )
                signature = stable_hash(payloads)
                if rec is not None:
                    if rec.get("ambiguous"):
                        raise ValueError(
                            "partially delivered article had an ambiguous webhook outcome; manual review required"
                        )
                    msgs = rec.get("messages")
                    if (
                        not isinstance(msgs, list)
                        or not msgs
                        or any(not isinstance(m, str) or not m for m in msgs)
                        or not rec.get("signature")
                        or len(msgs) > len(payloads)
                    ):
                        raise ValueError("incomplete partial delivery state; manual review required")
                    if rec.get("signature") != signature:
                        raise ValueError("partially delivered article changed; manual review required")
                if rec is None:
                    rec = {"status": "partial", "ts": row["ts"], "row": row,
                           "signature": signature, "messages": []}
                for payload in payloads[len(rec["messages"]):]:
                    result = await ctx.webhook.send(hook, payload, stop_on_ambiguous=True)
                    if result.ambiguous or (result.ok and not result.message_id):
                        rec["ambiguous"] = True
                        records[row["id"]] = rec
                        if not ephemeral:
                            ctx.state.save()
                        raise ValueError("ambiguous Discord webhook outcome; held to prevent duplicate repost")
                    if not result.ok:
                        raise ValueError(f"Discord delivery failed ({result.status}); retry required")
                    rec["messages"].append(result.message_id)
                    records[row["id"]] = rec
                    if not ephemeral:
                        ctx.state.save()
                rec["status"] = "posted"
                rec.pop("row", None)
                rec.pop("signature", None)
                if not ephemeral:
                    ctx.state.save()
                delivered += 1
                ctx.report.append(f"📰 {game.short} news: delivered {item.id} ({len(payloads)} message(s), "
                                  f"{sum(len(p['components']) for p in payloads)} container(s))")
            except Exception as exc:  # noqa: BLE001 — hold single article without aborting remaining news
                failures += 1
                ctx.warnings.append(f"{game.short} news {row['id']}: {type(exc).__name__}: {exc}")
        ctx.report.append(f"📰 {game.short} news: discovered={len(rows)}, eligible={len(eligible)}, "
                          f"attempted={attempted}, delivered={delivered}, deferred/error={failures}")
