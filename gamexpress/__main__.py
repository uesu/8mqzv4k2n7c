"""Command line entry point:  python -m gamexpress <command> [options]

  run             one monitor pass (GitHub Actions uses this)            [default]
  test-card       post the sample cards (your 4 reference cards + one codes card per game),
                  labelled 🧪 TEST; no ping unless --ping
  check-webhooks  send ONE small "✅ connected" card to every configured webhook, listing
                  which game/feature cards that channel will receive
  preview         write previews/index.html (Discord-like view of every sample card) + JSON
  validate        check config/games.json + env and print the resolved routing
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from . import build_id
from .cards import (
    IS_COMPONENTS_V2,
    NEWS_DEFAULT_MAX_PAGES,
    codes_payloads,
    container,
    mark_test,
    schedule_payload,
    text,
    validate_payload,
)
from .config import ROOT, Ping, game_is_on, load_games, load_settings, parse_ping
from .samples import CODE_SAMPLES, SCHEDULE_SAMPLES

log = logging.getLogger("gamexpress")


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass


def sample_payloads(kind: str = "all", game: str = "", *, mark: bool = False,
                    ping: bool = True, unlaunched: bool = False) -> list[tuple[str, str, dict]]:
    """[(feature, name, payload)] for every sample card. mark=True labels them 🧪 TEST;
    ping=False strips the role ping (test posts should not notify a whole role);
    unlaunched=True keeps ONLY the games that are not RELEASED yet ("released": false in
    games.json) — a game that is not out has no real code to fetch, so its card and its channel
    can only be checked with example data. Note this is independent of `enabled`: a pre-release
    game is still monitored for its announcements, it just has nothing real to redeem yet."""
    settings = load_settings()
    games = load_games()
    out = []

    def _ping(feature: str, key: str) -> Ping:
        return settings.ping(feature, key) if ping else Ping()

    if kind in ("all", "schedule"):
        for key, data in SCHEDULE_SAMPLES.items():
            if key in games and (not game or game == key):
                out.append(("schedule", f"schedule_{key}",
                            schedule_payload(games[key], data, settings, _ping("schedule", key))))
    if kind in ("all", "codes"):
        for key, codes in CODE_SAMPLES.items():
            if key not in games or (game and game != key):
                continue
            if unlaunched and games[key].released:
                continue                     # live games get their REAL codes from the real run
            for i, p in enumerate(codes_payloads(games[key], codes, settings,
                                                 _ping("codes", key), int(time.time()))):
                out.append(("codes", f"codes_{key}{'_' + str(i + 1) if i else ''}", p))
    if kind == "news":
        from .cards import news_payloads
        from .models import Item

        for key in ("genshin", "starrail", "zzz", "wuwa", "hna"):
            if game and key != game:
                continue
            item = Item("kuro" if key == "wuwa" else "hoyolab", key, "sample", "https://example.com/news",
                        "OFFLINE SAMPLE — official news layout", "", 1792281600,
                        article_blocks=[{"kind": "text", "text": "Sample full article text. This is not a real announcement."},
                                        {"kind": "image", "url": "https://example.com/sample.gif"},
                                        {"kind": "text", "text": "More article text after the sample media."}],
                        author="Official source (sample)")
            for i, payload in enumerate(news_payloads(games[key], item)):
                out.append(("news", f"news_{key}_{i + 1}", payload))
    if kind == "beta":
        from .cards import beta_payloads
        from .models import BetaCharacter, BetaEquipment, BetaPatch, BetaSectionEntry

        # Offline shape check: one new character with a two-page-worth kit plus one
        # equipment entry, so both the kit and the gallery paths are exercised.
        long_lines = [
            "Deals Spectro DMG equal to 512.40% of ATK to all enemies in front.",
            "When the target is under [Contamination], this DMG is increased by 30%.",
            "Each stack of [Radiant Edge] consumed increases this skill's CRIT DMG by 12%.",
            "Restores 10 points of Concerto Energy. This effect can trigger once every 20s.",
        ]
        # Game-accurate labels: `role` is a weapon for GI/WuWa, a Path for HSR and a Specialty for
        # ZZZ in the real cards, so the sample must not print "Weapon: Defense".
        sample_shape = {
            "genshin": ("Cryo", "Polearm", "Weapons"),
            "starrail": ("Quantum", "Preservation", "Light Cones"),
            "zzz": ("Ether", "Defense", "W-Engines"),
            "wuwa": ("Glacio", "Sword", "Weapons"),
        }
        for key in ("genshin", "starrail", "zzz", "wuwa"):
            if game and key != game:
                continue
            if key not in games:
                continue
            sample_element, sample_role, sample_equip_label = sample_shape[key]
            sample_char = BetaCharacter(
                id=f"{key}:sample", name="Sample Newcomer", rarity=5,
                element=sample_element, role=sample_role,
                subtext="-# Sample kit — offline preview data, not a real beta entry",
                stats_lines=["Lv. 60 — HP 8754 · ATK 750 · DEF 706", "CRIT Rate 5%", "CRIT DMG 50%"],
                sections=[
                    BetaSectionEntry(title="Basic Attack: Sample Strike", category="Basic Attack",
                                     tag="[NEW]", meta="", lines=list(long_lines) * 3),
                    BetaSectionEntry(title="Special Attack: Sample Blast", category="Special Attack",
                                     tag="[NEW]", meta="Single Target", lines=list(long_lines) * 3),
                    BetaSectionEntry(title="Core Passive: Sample Resolve", category="Core Passive",
                                     tag="[NEW]", meta="", lines=list(long_lines) * 2),
                ],
                is_new=True,
            )
            sample_equip = BetaEquipment(
                id=f"{key}:sample-equip", name="Sample Signature", category_label=sample_equip_label,
                rarity=5, tag="[NEW]", stats_line="Base ATK 713", effect_title="Sample Effect",
                lines=["Increases the equipper's CRIT Rate by 24%.",
                       "When the equipper applies a shield, squad DEF increases by 10% for 10s."],
            )
            sample_patch = BetaPatch(
                game_key=key, beta_version="0.0", rev_label="v0.0 Rev. 00000000 — offline sample",
                rev_id="sample", characters=[sample_char], equipment=[sample_equip],
            )
            for i, payload in enumerate(beta_payloads(games[key], sample_patch, settings,
                                                      _ping("beta", key))):
                out.append(("beta", f"beta_{key}_{i + 1}", payload))
    if kind == "maintenance":
        from .cards import maintenance_payload

        sample_versions = {
            "genshin": ("7.2", 1793570400, 1793725200, 1793743200),
            "starrail": ("4.7", 1794013200, 1794157200, 1794175200),
            "zzz": ("3.3", 1792382400, 1792533600, 1792551600),
            "wuwa": ("3.7", 1790560800, 1790712000, 1790737200),
            "hna": ("1.0", 1795000000, 1795140000, None),
            "ananta": ("1.0", 1800000000, 1800144000, None),
        }
        for key, (ver, pre_ts, start_ts, end_ts) in sample_versions.items():
            if game and key != game:
                continue
            media = "https://example.com/sample-launch.mp4" if key not in ("hna", "ananta") else None
            out.append((
                "maintenance",
                f"maintenance_{key}_preload",
                maintenance_payload(
                    games[key], ver, "preload", start_ts=pre_ts,
                    ping=_ping("maintenance", key), media_url=media,
                ),
            ))
            out.append((
                "maintenance",
                f"maintenance_{key}_start",
                maintenance_payload(
                    games[key], ver, "maintenance", start_ts=start_ts, end_ts=end_ts,
                    ping=_ping("maintenance", key), media_url=media,
                ),
            ))
    if mark:
        out = [(f, n, mark_test(p)) for f, n, p in out]
    return out


async def cmd_test_card(args) -> int:
    import aiohttp

    from .discord import WebhookClient
    settings = load_settings()
    if args.kind in ("news", "maintenance", "beta"):
        print(f"{args.kind.title()} samples are preview-only; use preview --kind {args.kind}. No messages sent.")
        return 0
    rc = 0
    async with aiohttp.ClientSession() as session:
        client = WebhookClient(session, dry_run=settings.dry_run or args.dry_run)
        for feature, name, payload in sample_payloads(args.kind, args.game, mark=True, ping=args.ping,
                                                      unlaunched=args.unlaunched):
            key = name.split("_", 1)[1].split("_")[0]
            secret, url = settings.webhook_source(feature, key)
            if not url:
                print(f"[{name}] no webhook for {feature}/{key} (add {settings.expected_webhook_names(feature, key)})"
                      " — printing the payload instead")
                print(json.dumps(payload, ensure_ascii=False, indent=2)[:3000])
                continue
            res = await client.send(url, payload)
            print(f"[{name}] -> {secret}: {'OK' if res.ok else 'FAILED'} {res.status} {res.error}")
            rc |= 0 if res.ok else 1
    return rc


async def cmd_check_webhooks(args) -> int:
    """One small V2 card per UNIQUE webhook, listing every game/feature routed to it."""
    import aiohttp

    from .discord import WEBHOOK_RE, WebhookClient
    settings = load_settings()
    games = load_games()
    feats = [f for f in ("schedule", "codes") if args.kind in ("all", f)]
    targets = [g for g in games.values() if not args.game or g.key == args.game]
    routes: dict[str, dict] = {}
    missing: list[str] = []
    for f in feats:
        for g in targets:
            secret, url = settings.webhook_source(f, g.key)
            if not url:
                if game_is_on(g, settings):
                    missing.append(f"{f}/{g.key} -> add {settings.expected_webhook_names(f, g.key)}")
                continue
            r = routes.setdefault(url, {"secrets": set(), "routes": []})
            r["secrets"].add(secret)
            r["routes"].append((f, g))
    rc = 0
    async with aiohttp.ClientSession() as session:
        client = WebhookClient(session, dry_run=settings.dry_run or args.dry_run)
        for url, r in routes.items():
            names = ", ".join(sorted(r["secrets"]))
            if not WEBHOOK_RE.match(url):
                print(f"! {names}: this value is not a Discord webhook URL "
                      "(expected https://discord.com/api/webhooks/<id>/<token>) — re-copy it")
                rc = 1
                continue
            lines = []
            for f, g in r["routes"]:
                p = settings.ping(f, g.key)
                state = "" if game_is_on(g, settings) else " · *prepared (off)*"
                lines.append(f"✦ **{f.title()}** · {g.name}{state} — ping: {p.text or 'no ping'}")
            card = container([
                text("### ✅ Game-Express webhook check"),
                text("This channel is connected and will receive:\n" + "\n".join(lines)),
                text(f"-# secret: {names} • sent by the monitor's test bench • nobody was pinged"),
            ], 0x57F287)
            payload = {"flags": IS_COMPONENTS_V2, "allowed_mentions": {"parse": []}, "components": [card]}
            res = await client.send(url, payload)
            print(f"[{names}] {len(r['routes'])} route(s): {'OK' if res.ok else 'FAILED'} {res.status} {res.error}")
            rc |= 0 if res.ok else 1
    for m in missing:
        print(f"  · no webhook: {m}")
    if not routes:
        print("No webhook configured at all — add DISCORD_WEBHOOK_SCHEDULE / DISCORD_WEBHOOK_CODES (or per game).")
        rc = 1
    return rc


def cmd_preview(args) -> int:
    from .preview_html import render_page
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cards = []
    for feature, name, payload in sample_payloads(args.kind, args.game):
        problems = validate_payload(payload)
        (out / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{name}.json  {'OK' if not problems else 'PROBLEMS: ' + '; '.join(problems)}")
        group = {
            "schedule": "Schedule cards",
            "codes": "Code cards",
            "news": "Official news samples",
            "maintenance": "Pre-Load & Maintenance samples",
            "beta": "Beta kit & changelog samples",
        }[feature]
        cards.append((group, name, payload))
    if args.kind in ("all", "codes") and (not args.game or args.game == "starrail"):
        # how a posted card looks after the automatic silent edit once a code has expired
        settings, games = load_settings(), load_games()
        codes = [dict(c, expired=(i == 0)) for i, c in enumerate(CODE_SAMPLES["starrail"])]
        payload = codes_payloads(games["starrail"], codes, settings, settings.ping("codes", "starrail"),
                                 int(time.time()) - 3 * 86400)[0]
        (out / "codes_starrail_after_expiry.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                                              encoding="utf-8")
        cards.append(("Code card after a code expired (silent edit, no ping)", "codes_starrail_after_expiry",
                      payload))
    (out / "index.html").write_text(render_page(cards), encoding="utf-8")
    print(f"\nWrote {out}/index.html — open it in a browser for a Discord-like preview, or paste a JSON "
          "file into https://discohook.app (Components V2).")
    return 0


def cmd_speculate(args) -> int:
    """Show what the bot would predict for each game's next version, and why.

    Read-only: it posts nothing, edits nothing and writes no state. It is the way to check a
    prediction against reality before trusting it on a live card.
    """
    from datetime import datetime, timezone

    from .schedule import observed_lead_h, observed_starts, predict_cycle
    from .state import State
    settings, games = load_settings(), load_games()
    state = State.load(settings.state_path)
    now = int(args.now) if getattr(args, "now", 0) else int(time.time())
    conf_mark = {"high": "●●●", "medium": "●●○", "low": "●○○"}
    rows = 0
    print(f"Speculated cycles at {datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d %H:%M} UTC — "
          "estimates only, every one is replaced by the official notice.\n")
    for g in games.values():
        if args.game and g.key != args.game:
            continue
        if g.cadence is None:
            continue
        records = state.schedule_records(g.key)
        guess = predict_cycle(g, records, now, lead_h=observed_lead_h(records))
        if not guess:
            print(f"{g.short:5} {g.name:22} no prediction (no anchor, or the next cycle is "
                  "outside the 120-day horizon)")
            continue
        rows += 1
        tz = g.cadence.tz

        def fmt(ts, tz=tz) -> str:
            return datetime.fromtimestamp(ts, tz).strftime("%a %d %b %Y %H:%M") if ts else "—"

        label = guess["version"] or "?"
        if guess["version_alt"]:
            label += f" (or {guess['version_alt']} if the major rolls over)"
        learned = (f"learned from {guess['observed']} real notices"
                   if guess["learned"] else
                   f"shipped cold-start value ({guess['observed']} real notice(s) on file, "
                   f"{2 - guess['observed']} more needed to learn)")
        print(f"{g.short:5} {g.name} {label}")
        print(f"      confidence {conf_mark.get(guess['confidence'], '?')} {guess['confidence']:6} "
              f"| cadence {guess['cadence_days']:g}d, {learned}")
        print(f"      anchored on the last real maintenance {fmt(guess['anchor_ts'])}")
        print(f"      {g.program_label:17} {fmt(guess['program_ts'])}")
        print(f"      {'Pre-install':17} {fmt(guess['preinstall_ts'])}")
        print(f"      {'Maintenance':17} {fmt(guess['maint_start_ts'])} → "
              f"{fmt(guess['maint_end_ts'])}  (all times UTC+8)")
        if args.verbose:
            starts = observed_starts(records)
            seen = [datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d") for _v, ts in starts]
            print(f"      real maintenance dates on file: {', '.join(seen) or 'none'}")
        print()
    if not rows:
        print("Nothing to predict. Add a 'cadence' block to a game in config/games.json.")
    return 0


def _beta_ping_names(settings, games: dict) -> list[str]:
    """Every env name that could carry the feature-5 ping (checked, never inherited)."""
    names = ["PING_BETA"]
    for g in games.values():
        for slug_name in settings._game_slugs(g.key):
            names.append(f"PING_BETA_{slug_name}")
    return names


def _beta_budget_text(settings) -> str:
    from .beta import _budget

    class _Shim:
        def __init__(self, s):
            self.settings = s

    b = _budget(_Shim(settings))
    return (f"chars≤{b['max_chars']} changed≤{b['max_changed']} equip≤{b['max_equip']} "
            f"requests≤{b['max_requests']} repoll≥{b['min_poll_min']}m")


def cmd_validate(_args) -> int:
    from .sources.reddit import format_reddit_feed_query

    settings = load_settings()
    games = load_games()
    feed_q = format_reddit_feed_query(settings.reddit_token)
    print(f"Game-Express {build_id()} | features={sorted(settings.features) or 'NONE'} | "
          f"instance={settings.instance_name} ({settings.instance_role}) | dry_run={settings.dry_run}")
    print(f"peer_state_url={'set' if settings.peer_state_url else '—'} | X={'on' if settings.x_enabled else 'off'} "
          f"| nitter instances={len(settings.nitter_instances)} | token={'set' if settings.nitter_token else '—'}")
    print(f"Reddit={'on' if settings.reddit_enabled else 'off'} "
          f"| reddit instances={len(settings.reddit_instances)} | proxies={len(settings.reddit_proxies)} "
          f"| feed_token={'set' if feed_q else ('invalid' if settings.reddit_token else '—')}")
    print(f"news={'on' if 'news' in settings.features else 'off'} | news games={','.join(sorted(settings.news_games))} "
          f"| article count cap=none "
          f"| timestamps={'on' if settings.news_timestamps else 'off'} "
          f"| expand full={'on' if settings.news_expand_full else 'off'} "
          f"| group long strips={'on' if settings.news_group_long_strips else 'off'} | dedicated destinations only")
    print(f"maintenance={'on' if 'maintenance' in settings.features else 'off'} "
          f"| maintenance games={','.join(sorted(settings.maintenance_games)) or 'none'} | dedicated destinations only")
    print(f"beta={'on' if 'beta' in settings.features else 'off'} "
          f"| beta games={','.join(sorted(settings.beta_games)) or 'none'} "
          f"| ping={'configured' if any(str(settings.env.get(n, '') or '').strip() for n in _beta_ping_names(settings, games)) else 'off (no PING_BETA set)'} "
          f"| budget={_beta_budget_text(settings)}")
    print("emoji: " + ", ".join(f"{k}={'✓' if v else '—'}" for k, v in settings.emoji.items()))
    ok = True
    from .discord import WEBHOOK_RE, webhook_fingerprint
    for g in games.values():
        on = game_is_on(g, settings)
        cols = []
        for feature in ("schedule", "codes", "news", "maintenance", "beta"):
            secret, url = settings.webhook_source(feature, g.key)
            hook = f"✓ {secret}" if url else "— none"
            if url and not WEBHOOK_RE.match(url):
                hook = f"✗ {secret} is not a webhook URL"
                ok = False
            mirror = ""
            msecret, murl = (
                settings.mirror_webhook_source(feature, g.key)
                if feature not in ("news", "maintenance")
                else ("", None)
            )
            if murl and not WEBHOOK_RE.match(murl):
                mirror = f" | ✗ {msecret} is not a webhook URL"
                ok = False
            elif murl and url and webhook_fingerprint(murl) == webhook_fingerprint(url):
                mirror = f" | ⚠ {msecret} is the same channel as {secret} — no copy sent"
            elif murl:
                mirror = f" | 🪞 also {msecret}"
            ping_text = settings.ping(feature, g.key).text if feature != "news" else ""
            cols.append(f"{feature}[{hook} | {ping_text or 'no ping'}{mirror}]")
        flag = "✓" if on else "·"
        note = "" if on else (f" (off; auto-on {g.auto_enable_on})" if g.auto_enable_on else " (off)")
        print(f"  {flag} {g.key:9} {g.name:22}{note}\n      {cols[0]}\n      {cols[1]}\n      {cols[2]}\n"
              f"      {cols[3]}\n      {cols[4]}\n"
              f"      X={','.join(g.x_accounts) or '—'} Reddit={','.join(g.subreddits) or '—'} "
              f"code_sources={','.join(g.codes.get('sources', [])) or '—'} "
              f"4★/phase={g.four_star_count or 'count not set (set four_star_count at launch)'}"
              + (f"\n      beta={g.beta.get('nanoka') or '—'} "
                 f"changelog={g.beta.get('changelog') or '—'}" if g.beta else ""))
    valid_bools = {"1", "true", "yes", "on", "y", "0", "false", "no", "off", "n"}
    if settings.reddit_token and not feed_q:
        print("  ! REDDIT_FEED_TOKEN has no usable 'feed=...' key "
              "(expected https://old.reddit.com/.rss?feed=<40-char-key>&user=<username>, "
              "https://www.reddit.com/.rss?feed=<40-char-key>&user=<username>, or <40-char-key>&user=<username>)")
        ok = False
    for name in (
        "REDDIT_ENABLED",
        "NEWS_ENABLED",
        "NEWS_TIMESTAMPS",
        "NEWS_EXPAND_FULL",
        "NEWS_GROUP_LONG_STRIPS",
        "MAINTENANCE_ENABLED",
        "BETA_ENABLED",
    ):
        raw_val = str(settings.env.get(name, "") or "").strip()
        if raw_val and raw_val.lower() not in valid_bools:
            print(f"  ! {name}={raw_val!r} is not a recognized boolean ('0'/'1', 'false'/'true')")
            ok = False
    for name, gset in (("NEWS_GAMES", settings.news_games),
                       ("MAINTENANCE_GAMES", settings.maintenance_games),
                       ("BETA_GAMES", settings.beta_games)):
        unknown = sorted(k for k in gset if k not in games)
        if unknown:
            print(f"  ! {name} contains unknown game key(s): {', '.join(unknown)}")
            ok = False
    # Feature 5 is scoped to the four released games: a game that has not shipped has no
    # beta to watch, and saying so here beats silently polling a changelog that is empty.
    for key in sorted(settings.beta_games):
        g = games.get(key)
        if g is not None and not g.beta:
            print(f"  ! BETA_GAMES includes {key}, which has no 'beta' block in config/games.json")
            ok = False
    ping_env_names = ["PING_ROLE_ID", "PING_SCHEDULE", "PING_CODES", "PING_MAINTENANCE"]
    for g in games.values():
        for slug_name in settings._game_slugs(g.key):
            ping_env_names.append(f"PING_MAINTENANCE_{slug_name}")
    ping_env_names.extend(_beta_ping_names(settings, games))
    for name in ping_env_names:
        v = str(settings.env.get(name, "") or "")
        if v and parse_ping(v) is not None and not parse_ping(v) and v.lower() not in ("none", "off", "no", "false", "0", "-"):
            print(f"  ! {name}={v!r} has no valid role id (expected digits, 'everyone', 'here' or 'none')")
            ok = False
    for check_kind in ("all", "news", "maintenance", "beta"):
        for _f, name, payload in sample_payloads(check_kind):
            problems = validate_payload(payload)
            if problems:
                ok = False
                print(f"  ! sample {name}: {'; '.join(problems)}")
    print("config OK" if ok else "config has problems (see the '!' / '✗' lines)")
    return 0 if ok else 1


# The standard benchmark deliberately keeps its default-off news baseline unchanged.  The full
# BETA_ENABLED=1 monitor harness lives in beta_harness.py; this compact in-memory set remains its
# supplementary renderer timing companion for every released beta game.
_BETA_BENCHMARK_GAME_KEYS = ("genshin", "starrail", "zzz", "wuwa")


def _beta_benchmark_patches(games: dict):
    """Deterministic new-kit and retained-revision render cases for every beta game.

    These are intentionally synthetic renderer fixtures, not source captures or beta claims.
    Parser/discovery/revision-walking coverage remains in the offline smoke fixtures; this helper
    gives the benchmark a portable enabled-feature render/validation path too.
    """
    from .models import BetaCharacter, BetaEquipment, BetaPatch, BetaSectionEntry

    labels = {
        "genshin": ("Benchmark Traveler", "Sword", "Weapons"),
        "starrail": ("Benchmark Trailblazer", "Light Cone", "Light Cones"),
        "zzz": ("Benchmark Agent", "W-Engine", "W-Engines"),
        "wuwa": ("Benchmark Resonator", "Weapon", "Weapons"),
    }
    cases: dict[str, dict[str, BetaPatch]] = {}
    for key in _BETA_BENCHMARK_GAME_KEYS:
        game = games.get(key)
        if game is None or not game.beta:
            raise ValueError(f"beta benchmark game is not configured: {key}")
        name, equipment_name, equipment_label = labels[key]
        root = f"https://example.com/game-express-benchmark/{key}"
        new_character = BetaCharacter(
            id=f"{key}:benchmark-new", name=name, rarity=5, element="Benchmark", role="Test",
            subtext="-# Offline BETA_ENABLED=1 benchmark fixture — not a real beta entry.",
            stats_lines=["Lv. 90 — HP 12000 · ATK 800 · DEF 700"],
            sections=[
                BetaSectionEntry(
                    title="Basic Attack: Benchmark Strike", category="Basic Attack", tag="[NEW]",
                    lines=[
                        "Deals Benchmark DMG equal to 100% of ATK.",
                        "Restores 10 energy when the target is defeated.",
                    ],
                    params=[{"name": "DMG Multiplier", "value": 100, "text": "100%"}],
                    values_label="Lv. 10",
                ),
                BetaSectionEntry(
                    title="Core Passive: Benchmark Loop", category="Core Passive", tag="[NEW]",
                    lines=["After using the skill, gain 1 stack of Benchmark State for 10s."],
                ),
            ],
            image_url=f"{root}/new-character.webp", page_url=f"{root}/new-character",
            is_new=True,
        )
        new_equipment = BetaEquipment(
            id=f"{key}:benchmark-new-equipment", name=f"Benchmark {equipment_name}",
            category_label=equipment_label, rarity=5, tag="[NEW]", stats_line="Base ATK 700",
            effect_title="Benchmark Effect", lines=["Increases the user's Benchmark DMG by 20%."],
            image_url=f"{root}/new-equipment.webp", page_url=f"{root}/new-equipment",
        )
        revised_character = BetaCharacter(
            id=f"{key}:benchmark-revision", name=name, rarity=5, element="Benchmark", role="Test",
            subtext="-# Retained-baseline revision fixture — only changed source blocks are shown.",
            stats_lines=["Lv. 90 — HP 12000 · ATK 800 · DEF 700"],
            sections=[
                BetaSectionEntry(
                    title="Basic Attack: Benchmark Strike", category="Basic Attack", tag="[BUFF]",
                    lines=["**[BUFF]** Direct DMG multiplier increased from ~~**100%**~~ → **120%**."],
                    params=[{"name": "DMG Multiplier", "value": 120, "text": "120%"}],
                    values_label="Lv. 10",
                ),
                BetaSectionEntry(
                    title="Core Passive: Benchmark Loop", category="Core Passive", tag="[TEXT FIX]",
                    lines=["**[TEXT FIX]** Wording updated with no inferred mechanical direction."],
                ),
            ],
            image_url=f"{root}/revision-character.webp", page_url=f"{root}/revision-character",
            diff_url=f"{root}/revision-character-diff", is_new=False,
        )
        revised_equipment = BetaEquipment(
            id=f"{key}:benchmark-revision-equipment", name=f"Benchmark {equipment_name}",
            category_label=equipment_label, rarity=5, tag="[CHANGED]",
            lines=["**[CHANGED]** Effect wording changed from ~~20%~~ → **24%**."],
            image_url=f"{root}/revision-equipment.webp", page_url=f"{root}/revision-equipment",
            diff_url=f"{root}/revision-equipment-diff",
        )
        cases[key] = {
            "new": BetaPatch(
                game_key=key, beta_version="0.0", rev_label="v0.0 Rev. 00000000 — offline benchmark",
                rev_id=f"benchmark-new-{key}", characters=[new_character], equipment=[new_equipment],
            ),
            "revision": BetaPatch(
                game_key=key, beta_version="0.0", rev_label="v0.0 Rev. 00000001 — offline benchmark",
                rev_id=f"benchmark-revision-{key}", characters=[revised_character],
                equipment=[revised_equipment],
            ),
        }
    return cases


def cmd_benchmark(args) -> int:
    """Reproducible offline micro-benchmark plus an optional full enabled-beta harness.

    `--beta-enabled` is deliberately local to this command. It first executes an in-memory
    `BETA_ENABLED=1` monitor harness for Genshin, Star Rail, ZZZ, and WuWa: source discovery,
    namespace parsing, image preflight, Components V2 validation, page delivery/pacing, state
    settlement, retained revisions, deduplication, and quiet/failed/ambiguous paths. It then
    records the smaller renderer timings separately. No live source, Discord, or state-file
    mutation is possible in either part.
    """
    import platform
    import statistics
    import subprocess
    from datetime import datetime, timezone
    from unittest.mock import patch

    from . import news_timestamps
    from .cards import news_payloads
    from .models import Item
    from .news_timestamps import convert_news_timestamps

    repo_root = Path(__file__).resolve().parent.parent
    try:
        commit_sha = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        tree_clean = subprocess.check_output(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip() == ""
    except Exception:
        commit_sha = build_id()
        tree_clean = False

    games = load_games()
    iters = max(20, int(getattr(args, "iterations", 200) or 200))
    beta_enabled_fixture = bool(getattr(args, "beta_enabled", False))
    cmd_str = f"python -m gamexpress benchmark --iterations {iters}"
    if beta_enabled_fixture:
        cmd_str += " --beta-enabled"

    short_body = (
        "Dear Proxies,\n"
        "Version 3.3 Pre-Installation begins on September 30, 12:00 (server time) — "
        "October 20, 14:59 (server time).\n"
        "Special Program airs September 12, 2026, 08:00 (UTC-4). "
        "Event window: From June 22, 2026 12:00 (UTC+8) to July 3, 2026 23:59 (UTC+8).\n"
        "Reference link: https://hoyo.link/example and inline code `2026/10/21 06:00 (UTC+8)`."
    )
    med_para = (
        "Version 7.2 event details open on October 25, 2026 10:00 (UTC+8) with domains, "
        "story quests, and seasonal rewards available across all regions."
    )
    medium_blocks = [
        {"kind": "text", "text": "\n\n".join([med_para] * 17)},
        {"kind": "image", "url": "https://upload-os-bbs.hoyolab.com/bench-mid.jpg"},
        {"kind": "text", "text": "\n\n".join([med_para] * 17)},
    ]
    long_body = "\n\n".join([short_body] * 36)
    short_item = Item(
        "hoyolab", "zzz", "bench-short", "https://www.hoyolab.com/article/46863847",
        "Version 3.3 Event & Pre-Download Details", "", 1790650000,
        article_blocks=[
            {"kind": "text", "text": short_body},
            {"kind": "image", "url": "https://upload-os-bbs.hoyolab.com/bench-lead.jpg"},
        ],
        author="Zenless Zone Zero",
    )
    medium_item = Item(
        "hoyolab", "genshin", "bench-2page", "https://www.hoyolab.com/article/47151000",
        "Version 7.2 Event Overview", "", 1792555204,
        article_blocks=medium_blocks,
        author="Genshin Impact",
    )
    long_blocks = []
    for idx in range(18):
        long_blocks.append({"kind": "text", "text": (short_body + "\n\n") * 2})
        long_blocks.append({"kind": "image", "url": f"https://hw-media-cdn-mingchao.kurogame.com/object/{idx}.gif"})
    long_item = Item(
        "kuro", "wuwa", "bench-long", "https://wutheringwaves.kurogames.com/en/main/news/detail/5571",
        "Version 3.7 Patch Notes & Adjustments", "", 1791000000,
        article_blocks=long_blocks,
        author="Kuro Games",
    )

    # Verify default-off parity, timestamp-converter bypass, and 7000/2-page sequence before timing.
    assert convert_news_timestamps(long_body, published_ts=1790650000, enabled=False) is long_body
    ts_calls = 0
    real_convert = news_timestamps.convert_news_timestamps

    def _spy_convert(*c_args, **c_kwargs):
        nonlocal ts_calls
        ts_calls += 1
        return real_convert(*c_args, **c_kwargs)

    with patch.object(news_timestamps, "convert_news_timestamps", side_effect=_spy_convert):
        default_short = news_payloads(games["zzz"], short_item)
        base_short = news_payloads(games["zzz"], short_item, timestamps=False, expand_full=False)
        default_medium = news_payloads(games["genshin"], medium_item)
        base_medium = news_payloads(games["genshin"], medium_item, timestamps=False, expand_full=False)
        default_long = news_payloads(games["wuwa"], long_item)
        base_long = news_payloads(games["wuwa"], long_item, timestamps=False, expand_full=False)
    assert ts_calls == 0
    assert base_short == default_short
    assert base_medium == default_medium
    assert base_long == default_long
    expanded_medium = news_payloads(games["genshin"], medium_item, timestamps=False, expand_full=True)
    expanded_long = news_payloads(games["wuwa"], long_item, timestamps=False, expand_full=True)
    # Default policy smart fits the whole article: 1 message when it fits, otherwise as many
    # pages as it needs (never an excerpt while it is inside the default page ceiling).
    assert len(base_short) == 1 and 1 <= len(base_medium) <= 2
    assert 2 <= len(base_long) <= NEWS_DEFAULT_MAX_PAGES
    assert len(expanded_medium) == len(base_medium) and len(expanded_long) == len(base_long)

    epochs = [1789214400 + i * 3600 for i in range(16)]
    utc8 = timezone.utc

    def _measure(label: str, fn) -> dict:
        samples_us: list[float] = []
        fn()  # warmup
        for _ in range(iters):
            t0 = time.perf_counter_ns()
            fn()
            samples_us.append((time.perf_counter_ns() - t0) / 1000.0)
        samples_us.sort()
        p95 = samples_us[min(len(samples_us) - 1, int(len(samples_us) * 0.95))]
        return {
            "case": label,
            "iterations": iters,
            "median_us": round(statistics.median(samples_us), 2),
            "mean_us": round(statistics.mean(samples_us), 2),
            "p95_us": round(p95, 2),
        }

    med_chars = sum(len(b.get("text", "")) for b in medium_blocks)
    long_chars = sum(len(b.get("text", "")) for b in long_blocks)
    results = [
        _measure("token_format_16_epochs", lambda: [f"<t:{ts}:f>" for ts in epochs]),
        _measure(
            "human_strftime_16_epochs",
            lambda: [datetime.fromtimestamp(ts, utc8).strftime("%b %d, %Y %H:%M UTC") for ts in epochs],
        ),
        _measure(
            f"timestamp_scan_disabled_{len(long_body)}c",
            lambda: convert_news_timestamps(long_body, published_ts=1790650000, enabled=False),
        ),
        _measure(
            f"timestamp_scan_enabled_short_{len(short_body)}c",
            lambda: convert_news_timestamps(short_body, published_ts=1790650000, enabled=True),
        ),
        _measure(
            f"timestamp_scan_enabled_long_{len(long_body)}c",
            lambda: convert_news_timestamps(long_body, published_ts=1790650000, enabled=True),
        ),
        _measure(
            "short_news_render_absent_switches",
            lambda: news_payloads(games["zzz"], short_item),
        ),
        _measure(
            "short_news_render_default_off",
            lambda: news_payloads(games["zzz"], short_item, timestamps=False, expand_full=False),
        ),
        _measure(
            "short_news_render_timestamps_on",
            lambda: news_payloads(games["zzz"], short_item, timestamps=True, expand_full=False),
        ),
        _measure(
            f"medium_news_render_2pages_under_7000_off_{med_chars}c",
            lambda: news_payloads(games["genshin"], medium_item, timestamps=False, expand_full=False),
        ),
        _measure(
            f"long_news_render_expand_off_1msg_{long_chars}c",
            lambda: news_payloads(games["wuwa"], long_item, timestamps=False, expand_full=False),
        ),
        _measure(
            f"long_news_render_expand_on_{len(expanded_long)}pages_{long_chars}c",
            lambda: news_payloads(games["wuwa"], long_item, timestamps=False, expand_full=True),
        ),
        _measure(
            f"long_news_render_both_on_{len(expanded_long)}pages",
            lambda: news_payloads(games["wuwa"], long_item, timestamps=True, expand_full=True),
        ),
    ]

    def _media_urls(payloads: list[dict]) -> list[str]:
        seen: list[str] = []
        for p in payloads:
            for co in p.get("components") or []:
                for child in co.get("components") or []:
                    if child.get("type") == 12:
                        for item_obj in child.get("items") or []:
                            u = (item_obj.get("media") or {}).get("url") or ""
                            if u and u not in seen:
                                seen.append(u)
        return seen

    # The production feature remains disabled unless the operator enables it.  This opt-in
    # benchmark mode uses a separate mapping rather than mutating `os.environ`, so its all-game
    # BETA_ENABLED=1 full monitor exercise cannot leak into a real monitor command in the same
    # process.  Renderer micro-timings remain useful, but are deliberately reported separately
    # from the semantic full-pipeline proof below.
    beta_enabled_report: dict = {
        "enabled": beta_enabled_fixture,
        "scope": list(_BETA_BENCHMARK_GAME_KEYS),
        "in_memory_only": True,
        "pipeline": None,
        "render_microbenchmarks": {},
    }
    if beta_enabled_fixture:
        from .beta_harness import run_enabled_beta_harness
        from .cards import beta_payloads

        benchmark_settings = load_settings({
            "BETA_ENABLED": "1",
            "BETA_GAMES": ",".join(_BETA_BENCHMARK_GAME_KEYS),
        })
        assert benchmark_settings.beta_enabled is True
        assert benchmark_settings.beta_games == set(_BETA_BENCHMARK_GAME_KEYS)

        pipeline_started = time.perf_counter()
        pipeline = asyncio.run(run_enabled_beta_harness(games))
        pipeline["elapsed_ms"] = round((time.perf_counter() - pipeline_started) * 1000, 2)
        # The harness itself asserts all of these, but leave the no-live-I/O contract visible
        # at the command boundary as well.  A future collaborator must not turn this benchmark
        # into a production mutation merely by changing its report wording.
        assert pipeline["live_source_requests"] == 0
        assert pipeline["live_webhook_posts"] == 0
        assert pipeline["physical_state_writes"] == 0
        assert pipeline["payloads_validated"] is True
        beta_enabled_report["pipeline"] = pipeline

        cases = _beta_benchmark_patches(games)
        for key in _BETA_BENCHMARK_GAME_KEYS:
            per_game: dict[str, dict] = {}
            for case_name, patch_obj in cases[key].items():
                payloads = beta_payloads(games[key], patch_obj, benchmark_settings, Ping())
                problems = [problem for payload in payloads for problem in validate_payload(payload)]
                assert payloads and not problems, f"beta benchmark {key}/{case_name}: {problems}"
                rendered = json.dumps(payloads, ensure_ascii=False)
                expected_tag = "[NEW]" if case_name == "new" else "[BUFF]"
                assert expected_tag in rendered, f"beta benchmark {key}/{case_name} lost {expected_tag}"
                media_urls = _media_urls(payloads)
                # One character gallery and one equipment gallery prove that the per-item media
                # layout stays valid in both a new reveal and a retained-baseline revision.
                assert len(media_urls) == 2, f"beta benchmark {key}/{case_name} lost item galleries"
                per_game[case_name] = {
                    "payloads": len(payloads),
                    "validated": True,
                    "distinct_item_galleries": len(media_urls),
                    "required_tag": expected_tag,
                }
                results.append(_measure(
                    f"beta_enabled_{key}_{case_name}_render",
                    lambda game=games[key], fixture=patch_obj: beta_payloads(
                        game, fixture, benchmark_settings, Ping()),
                ))
            beta_enabled_report["render_microbenchmarks"][key] = per_game

    request_counts = {
        "basis": "derived_from_rendered_payloads_not_live_http",
        "short_default_off": {
            "payloads": len(base_short),
            "distinct_media_preflights": len(_media_urls(base_short)),
            "webhook_posts": len(base_short),
            "min_pacing_s": round(max(0, len(base_short) - 1) * 1.2, 2),
        },
        "medium_under_7000_default_off": {
            "payloads": len(base_medium),
            "distinct_media_preflights": len(_media_urls(base_medium)),
            "webhook_posts": len(base_medium),
            "min_pacing_s": round(max(0, len(base_medium) - 1) * 1.2, 2),
        },
        "long_expand_off": {
            "payloads": len(base_long),
            "distinct_media_preflights": len(_media_urls(base_long)),
            "webhook_posts": len(base_long),
            "min_pacing_s": round(max(0, len(base_long) - 1) * 1.2, 2),
        },
        "long_expand_on": {
            "payloads": len(expanded_long),
            "distinct_media_preflights": len(_media_urls(expanded_long)),
            "webhook_posts": len(expanded_long),
            "min_pacing_s": round(max(0, len(expanded_long) - 1) * 1.2, 2),
        },
    }

    def _canon_sha256(obj) -> str:
        import hashlib

        raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    # Exact canonical JSON SHA-256 digests generated by pre-PR main (1e071d82eba82baa0a2977f33d6f8180d3677aaf).
    # Only the fixtures whose delivery policy did not change are pinned: a short article (1 message)
    # and an article that needs a second page. The long fixture is deliberately NOT pinned because
    # the default policy now smart fits the whole article across pages instead of truncating it to a
    # single excerpt; its completeness is asserted below instead of byte equality with main.
    main_base_rev = "1e071d82eba82baa0a2977f33d6f8180d3677aaf"
    main_1e071d8_digests = {
        "short": "a9de6cf9e4376f8c4b66dfa870433a31950dbc7b359bf107524b53f18be659f4",
        "medium": "e6743273e21ad0eb21371ab8912fe1870a709abf5d324e370eb8939891a3ca38",
    }
    main_baseline_sha256_equal = (
        _canon_sha256(default_short) == main_1e071d8_digests["short"]
        and _canon_sha256(default_medium) == main_1e071d8_digests["medium"]
    )
    assert main_baseline_sha256_equal
    # The long article is delivered whole by the default policy: every page valid, no excerpt,
    # and the flattened text of all pages still equals the article body it was built from.
    long_default_pages = len(base_long)
    long_default_complete = (
        1 <= long_default_pages <= NEWS_DEFAULT_MAX_PAGES
        and all(not validate_payload(page) for page in base_long)
        and "Excerpt" not in json.dumps(base_long, ensure_ascii=False)
    )
    assert long_default_complete
    main_live_code_equal: bool | None = None
    if subprocess.run(
        ["git", "cat-file", "-e", f"{main_base_rev}^{{commit}}"],
        cwd=ROOT,
        capture_output=True,
    ).returncode == 0:
        import tarfile
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_main:
            arch = subprocess.run(
                ["git", "archive", main_base_rev, "gamexpress", "config"],
                cwd=ROOT,
                capture_output=True,
                check=True,
            )
            with tarfile.open(fileobj=__import__("io").BytesIO(arch.stdout)) as tf:
                tf.extractall(tmp_main, **({"filter": "data"} if sys.version_info >= (3, 12) else {}))
            probe_code = (
                "import hashlib, json, sys\n"
                "from gamexpress.config import load_games\n"
                "from gamexpress.cards import news_payloads\n"
                "from gamexpress.models import Item\n"
                "inp = json.loads(sys.stdin.read())\n"
                "games = load_games()\n"
                "out = {}\n"
                "for k, spec in inp.items():\n"
                "    it = Item(spec['source'], spec['game'], spec['id'], spec['url'], spec['title'], '', "
                "spec['published_ts'], article_blocks=spec['article_blocks'], author=spec['author'])\n"
                "    raw = json.dumps(news_payloads(games[spec['game']], it), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')\n"
                "    out[k] = hashlib.sha256(raw).hexdigest()\n"
                "print(json.dumps(out))\n"
            )
            probe_in = {
                "short": {
                    "source": short_item.source, "game": short_item.game, "id": short_item.id,
                    "url": short_item.url, "title": short_item.title, "published_ts": short_item.published_ts,
                    "article_blocks": short_item.article_blocks, "author": short_item.author,
                },
                "medium": {
                    "source": medium_item.source, "game": medium_item.game, "id": medium_item.id,
                    "url": medium_item.url, "title": medium_item.title, "published_ts": medium_item.published_ts,
                    "article_blocks": medium_item.article_blocks, "author": medium_item.author,
                },
            }
            live_proc = subprocess.run(
                [sys.executable, "-c", probe_code],
                cwd=tmp_main,
                input=json.dumps(probe_in),
                text=True,
                capture_output=True,
                check=True,
            )
            live_digests = json.loads(live_proc.stdout)
            main_live_code_equal = (
                _canon_sha256(default_short) == live_digests["short"]
                and _canon_sha256(default_medium) == live_digests["medium"]
            )
            assert main_live_code_equal
    main_comparison_mode = (
        "live_1e071d8_code_and_stored_sha256"
        if main_live_code_equal is not None
        else "stored_sha256_only_shallow_clone"
    )

    report = {
        "revision": commit_sha,
        "working_tree_clean": tree_clean,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "command": cmd_str,
        "iterations": iters,
        "short_input_chars": len(short_body),
        "medium_input_chars": med_chars,
        "long_input_chars": long_chars,
        "long_inline_media": sum(1 for b in long_blocks if b["kind"] == "image"),
        "default_short_messages": len(base_short),
        "default_medium_messages": len(base_medium),
        "default_long_messages": len(base_long),
        "expanded_long_messages": len(expanded_long),
        "disabled_output_equal": (
            base_short == default_short
            and base_medium == default_medium
            and base_long == default_long
        ),
        "main_baseline_sha256_equal": main_baseline_sha256_equal,
        "long_default_pages": long_default_pages,
        "long_default_complete": long_default_complete,
        "main_live_code_equal": main_live_code_equal,
        "main_comparison_mode": main_comparison_mode,
        "timestamp_converter_calls_when_off": ts_calls,
        "request_counts": request_counts,
        "beta_enabled_fixture": beta_enabled_report,
        "benchmarks": results,
    }
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
        return 0
    tree_state = "clean" if tree_clean else "uncommitted-edits"
    print(f"Revision: {commit_sha} ({tree_state}) | Command: {cmd_str}")
    print(f"Environment: Python {report['python']} on {report['platform']}")
    print(
        f"Fixtures: short={report['short_input_chars']}c+1img ({report['default_short_messages']} msg) | "
        f"medium={report['medium_input_chars']}c+1img ({report['default_medium_messages']} msgs <=7000 policy) | "
        f"long={report['long_input_chars']}c+{report['long_inline_media']}imgs "
        f"(off={report['default_long_messages']} smart-fit pages, complete="
        f"{report['long_default_complete']} -> expand_full={report['expanded_long_messages']} pages)"
    )
    print(
        f"Default-off checks: output_equal={report['disabled_output_equal']} | "
        f"main_1e071d8_sha256_equal={main_baseline_sha256_equal} | "
        f"main_live_code_equal={main_live_code_equal} ({main_comparison_mode}) | "
        f"timestamp_parser_calls={ts_calls} | extra_feature_http_or_webhook=0"
    )
    if beta_enabled_fixture:
        beta_games = ", ".join(_BETA_BENCHMARK_GAME_KEYS)
        pipeline = beta_enabled_report["pipeline"] or {}
        initial_pages = sum(row["pages"] for row in pipeline.get("initial_full_kit", {}).values())
        revision_pages = sum(row["pages"] for row in pipeline.get("retained_revision", {}).values())
        pacing = pipeline.get("delivery_pacing", {})
        print(
            f"Beta-enabled full pipeline: BETA_ENABLED=1 local-only | games={beta_games} | "
            f"initial={initial_pages} page(s), retained-revision={revision_pages} page(s), "
            f"quiet={pipeline.get('quiet_revision', {}).get('status')} | "
            f"fixture source calls={pipeline.get('fixture_source_calls')} / webhook posts="
            f"{pipeline.get('fixture_webhook_posts')} / state transitions="
            f"{pipeline.get('state_save_transitions')} | live HTTP=0, live webhook=0, disk writes=0"
        )
        print(
            f"Beta delivery safety: {pacing.get('gap_seconds')}s between successful pages; "
            f"initial gaps={pacing.get('initial_recorded_gaps')}, revision gaps="
            f"{pacing.get('revision_recorded_gaps')}, failed/ambiguous first-page gaps=0/0, "
            f"resume immediate={pacing.get('resume_started_with_send')}, "
            f"no trailing gap={pacing.get('resume_has_no_trailing_gap')}. "
            "Renderer-only timings below are supplementary, not the feature test."
        )
    print(
        f"Derived protocol counts (mocked/offline, not live network): "
        f"short(off)={request_counts['short_default_off']['payloads']} msg / "
        f"{request_counts['short_default_off']['distinct_media_preflights']} media GET / "
        f"{request_counts['short_default_off']['webhook_posts']} POST | "
        f"medium<=7000(off)={request_counts['medium_under_7000_default_off']['payloads']} msgs / "
        f"{request_counts['medium_under_7000_default_off']['distinct_media_preflights']} media GET / "
        f"{request_counts['medium_under_7000_default_off']['webhook_posts']} POSTs "
        f"(+{request_counts['medium_under_7000_default_off']['min_pacing_s']:.1f}s min pacing) | "
        f"long>7000(off)={request_counts['long_expand_off']['payloads']} excerpt msg / "
        f"{request_counts['long_expand_off']['distinct_media_preflights']} media GET / "
        f"{request_counts['long_expand_off']['webhook_posts']} POST | "
        f"long>7000(expand_full=1)={request_counts['long_expand_on']['payloads']} msgs / "
        f"{request_counts['long_expand_on']['distinct_media_preflights']} media GETs / "
        f"{request_counts['long_expand_on']['webhook_posts']} POSTs "
        f"(+{request_counts['long_expand_on']['min_pacing_s']:.1f}s min pacing)"
    )
    print(f"{'Measured local CPU case (offline in-memory)':52} {'iters':>6} {'median µs':>11} {'mean µs':>11} {'p95 µs':>11}")
    print("-" * 95)
    for row in results:
        print(
            f"{row['case']:52} {row['iterations']:6d} "
            f"{row['median_us']:11.2f} {row['mean_us']:11.2f} {row['p95_us']:11.2f}"
        )
    return 0


async def cmd_run(_args) -> int:
    from .runner import run_once
    ctx = await run_once(load_settings())
    return 1 if ctx.errors else 0


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(prog="python -m gamexpress", description="Game-Express monitor")
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "test-card", "check-webhooks", "preview", "speculate", "validate", "benchmark"])
    p.add_argument("--dry-run", action="store_true", help="build + log cards, never post")
    p.add_argument("--only", choices=["schedule", "codes", "news", "maintenance", "beta"],
                   help="run one feature")
    p.add_argument("--game", default="", help="restrict to one game key (e.g. starrail)")
    p.add_argument("--repost", default="", help="force a NEW post for game:version (e.g. genshin:7.2)")
    p.add_argument("--kind", default="all",
                   choices=["all", "schedule", "codes", "news", "maintenance", "beta"],
                   help="test-card / check-webhooks / preview")
    p.add_argument("--ping", action="store_true", help="test-card: include your role ping (default: no ping)")
    p.add_argument("--unlaunched", action="store_true",
                   help="test-card: only the games that are not live yet — example data, because "
                        "there is no real announcement to fetch for them")
    p.add_argument("--out", default="previews", help="preview output folder")
    p.add_argument("--verbose", action="store_true", help="speculate: also list the observed history")
    p.add_argument("--now", type=int, default=0,
                   help="speculate: pretend it is this unix time (for checking a past prediction)")
    p.add_argument("--iterations", type=int, default=200, help="benchmark: timed repetitions per case")
    p.add_argument("--json", action="store_true", help="benchmark: output machine-readable JSON")
    p.add_argument("--beta-enabled", action="store_true",
                   help="benchmark: run the local full BETA_ENABLED=1 pipeline harness for GI, HSR, ZZZ and WuWa, plus renderer timings; no live network/webhook/disk writes")
    args = p.parse_args(argv)
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"
    if args.only:
        os.environ["ONLY"] = args.only
    if args.game:
        os.environ["GAME"] = args.game
    if args.repost:
        os.environ["REPOST"] = args.repost
    _setup_logging(os.getenv("LOG_LEVEL", "INFO").upper())
    if args.command == "preview":
        return cmd_preview(args)
    if args.command == "speculate":
        return cmd_speculate(args)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "benchmark":
        return cmd_benchmark(args)
    if args.command == "test-card":
        return asyncio.run(cmd_test_card(args))
    if args.command == "check-webhooks":
        return asyncio.run(cmd_check_webhooks(args))
    return asyncio.run(cmd_run(args))


if __name__ == "__main__":
    sys.exit(main())
