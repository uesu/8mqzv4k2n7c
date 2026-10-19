"""Offline smoke + golden tests — no network, no secrets.

Run:   python tests/test_smoke.py        (plain runner, what CI uses)
   or: python -m pytest -q tests          (pytest also works)
Regenerate golden cards after an INTENTIONAL card change:
       UPDATE_GOLDEN=1 python tests/test_smoke.py

The parsers are fed REAL official posts captured on 2026-09-25 and must reproduce
the exact timestamps of the user's four reference cards.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import time
import traceback
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"
GOLDEN = FIX / "golden"

from gamexpress import __main__ as gxmain  # noqa: E402
from gamexpress import cards, codeposter, media, schedule  # noqa: E402
from gamexpress import config as gconfig  # noqa: E402
from gamexpress.config import load_games, load_overrides, load_settings, parse_emoji, parse_ping  # noqa: E402
from gamexpress.discord import SendResult, WebhookClient, _split, webhook_fingerprint  # noqa: E402
from gamexpress.models import CodeHit, Item  # noqa: E402
from gamexpress.runner import Ctx, failover_check  # noqa: E402
from gamexpress.samples import CODE_SAMPLES, SCHEDULE_SAMPLES  # noqa: E402
from gamexpress.sources import (  # noqa: E402
    banner_search,  # noqa: E402
    calendarfeed,
    countdown,
    gachawiki,
    hoyolab,
    newspage,
)
from gamexpress.sources import codes as csrc  # noqa: E402
from gamexpress.sources.hoyolab import _post_text  # noqa: E402
from gamexpress.sources.kuro import parse_launcher_index  # noqa: E402
from gamexpress.sources.launcher import parse_branches  # noqa: E402
from gamexpress.sources.twitter import item_from_fx_json, nitter_pic_to_twimg  # noqa: E402
from gamexpress.state import State  # noqa: E402
from gamexpress.timeparse import discord_ts, find_datetimes, find_duration_hours, find_time_ranges  # noqa: E402

GAMES = load_games()
HOOK = "https://discord.com/api/webhooks/123456789012345678/tok_en-ABC"
BASE_ENV = {"PING_ROLE_ID": "1296268365593186426", "DISCORD_WEBHOOK_URL": HOOK}


def fx(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def settings(**extra):
    env = dict(BASE_ENV)
    env.update({k: str(v) for k, v in extra.items()})
    return load_settings(env)


def hoyolab_item(fixture: str, game: str) -> Item:
    post = fx(fixture)["data"]["post"]
    text, links, imgs = _post_text(post["post"])
    pid = post["post"]["post_id"]
    return Item("hoyolab", game, pid, f"https://www.hoyolab.com/article/{pid}", post["post"]["subject"], text,
                int(post["post"]["created_at"]), imgs, links)


# =========================================================================== time parsing
def test_time_formats_reproduce_reference_cards():
    assert [f.ts for f in find_datetimes("on 09/12/2026 at 08:00 AM (UTC-4)!", 1788753605)] == [1789214400]  # GI 7.1
    assert [f.ts for f in find_datetimes("on September 19, 2026, at 19:00 (UTC+8).", 1789182000)] == [1789815600]  # WW 3.7
    zzz = find_datetimes("will begin on December 19 at 19:30 (UTC+8).", 1765772100)
    assert zzz[0].ts == 1766143800 and zzz[0].year_inferred
    assert find_datetimes("2026/11/09 03:59 (server time)", 1790307006) == []   # server time is never trusted
    assert find_duration_hours("The update will take approximately 5 hours.") == 5.0
    assert find_duration_hours("is estimated to take 5 hours") == 5.0


def test_time_ranges():
    assert find_time_ranges("Maintenance: 2026/09/30 04:00 - 11:00 (UTC+8)", 1790000000) == [(1790712000, 1790737200)]
    assert find_time_ranges("September 30, 2026, 04:00 – 11:00 (UTC+8)", 1790000000) == [(1790712000, 1790737200)]


# =========================================================================== extraction (real posts)
def test_hsr_maintenance_notice_matches_reference_card():
    item = hoyolab_item("hoyolab_starrail_46814308_full.json", "starrail")
    assert "pre-installation will begin" in item.text           # content=='en-us' quirk handled
    e = schedule.extract(GAMES["starrail"], item)
    assert e and e.kind == "maintenance" and e.version == "4.6"
    assert e.fields["preinstall_ts"] == 1790229600
    assert e.fields["maint_start_ts"] == 1790546400
    assert e.fields["maint_end_ts"] == 1790564400
    assert e.fields["compensation"] == "Stellar Jade ×300"


def test_gi_program_article_matches_reference_card():
    item = hoyolab_item("hoyolab_genshin_46604275_full.json", "genshin")
    e = schedule.extract(GAMES["genshin"], item)
    assert e and e.kind == "program" and e.version == "7.1"
    assert e.fields["program_ts"] == 1789214400
    assert e.fields["program_name"] == "Special Program"


def test_gi_update_details_matches_reference_card():
    w = fx("hoyolab_genshin_list_update_details.json")["data"]["list"][0]["post"]
    item = Item("hoyolab", "genshin", w["post_id"], "https://www.hoyolab.com/article/46791577", w["subject"],
                w["content"], w["created_at"])
    e = schedule.extract(GAMES["genshin"], item)
    assert e and e.kind == "maintenance" and e.version == "7.1"
    assert e.fields["maint_start_ts"] == 1790114400 and e.fields["maint_end_ts"] == 1790132400
    assert e.fields["compensation"] == "Primogems ×300"
    assert "preinstall_ts" not in e.fields                   # compensation deadlines are never mistaken


def test_ww_broadcast_tweet():
    item = item_from_fx_json("wuwa", fx("fx_wuwa_3_7_broadcast.json"))
    e = schedule.extract(GAMES["wuwa"], item)
    assert e and e.kind == "program" and e.version == "3.7"
    assert e.fields["program_ts"] == 1789815600
    assert e.fields["program_name"] == "Special Broadcast"
    assert e.fields["youtube_video"] == "https://www.youtube.com/watch?v=nMa_e5ChL6w"
    assert "HR70hTAaoAA8Dzz" in e.fields["images"][0]         # the same image as the reference card


def test_hsr_and_zzz_tweets():
    e = schedule.extract(GAMES["starrail"], item_from_fx_json("starrail", fx("fx_starrail_4_5_program.json")))
    assert e.version == "4.5" and e.fields["program_ts"] == 1786707000
    assert e.fields["version_name"] == "To Roll the Stars in Astropolis"
    z = schedule.extract(GAMES["zzz"], item_from_fx_json("zzz", fx("fx_zzz_2_5_program.json")))
    assert z.version == "2.5" and z.fields["program_ts"] == 1766143800


def test_genshin_tweet_without_number_gets_version_from_launcher():
    e = schedule.extract(GAMES["genshin"], item_from_fx_json("genshin", fx("fx_genshin_luna2_program.json")))
    assert e.kind == "program" and e.version is None and e.fields["program_ts"] == 1760097600
    schedule.infer_versions([e], live="6.0", records={})
    assert e.version == "6.1" and e.version_inferred        # "Luna II" == 6.1


def test_non_announcements_are_ignored():
    codes_post = Item("x", "genshin", "1", "u", "", "The special program has ended! Redemption codes: ABCD1234EF", 1)
    assert schedule.classify(GAMES["genshin"], codes_post) is None or \
        schedule.extract(GAMES["genshin"], codes_post) is None
    merch = Item("x", "genshin", "2", "u", "", "New plushies are available in the official store!", 1)
    assert schedule.extract(GAMES["genshin"], merch) is None


def test_preview_article_event_dates_are_not_program_times():
    text = ("Greetings, Trailblazer! As revealed in the Special Program, here are the Version 4.6 events.\n"
            "Event period: 2026/10/01 10:00 (UTC+8) – 2026/10/20 03:59 (UTC+8).")
    item = Item("hoyolab", "starrail", "1", "u", "\"Dance With the Beast\" Version 4.6 Preview", text, 1790200000)
    assert schedule.extract(GAMES["starrail"], item) is None


def test_banner_extraction_conservative():
    text = ('Event Wish "Ballad" Phase II: the event-exclusive 5-star character "Vodyanitsa (Hydro)" and the '
            '4-star characters "Bennett (Pyro)", "Xingqiu (Hydro)" and "Sucrose (Anemo)" will get a huge drop-rate boost! '
            'The 5-star weapon "Some Sword" is not a character.')
    b = schedule.extract_banner(Item("hoyolab", "genshin", "1", "u", "Event Wish", text, 1))
    assert b["banner_five"] == ["Vodyanitsa"] and b["banner_phase"] == 2
    assert b["banner_four"] == ["Bennett", "Xingqiu", "Sucrose"]


# =========================================================================== cards
def _render(key):
    s = settings()
    return cards.schedule_payload(GAMES[key], SCHEDULE_SAMPLES[key], s, s.ping("schedule", key))


def test_golden_reference_cards():
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for key in SCHEDULE_SAMPLES:
        payload = _render(key)
        path = GOLDEN / f"schedule_{key}.json"
        if os.getenv("UPDATE_GOLDEN") or not path.exists():
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        assert json.loads(path.read_text(encoding="utf-8")) == payload, f"golden mismatch: {path.name}"


def test_a_card_has_nothing_above_it_and_pings_from_inside():
    """The mention used to sit on a bare line above the card. It now lives INSIDE the
    container — on the legend line of a schedule card, on the '… detected …' line of a codes
    card — so the post reads as one block. Nothing may float above the container."""
    s = settings()
    sched = _render("starrail")
    assert [c["type"] for c in sched["components"]] == [17]          # the container, alone
    assert _legend(sched) == ("-# STC — Subject to Change • TBA — To be Announced "
                              "<@&1296268365593186426>")
    assert sched["allowed_mentions"] == {"parse": [], "roles": ["1296268365593186426"]}

    # A codes card says "code" for one and "codes" for several, in both the heading and the
    # count line, and mentions the role exactly once — on the count line.
    g, ping = GAMES["genshin"], s.ping("codes", "genshin")
    for n, word in ((1, "Code"), (2, "Codes"), (3, "Codes")):
        codes = [{"code": f"GENSHIN{i:02d}", "rewards": [{"name": "Primogem", "qty": 60}],
                  "sources": ["ennead"]} for i in range(n)]
        p = cards.codes_payloads(g, codes, s, ping, 1791149429)[0]
        assert [c["type"] for c in p["components"]] == [17]
        head, sub = (c["content"] for c in p["components"][0]["components"][0]["components"][:2])
        assert head == f"## 🎁 Genshin Impact Redemption {word}"
        assert sub == (f"-# {n} new code{'' if n == 1 else 's'} • detected "
                       f"<t:1791149429:R> <@&1296268365593186426>")
        assert json.dumps(p, ensure_ascii=False).count("<@&1296268365593186426>") == 1

    # A split drop notifies once: the follow-up parts carry neither mention nor permission.
    many = [{"code": f"GI{i:02d}", "sources": ["ennead"]} for i in range(13)]
    parts = cards.codes_payloads(g, many, s, ping, 1791149429)
    assert len(parts) == 2
    assert parts[1]["allowed_mentions"] == {"parse": []}
    assert "<@&" not in json.dumps(parts[1], ensure_ascii=False)


def test_the_ping_is_never_silently_dropped_when_the_legend_is_off():
    """SHOW_LEGEND=0 removes the line the mention normally rides on. It must then get a line
    of its own rather than vanish — a configured ping that notifies nobody is a silent bug."""
    s = settings(SHOW_LEGEND="0")
    p = cards.schedule_payload(GAMES["zzz"], SCHEDULE_SAMPLES["zzz"], s, s.ping("schedule", "zzz"))
    foot = [c["content"] for c in p["components"][0]["components"]
            if str(c.get("content", "")).startswith("-# ")]
    assert foot == ["-# <@&1296268365593186426>"]
    assert p["allowed_mentions"] == {"parse": [], "roles": ["1296268365593186426"]}


def test_reference_card_text_is_exact():
    p = _render("starrail")
    (box,) = p["components"]                  # one container, nothing floating above it
    assert _legend(p).endswith("To be Announced <@&1296268365593186426>")
    body = "\n".join(c["content"] for c in box["components"] if c["type"] == 10)
    for line in ("## [Honkai: Star Rail Version 4.6 Special Program](https://x.com/honkaistarrail/status/2099440781115211916) <:ananta1:1558340472525693018>",
                 "<t:1789903800:F> or <t:1789903800:R>", "**Version 4.6 Banners (STC)**",
                 "✦ First Half/Phase: Pearl", "- 4 Star Characters: TBA", "**Maintenance Details (STC)**",
                 "✦ Pre-Install: <t:1790229600:F>", "✦ Start: <t:1790546400:F>", "✦ End: <t:1790564400:F>"):
        assert line in body, line
    ww = "\n".join(c["content"] for c in _render("wuwa")["components"][0]["components"] if c["type"] == 10)
    assert 'Wuthering Waves Version 3.7 "Special Broadcast"' in ww
    assert "✦ Maintenance: <t:1790712000:f> to <t:1790737200:t>" in ww
    assert "※ 4 Star Characters: Buling, Taoqi, Youhu, Lumi, Danjin, Yangyang" in ww
    assert ww.index("Maintenance Time & Compensation Details") < ww.index("Banners (STC)")
    gi = "\n".join(c["content"] for c in _render("genshin")["components"][0]["components"] if c["type"] == 10)
    assert "**[Version 7.1 Banners (STC)](https://lunaris.moe/banners)**" in gi
    assert "※ Re-runs: Skirk, Escoffier" in gi


def test_title_emoji_is_configurable():
    # Default: the ananta scroll ends every game's card headline.
    for key in SCHEDULE_SAMPLES:
        head = _render(key)["components"][0]["components"][0]["content"].split("\n")[0]
        assert head.endswith("<:ananta1:1558340472525693018>"), key
    # EMOJI_TITLE swaps it: a unicode emoji, an animated custom one, or none at all.
    for value, tail in (("🎁", " 🎁"), ("a:starward11:1439878792653832253",
                                        " <a:starward11:1439878792653832253>")):
        s = settings(EMOJI_TITLE=value)
        p = cards.schedule_payload(GAMES["starrail"], SCHEDULE_SAMPLES["starrail"], s,
                                   s.ping("schedule", "starrail"))
        head = p["components"][0]["components"][0]["content"].split("\n")[0]
        assert head.endswith(tail), value
    s = settings(EMOJI_TITLE="none")
    p = cards.schedule_payload(GAMES["starrail"], SCHEDULE_SAMPLES["starrail"], s,
                               s.ping("schedule", "starrail"))
    head = p["components"][0]["components"][0]["content"].split("\n")[0]
    assert "📜" not in head and "<:" not in head and not head.endswith(" ")


def test_buttons_are_inside_the_card():
    for key in SCHEDULE_SAMPLES:
        p = _render(key)
        assert p["flags"] == cards.IS_COMPONENTS_V2 and "content" not in p and "embeds" not in p
        assert all(c["type"] in (10, 17) for c in p["components"])          # no top-level buttons
        box = p["components"][-1]
        rows = [c for c in box["components"] if c["type"] == 1]
        assert rows, "action row must be nested in the container"
        labels = [b["label"] for b in rows[0]["components"]]
        assert labels[:2] == ["Youtube", "Twitch"]
        assert rows[0]["components"][0]["emoji"]["name"] == "kurobbseventcalendar"
        assert not cards.validate_payload(p)


def test_codes_card():
    s = settings()
    for key, codes in CODE_SAMPLES.items():
        for p in cards.codes_payloads(GAMES[key], codes, s, s.ping("codes", key), 1790000000):
            assert not cards.validate_payload(p), cards.validate_payload(p)
    p = cards.codes_payloads(GAMES["genshin"], CODE_SAMPLES["genshin"], s, s.ping("codes", "genshin"), 1)[0]
    flat = json.dumps(p, ensure_ascii=False)
    assert "https://genshin.hoyoverse.com/en/gift?code=VESNAONPATROL" in flat
    assert "Primogem ×40 • Mora ×20000 • Hero's Wit ×3" in flat
    many = [{"code": f"CODE{i:04d}X", "sources": ["seria"]} for i in range(23)]
    parts = cards.codes_payloads(GAMES["zzz"], many, s, s.ping("codes", "zzz"), 1)
    assert len(parts) == 3 and all(not cards.validate_payload(x) for x in parts)
    assert parts[1]["allowed_mentions"] == {"parse": []}                     # only the first part pings


def test_validate_payload_catches_limits():
    too_many = {"flags": cards.IS_COMPONENTS_V2, "components": [cards.text("x")] * 41}
    assert any("components" in p for p in cards.validate_payload(too_many))
    too_long = {"flags": cards.IS_COMPONENTS_V2, "components": [cards.text("x" * 4001)]}
    assert any("text chars" in p for p in cards.validate_payload(too_long))
    loose = {"flags": cards.IS_COMPONENTS_V2, "components": [cards.link_button("a", "https://x.y")]}
    assert any("outside" in p for p in cards.validate_payload(loose))
    assert cards.validate_payload({"components": [cards.text("x")]})       # missing flag


# =========================================================================== config / routing
def test_ping_resolution():
    s = settings()
    assert s.ping("schedule", "genshin").text == "<@&1296268365593186426>"
    s = settings(PING_ROLE_ID="")
    assert not s.ping("schedule", "genshin") and s.ping("codes", "zzz").allowed_mentions == {"parse": []}
    s = settings(PING_CODES="none", PING_SCHEDULE_WUWA="111111111111111111,222222222222222222")
    assert not s.ping("codes", "genshin")
    assert s.ping("schedule", "wuwa").roles == ["111111111111111111", "222222222222222222"]
    assert s.ping("schedule", "genshin").roles == ["1296268365593186426"]
    assert parse_ping("everyone").allowed_mentions == {"parse": ["everyone"]}
    assert parse_ping("<@&1296268365593186426>").roles == ["1296268365593186426"]


def test_emoji_and_webhook_routing():
    assert parse_emoji("a:kurobbseventcalendar:1508619925940473966") == {
        "id": "1508619925940473966", "name": "kurobbseventcalendar", "animated": True}
    assert parse_emoji("<:x:123456789012345678>") == {"id": "123456789012345678", "name": "x"}
    assert parse_emoji("none") is None and parse_emoji("🎁") == {"name": "🎁"}
    s = settings(DISCORD_WEBHOOK_SCHEDULE="https://discord.com/api/webhooks/1/sched",
                 DISCORD_WEBHOOK_CODES_WUWA="https://discord.com/api/webhooks/2/wwcodes")
    assert s.webhook("schedule", "zzz").endswith("/sched")
    assert s.webhook("codes", "wuwa").endswith("/wwcodes")
    assert s.webhook("codes", "genshin") == HOOK
    base, keep = _split(HOOK + "?thread_id=999&wait=false")
    assert keep == {"thread_id": "999"} and "?" not in base
    assert webhook_fingerprint(HOOK) == webhook_fingerprint(HOOK + "?thread_id=1") and len(webhook_fingerprint(HOOK)) == 12


def test_games_config_is_valid():
    assert set(GAMES) >= {"genshin", "starrail", "zzz", "wuwa", "hna", "ananta"}
    for g in GAMES.values():
        if g.enabled:
            # YouTube is where every one of these games streams its program, so an enabled game
            # must have it. Twitch is NOT universal: HoYoverse has announced no Twitch channel for
            # Nexus Anima and NetEase lists none for ANANTA, and cards.py only adds the button
            # when the field is filled, so an empty twitch is a fact about the game, not a gap.
            assert g.youtube and g.x_accounts and g.codes.get("sources"), g.key
            assert g.youtube.startswith("https://www.youtube.com/@"), g.key
            assert not g.twitch or g.twitch.startswith("https://www.twitch.tv/"), g.key
        cards.program_title(g, {"version": "1.0"})
    # both pre-release games are switched ON (2026-10-02) so nothing is missed before launch
    assert GAMES["hna"].enabled and GAMES["hna"].hoyolab_gid == 9
    assert GAMES["ananta"].enabled and not GAMES["ananta"].card.show_banners
    assert GAMES["ananta"].auto_enable_on == "2027-01-15"        # kept as a safety net
    # ...but neither is RELEASED, which is a separate fact: it is what the test bench uses to
    # decide a game has no real codes to fetch yet (--unlaunched), not whether it is monitored.
    assert not GAMES["hna"].released and not GAMES["ananta"].released
    assert all(GAMES[k].released for k in ("genshin", "starrail", "zzz", "wuwa"))
    assert isinstance(load_overrides(), dict)


# =========================================================================== sources
def test_code_parsers():
    assert [h.code for h in csrc.parse_seria(fx("seria_nap.json"))] == ["ZENLESSGIFT", "ZZZINK32", "ZZZVOID32", "ZZZGRIND32"]
    en = csrc.parse_ennead(fx("ennead_starrail_trimmed.json"))
    active = [h for h in en if not h.expired]
    assert [h.code for h in active] == ["STARRAILGIFT", "4TKSX77Y58QK", "CREATIONNYMPH"] and active[1].rewards is None
    assert [h.code for h in en if h.expired] == ["0206GRANDOPEN"]              # inactive list = expired flag
    assert csrc.parse_hoyolab_material(fx("hoyolab_material_empty.json")) == []
    live = csrc.parse_hoyolab_material(fx("hoyolab_material_live_SYNTHETIC.json"))
    assert [h.code for h in live] == ["VESNAONPATROL"] and live[0].verified
    assert [h.code for h in csrc.parse_fandom(fx("fandom_hsr_SYNTHETIC.json")) if not h.expired] == ["STARRAILGIFT"]
    assert [h.code for h in csrc.parse_fandom(fx("fandom_ww_table_SYNTHETIC.json")) if not h.expired] == [
        "WAKINGMOON", "FINDSENTINEL"]
    assert [h.code for h in csrc.parse_codehub(fx("codehub_trimmed.json"), "wuthering-waves")] == ["WAKINGMOON", "FINDSENTINEL"]
    assert csrc.sanitize("vesnaonpatrol[1] NEW!") == "VESNAONPATROL" and csrc.sanitize("123456") == ""


def test_code_extraction_from_official_posts():
    ww = ("Wuthering Waves Version 3.6 Special Broadcast Redemption Codes:\nHEARTOFSWORD\nETERNALFLAME\n"
          "THEANSWER\nRedeem before the version ends! #WutheringWaves")
    assert csrc.extract_codes_from_text(ww) == ["HEARTOFSWORD", "ETERNALFLAME", "THEANSWER"]
    for name in ("fx_genshin_luna2_program.json", "fx_zzz_2_5_program.json", "fx_wuwa_3_7_broadcast.json"):
        assert csrc.extract_codes_from_text(fx(name)["tweet"]["text"]) == [], name   # "drop some codes" != codes
    assert csrc.extract_codes_from_text("IMPORTANT UPDATE: code: VERSION SPECIAL") == []


def test_launcher_parsers():
    b = parse_branches(fx("hoyoplay_branches_trimmed.json"))
    assert b["U5hbdsT9W7"] == {"live": "3.2", "pre": None}
    assert b["4ziysqXOQ8"] == {"live": "4.5", "pre": "4.6"}
    assert parse_launcher_index(fx("kuro_launcher_index_trimmed.json")) == {"live": "3.6", "pre": "3.7"}
    assert nitter_pic_to_twimg("https://nitter.cf/pic/media%2FHR70hTAaoAA8Dzz.jpg") == \
        "https://pbs.twimg.com/media/HR70hTAaoAA8Dzz.jpg"


# =========================================================================== pipelines (offline)
class FakeCodeSources:
    def __init__(self, table):
        self.table = table

    async def fetch(self, spec):
        return self.table.get(spec, [])


def make_ctx(state_path, items=None, now=1789300000, codes_table=None, versions=None, overrides=None, **env):
    s = settings(**env)
    s.state_path = Path(state_path)
    return Ctx(settings=s, games=[g for g in GAMES.values() if g.enabled], state=State.load(Path(state_path)),
               fetcher=None, webhook=WebhookClient(None, dry_run=True), x=None,
               code_sources=FakeCodeSources(codes_table or {}), overrides=overrides or {}, now=now,
               versions=versions or {}, items=items or {})


def test_schedule_post_once_then_edit_silently():
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        ctx = make_ctx(sp)                                     # 1) first run = silent seed
        asyncio.run(schedule.run(ctx))
        assert ctx.webhook.sent == [] and ctx.state.is_bootstrapped("schedule", "wuwa")
        ctx.state.save()
        ww = item_from_fx_json("wuwa", fx("fx_wuwa_3_7_broadcast.json"))
        ctx = make_ctx(sp, items={"wuwa": [ww]}, now=1789300000)   # 2) broadcast announced -> ONE post
        asyncio.run(schedule.run(ctx))
        posts = [x for x in ctx.webhook.sent if x["method"] == "POST"]
        assert len(posts) == 1
        assert _legend(posts[0]["payload"]).endswith("To be Announced <@&1296268365593186426>")
        assert posts[0]["payload"]["allowed_mentions"] == {"parse": [], "roles": ["1296268365593186426"]}
        ctx.state.save()
        ctx = make_ctx(sp, items={"wuwa": [ww]}, now=1789303600)   # 3) same item again -> nothing
        asyncio.run(schedule.run(ctx))
        assert ctx.webhook.sent == []
        ctx.state.save()
        notice = Item("kuro", "wuwa", "9001", "https://wutheringwaves.kurogames.com/en/main/news/detail/9001",
                      "Version 3.7 Update Maintenance Notice",
                      "Version 3.7 pre-download will begin at 2026/09/28 10:00 (UTC+8).\n"
                      "Maintenance Time: 2026/09/30 04:00 - 11:00 (UTC+8)\nCompensation: Astrite ×300",
                      1790000000)
        ctx = make_ctx(sp, items={"wuwa": [ww, notice]}, now=1790001000)   # 4) notice -> EDIT, no ping
        asyncio.run(schedule.run(ctx))
        methods = [x["method"] for x in ctx.webhook.sent]
        assert methods == ["PATCH"], methods
        edit = ctx.webhook.sent[0]["payload"]
        assert edit["allowed_mentions"] == {"parse": []}
        flat = json.dumps(edit, ensure_ascii=False)
        assert "<t:1790560800:F>" in flat and "<t:1790712000:f> to <t:1790737200:t>" in flat
        assert "Astrite ×300" in flat


def test_overrides_edit_card():
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        ww = item_from_fx_json("wuwa", fx("fx_wuwa_3_7_broadcast.json"))
        ctx = make_ctx(sp, items={"wuwa": [ww]}, BOOTSTRAP_POST="1")
        asyncio.run(schedule.run(ctx))
        assert [x["method"] for x in ctx.webhook.sent] == ["POST"]
        ctx.state.save()
        ov = {"wuwa": {"3.7": {"banners": {"phase1": ["Hsin"], "phase2": ["Suoming"]}}}}
        ctx = make_ctx(sp, items={"wuwa": [ww]}, overrides=ov, now=1789300600)
        asyncio.run(schedule.run(ctx))
        assert [x["method"] for x in ctx.webhook.sent] == ["PATCH"]
        assert "✦ First Half/Phase: Hsin" in json.dumps(ctx.webhook.sent[0]["payload"], ensure_ascii=False)


def test_stale_announcements_are_not_posted():
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        ww = item_from_fx_json("wuwa", fx("fx_wuwa_3_7_broadcast.json"))
        ctx = make_ctx(sp, items={"wuwa": [ww]}, now=1789815600 + 3 * 86400, BOOTSTRAP_POST="1")
        asyncio.run(schedule.run(ctx))
        assert ctx.webhook.sent == []                        # program aired 3 days ago -> no late post


def test_codes_gate_pending_and_no_duplicates():
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        table = {"seria:nap": csrc.parse_seria(fx("seria_nap.json"))}
        ctx = make_ctx(sp, codes_table=table, GAME="zzz")
        ctx.games = [GAMES["zzz"]]
        asyncio.run(codeposter.run(ctx))                     # first run: seed silently
        assert ctx.webhook.sent == [] and len(ctx.state.code_records("zzz")) == 4
        ctx.state.save()
        table["seria:nap"].append(CodeHit("ZZZNEW33", "seria", "Polychrome*30", verified=True))
        table["ennead:zenless"] = [CodeHit("ONLYONESRC", "ennead", None)]
        ctx = make_ctx(sp, codes_table=table)
        ctx.games = [GAMES["zzz"]]
        asyncio.run(codeposter.run(ctx))
        assert len(ctx.webhook.sent) == 1
        flat = json.dumps(ctx.webhook.sent[0]["payload"])
        assert "ZZZNEW33" in flat and "ONLYONESRC" not in flat
        assert ctx.state.code_records("zzz")["ONLYONESRC"]["status"] == "pending"
        ctx.state.save()
        table["fandom:zenless-zone-zero/Redemption_Code"] = [CodeHit("ONLYONESRC", "fandom", None)]
        ctx = make_ctx(sp, codes_table=table)
        ctx.games = [GAMES["zzz"]]
        asyncio.run(codeposter.run(ctx))                     # second independent source -> posted
        assert len(ctx.webhook.sent) == 1 and "ONLYONESRC" in json.dumps(ctx.webhook.sent[0]["payload"])
        assert "ZZZNEW33" not in json.dumps(ctx.webhook.sent[0]["payload"])   # never twice


def test_identical_rerun_changes_nothing():
    """The state file (committed by the workflow) must not change when nothing new happened."""
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        table = {"seria:nap": csrc.parse_seria(fx("seria_nap.json"))}
        ww = item_from_fx_json("wuwa", fx("fx_wuwa_3_7_broadcast.json"))
        for i, now in enumerate((1789300000, 1789300600, 1789301200)):
            ctx = make_ctx(sp, items={"wuwa": [ww]}, codes_table=table, now=now, BOOTSTRAP_POST="1")
            asyncio.run(schedule.run(ctx))
            asyncio.run(codeposter.run(ctx))
            changed = ctx.state.save()
            if i == 0:
                assert changed
            else:
                assert not changed, f"run {i} rewrote the state file"


def test_official_codes_from_items():
    ww_codes = Item("x", "wuwa", "5", "https://x.com/Wuthering_Waves/status/5", "",
                    "Version 3.7 Special Broadcast Redemption Codes: WAKINGMOON, FINDSENTINEL, FALLINGSANCTUM", 1)
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        st = State.load(sp)
        st.mark_bootstrapped("codes", "wuwa")
        st.save()
        ctx = make_ctx(sp, items={"wuwa": [ww_codes]})
        ctx.games = [GAMES["wuwa"]]
        asyncio.run(codeposter.run(ctx))
        flat = json.dumps(ctx.webhook.sent[0]["payload"])
        assert all(c in flat for c in ("WAKINGMOON", "FINDSENTINEL", "FALLINGSANCTUM"))
        assert "redeem" not in flat.lower().split("gift?code")[0][:0]      # WW: no web redeem links
        assert "gift?code" not in flat


def test_failover():
    async def run(peer, role="standby", minutes=90, now=1790000000):
        with tempfile.TemporaryDirectory() as tmp:
            class F:
                async def get_json(self, *a, **k):
                    return peer
            ctx = make_ctx(Path(tmp) / "s.json", now=now, INSTANCE_ROLE=role, PEER_STATE_URL="https://raw/x.json",
                           FAILOVER_AFTER_MINUTES=minutes)
            ctx.fetcher = F()
            await failover_check(ctx)
            return ctx
    fresh = {"heartbeat": {"last_success": 1790000000 - 600}, "schedule": {"wuwa": {"3.7": {"status": "posted", "message_id": "9"}}}}
    ctx = asyncio.run(run(fresh))
    assert ctx.active is False and ctx.state.schedule_record("wuwa", "3.7")["message_id"] == "9"
    stale = {"heartbeat": {"last_success": 1790000000 - 3 * 3600}}
    assert asyncio.run(run(stale)).active is True
    assert asyncio.run(run(None)).active is False          # unreachable peer never triggers posting
    daily = {"heartbeat": {"last_success": 1790000000 - 5 * 3600, "every_min": 1440}}
    assert asyncio.run(run(daily)).active is False         # 5 h old but primary beats daily -> no false fail-over
    assert asyncio.run(run(fresh, role="primary")).active is True


def test_secrets_and_vars_json_blobs():
    s = load_settings({"GE_SECRETS_JSON": json.dumps({"DISCORD_WEBHOOK_CODES_WUWA": "https://discord.com/api/webhooks/7/w",
                                                      "github_token": "x"}),
                       "GE_VARS_JSON": json.dumps({"PING_ROLE_ID": "1296268365593186426", "DRY_RUN": "1"}),
                       "DRY_RUN": ""})
    assert s.webhook("codes", "wuwa").endswith("/7/w") and s.ping("schedule", "zzz").roles == ["1296268365593186426"]
    assert s.dry_run is True                                # empty explicit env -> the variable applies
    s2 = load_settings({"GE_VARS_JSON": json.dumps({"DRY_RUN": "1"}), "DRY_RUN": "0"})
    assert s2.dry_run is False                              # explicit input wins


def test_state_saves_only_on_change():
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        st = State.load(sp)
        assert st.save() is False and not sp.exists()
        st.code_records("genshin")["X"] = {"status": "seeded"}
        assert st.save() is True and sp.exists()
        st2 = State.load(sp)
        assert st2.save() is False
        st2.heartbeat("alpha", 60, 1000)
        assert st2.save() is True
        st2.heartbeat("alpha", 60, 1500)          # < 60 min later -> no new commit
        assert st2.save() is False


def test_hoyolab_quirk_and_html():
    post = fx("hoyolab_starrail_46814308_full.json")["data"]["post"]["post"]
    text, links, imgs = _post_text(post)
    assert text.startswith("Hello, Trailblazers!") and imgs and links
    gi = fx("hoyolab_genshin_46604275_full.json")["data"]["post"]["post"]
    t2, l2, i2 = _post_text(gi)
    assert "Version 7.1" in t2 and "https://www.youtube.com/@GenshinImpact" in l2 and i2


# =========================================================================== session 2 (v1.1)
def test_per_game_codes_webhooks_and_aliases():
    hooks = {k: f"https://discord.com/api/webhooks/{i}/codes-{k}" for i, k in
             enumerate(("genshin", "starrail", "hna", "zzz", "wuwa", "ananta"), start=10)}
    env = {f"DISCORD_WEBHOOK_CODES_{k.upper()}": v for k, v in hooks.items()}
    s = settings(**env)
    for key, url in hooks.items():
        assert s.webhook_source("codes", key) == (f"DISCORD_WEBHOOK_CODES_{key.upper()}", url), key
    assert s.webhook("schedule", "genshin") == HOOK                           # schedule falls back to URL
    s = settings(DISCORD_WEBHOOK_CODES_HSR="https://discord.com/api/webhooks/7/hsr",
                 DISCORD_WEBHOOK_CODES_NEXUSANIMA="https://discord.com/api/webhooks/8/hna",
                 DISCORD_WEBHOOK_CODES="https://discord.com/api/webhooks/9/all")
    assert s.webhook_source("codes", "starrail")[0] == "DISCORD_WEBHOOK_CODES_HSR"      # short name works
    assert s.webhook_source("codes", "hna")[0] == "DISCORD_WEBHOOK_CODES_NEXUSANIMA"    # alias works
    assert s.webhook_source("codes", "zzz")[0] == "DISCORD_WEBHOOK_CODES"               # feature fallback
    s = settings(FORCE_WEBHOOK="https://discord.com/api/webhooks/1/test", **env)
    assert s.webhook_source("codes", "wuwa") == ("FORCE_WEBHOOK", "https://discord.com/api/webhooks/1/test")
    assert "DISCORD_WEBHOOK_CODES_WUWA" in settings().expected_webhook_names("codes", "wuwa")


def test_no_ping_and_test_marker():
    s = settings(NO_PING="1", PING_CODES_GENSHIN="111111111111111111")
    assert not s.ping("codes", "genshin") and not s.ping("schedule", "wuwa")
    s = settings()
    p = cards.codes_payloads(GAMES["genshin"], CODE_SAMPLES["genshin"], s, s.ping("codes", "genshin"), 1)[0]
    t = cards.mark_test(p)
    assert "TEST CARD" in t["components"][0]["components"][0]["content"] and not cards.validate_payload(t)
    assert cards.mark_test(t) == t                       # idempotent: never stacks a 2nd banner
    assert "TEST CARD" not in json.dumps(p, ensure_ascii=False)                 # original untouched
    # the codes mention rides on the "… new code(s) • detected …" line, inside the card
    assert p["components"][0]["components"][0]["components"][1]["content"].endswith(
        "<@&1296268365593186426>")
    for key in CODE_SAMPLES:                                                    # one sample per game (6)
        assert key in GAMES
    assert set(CODE_SAMPLES) == {"genshin", "starrail", "zzz", "wuwa", "hna", "ananta"}


def test_a_feed_full_of_old_announcements_still_posts_only_the_newest():
    """A real timeline is not sorted and is mostly history: the 3.5 and 3.6 broadcasts are
    still sitting there when 3.7 is announced. Whatever order a mirror hands them over in, the
    run must post the current version once and leave the older ones alone."""
    now = 1789300000
    newest = item_from_fx_json("wuwa", fx("fx_wuwa_3_7_broadcast.json"))

    def older(version, days):
        old = item_from_fx_json("wuwa", fx("fx_wuwa_3_7_broadcast.json"))
        old.id = f"old{version.replace('.', '')}"
        old.url = old.url.replace("status/", f"status/{version.replace('.', '')}")
        old.title = old.title.replace("3.7", version)
        old.text = old.text.replace("3.7", version)
        old.published_ts = newest.published_ts - days * 86400
        return old

    history = [older("3.5", 84), older("3.6", 42)]

    for order in (history + [newest],                 # oldest -> newest
                  [newest] + history,                 # newest -> oldest
                  [history[1], newest, history[0]]):  # shuffled, like a real mirror
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            ctx = make_ctx(sp)                        # seed run: nothing is posted
            asyncio.run(schedule.run(ctx))
            ctx.state.save()

            ctx = make_ctx(sp, items={"wuwa": list(order)}, now=now)
            asyncio.run(schedule.run(ctx))
            posts = [x for x in ctx.webhook.sent if x["method"] == "POST"]
            assert len(posts) == 1, (len(posts), [i.title for i in order])
            blob = json.dumps(posts[0]["payload"], ensure_ascii=False)
            assert "3.7" in blob and "3.5" not in blob and "3.6" not in blob

            # and a second run over the same feed repeats nothing
            ctx.state.save()
            ctx2 = make_ctx(sp, items={"wuwa": list(order)}, now=now + 600)
            asyncio.run(schedule.run(ctx2))
            assert [x for x in ctx2.webhook.sent if x["method"] == "POST"] == []


def test_a_source_dumping_hundreds_of_codes_cannot_flood_the_channel():
    """Insurance against a broken or tampered-with source: a run posts at most
    MAX_CARDS_PER_RUN cards per game and the leftovers go out on the next run, instead of
    dumping 20 cards into one channel in one minute."""
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        table = {"seria:nap": [CodeHit("ZZZSEED01", "seria", "Polychrome*30", verified=True)]}
        ctx = make_ctx(sp, codes_table=table, GAME="zzz")
        ctx.games = [GAMES["zzz"]]
        asyncio.run(codeposter.run(ctx))                 # first run only seeds
        ctx.state.save()

        table["seria:nap"] += [CodeHit(f"ZZZFLOOD{i:03d}", "seria", "Polychrome*30", verified=True)
                               for i in range(120)]      # a source goes haywire
        ctx = make_ctx(sp, codes_table=table, GAME="zzz")
        ctx.games = [GAMES["zzz"]]
        asyncio.run(codeposter.run(ctx))

        assert len(ctx.webhook.sent) == codeposter.MAX_CARDS_PER_RUN   # 5 cards, not 13
        assert any("120 codes at once" in e for e in ctx.errors)       # the operator is told
        recs = ctx.state.code_records("zzz")
        waiting = [k for k, r in recs.items() if r.get("status") != "posted"]
        assert len(waiting) == 121 - codeposter.MAX_CARDS_PER_RUN * cards.CODES_PER_CARD
        # and the next run drains them instead of losing them
        ctx.state.save()
        ctx = make_ctx(sp, codes_table=table, GAME="zzz")
        ctx.games = [GAMES["zzz"]]
        asyncio.run(codeposter.run(ctx))
        assert len(ctx.webhook.sent) == codeposter.MAX_CARDS_PER_RUN


def test_invisible_and_bidi_characters_are_stripped_from_scraped_text():
    """Text is scraped, so it can carry characters that are not text: zero-width spaces that
    break a code in half, and bidi overrides that make a line *display* differently from what
    it says. clean_text() is the chokepoint every source goes through."""
    from gamexpress.textutil import clean_text, strip_invisible

    assert clean_text("Version\u200b 4.6\u202e Update\ufeff") == "Version 4.6 Update"
    assert clean_text("GENSHIN\u2066GIFT") == "GENSHINGIFT"
    assert strip_invisible("a\u0007b\u009fc\u2063d") == "abcd"
    assert clean_text("line one\nline two\n\n\n\nline three") == "line one\nline two\n\nline three"
    assert clean_text("real\u00a0space") == "real space"      # nbsp stays a space, not removed

    # and the whole way through a card: an invisible character cannot hide inside a title
    s_ = settings()
    d = dict(SCHEDULE_SAMPLES["starrail"])
    d["program_name"] = "Special\u202e Program"
    blob = json.dumps(cards.schedule_payload(GAMES["starrail"], d, s_,
                                             s_.ping("schedule", "starrail")), ensure_ascii=False)
    assert "\u202e" not in blob and "\u200b" not in blob


def test_a_hostile_source_cannot_inject_links_or_kill_a_card():
    """Everything on a card except the game's own config is scraped from a third party (16
    community nitter mirrors, wikis, code APIs). If one of them is taken over it must not be
    able to (a) smuggle an extra clickable link into a card readers trust, or (b) poison one
    field and take the whole announcement down with it."""
    s = settings()
    poisoned = dict(SCHEDULE_SAMPLES["starrail"])
    poisoned["title_url"] = "https://ok.example/post) [CLAIM 10000 FREE STELLAR JADE](https://evil.example"
    poisoned["source_url"] = "javascript:alert(document.cookie)"
    poisoned["source_label"] = "Source"
    poisoned["images"] = ["https://img.example/real.png", "data:text/html;base64,PHNjcmlwdD4="]

    p = cards.schedule_payload(GAMES["starrail"], poisoned, s, s.ping("schedule", "starrail"))
    blob = json.dumps(p, ensure_ascii=False)

    assert cards.validate_payload(p) == []          # the card still posts
    assert "evil.example" not in blob               # the breakout link never renders
    assert "javascript:" not in blob.lower()
    assert "data:text/html" not in blob.lower()
    assert blob.count("https://img.example/real.png") == 1   # the good image survived
    heads = [c["content"] for c in json.loads(blob)["components"]
             if isinstance(c, dict) and str(c.get("content", "")).startswith("## ")]
    heads += [c["content"] for top in json.loads(blob)["components"]
              for c in (top.get("components") or [])
              if isinstance(c, dict) and str(c.get("content", "")).startswith("## ")]
    assert heads and "](" not in heads[0]        # the title is shown, just not as a link
    container = next(c for c in p["components"] if c.get("type") == 17)
    rows = [c for c in container["components"] if c.get("type") == 1]
    assert rows and all(b["url"].startswith("https://") for r in rows for b in r["components"])

    # the same rule, directly
    assert cards.safe_url("javascript:alert(1)") is None
    assert cards.safe_url("https://ok.example/a b") is None          # whitespace
    assert cards.safe_url("https://ok.example/" + "x" * 2000) is None  # absurd length
    assert cards.safe_url(None) is None
    assert cards.safe_url("https://ok.example/a(b)c") == "https://ok.example/a%28b%29c"
    assert cards.link_button("x", "ftp://nope/") is None


def test_an_endless_response_body_is_cut_off_instead_of_eating_the_runner():
    """A hostile or broken host can stream forever; `resp.text()` would buffer all of it until
    the runner dies. The cap turns it into one ordinary source failure."""
    import asyncio

    from gamexpress import http as ghttp

    class Resp:
        status = 200
        charset = "utf-8"

        def __init__(self, size):
            self.content = self
            self._size = size

        async def read(self, n):
            return b"a" * min(n, self._size)

        async def iter_chunked(self, n):
            """The hostile host answers forever — in chunks, like a real one."""
            left = self._size
            while left > 0:
                take = min(n, left)
                left -= take
                yield b"a" * take

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def __init__(self, size):
            self.size = size
            self.calls = 0

        def get(self, url, **kw):
            self.calls += 1
            return Resp(self.size)

    big = Session(ghttp.MAX_BYTES * 4)
    f = ghttp.Fetcher(session=big)                      # type: ignore[arg-type]
    assert asyncio.run(f.get_text("https://hostile.example", source="s", retries=2)) is None
    assert big.calls == 1, "a body that big is not worth retrying"
    assert "larger than" in f.health["s"].last_error

    ok = Session(1024)
    f2 = ghttp.Fetcher(session=ok)                      # type: ignore[arg-type]
    assert asyncio.run(f2.get_text("https://fine.example", source="s")) == "a" * 1024


def test_every_official_feed_a_game_declares_is_actually_requested():
    """Coverage guard for the real source list, so a refactor cannot silently drop a feed.

    HoYoLAB's web UI splits a game's official circle into three tabs — notices / events / news
    (`page_sort=`) — which are `type=1 / 2 / 3` of the same `getNewsList` API. All three must be
    read for EVERY game that has a gid, or an announcement posted in the "wrong" tab is missed:

        https://www.hoyolab.com/circles/<gid>/<page_type>/official?page_sort=notices|events|news

    and every enabled game's X account must be probed across the nitter fleet.
    """
    import asyncio

    from gamexpress.config import active_games
    from gamexpress.sources import hoyolab
    from gamexpress.sources.twitter import XClient

    class Rec:
        health: dict = {}

        def __init__(self):
            self.calls = []

        async def get_json(self, url, **kw):
            self.calls.append((url, kw.get("params") or {}))
            return None

        async def get_text(self, url, **kw):
            self.calls.append((url, kw.get("params") or {}))
            return None

    st = settings()
    live = active_games(GAMES, st)
    assert [g.key for g in live] == ["genshin", "starrail", "zzz", "wuwa", "hna", "ananta"]

    rec = Rec()
    gids = {g.hoyolab_gid for g in live if g.hoyolab_gid}
    assert gids == {2, 6, 8, 9}, gids                      # GI, HSR, ZZZ, HNA (WW/ANANTA aren't on HoYoLAB)

    async def pull_official():
        for g in live:
            if g.hoyolab_gid:
                await hoyolab.official_items(rec, g, lambda _t: True, 0)
    asyncio.run(pull_official())
    asked = {(p["gids"], p["type"]) for u, p in rec.calls if "getNewsList" in u and "type" in p}
    assert asked == {(gid, t) for gid in gids for t in (1, 2, 3)}, sorted(asked)
    assert set(hoyolab.NEWS_TYPES) == {1, 2, 3}

    # every account, on the whole fleet — nitter.cf leads it and xitter.cf is still in it
    accounts = [a for g in live for a in g.x_accounts]
    assert accounts == ["GenshinImpact", "honkaistarrail", "ZZZ_EN", "Wuthering_Waves",
                        "HonkaiNA", "Ananta_EN"]
    rec2 = Rec()
    x = XClient(rec2, st)

    async def pull_timelines():
        for a in accounts:
            await x.timeline(a)
    asyncio.run(pull_timelines())
    urls = {u for u, _p in rec2.calls}
    for a in accounts:
        assert f"https://nitter.cf/{a}/rss" in urls, a
        assert f"https://xitter.cf/{a}/rss" in urls, a


def test_prepared_games_switch_on():
    """All six games ship ON now, but the prepared-game machinery must keep working: it is what
    an operator uses to run a subset, and what ANANTA's auto_enable_on date falls back to."""
    from gamexpress.config import active_games
    keys = lambda g, s, now=None: [x.key for x in active_games(g, s, now)]  # noqa: E731
    assert keys(GAMES, settings(), now=1790000000) == ["genshin", "starrail", "zzz", "wuwa", "hna", "ananta"]
    # a copy with both pre-release games switched back off -> the three switch-on paths still work
    off = dict(GAMES)
    off["hna"] = replace(GAMES["hna"], enabled=False)
    off["ananta"] = replace(GAMES["ananta"], enabled=False)
    assert keys(off, settings(), now=1790000000) == ["genshin", "starrail", "zzz", "wuwa"]
    assert "hna" in keys(off, settings(ENABLE_GAMES="hna"), now=1790000000)     # 1. ENABLE_GAMES
    jan15 = 1800000000                                                          # 2027-01-15 08:00 UTC
    assert "ananta" in keys(off, settings(), now=jan15) \
        and "ananta" not in keys(off, settings(), now=jan15 - 86400)            # 2. auto_enable_on
    assert keys(off, settings(GAMES="ananta"), now=1790000000) == ["ananta"]    # 3. explicit GAMES=


def test_four_star_names_rules():
    ok = ("Bennett", "March 7th", "Soldier 11", "Topaz & Numby", "Dan Heng • Imbibitor Lunae", "Yumemizuki Mizuki")
    bad = ("Event Wish", "2026", "Character Event", "Light Cone", "https://x.y", "TBA", "Limited 4-Star")
    assert all(schedule.plausible_name(n) for n in ok), [n for n in ok if not schedule.plausible_name(n)]
    assert not any(schedule.plausible_name(n) for n in bad), [n for n in bad if schedule.plausible_name(n)]
    txt = ('Warp "Butterfly" Phase I: the limited 5-star character "Pearl" and the 4-star characters '
           '"Event Wish", "Gallagher" and "Pela" will get a boost.')
    b = schedule.extract_banner(Item("hoyolab", "starrail", "1", "u", "Event Warp", txt, 1))
    assert b["banner_five"] == ["Pearl"] and b["banner_four_unsure"] is True


def _banner_extract(names4, five="Pearl", ts=1, source="hoyolab", unsure=False, phase=1):
    f = {"banner_five": [five], "banner_four": list(names4), "banner_four_unsure": unsure, "banner_phase": phase}
    return schedule.Extract(item=Item(source, "starrail", str(ts), f"u{ts}", "Warp", "x", ts), kind="banner",
                            version="4.7", fields=f)


def test_four_star_tba_when_the_list_is_incomplete():
    g = GAMES["starrail"]                                                       # 3 rate-up 4★ per phase
    rec, notes = {}, []
    d = schedule.merge(g, "4.7", [_banner_extract(["Gallagher", "Pela"])], rec, {}, {}, 1, notes)
    assert d["banners"]["phase1"] == ["Pearl"] and d["banners"]["phase1_4"] == []          # 2 of 3 -> TBA
    assert notes and "2 name(s) found, 3 expected" in notes[0]
    rec, notes = {}, []
    d = schedule.merge(g, "4.7", [_banner_extract(["Gallagher", "Pela", "Lynx"])], rec, {}, {}, 1, notes)
    assert d["banners"]["phase1_4"] == ["Gallagher", "Pela", "Lynx"] and not notes
    rec["data"] = d                                                             # a 2nd official post disagrees
    d = schedule.merge(g, "4.7", [_banner_extract(["Gallagher", "Pela", "Lynx"]),
                                  _banner_extract(["Gallagher", "Pela", "Asta"], ts=2)], rec, {}, {}, 3, notes)
    assert d["banners"]["phase1_4"] == [] and "disagree" in notes[-1]
    rec["data"], n = d, len(notes)                                              # sticky: stays TBA, no new note
    d = schedule.merge(g, "4.7", [_banner_extract(["Gallagher", "Pela", "Lynx"])], rec, {}, {}, 4, notes)
    assert d["banners"]["phase1_4"] == [] and len(notes) == n
    ov = {"banners": {"phase1_4": ["Gallagher", "Pela", "Lynx"]}}                # override is always trusted
    rec["data"] = d
    d = schedule.merge(g, "4.7", [_banner_extract(["Gallagher", "Pela", "Asta"], ts=2)], rec, ov, {}, 5, notes)
    assert d["banners"]["phase1_4"] == ["Gallagher", "Pela", "Lynx"]
    d = schedule.merge(g, "4.7", [_banner_extract([], unsure=True)], {}, {}, {}, 6, [])
    assert d["banners"]["phase1_4"] == []
    notes = []
    d = schedule.merge(g, "4.7", [_banner_extract(["Gallagher", "Pela", "Lynx"], unsure=True)],
                       {}, {}, {}, 7, notes)
    assert d["banners"]["phase1_4"] == ["Gallagher", "Pela", "Lynx"]
    card = json.dumps(cards.schedule_payload(g, {**d, "version": "4.7"}, settings(), settings().ping("schedule", "starrail")),
                      ensure_ascii=False)
    assert "4 Star Characters: TBA" in card


def test_new_code_sources_parsers():
    ogc = csrc.parse_ogc(fx("ogc_genshin_trimmed.json"))
    codes = [h.code for h in ogc]
    assert "TEST" not in codes and "UIVIBUQM6Q8AUIVI13C8X156" not in codes and "XVIZDH2B9WGXEHVE2TEAFY6O" not in codes
    assert {"EPIC2026", "VESNAONPATROL", "UIVIBUQM6Q8A", "XVIZDH2B9WGX"} <= set(codes)   # mixed case normalised
    assert all(h.source == "ogc" and not h.verified for h in ogc)
    ww = csrc.parse_ogc(fx("ogc_wuwa.json"))
    assert [h.code for h in ww][:2] == ["BAHAMUTKXMHM", "DCARD3VN7M"] and ww[0].rewards[0] == "Medium Energy Core ×5"
    hb = csrc.parse_humbao((FIX / "humbao_genshin.txt").read_text(encoding="utf-8"))
    assert [h.code for h in hb] == ["2BJ64QRZ7RT8", "GS71XDYGEO"] and all(h.verified for h in hb)
    wg = csrc.parse_wuthering_gg((FIX / "wuthering_gg_SYNTHETIC.html").read_text(encoding="utf-8"))
    assert [(h.code, h.expired) for h in wg] == [("WUTHERINGGIFT", False), ("NEWBROADCAST37", False),
                                                  ("WUWA4PC", True), ("BAHAMUTKXMHM", True)]
    assert wg[1].rewards == ["100 × Astrite"]
    seria = csrc.parse_seria({"codes": [{"code": "DEADCODE1", "status": "NOT_OK"}, {"code": "GOODCODE1", "status": "OK"}]})
    assert [(h.code, h.expired, h.verified) for h in seria] == [("DEADCODE1", True, False), ("GOODCODE1", False, True)]
    en = csrc.parse_ennead(fx("ennead_genshin_trimmed.json"))
    assert {h.code for h in en if h.expired} == {"GENSHINGIFT", "6ALMWAVKLK35"}
    assert csrc.family("ogc") == csrc.family("ennead") != csrc.family("fandom")


def test_code_gate_families_and_expired_veto():
    H = CodeHit
    g = lambda *hits: codeposter.gate(codeposter.group_hits(list(hits))["NEWCODE1"], 2)  # noqa: E731
    assert g(H("NEWCODE1", "ogc"), H("NEWCODE1", "ennead"))[0] is False        # same family = 1 source
    assert g(H("NEWCODE1", "ogc"), H("NEWCODE1", "fandom"))[0] is True
    ok, why = g(H("NEWCODE1", "ogc"), H("NEWCODE1", "fandom"), H("NEWCODE1", "wuthering.gg", expired=True))
    assert ok is False and "expired" in why                                     # any expired flag -> hold
    assert g(H("NEWCODE1", "seria", verified=True), H("NEWCODE1", "ennead", expired=True))[0] is True
    assert g(H("NEWCODE1", "humbao", verified=True), H("NEWCODE1", "seria", expired=True))[0] is False
    assert g(H("NEWCODE1", "x", verified=True), H("NEWCODE1", "seria", expired=True))[0] is True   # official wins
    assert g(H("NEWCODE1", "kuro", verified=True))[1] == "official"


def test_posted_code_card_is_edited_when_code_expires():
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        st = State.load(sp)
        st.mark_bootstrapped("codes", "wuwa")
        st.save()
        table = {"ogc:wuwa": [CodeHit("WAKINGMOON", "ogc", ["Astrite ×100"])],
                 "fandom:wutheringwaves/Redemption_Code": [CodeHit("WAKINGMOON", "fandom", None),
                                                            CodeHit("FINDSENTINEL", "fandom", None)],
                 "codehub:wuthering-waves": [CodeHit("FINDSENTINEL", "codehub", None)]}
        ctx = make_ctx(sp, codes_table=table)
        ctx.games = [GAMES["wuwa"]]
        asyncio.run(codeposter.run(ctx))
        assert [x["method"] for x in ctx.webhook.sent] == ["POST"]
        rec = ctx.state.code_records("wuwa")["WAKINGMOON"]
        assert rec["msg_codes"] == ["FINDSENTINEL", "WAKINGMOON"] and rec["status"] == "posted"
        ctx.state.save()
        table = {"wuthering.gg": [CodeHit("WAKINGMOON", "wuthering.gg", None, expired=True)],
                 "fandom:wutheringwaves/Redemption_Code": [CodeHit("WAKINGMOON", "fandom", None, expired=True),
                                                            CodeHit("FINDSENTINEL", "fandom", None)]}
        ctx = make_ctx(sp, codes_table=table, now=1789400000)
        ctx.games = [GAMES["wuwa"]]
        asyncio.run(codeposter.run(ctx))
        assert [x["method"] for x in ctx.webhook.sent] == ["PATCH"]
        edit = ctx.webhook.sent[0]["payload"]
        flat = json.dumps(edit, ensure_ascii=False)
        assert "~~`WAKINGMOON`~~ · expired" in flat and "`FINDSENTINEL`" in flat and "1 expired" in flat
        assert edit["allowed_mentions"] == {"parse": []}
        ctx.state.save()
        ctx = make_ctx(sp, codes_table=table, now=1789400600)                  # no repeated edits
        ctx.games = [GAMES["wuwa"]]
        asyncio.run(codeposter.run(ctx))
        assert ctx.webhook.sent == []


def test_missing_codes_webhook_names_the_secret():
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        st = State.load(sp)
        st.mark_bootstrapped("codes", "zzz")
        st.save()
        ctx = make_ctx(sp, codes_table={"seria:nap": [CodeHit("ZZZNEW99", "seria", None, verified=True)]},
                       DISCORD_WEBHOOK_URL="")
        ctx.games = [GAMES["zzz"]]
        asyncio.run(codeposter.run(ctx))
        assert any("DISCORD_WEBHOOK_CODES_ZZZ" in r for r in ctx.report)
        assert ctx.state.code_records("zzz")["ZZZNEW99"]["status"] == "pending"   # retried once a webhook exists


def test_webhook_spacing_is_never_charged_after_the_last_post():
    """n posts must cost n-1 gaps, not n: the old code slept 1.2s even after the final send."""
    from gamexpress.discord import WEBHOOK_SPACING

    class Resp:
        status = 200
        headers: dict = {}

        async def text(self):
            return '{"id": "1234567890"}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def __init__(self):
            self.n = 0

        def request(self, method, url, **kw):
            self.n += 1
            return Resp()

    st = settings()
    payload = cards.schedule_payload(GAMES["genshin"], SCHEDULE_SAMPLES["genshin"], st,
                                     st.ping("schedule", "genshin"), 1789815600)

    async def post(times: int) -> float:
        session = Session()
        wh = WebhookClient(session)
        t0 = time.monotonic()
        for _ in range(times):
            assert (await wh.send(HOOK, payload)).ok
        assert session.n == times
        return time.monotonic() - t0

    one = asyncio.run(post(1))
    assert one < WEBHOOK_SPACING / 2, one                      # single post: no trailing wait at all
    two = asyncio.run(post(2))
    assert WEBHOOK_SPACING <= two < 2 * WEBHOOK_SPACING, two   # exactly one gap, and still spaced


def test_test_mode_posts_latest_card_marked_test():
    with tempfile.TemporaryDirectory() as tmp:
        ww = item_from_fx_json("wuwa", fx("fx_wuwa_3_7_broadcast.json"))
        ctx = make_ctx(Path(tmp) / "s.json", items={"wuwa": [ww]}, now=1789815600 + 5 * 86400, TEST_MODE="1")
        asyncio.run(schedule.run(ctx))                                          # 5 days old, fresh state
        posts = [x for x in ctx.webhook.sent if x["method"] == "POST"]
        assert len(posts) == 1
        assert posts[0]["payload"]["components"][0]["components"][0]["content"].startswith("-# 🧪 TEST CARD")


def test_parallel_probe_counts_per_game():
    from gamexpress.http import Probe

    class Slow:
        health = {}

        async def get_json(self, url, **k):
            await asyncio.sleep(0.01)
            return {"ok": 1} if "good" in url else None

        async def get_text(self, url, **k):
            return None

    async def main():
        a, b = Probe(Slow()), Probe(Slow())
        await asyncio.gather(a.get_json("https://good/1"), b.get_json("https://bad/1"), a.get_json("https://good/2"))
        return a.ok, b.ok, a.health is b.health
    assert asyncio.run(main()) == (2, 0, True)


RSS = """<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>x</title>
<item><title>t</title><dc:creator>@Wuthering_Waves</dc:creator><link>https://n/Wuthering_Waves/status/{id}#m</link>
<description>Version 3.7 Special Broadcast</description><pubDate>Fri, 18 Sep 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_nitter_fleet_probed_in_parallel_batches():
    from gamexpress.sources.twitter import XClient
    calls = []

    class F:
        async def get_text(self, url, **k):
            calls.append(url)
            await asyncio.sleep(0.01)
            if "inst3" in url:
                return RSS.format(id="1111111111")
            if "inst6" in url:
                return RSS.format(id="2222222222")
            return None
    s = settings(NITTER_INSTANCES=",".join(f"https://inst{i}" for i in range(1, 11)))
    x = XClient(F(), s)
    tl = asyncio.run(x.timeline("Wuthering_Waves"))
    assert [e["id"] for e in tl] and {e["id"] for e in tl} == {"1111111111", "2222222222"}
    assert len(calls) == 8 and "wuthering_waves" in x.reachable                 # 2 batches of 4, then stop


def _nitter_fleet_fetcher(ids: dict, hang: str, hung: asyncio.Event, calls: list):
    """Fake Fetcher: every mirror answers with its own tweet id, except `hang`, which never does."""
    class F:
        async def get_text(self, url, **k):
            calls.append(url)
            host = url.split("/")[2]
            if host == hang:
                hung.set()
                await asyncio.sleep(60)          # stands in for the 12 s the live mirror burned
                return None
            return RSS.format(id=ids[host])
    return F()


def test_a_hung_mirror_no_longer_holds_the_batch_once_two_answered():
    """Live run #217 took 16.2 s where #215/#216/#218 took 4.9-6 s, for one reason: nitter.cf
    timed out and asyncio.gather() waited out its whole 12 s NITTER_TIMEOUT even though two
    higher-ranked mirrors in the same batch had already returned the timeline. The batch now
    stops as soon as the winners are decided and cancels the rest, which also frees the
    Fetcher's global request slot for the HoYoLAB / Kuro / code requests queued behind it."""
    from gamexpress.sources.twitter import XClient
    calls, hung = [], asyncio.Event()
    ids = {"m1": "1111111111", "m2": "2222222222", "m3": "3333333333"}
    fleet = ["https://m1", "https://m2", "https://m3", "https://dead"]
    x = XClient(_nitter_fleet_fetcher(ids, "dead", hung, calls), settings(NITTER_INSTANCES=",".join(fleet)))

    async def run():
        started = time.monotonic()
        tl = await asyncio.wait_for(x.timeline("Wuthering_Waves"), timeout=20)
        return tl, time.monotonic() - started
    tl, elapsed = asyncio.run(run())
    assert {e["id"] for e in tl} == {"1111111111", "2222222222"}     # the top two, as before
    assert len(calls) == 4 and hung.is_set()                        # all four were still asked
    assert elapsed < 1, elapsed      # but nothing waited for the hang (old code: the full 60 s)


def test_a_hung_top_ranked_mirror_only_costs_the_grace_window():
    """A mirror ranked ABOVE the answers would have won the merge, so it still gets a grace
    window — but NITTER_GRACE seconds, not the full 12 s timeout. Ranking beats speed: the two
    highest-ranked answers are merged, never just the two fastest."""
    from gamexpress.sources import twitter as tw
    calls, hung = [], asyncio.Event()
    ids = {"m2": "2222222222", "m3": "3333333333", "m4": "4444444444"}
    fleet = ["https://dead", "https://m2", "https://m3", "https://m4"]
    x = tw.XClient(_nitter_fleet_fetcher(ids, "dead", hung, calls), settings(NITTER_INSTANCES=",".join(fleet)))
    grace, tw.NITTER_GRACE = tw.NITTER_GRACE, 0.3
    try:
        async def run():
            started = time.monotonic()
            tl = await asyncio.wait_for(x.timeline("Wuthering_Waves"), timeout=20)
            return tl, time.monotonic() - started
        tl, elapsed = asyncio.run(run())
    finally:
        tw.NITTER_GRACE = grace
    assert {e["id"] for e in tl} == {"2222222222", "3333333333"}     # m2 + m3, never m4
    assert 0.3 <= elapsed < 5, elapsed                               # the grace window, not 60 s


def test_workflows_cron_job_org_and_test_bench():
    try:
        import yaml
    except ImportError:
        print("    (pyyaml not installed — workflow checks skipped)")
        return
    wf = ROOT / ".github" / "workflows"
    mon = yaml.safe_load((wf / "monitor.yml").read_text(encoding="utf-8"))
    on = mon.get("on") or mon.get(True)
    assert "schedule" not in on and "workflow_dispatch" in on                  # cron-job.org is the only trigger
    env = mon["jobs"]["monitor"]["env"]
    for key in ("GENSHIN", "STARRAIL", "HNA", "ZZZ", "WUWA", "ANANTA"):
        assert env[f"DISCORD_WEBHOOK_CODES_{key}"] == f"${{{{ secrets.DISCORD_WEBHOOK_CODES_{key} }}}}"
    assert mon["concurrency"]["cancel-in-progress"] is False
    # v1.6.0: two modes and nothing else — a live run, or a test that uses REAL live data.
    assert not (wf / "test.yml").exists()
    inputs = on["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"mode", "only", "game", "repost", "test", "ping"}   # no dry_run/probe/kind
    assert inputs["mode"]["options"] == ["live", "test"] and inputs["mode"]["default"] == "live"
    # cron-job.org sends NO inputs at all, so the defaults must be a plain live run
    assert inputs["only"]["default"] == "all" and inputs["game"]["default"] == "" \
        and inputs["repost"]["default"] == ""
    # ⑥ (ping) is documented as a TEST switch, but job-level env reaches the LIVE run too, and
    # cron-job.org sends no inputs -- so an unscoped `!inputs.ping` made NO_PING=1 on every
    # scheduled run: settings.ping() short-circuits on it, PING_SCHEDULE was dead weight, and a
    # real version announcement posted without pinging the role (seen in the 2026-09-27 runs:
    # live = "no ping", test+ping = "<@&1296268365593186426>").
    assert env["NO_PING"] == "${{ inputs.mode != 'live' && !inputs.ping && '1' || '0' }}"
    assert inputs["test"]["options"] == ["webhooks", "codes", "schedule", "all"]
    job = mon["jobs"]["monitor"]
    assert "|| inputs.mode == 'test'" in str(job["if"])          # a test still runs on the dev repo
    steps = {str(s.get("name", "")): s for s in job["steps"]}
    live = {n: s for n, s in steps.items() if n.startswith("LIVE")}
    tests = {n: s for n, s in steps.items() if n.startswith("TEST")}
    assert len(live) == 2 and len(tests) == 4, (sorted(live), sorted(tests))
    assert live["LIVE · monitor run"]["if"] == "inputs.mode == 'live'"
    commit = next(s for n, s in live.items() if "commit state" in n)
    assert "inputs.mode == 'live'" in commit["if"] and "dry_run" not in commit["if"]
    for n, s in tests.items():                                   # every test is mode-gated…
        assert s["if"].startswith("inputs.mode == 'test'"), n
        assert "git commit" not in str(s.get("run", "")) and "git push" not in str(s.get("run", ""))
    # the codes/schedule tests are REAL runs (not sample cards) that can never save the state,
    # so the live run still posts the real thing later
    real = [s for n, s in tests.items() if "codes — the REAL" in n or "schedule — the REAL" in n]
    assert len(real) == 2
    for s in real:
        assert s["run"].strip() == "python -m gamexpress run"
        assert s["env"]["TEST_MODE"] == "1" and s["env"]["BOOTSTRAP_POST"] == "1"
        assert "runner.temp" in s["env"]["STATE_PATH"] and s["env"]["PEER_STATE_URL"] == "none"
    assert {s["env"]["ONLY"] for s in real} == {"codes", "schedule"}
    # a game that is not live yet has nothing real to fetch, so it is checked with example codes
    unl = next(s for n, s in tests.items() if "not live yet" in n)
    assert "--unlaunched" in unl["run"] and "--kind codes" in unl["run"]
    for name in ("monitor.yml", "ci.yml"):
        text = (wf / name).read_text(encoding="utf-8")
        assert "uses: actions/checkout@" in text and "uses: actions/setup-python@" in text, name
    am = yaml.safe_load((wf / "ci.yml").read_text(encoding="utf-8"))["jobs"]["automerge"]
    assert am["needs"] == "test" and "vars.AUTO_MERGE_DEPENDABOT == 'yes'" in am["if"]   # only after green tests
    assert "dependabot[bot]" in am["if"] and am["permissions"] == {"contents": "write", "pull-requests": "write"}
    assert not any("checkout" in str(step.get("uses", "")) for step in am["steps"])      # PR code never runs here
    assert not (wf / "dependabot_auto_merge.yml").exists()
    dep = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    assert all("labels" not in u for u in dep["updates"])            # custom labels must pre-exist -> none
    assert dep["updates"][0]["versioning-strategy"] == "increase-if-necessary"


def test_ci_and_the_python_bump_gate_run_every_offline_test_file():
    """Every tests/test_*.py file has a plain runner, and every one of them is a CI gate.

    Until 2026-10-15 ci.yml ran only tests/test_smoke.py: the 20 regressions in
    test_schedule_epithet_and_settled.py (the 2026-10-08 schedule-card faults that AGENTS.md
    invariants #6 and #15 lean on) and the 3 in test_gachawiki_placeholder.py only ever ran under
    pytest, which CI does not install -- so a PR could break them and merge green. The
    python-bump workflow promises "the exact commands ci.yml runs", so it is held to the same
    list. A new test file fails here until both workflows run it.
    """
    try:
        import yaml
    except ImportError:
        print("    (pyyaml not installed — workflow checks skipped)")
        return
    wf = ROOT / ".github" / "workflows"
    test_files = sorted(p.name for p in (ROOT / "tests").glob("test_*.py"))
    assert len(test_files) >= 3, test_files
    for name in test_files:                                   # each one runs on its own
        src = (ROOT / "tests" / name).read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in src, f"{name} has no plain runner"
        assert "sys.path.insert(0, str(ROOT))" in src, f"{name} cannot import gamexpress standalone"

    ci = yaml.safe_load((wf / "ci.yml").read_text(encoding="utf-8"))
    ci_runs = "\n".join(str(s.get("run", "")) for s in ci["jobs"]["test"]["steps"])
    bump = yaml.safe_load((wf / "python_version_bump.yml").read_text(encoding="utf-8"))
    gate = next(s for job in bump["jobs"].values() for s in job["steps"]
                if "tests/test_smoke.py" in str(s.get("run", "")))
    for name in test_files:
        assert f"python tests/{name}" in ci_runs, f"ci.yml's test job does not run tests/{name}"
        assert f"python tests/{name}" in gate["run"], f"the python-bump gate does not run tests/{name}"
    assert ".github/scripts" in ci_runs and ".github/scripts" in gate["run"]   # compileall covers the bump script too


def test_every_action_is_pinned_to_a_commit_sha_with_a_readable_version_comment():
    """A tag is mutable. In March 2025 an attacker with a stolen bot token repointed every
    tag of tj-actions/changed-files (v1 … v45.0.7) at one malicious commit that dumped runner
    memory - including secrets - into the logs of ~23 000 repositories (CVE-2025-30066); the
    same week reviewdog/action-setup was compromised the same way (CVE-2025-30154), and this
    repo uses a reviewdog action. A 40-character commit SHA cannot be repointed, so every
    `uses:` here is a SHA plus a `# vX.Y.Z` comment (Dependabot updates both).

    The comment also keeps the older astral-sh rule readable: astral-sh publishes no floating
    major tag (no `v10` for setup-uv, no `v4` for ruff-action), and an unresolvable `uses:`
    fails during *Set up job* before `continue-on-error` can rescue anything.
    """
    import re
    wf = ROOT / ".github" / "workflows"
    seen = []
    for path in sorted(wf.glob("*.yml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.search(r"uses:\s*([\w.-]+/[\w.-]+)@(\S+)", line)
            if not m:
                continue
            repo, ref = m.group(1), m.group(2)
            seen.append(f"{repo}@{ref}")
            assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{path.name}: {repo}@{ref} is not a SHA"
            ver = re.search(r"#\s*(v[\w.-]+)", line)
            assert ver, f"{path.name}: {repo} pinned without a version comment"
            if repo.startswith("astral-sh/"):
                assert re.fullmatch(r"v\d+\.\d+\.\d+", ver.group(1)), f"{path.name}: {ver.group(1)}"
    assert len(seen) >= 9, seen


def test_preview_html_renders_cards():
    from gamexpress.preview_html import inline, render_page
    s = settings()
    pages = [("Code cards", k, p) for k, codes in CODE_SAMPLES.items()
             for p in cards.codes_payloads(GAMES[k], codes, s, s.ping("codes", k), 1790000000)]
    pages += [("Schedule cards", k, _render(k)) for k in SCHEDULE_SAMPLES]
    page = render_page(pages)
    assert page.count('class="container"') == len(pages) and 'class="btn"' in page
    assert "&lt;t:" in page and 'data-f="R"' in page and "@ping-role" in page
    assert "<script>" in page and "cdn.discordapp.com/emojis/1508619925940473966.gif" in page
    assert inline("**a** `<b>` [x](https://e.x) <t:1:R>").startswith("<strong>a</strong> <code>&lt;b&gt;</code>")



# =========================================================================== 1.1.1 (first live runs)
LIVE_NOW = 1790322634          # 2026-09-25 07:50 UTC — the user's first Test/Monitor runs


def test_fandom_live_tables_expiry_and_multi_code_rows():
    ww = {h.code: h for h in csrc.parse_fandom(fx("fandom_ww_live_trimmed.json"), LIVE_NOW)}
    assert list(ww) == ["WUTHERINGGIFT", "FALLINGSANCTUM", "FINDSENTINEL", "WAKINGMOON", "HEARTOFSWORD", "BURNINGSUN"]
    assert not ww["WUTHERINGGIFT"].expired and ww["WUTHERINGGIFT"].expires_at is None
    assert ww["WAKINGMOON"].expired and ww["WAKINGMOON"].expires_at == 1790006399        # Sep 21 08:59:59 PDT
    assert ww["WAKINGMOON"].rewards == "Astrite*100;Premium Tuner*20;Advanced Sealed Tube*5"
    hsr = {h.code: h for h in csrc.parse_fandom(fx("fandom_hsr_live_trimmed.json"), LIVE_NOW)}
    assert hsr["MALSV2F247FP"].expired and hsr["MALSV2F247FP"].expires_at == 1790035199  # end of 2026-09-21 UTC
    assert not hsr["OMEGA"].expired and not hsr["CREATIONNYMPH"].expired and not hsr["NSJR3B97ZZ5X"].expired
    assert hsr["MH5KC"].expired and hsr["MH5KC"].expires_at is None and hsr["SILVERWOLFLV999"].expired
    assert hsr["CREATIONNYMPH"].rewards == "Stellar Jade*60;Fuel*1;Heroic Variable*1"   # [[a|b]] inside ref=
    assert csrc.parse_fandom(fx("fandom_hsr_live_trimmed.json"), 1790000000)[0].expired is False  # before the date
    gi = [h.code for h in csrc.parse_fandom(fx("fandom_gi_live_trimmed.json"), LIVE_NOW)]
    assert gi == ["VESNAONPATROL", "GS71XAVWDS", "GS71XDYGEO", "GS71XYNSYJ", "GS71XOXYLG", "2BJ64QRZ7RT8"]  # CN skipped
    assert csrc.parse_expiry("December 14, 2025 07:59 (PT)", LIVE_NOW) == (True, 1765727999)       # PST in winter
    assert csrc.parse_expiry("indef") == (False, None) and csrc.parse_expiry("exp") == (True, None)
    assert csrc.parse_expiry("2026-10-01", LIVE_NOW) == (False, 1790899199)
    # a row without its valid-until column: the discovered date (4th field) is NOT an expiry
    no_until = {"query": {"pages": {"1": {"revisions": [{"*": "{{Code Row|NEWCODE123|G|Primogem*60|2026-09-20}}"}]}}}}
    assert [(h.code, h.expired, h.expires_at) for h in csrc.parse_fandom(no_until, LIVE_NOW)] == [("NEWCODE123", False, None)]


def test_ogc_reward_cleanup():
    ogc = {h.code: h.rewards for h in csrc.parse_ogc(fx("ogc_starrail_trimmed.json"))}
    assert ogc["7S4AD2X35NE3"] == ["Stellar Jade ×100", "Traveler's Guide ×5"]           # no "Unknown reward (hash)"
    assert ogc["AT45Q"] == ["Credit ×50,000", "Stellar Jade ×100"]                         # duplicate dropped
    assert ogc["BESTCOFFEEEVER"] == ["Express Special Blend - Rustic Infusion ×2", "Traveler's Guide ×3"]
    assert "BLADEFITCHECK" in ogc


def test_aggregator_copies_count_as_their_upstream():
    H = CodeHit
    g = lambda *hits: codeposter.gate(codeposter.group_hits(list(hits), LIVE_NOW)["NEWCODE1"], 2)
    assert g(H("NEWCODE1", "fandom"), H("NEWCODE1", "codehub", origin="fandom"))[0] is False   # same source twice
    assert g(H("NEWCODE1", "ogc"), H("NEWCODE1", "codehub", origin="fandom"))[0] is True
    hub = csrc.parse_codehub(fx("codehub_live_trimmed.json"), "honkai-star-rail")
    assert [(h.code, h.origin) for h in hub] == [("MALSV2F247FP", "seria")]
    results = {"seria:hkrpg": [H("OMEGA", "seria", verified=True)], "codehub:honkai-star-rail": hub}
    kept = codeposter.drop_stale_copies([h for r in results.values() for h in r], results)
    assert [h.code for h in kept] == ["OMEGA"]                 # seria dropped it -> PromoGacha's copy is stale
    results["seria:hkrpg"] = None                               # seria down: the copy is the best we have
    assert [h.code for h in codeposter.drop_stale_copies(hub, results)] == ["MALSV2F247FP"]


# =========================================================================== fandom code pages
# The four community wikis the codes monitor reads, exactly as listed in the request. A rename or
# a typo in config/games.json silently drops a whole source, so the mapping is asserted here.
FANDOM_PAGES = {
    "genshin": "fandom:genshin-impact/Promotional_Code",       # /wiki/Promotional_Code
    "starrail": "fandom:honkai-star-rail/Redemption_Code",     # /wiki/Redemption_Code
    "zzz": "fandom:zenless-zone-zero/Redemption_Code",         # /wiki/Redemption_Code
    "wuwa": "fandom:wutheringwaves/Redemption_Code",           # /wiki/Redemption_Code
}


def test_every_game_watches_the_fandom_page_the_community_maintains():
    for key, spec in FANDOM_PAGES.items():
        assert spec in GAMES[key].codes.get("sources", []), (key, GAMES[key].codes["sources"])


def test_the_live_genshin_fandom_page_yields_every_active_code_exactly_once():
    """Verbatim 'Active Codes' section of genshin-impact.fandom.com/wiki/Promotional_Code,
    captured 2026-09-27 (pageid 10893, revid 2183997, 2026-09-25T12:05:33Z). It carries every
    awkwardness the GI table has: a four-code row, a mixed-case code, a |ref= field full of
    URLs, and a China-only row that must not reach a global channel."""
    hits = csrc.parse_fandom(fx("fandom_gi_live_active_codes.json"), LIVE_NOW)
    codes = [h.code for h in hits]
    assert codes == ["EPIC2026", "VESNAONPATROL",                       # mixed case -> upper
                     "GS71XAVWDS", "GS71XDYGEO", "GS71XYNSYJ", "GS71XOXYLG",   # one row, 4 codes
                     "Y6JYMKV6JKSL", "YOAL3V36XHS7", "DUGODWKRHAKDNJ", "BALLETCOLLAB",
                     "2BJ64QRZ7RT8"]
    assert len(codes) == len(set(codes)), "the same code twice from one page"
    assert "YUANSHEN" not in codes                       # CN-only row
    assert not [h for h in hits if h.expired]            # the whole section is under ==Active==
    by_code = {h.code: h for h in hits}
    assert by_code["EPIC2026"].rewards == "Primogem*40;Mora*20000;Hero's Wit*5"
    assert by_code["GS71XOXYLG"].rewards == "Mora*30000;Hero's Wit*3;Mystic Enhancement Ore*5"


def test_an_expired_heading_in_column_zero_still_expires_its_codes():
    """The MediaWiki API returns a page from its first byte, so a wiki whose very first line is
    '==Expired Codes==' has no newline in front of it. The marker regex was anchored on \n, so
    that heading was invisible and every dead code on the page was posted as active."""
    wikitext = ("==Expired Codes==\n"
                "{{Code Row|DEADCODE1|G|Primogem*60|2026-01-01|unknown}}\n"
                "==Active Codes==\n"
                "{{Code Row|LIVECODE1|G|Primogem*60|2026-09-01|unknown}}\n")
    data = {"query": {"pages": {"1": {"revisions": [{"slots": {"main": {"*": wikitext}}}]}}}}
    hits = {h.code: h for h in csrc.parse_fandom(data, LIVE_NOW)}
    assert hits["DEADCODE1"].expired is True
    assert hits["LIVECODE1"].expired is False


def test_an_aggregator_copy_is_never_a_second_independent_source():
    """PromoGacha (codehub) copies other collectors and never deletes entries, so a copy counts
    as its UPSTREAM -- including an Open Gacha Codes / api.ennead.cc copy, which is the same
    backend as `ogc`. Otherwise one real source could satisfy a two-source gate all by itself."""
    hub = {"codes": [{"game": "genshin", "code": "GS71XAVWDS", "source": {"name": "Open Gacha Codes"}},
                     {"game": "genshin", "code": "EPIC2026", "source": {"name": "Fandom Wiki"}},
                     {"game": "genshin", "code": "DUGODWKRHAKDNJ", "source": {"name": "hoyo-codes"}},
                     {"game": "genshin", "code": "YOAL3V36XHS7", "source": {"name": "api.ennead.cc"}}]}
    origins = {h.code: h.origin for h in csrc.parse_codehub(hub, "genshin")}
    assert origins == {"GS71XAVWDS": "ennead", "EPIC2026": "fandom",
                       "DUGODWKRHAKDNJ": "seria", "YOAL3V36XHS7": "ennead"}

    # and the gate sees ONE family, not two, when ogc and its codehub copy are the only sources
    hits = [CodeHit("GS71XAVWDS", "ogc", None),
            next(h for h in csrc.parse_codehub(hub, "genshin") if h.code == "GS71XAVWDS")]
    info = codeposter.group_hits(hits, LIVE_NOW)["GS71XAVWDS"]
    assert info["families"] == {"ennead"}
    assert codeposter.gate(info, 2)[0] is False


def test_valid_until_date_beats_every_other_source():
    H = CodeHit
    dead = H("NEWCODE1", "fandom", None, expired=True, expires_at=LIVE_NOW - 3600)
    info = codeposter.group_hits([H("NEWCODE1", "ogc"), H("NEWCODE1", "humbao", verified=True),
                                  H("NEWCODE1", "x", verified=True), dead], LIVE_NOW)["NEWCODE1"]
    ok, why = codeposter.gate(info, 2)
    assert ok is False and why.startswith("expired 2026-09-25")
    alive = H("NEWCODE1", "fandom", None, expires_at=LIVE_NOW + 86400)          # a future date is fine
    assert codeposter.gate(codeposter.group_hits([H("NEWCODE1", "ogc"), alive], LIVE_NOW)["NEWCODE1"], 2)[0] is True


def test_live_2026_09_25_expired_livestream_codes_are_not_posted():
    """Regression from the first real dry run: 3 HSR + 3 WW livestream codes expired on
    2026-09-21 but the wikis still listed them under 'Active', PromoGacha never deletes and
    Open Gacha Codes lags — they must NOT be posted; the genuinely active ones must be."""
    hsr_hub = csrc.parse_codehub(fx("codehub_live_trimmed.json"), "honkai-star-rail")
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "s.json"
        st = State.load(sp)
        st.mark_bootstrapped("codes", "wuwa")
        st.mark_bootstrapped("codes", "starrail")
        st.save()
        table = {"fandom:wutheringwaves/Redemption_Code": csrc.parse_fandom(fx("fandom_ww_live_trimmed.json"), LIVE_NOW),
                 "codehub:wuthering-waves": csrc.parse_codehub(fx("codehub_live_trimmed.json"), "wuthering-waves"),
                 "ogc:wuwa": [CodeHit("WUTHERINGGIFT", "ogc", ["Astrite ×50"])],
                 "fandom:honkai-star-rail/Redemption_Code": csrc.parse_fandom(fx("fandom_hsr_live_trimmed.json"), LIVE_NOW),
                 "codehub:honkai-star-rail": hsr_hub,
                 "ogc:starrail": csrc.parse_ogc(fx("ogc_starrail_trimmed.json")),
                 "seria:hkrpg": [CodeHit("OMEGA", "seria", "60 stellar jade and one fuel", verified=True)]}
        ctx = make_ctx(sp, codes_table=table, now=LIVE_NOW)
        ctx.games = [GAMES["wuwa"], GAMES["starrail"]]
        asyncio.run(codeposter.run(ctx))
        posted = " ".join(json.dumps(x["payload"], ensure_ascii=False) for x in ctx.webhook.sent)
        for dead in ("FALLINGSANCTUM", "FINDSENTINEL", "WAKINGMOON", "MALSV2F247FP", "7S4AD2X35NE3"):
            assert dead not in posted, dead
        assert "WUTHERINGGIFT" in posted and "OMEGA" in posted
        assert ctx.state.code_records("starrail")["7S4AD2X35NE3"]["status"] == "rejected"
        assert any(r.startswith("🧊 HSR: 1 code(s) ignored — already expired") for r in ctx.report)
        assert any(r.startswith("⏳ HSR: ") and "NSJR3B97ZZ5X (only fandom)" in r for r in ctx.report)
        assert not any("pending (listed as expired" in r for r in ctx.report)        # no more 100-line summaries


def test_posted_code_is_struck_when_its_valid_until_date_passes():
    t0 = 1789900000
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        st = State.load(sp)
        st.mark_bootstrapped("codes", "starrail")
        st.save()
        table = {"seria:hkrpg": [CodeHit("LIVE4610", "seria", "Stellar Jade ×100", verified=True)],
                 "fandom:honkai-star-rail/Redemption_Code": [CodeHit("LIVE4610", "fandom", None, expires_at=t0 + 86400)]}
        ctx = make_ctx(sp, codes_table=table, now=t0)
        ctx.games = [GAMES["starrail"]]
        asyncio.run(codeposter.run(ctx))
        assert [x["method"] for x in ctx.webhook.sent] == ["POST"]
        ctx.state.save()
        table = {"seria:hkrpg": [CodeHit("LIVE4610", "seria", None, verified=True)],    # not re-checked yet
                 "ogc:starrail": [CodeHit("LIVE4610", "ogc", None)],                     # lagging aggregator
                 "fandom:honkai-star-rail/Redemption_Code": [
                     CodeHit("LIVE4610", "fandom", None, expired=True, expires_at=t0 + 86400)]}
        ctx = make_ctx(sp, codes_table=table, now=t0 + 2 * 86400)
        ctx.games = [GAMES["starrail"]]
        asyncio.run(codeposter.run(ctx))
        assert [x["method"] for x in ctx.webhook.sent] == ["PATCH"]
        assert "~~`LIVE4610`~~ · expired" in json.dumps(ctx.webhook.sent[0]["payload"], ensure_ascii=False)


def test_program_tba_line_hidden_once_the_update_is_known():
    s = settings()
    d = {"version": "4.6", "maint_start_ts": 1790546400, "maint_end_ts": 1790564400, "preinstall_ts": 1790229600}
    txt = json.dumps(cards.schedule_payload(GAMES["starrail"], d, s, s.ping("schedule", "starrail")), ensure_ascii=False)
    assert "Special Program: TBA" not in txt and "<t:1790546400:F>" in txt
    teaser = cards.schedule_payload(GAMES["starrail"], {"version": "4.7"}, s, s.ping("schedule", "starrail"))
    assert "Special Program: TBA" in json.dumps(teaser, ensure_ascii=False)          # nothing known yet -> TBA
    ww = cards.schedule_payload(GAMES["wuwa"], {"version": "3.7", "maint_start_ts": 1790712000}, s, s.ping("schedule", "wuwa"))
    assert "Special Broadcast: TBA" not in json.dumps(ww, ensure_ascii=False) and not cards.validate_payload(ww)


def test_repost_feedback_and_first_run_notes():
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        for value, expected in (("genshin:7.2", "version 7.2 isn't tracked"), ("genshin7.2", "use game:version"),
                                ("gensin:7.1", "unknown or inactive game 'gensin'")):
            ctx = make_ctx(sp, REPOST=value)
            asyncio.run(schedule.run(ctx))
            assert any(expected in r for r in ctx.report), (value, ctx.report)
        sp2 = Path(tmp) / "state2.json"
        ww = item_from_fx_json("wuwa", fx("fx_wuwa_3_7_broadcast.json"))
        late = 1789815600 + 10 * 86400                         # the broadcast was 10 days ago
        ctx = make_ctx(sp2, items={"wuwa": [ww]}, now=late)
        asyncio.run(schedule.run(ctx))
        assert ctx.webhook.sent == [] and any(r.startswith("🗂 WW 3.7: already out") for r in ctx.report)
        ctx.state.save()
        ctx = make_ctx(sp2, items={"wuwa": [ww]}, now=late + 600, REPOST=" WUWA:3.7 ")    # spaces / case forgiven
        asyncio.run(schedule.run(ctx))
        assert [x["method"] for x in ctx.webhook.sent] == ["POST"] and not any("repost" in r for r in ctx.report)



# =========================================================================== 1.2.0 (card buttons + countdown estimates)
def test_code_card_buttons_are_the_code_links_then_the_community_row():
    s = settings()
    codes = [{"code": "VESNAONPATROL", "sources": ["ogc", "fandom"], "rewards": "Primogem ×40"}]
    p = cards.codes_payloads(GAMES["genshin"], codes, s, s.ping("codes", "genshin"), LIVE_NOW)[0]
    flat = json.dumps(p, ensure_ascii=False)
    box = p["components"][-1]["components"]
    rows = [c for c in box if c["type"] == 1]
    assert [b["label"] for b in rows[0]["components"]] == ["VESNAONPATROL"]       # one Redeem link per code
    assert [b["label"] for b in rows[-1]["components"]] == ["Citlali News"]       # community row, at the end
    assert rows[-1]["components"][0]["url"] == "https://discord.gg/HyrVP9wRXu"
    assert rows[-1]["components"][0]["emoji"] == {"id": "1439878792653832253", "name": "starward11",
                                                  "animated": True}
    assert box[box.index(rows[-1]) - 1]["type"] == 14            # a separator in front of the row
    for gone in ("Redeem Page", "Youtube", "Twitch"):            # livestream buttons, not code buttons
        assert gone not in flat, gone
    assert not cards.validate_payload(p)
    ww = cards.codes_payloads(GAMES["wuwa"], [{"code": "WUTHERINGGIFT", "sources": ["ogc"]}],
                              s, s.ping("codes", "wuwa"), LIVE_NOW)[0]
    rows = [c for c in ww["components"][-1]["components"] if c["type"] == 1]
    assert [b["label"] for b in rows[-1]["components"]] == ["Citlali News"]       # in-game-only game too
    # the livestream card keeps Youtube / Twitch
    labels = [b["label"] for c in _render("starrail")["components"][0]["components"] if c["type"] == 1
              for b in c["components"]]
    assert labels[:2] == ["Youtube", "Twitch"]
    # configurable: COMMUNITY_BUTTONS=none -> no row at all
    off = settings(COMMUNITY_BUTTONS="none")
    assert off.community_buttons == []
    p2 = cards.codes_payloads(GAMES["genshin"], codes, off, off.ping("codes", "genshin"), LIVE_NOW)[0]
    assert [c for c in p2["components"][-1]["components"] if c["type"] == 1] == [
        {"type": 1, "components": [cards.link_button("VESNAONPATROL",
                                                    "https://genshin.hoyoverse.com/en/gift?code=VESNAONPATROL",
                                                    {"name": "🎁"})]}]
    custom = settings(COMMUNITY_BUTTONS='[{"label":"My Server","url":"https://discord.gg/abc"}]')
    assert [b["label"] for b in custom.community_buttons] == ["My Server"]


def test_countdown_pages_are_parsed():
    now = LIVE_NOW
    gengamer = (FIX / "countdown_gengamer_gi_livestream.html").read_text(encoding="utf-8")
    hit = countdown.parse_page("program", gengamer, now)
    assert hit["version"] == "7.2" and hit["ts"] == 1792756800        # Friday, October 23 at 8:00 AM EDT
    gacha = (FIX / "countdown_gachacountdown_gi.html").read_text(encoding="utf-8")
    hit2 = countdown.parse_page("update", gacha, now)
    assert hit2["version"] == "7.2"
    assert hit2["ts"] == now + 39 * 86400 + 14 * 3600 + 9 * 60 + 8    # 39d 14h 09m 08s from now
    assert countdown.find_version("Countdown to Version 7.2") == "7.2"
    assert "Hello & bye" in countdown.strip_html("<script>x=1</script><p>Hello &amp; bye</p>")
    assert countdown.SOURCES["wuwa"] and not countdown.SOURCES["ananta"]      # not released yet


def test_apply_estimates_never_overwrites_official_times():
    data, prov = {"maint_start_ts": 100}, {}
    assert schedule.apply_estimates(data, prov, {"maint_start_ts": 200}, 0) == []
    assert data["maint_start_ts"] == 100 and "estimated" not in data
    assert schedule.apply_estimates({}, {}, {"program_ts": 10 ** 10}, 0) == []        # absurd -> ignored
    data2, prov2 = {}, {}
    added = schedule.apply_estimates(data2, prov2, {"maint_start_ts": 1790712000,
                                                    "labels": ["Gacha Countdown"]}, 1790000000)
    assert added == ["maint_start_ts", "maint_end_ts"]                            # +5 h window
    assert data2["maint_end_ts"] == 1790712000 + 5 * 3600
    assert data2["estimated"] == ["maint_start_ts", "maint_end_ts"] and data2["estimate_sources"] == ["Gacha Countdown"]
    assert prov2["maint_start_ts"][0] == schedule.PRIORITY["countdown"] < schedule.PRIORITY["kuro"]


def test_estimated_maintenance_times_fill_only_what_official_misses():
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        ww = item_from_fx_json("wuwa", fx("fx_wuwa_3_7_broadcast.json"))
        estimate = {"wuwa": {"version": "3.7", "maint_start_ts": 1790712000, "labels": ["Gacha Countdown"]}}
        ctx = make_ctx(sp, items={"wuwa": [ww]}, now=1789300000, BOOTSTRAP_POST=1)
        ctx.estimates = estimate
        asyncio.run(schedule.run(ctx))
        posts = [x for x in ctx.webhook.sent if x["method"] == "POST"]
        assert len(posts) == 1
        flat = json.dumps(posts[0]["payload"], ensure_ascii=False)
        assert "<t:1790712000:f>" in flat and "<t:1790730000:t>" in flat       # start + the 5 h estimate
        assert "estimated from Gacha Countdown" in flat
        assert any(r.startswith("🕒 WW 3.7: pre-install, maintenance start, maintenance end estimated")
                   for r in ctx.report)
        ctx.state.save()
        # the official maintenance notice arrives -> it wins and the 🕒 line disappears silently
        notice = Item("kuro", "wuwa", "9001", "https://wutheringwaves.kurogames.com/en/main/news/detail/9001",
                      "Version 3.7 Update Maintenance Notice",
                      "Version 3.7 pre-download will begin at 2026-09-28 10:00 (UTC+8).\n"
                      "Maintenance Time: 2026-09-30 04:00 - 11:00 (UTC+8)\nCompensation: Astrite ×300",
                      1790000000)
        ctx = make_ctx(sp, items={"wuwa": [ww, notice]}, now=1790001000)
        ctx.estimates = estimate
        asyncio.run(schedule.run(ctx))
        assert [x["method"] for x in ctx.webhook.sent] == ["PATCH"]
        flat2 = json.dumps(ctx.webhook.sent[0]["payload"], ensure_ascii=False)
        assert "<t:1790712000:f>" in flat2 and "<t:1790737200:t>" in flat2     # the official window
        assert "estimated from" not in flat2
        assert ctx.state.schedule_records("wuwa")["3.7"]["data"].get("estimated") is None


def test_an_estimate_for_another_version_is_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        ww = item_from_fx_json("wuwa", fx("fx_wuwa_3_7_broadcast.json"))
        ctx = make_ctx(sp, items={"wuwa": [ww]}, now=1789300000, BOOTSTRAP_POST=1)
        ctx.estimates = {"wuwa": {"version": "9.9", "maint_start_ts": 1790712000, "labels": ["X"]}}
        asyncio.run(schedule.run(ctx))
        assert [x["method"] for x in ctx.webhook.sent] == ["POST"]
        assert "estimated from" not in json.dumps(ctx.webhook.sent[0]["payload"], ensure_ascii=False)


# =========================================================================== 1.3.0 (program media)
def test_images_are_upgraded_to_the_full_size_rendition():
    # a tweet photo: X serves `small` unless you ask for the original upload
    assert media.twimg_orig("https://pbs.twimg.com/media/HR70hTAaoAA8Dzz.jpg") == \
        "https://pbs.twimg.com/media/HR70hTAaoAA8Dzz.jpg?name=orig"
    assert media.twimg_orig("https://pbs.twimg.com/media/HR70hTAaoAA8Dzz?format=jpg&name=large") == \
        "https://pbs.twimg.com/media/HR70hTAaoAA8Dzz?format=jpg&name=large"      # already sized — untouched
    # a nitter mirror's pic proxy -> the twimg CDN, then the original
    assert media.upgrade("https://nitter.cf/pic/media%2FHR70hTAaoAA8Dzz.jpg") == \
        "https://pbs.twimg.com/media/HR70hTAaoAA8Dzz.jpg?name=orig"
    # a YouTube thumbnail: 480x360 -> 1280x720, from a watch URL, a short link or a bare id
    assert media.youtube_thumb("https://www.youtube.com/watch?v=ItNs39qvw_w") == \
        "https://i.ytimg.com/vi/ItNs39qvw_w/maxresdefault.jpg"
    assert media.youtube_thumb("ItNs39qvw_w") == "https://i.ytimg.com/vi/ItNs39qvw_w/maxresdefault.jpg"
    assert media.upgrade("https://i.ytimg.com/vi/ItNs39qvw_w/hqdefault.jpg") == \
        "https://i.ytimg.com/vi/ItNs39qvw_w/maxresdefault.jpg"
    # an official article cover is already full size -> never rewritten to another host
    cover = "https://fastcdn.hoyoverse.com/content-v2/hkrpg/166179/c66081f0bbfae6d938b50c41e595c185_1.jpg"
    assert media.upgrade(cover) == cover and media.upgrade("") == ""
    # ranking: livestream artwork first, then a full-size tweet photo, then a cover
    ranked = media.rank([cover, "https://pbs.twimg.com/media/HR70hTAaoAA8Dzz.jpg",
                         "https://i.ytimg.com/vi/ItNs39qvw_w/maxresdefault.jpg", ""])
    assert ranked == ["https://i.ytimg.com/vi/ItNs39qvw_w/maxresdefault.jpg",
                      "https://pbs.twimg.com/media/HR70hTAaoAA8Dzz.jpg?name=orig", cover]
    assert media.rank(None) == [] and media.rank([cover] * 9) == [cover]         # duplicates collapse
    assert len(media.rank([cover.replace("/166179/", f"/{i}/") for i in range(9)])) == 4   # capped at 4


def test_official_news_page_entries_are_parsed():
    page = (FIX / "newspage_hsr_news.html").read_text(encoding="utf-8")
    entries = newspage.parse_news_page(page, "https://hsr.hoyoverse.com/en-us/news")
    assert [e["id"] for e in entries] == ["166181", "166180", "166179", "166100", "166116", "165975"]
    first = entries[0]
    assert first["url"] == "https://hsr.hoyoverse.com/en-us/news/166181"          # relative href resolved
    assert first["title"] == 'Improv Tour Trailer: "It Became Art"'               # site suffix stripped
    assert first["image"].startswith("https://fastcdn.hoyoverse.com/content-v2/hkrpg/166181/")
    assert first["ts"] == 0                                   # a bare M/D/YYYY carries no time — never guessed
    # only the program entry matches, and only for its own version
    prog = [e for e in entries if newspage._matches(GAMES["starrail"], e["title"], "4.6")]
    assert [e["id"] for e in prog] == ["166100"]
    assert newspage._matches(GAMES["starrail"], prog[0]["title"], "4.5") is False
    assert newspage._matches(GAMES["starrail"], entries[2]["title"], "4.6") is False   # a trailer is not it
    # the article page yields the embedded stream -> the 1280x720 artwork
    art = newspage.parse_article((FIX / "newspage_hsr_article.html").read_text(encoding="utf-8"))
    assert art["youtube"] == "https://www.youtube.com/watch?v=ItNs39qvw_w"
    assert "166179" in art["images"][0] and "Tap to unmute" not in art["text"]


def test_hoyolab_program_article_is_picked_not_the_maintenance_notice():
    page = fx("hoyolab_starrail_newslist_program.json")
    hit = hoyolab.pick_program([page], "4.6", GAMES["starrail"].program_patterns)
    assert hit and hit[0] == "46691962"                        # the Special Program preview, not 46814308
    assert hit[2]["subject"] == "Version 4.6 Special Program Preview"
    assert hoyolab.pick_program([page], "4.5", GAMES["starrail"].program_patterns) is None   # other version
    assert hoyolab.pick_program([{"retcode": 1}], "4.6", ["special program"]) is None        # API error page
    assert hoyolab.pick_program([], "4.6", ["special program"]) is None


def test_the_program_announcement_replaces_the_maintenance_notice_on_the_card():
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        notice = hoyolab_item("hoyolab_starrail_46814308_full.json", "starrail")
        ctx = make_ctx(sp, items={"starrail": [notice]}, now=1790000000, BOOTSTRAP_POST=1)
        asyncio.run(schedule.run(ctx))
        # NEW POLICY: a maintenance notice may fill a card but must never open one, so this run
        # posts nothing at all -- it only records what the notice said and flags the lookup.
        assert ctx.webhook.sent == []
        assert schedule.needs_media(ctx.state, "starrail", 1790000000)     # -> worth one lookup
        ctx.state.save()
        # the lookup finds the Special Program preview: link + key art + air time
        found = {"url": "https://www.hoyolab.com/article/46691962",
                 "title": "Version 4.6 Special Program Preview",
                 "images": ["https://pbs.twimg.com/media/HR70hTAaoAA8Dzz.jpg"],
                 "youtube": "https://www.youtube.com/watch?v=EXAMPLE1234",
                 "program_ts": 1789860600, "source": "HoYoLAB"}
        ctx = make_ctx(sp, items={"starrail": [notice]}, now=1790000000)
        ctx.media = {"starrail": {"4.6": found}}
        asyncio.run(schedule.run(ctx))
        # The lookup finds it -- but this programme aired 39 h ago, and an already-aired
        # programme is history, not an announcement. Game-Express was deployed after Genshin 7.1
        # and Wuthering Waves 3.7 had already aired, and must not open cards for them. So the
        # announcement is adopted SILENTLY: the record gains the real link, the real key art and
        # the real air time, and not a single request is made.
        assert ctx.webhook.sent == []
        rec = ctx.state.schedule_records("starrail")["4.6"]
        assert rec["status"] == "tracked"
        assert rec["data"]["title_url"] == "https://www.hoyolab.com/article/46691962"
        assert "HR70hTAaoAA8Dzz" in rec["data"]["images"][0]   # the programme's key art
        assert "upload-os-bbs" not in rec["data"]["images"][0]  # never the notice's cover
        assert rec["data"]["program_ts"] == 1789860600          # the real air time
        assert "46814308" not in rec["data"]["title_url"]       # never the notice's link
        # the title would link the ANNOUNCEMENT, not the stream it happens to mention
        assert rec["data"]["source_url"] == "https://www.hoyolab.com/article/46691962"
        assert rec["data"]["youtube_video"] == "https://www.youtube.com/watch?v=EXAMPLE1234"
        assert rec["data"]["media_from"] == "HoYoLAB"
        assert "program_seen" not in rec["data"]                # the lookup never claims a real post
        assert not schedule.needs_media(ctx.state, "starrail", 1790000000)     # never looked up twice
    # PROGRAM_MEDIA=0 -> the lookup is skipped entirely, so the announcement is never adopted.
    # The notice alone may not open a card, so this run stays silent and keeps asking for the
    # lookup. That is the guard: no programme known, no card -- never a card built on the notice.
    with tempfile.TemporaryDirectory() as tmp:
        sp2 = Path(tmp) / "state.json"
        notice = hoyolab_item("hoyolab_starrail_46814308_full.json", "starrail")
        ctx = make_ctx(sp2, items={"starrail": [notice]}, now=1790000000, PROGRAM_MEDIA=0, BOOTSTRAP_POST=1)
        ctx.media = {"starrail": {"4.6": found}}
        asyncio.run(schedule.run(ctx))
        assert ctx.webhook.sent == []
        rec = ctx.state.schedule_records("starrail")["4.6"]
        assert "program_ts" not in rec["data"] and "media_from" not in rec["data"]
        assert schedule.needs_media(ctx.state, "starrail", 1790000000)


def test_program_media_is_only_looked_up_when_it_is_missing():
    s = settings()
    assert s.program_media and not settings(PROGRAM_MEDIA=0).program_media
    assert GAMES["starrail"].news_url == "https://hsr.hoyoverse.com/en-us/news"
    assert GAMES["genshin"].news_url == "https://genshin.hoyoverse.com/en/news"
    assert GAMES["wuwa"].news_url == "https://wutheringwaves.kurogames.com/en/main/news"
    assert schedule.PRIORITY["news"] < schedule.PRIORITY["hoyolab"]       # an in-feed post still wins
    assert schedule.PRIORITY["news"] > schedule.PRIORITY["x"]
    # no media -> nothing changes, and an empty dict is not an error
    data, prov = {"title_url": "https://x/y"}, {}
    assert schedule.apply_program_media(data, prov, None, 0) == []
    assert schedule.apply_program_media(data, prov, {}, 0) == []
    assert data == {"title_url": "https://x/y"}
    # an absurd air time is dropped, but a past one is kept (it is an official post, not a guess)
    d2 = {}
    schedule.apply_program_media(d2, {}, {"url": "https://x/y", "program_ts": 10 ** 10}, 1790000000)
    assert "program_ts" not in d2
    d3 = {}
    schedule.apply_program_media(d3, {}, {"url": "https://x/y", "program_ts": 1789860600}, 1790000000)
    assert d3["program_ts"] == 1789860600
    # an existing official program time is never overwritten by the lookup
    d4 = {"program_ts": 1789860600}
    schedule.apply_program_media(d4, {}, {"url": "https://x/y", "program_ts": 1780000000}, 1790000000)
    assert d4["program_ts"] == 1789860600


 # =========================================================================== runner
def test_the_program_lookup_falls_back_past_x_to_hoyolab_then_the_news_page():
    """runner.find_program is the ONE lookup, X first (v1.8.0). Without an X client the backups
    run in their proven order: the HoYoLAB news list, then the official news page (which
    archives every announcement and carries the key art), then the feed mirror."""
    from gamexpress.runner import find_program

    class News:
        def __init__(self, html):
            self.html, self.urls = html, []

        async def get_json(self, url, **k):
            return None                                    # HoYoLAB down -> the next backup runs

        async def get_text(self, url, **k):
            self.urls.append(url)
            last = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            return ((FIX / "newspage_hsr_article.html").read_text(encoding="utf-8")
                    if last.isdigit() else self.html)

    def ctx_of(fetcher, tmp):
        ctx = make_ctx(Path(tmp) / "s.json", now=1790000000)   # make_ctx has no X client (x=None)
        ctx.fetcher = fetcher
        return ctx

    f = News((FIX / "newspage_hsr_news.html").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        hit = asyncio.run(find_program(ctx_of(f, tmp), GAMES["starrail"], "4.6"))
    assert hit["url"] == "https://hsr.hoyoverse.com/en-us/news/166100"      # the Special Program…
    assert "46814308" not in hit["url"]                                     # …not the maintenance notice
    assert hit["images"][0] == "https://i.ytimg.com/vi/ItNs39qvw_w/maxresdefault.jpg"   # 1280x720
    assert hit["source"] == "Official News"
    assert f.urls[0].endswith("?type=notice") and len(f.urls) == 2          # first tab wins -> 2 fetches
    # HoYoLAB answers -> it wins and the news page is never even fetched
    calls = []

    async def fake_list(fetcher, game, version, **k):
        calls.append(version)
        return {"url": "https://www.hoyolab.com/article/46691962",
                "title": "Version 4.6 Special Program Preview",
                "images": ["https://pbs.twimg.com/media/AAA.jpg"], "text": "", "ts": 1789000000}

    old = hoyolab.find_program
    hoyolab.find_program = fake_list
    try:
        f2 = News((FIX / "newspage_hsr_news.html").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            hit2 = asyncio.run(find_program(ctx_of(f2, tmp), GAMES["starrail"], "4.6"))
    finally:
        hoyolab.find_program = old
    assert calls == ["4.6"] and hit2["url"].endswith("46691962")
    assert hit2["images"] == ["https://pbs.twimg.com/media/AAA.jpg"]
    assert f2.urls == []                                    # the news page was never asked


def test_sample_cards_are_only_for_the_games_with_nothing_real_to_fetch():
    """The codes test posts the REAL codes of the live games, so the sample cards are kept for
    the games that are not out yet only — a sample must never stand in for a real source."""
    from gamexpress.__main__ import sample_payloads

    def names(**kw):
        return [n for _f, n, _p in sample_payloads("codes", **kw)]

    assert set(names()) >= {"codes_genshin", "codes_starrail", "codes_zzz", "codes_wuwa",
                            "codes_hna", "codes_ananta"}
    assert names(unlaunched=True) == ["codes_hna", "codes_ananta"]       # the unreleased games only
    assert names(unlaunched=True, game="ananta") == ["codes_ananta"]
    assert names(game="genshin") == ["codes_genshin"]          # one card per game, all its codes


# ================================================== the 2026-09-26 schedule-card fixes
def _it(source="hoyolab", game="zzz", title="", text="", url="https://x/y", ts=1790000000, imgs=None):
    return Item(source=source, game=game, id="1", url=url, title=title, text=text,
                published_ts=ts, images=list(imgs or []))


def test_the_program_lookup_runs_on_a_first_run_when_the_state_is_empty():
    now = 1790000000
    assert schedule.needs_program_lookup([], {}, now)
    seen = [schedule.Extract(_it(), "program", "3.2")]
    assert not schedule.needs_program_lookup(seen, {}, now)
    assert not schedule.needs_program_lookup([], {"data": {"program_seen": True}}, now)
    assert not schedule.needs_program_lookup([], {"data": {"media_from": "Official News"}}, now)
    assert not schedule.needs_program_lookup([], {"data": {"maint_start_ts": now - 13 * 3600}}, now)
    assert callable(schedule.version_extracts)


def test_an_event_post_is_never_mistaken_for_the_program_announcement():
    prize = _it(title="[Prize Event] Participate in the event for a chance to obtain Master Tape x10!",
                text="The Zenless Zone Zero Version 3.2 Special Program will air on August 28.")
    assert schedule.classify(GAMES["zzz"], prize) != "program"
    notice = _it(title="Version 4.6 Update and Maintenance Notice",
                 text="The Honkai: Star Rail Version 4.6 Special Program aired on 2026-09-20.")
    assert schedule.classify(GAMES["starrail"], notice) != "program"
    for key, title in (("zzz", 'Zenless Zone Zero Version 3.2 "Their Secret Histories" Special Program Announcement'),
                       ("starrail", 'Honkai: Star Rail Version 4.6 "Dance With the Beast Before Moonrise" Special Program'),
                       ("genshin", "Genshin Impact Version 7.1 Special Program Preview"),
                       ("wuwa", "Wuthering Waves Version 3.7 Preview Special Broadcast")):
        assert schedule.classify(GAMES[key], _it(title=title, text=title)) == "program", key
    assert not newspage._matches(GAMES["zzz"], "[Prize Event] Participate in the event", "3.2")
    assert newspage._matches(GAMES["zzz"], "Version 3.2 Special Program Announcement", "3.2")
    assert hoyolab.pick_program([{"retcode": 0, "data": {"list": [
        {"post": {"post_id": "46814308", "subject": "Version 4.6 Update and Maintenance Notice", "created_at": 1790300000}},
        {"post": {"post_id": "46691962", "subject": 'Honkai: Star Rail Version 4.6 "Dance With the Beast Before Moonrise" Special Program', "created_at": 1789100000}},
    ]}}], "4.6", ["special program"])[0] == "46691962"


def test_a_livestream_link_yields_a_real_thumbnail():
    assert media.youtube_thumb("https://youtube.com/live/drFgtruoPe8") == \
        "https://i.ytimg.com/vi/drFgtruoPe8/maxresdefault.jpg"
    assert media.youtube_thumb("https://youtube.com/live/nMa_e5ChL6w?feature=share") == \
        "https://i.ytimg.com/vi/nMa_e5ChL6w/maxresdefault.jpg"
    assert media.youtube_thumb("ItNs39qvw_w") == "https://i.ytimg.com/vi/ItNs39qvw_w/maxresdefault.jpg"
    assert media.youtube_thumb("not a video") == "" and media.youtube_thumb("") == ""
    assert newspage.YOUTUBE.search("on youtube.com/live/drFgtruoPe8 tonight").group(1) == "drFgtruoPe8"


def test_the_same_key_art_at_two_sizes_is_one_image():
    small = "https://upload-os-bbs.hoyolab.com/upload/community/2026/08/28/e81786764983f1b06b437847ce9f1569.jpg"
    big = "https://upload-os-bbs.hoyolab.com/upload/community/2026/08/28/d65d59264a6f377c0683cd3323c6b006.jpg"
    got = hoyolab._images({"image_list": [{"url": small, "width": 1200, "height": 675},
                                          {"url": big, "width": 1920, "height": 1080}]})
    assert got == [big]
    other = "https://upload-os-bbs.hoyolab.com/upload/community/2026/08/28/other.jpg"
    assert hoyolab._images({"image_list": [{"url": big, "width": 1920, "height": 1080},
                                           {"url": other, "width": 1080, "height": 1080}]}) == [big, other]
    assert hoyolab._images({"image_list": [{"url": small}, {"url": big}]}) == [small, big]
    assert media.rank([small + "?x-oss-process=image/resize,s_600", small]) == [small]


def test_the_maintenance_block_has_no_blank_line_between_pre_install_and_maintenance():
    d = {"preinstall_ts": 1790560800, "maint_start_ts": 1790712000, "maint_end_ts": 1790737200,
         "compensation": "Astrite x300, Crystal Solvent x2"}
    block = cards.maintenance_block(GAMES["wuwa"], d)
    assert "✦ Pre-Install: <t:1790560800:F>\n✦ Maintenance: <t:1790712000:f> to <t:1790737200:t>" in block
    assert "\n\n✦ Maintenance" not in block


def test_a_news_feed_mirror_finds_the_announcement():
    body = (FIX / "ww_articles_latest.xml").read_text(encoding="utf-8")
    entries = newspage.parse_feed_entries(body)
    assert [e["title"] for e in entries] == ["About Tiered Client Resource Downloads",
                                             "Version 3.7 Update Maintenance Notice",
                                             "Version 3.7 Preview Special Broadcast"]
    assert GAMES["wuwa"].program_feeds[0].endswith("articles_latest.xml")

    class Feed:
        async def get_text(self, url, **k):
            return body if url.endswith("articles_latest.xml") else ""

    hit = asyncio.run(newspage.fetch_program_feed(Feed(), GAMES["wuwa"], "3.7", 1790000000))
    assert hit["url"].endswith("/5441") and hit["title"] == "Version 3.7 Preview Special Broadcast"
    assert hit["images"] == ["https://hw-media-cdn-mingchao.kurogame.com/akiwebsite/website2.0/images/1789182000000/broadcast-1789182000.jpeg"]
    assert hit["youtube"] == "https://www.youtube.com/watch?v=nMa_e5ChL6w"


def test_the_tweet_data_chain_tries_fxtwitter_then_fixupx_then_vxtwitter():
    from gamexpress.sources.twitter import FX_SOURCES, VXTWITTER_URL, XClient
    assert [n for n, _ in FX_SOURCES] == ["fxtwitter", "fixupx"]
    assert VXTWITTER_URL == "https://api.vxtwitter.com/Twitter/status/{id}"
    fx_body = {"tweet": {"id": "1", "text": "hello", "created_timestamp": 5,
                          "media": {"photos": []}, "author": {"screen_name": "ZZZ_EN"},
                          "url": "https://x.com/ZZZ_EN/status/1"}}
    vx_body = {"tweetID": "1", "text": "hello", "date_epoch": 5, "mediaURLs": [],
               "user_screen_name": "ZZZ_EN", "tweetURL": "https://x.com/ZZZ_EN/status/1"}
    class F:
        def __init__(self, ok): self.ok, self.calls = set(ok), []
        async def get_json(self, url, source="", **k):
            self.calls.append(source)
            if source not in self.ok:
                return None
            return fx_body if source in ("fxtwitter", "fixupx") else vx_body
    st = settings()
    f = F(["fxtwitter"])
    c = XClient(f, st)
    assert asyncio.run(c.tweet("1"))["author"] == "ZZZ_EN" and f.calls == ["fxtwitter"]
    f = F(["fixupx"])
    c = XClient(f, st)
    asyncio.run(c.tweet("1"))
    assert f.calls == ["fxtwitter", "fixupx"] and c.source_used == {"fixupx": 1}
    f = F(["vxtwitter"])
    c = XClient(f, st)
    asyncio.run(c.tweet("1"))
    assert f.calls == ["fxtwitter", "fixupx", "vxtwitter"] and c.source_used == {"vxtwitter": 1}


def test_the_program_lookup_runs_end_to_end_on_a_fresh_state():
    """v1.8.0: on an empty state the card is built from the real announcement TWEET (seed file ->
    fxtwitter by id), not from a page — the whole point of the X-first change."""
    from gamexpress import runner
    notice = Item(source="hoyolab", game="starrail", id="46814308",
                  url="https://www.hoyolab.com/article/46814308",
                  title="Version 4.6 Update and Maintenance Notice",
                  text="The Version 4.6 pre-installation will begin at 2026-09-24 14:00 (UTC+8). Version update maintenance on 2026-09-28 06:00 (UTC+8) for 5 hours.",
                  published_ts=1790300000, images=[])
    with tempfile.TemporaryDirectory() as tmp:
        ctx, _f = _x_ctx(Path(tmp) / "state.json",
                         {"2099440781115211916": "fx_starrail_4_6_program.json"}, now=1790400000)
        ctx.items = {"starrail": [notice]}
        assert ctx.state.schedule_records("starrail") == {}            # a first run: nothing stored
        asyncio.run(runner.gather_program_media(ctx))
        hit = (ctx.media.get("starrail") or {}).get("4.6")
        assert hit, "the lookup did not run on a fresh state"
        assert hit["source"] == "x"
        assert hit["url"] == "https://x.com/honkaistarrail/status/2099440781115211916"
        assert "46814308" not in hit["url"]
        assert "https://pbs.twimg.com/media/HSK2Q2pXsAA4YJA.jpg?name=orig" in hit["images"]
        assert hit["program_ts"] == 1789903800
        d = schedule.merge(GAMES["starrail"], "4.6",
                           schedule.version_extracts(ctx, GAMES["starrail"])["4.6"],
                           {}, {}, {}, ctx.now, [], None, hit)
        # the title links the ANNOUNCEMENT (the tweet), not the livestream it mentions
        assert d["title_url"] == "https://x.com/honkaistarrail/status/2099440781115211916"
        assert d["source_url"] == "https://x.com/honkaistarrail/status/2099440781115211916"
        assert d["youtube_video"] == "https://www.youtube.com/watch?v=drFgtruoPe8"

# --------------------------------------------------------------- X-first schedule (v1.8.0)
# The 2026-09-27 run got all four games wrong: GI/HSR/ZZZ reported "no program announcement
# found" and WW linked a lore article. X is now the primary lookup, so these run against the
# four REAL api.fxtwitter.com responses captured on 2026-09-27, not example data.
REAL_PROGRAMS = {
    "genshin": ("7.1", "fx_genshin_7_1_program.json", "2096810691021689205",
                "https://pbs.twimg.com/media/HRlONCqXcAUhgGD.jpg?name=orig", 1789214400),
    "starrail": ("4.6", "fx_starrail_4_6_program.json", "2099440781115211916",
                 "https://pbs.twimg.com/media/HSK2Q2pXsAA4YJA.jpg?name=orig", 1789903800),
    "zzz": ("3.2", "fx_zzz_3_2_program.json", "2091737263398862915",
            "https://pbs.twimg.com/media/HQZWs-3WAAEj32y.jpg?name=orig", 1787916600),
    "wuwa": ("3.7", "fx_wuwa_3_7_broadcast.json", "2098607530780021002",
             "https://pbs.twimg.com/media/HR70hTAaoAA8Dzz.jpg?name=orig", 1789815600),
}


class SeedFetcher:
    """Serves api.fxtwitter.com/status/<id> from the captured fixtures, and records every other
    source that gets asked — so a test can prove the backups were never reached."""

    def __init__(self, ids):
        self.bodies = {i: fx(f) for i, f in ids.items()}
        self.asked: list[str] = []
        self.health: dict = {}

    async def get_json(self, url, source="", **kw):
        self.asked.append(source or url)
        import re as _re
        m = _re.search(r"/status/(\d+)", url)
        return self.bodies.get(m.group(1)) if m else None

    async def get_text(self, url, source="", **kw):
        self.asked.append(source or url)
        return ""


def _x_ctx(state_path, ids, now=1789300000):
    from gamexpress.sources.twitter import XClient
    f = SeedFetcher(ids)
    ctx = make_ctx(state_path, now=now)
    ctx.fetcher, ctx.x = f, XClient(f, ctx.settings)
    return ctx, f


def test_the_program_lookup_is_x_first_for_every_live_game():
    """All four real 2026-09 announcements resolve to the tweet itself — its URL, its `?name=orig`
    key art and its air time — and no backup source is consulted at all."""
    from gamexpress.runner import find_program
    ids = {tid: f for _, f, tid, _, _ in REAL_PROGRAMS.values()}
    with tempfile.TemporaryDirectory() as tmp:
        ctx, f = _x_ctx(Path(tmp) / "s.json", ids)
        for key, (ver, _, tid, img, ts) in REAL_PROGRAMS.items():
            hit = asyncio.run(find_program(ctx, GAMES[key], ver))
            assert hit, f"{key} {ver}: X-first lookup found nothing"
            assert hit["source"] == "x", f"{key} {ver}: fell back to {hit.get('source')}"
            assert hit["url"].endswith(f"/status/{tid}"), f"{key} {ver}: wrong link {hit['url']}"
            assert img in hit["images"], f"{key} {ver}: key art missing from {hit['images']}"
            assert hit["program_ts"] == ts, f"{key} {ver}: air time {hit['program_ts']} != {ts}"
        assert not [a for a in f.asked if a in ("hoyolab", "newspage")], \
            f"a backup source was asked: {f.asked}"


def test_the_seeded_tweet_makes_a_fresh_test_run_show_real_data():
    """`mode=test` starts from an empty state every run, and a nitter timeline only reaches back a
    few days — so the announcement id lives in config/program_announcements.json. That is what
    lets a test card show the real link and the real key art instead of TBA."""
    from gamexpress.runner import find_program
    from gamexpress.sources import twitter
    for key, (ver, _, tid, img, _) in REAL_PROGRAMS.items():
        seed = twitter.program_seed(key, ver)
        assert seed.get("id") == tid, f"{key} {ver} is not seeded with {tid} (got {seed.get('id')})"
        assert seed.get("url", "").endswith(f"/status/{tid}"), f"{key} {ver}: bad seeded url"
        assert seed.get("image") == img, f"{key} {ver}: seeded art is not the ?name=orig one"
    ids = {tid: f for _, f, tid, _, _ in REAL_PROGRAMS.values()}
    with tempfile.TemporaryDirectory() as tmp:                 # an EMPTY state, as a test run has
        ctx, _ = _x_ctx(Path(tmp) / "fresh.json", ids)
        for key, (ver, _, _, _, _) in REAL_PROGRAMS.items():
            hit = asyncio.run(find_program(ctx, GAMES[key], ver))
            assert hit and hit["images"], f"{key} {ver}: a fresh run produced no image"


def test_a_lore_article_is_never_the_program_announcement():
    """The exact post that put the wrong link on the WW card on 2026-09-27: it contains the words
    "Special Program" and is a story chapter. An announcement states WHEN it airs; this does not."""
    lore = ("Insider Channel: Special Program | Signs of Imprisonment: Part Three\nQiuyuan once "
            "investigated a medicinal-herb corruption case at Mingting's order during his service "
            "as the senior agent of the Internal Security Agency.")
    assert schedule.is_program_announcement(GAMES["wuwa"], lore) is False
    assert schedule.NOT_PROGRAM_TITLE.search("Insider Channel: Special Program")
    # ...while the real one still passes, giveaway clause and all
    real = fx("fx_wuwa_3_7_broadcast.json")["tweet"]["text"]
    assert schedule.is_program_announcement(GAMES["wuwa"], real) is True


def test_a_giveaway_inside_the_announcement_does_not_veto_it():
    """Three of the four real announcements mention codes or prizes in the BODY. Only the headline
    is screened, or every one of them would be rejected."""
    for key, (_, fixture, _, _, _) in REAL_PROGRAMS.items():
        text = fx(fixture)["tweet"]["text"]
        assert schedule.is_program_announcement(GAMES[key], text), f"{key}: real one rejected"


def test_a_stitched_vertical_strip_is_not_key_art():
    """The 2026-09-27 WW card carried a 1440x29482 image — a whole article as one vertical strip,
    which Discord renders as an unreadable sliver."""
    from gamexpress.media import sane_aspect
    assert sane_aspect(1440, 29482) is False
    assert sane_aspect(1920, 1080) is True          # HSR / ZZZ / WW key art
    assert sane_aspect(1200, 675) is True           # GI key art
    assert sane_aspect(None, None) is True          # most sources report no size at all


def test_a_timeline_scan_finds_a_new_announcement_and_seeds_it():
    """Discovery half: when the announcement is still new enough to be in the nitter timeline it is
    matched there and written back to the seed file for the runs that come later."""
    import shutil

    from gamexpress.sources import twitter
    entry = {"id": "9", "account": "GenshinImpact",
             "url": "https://x.com/GenshinImpact/status/9", "posted_ts": 1, "image": "i"}
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "backup.json"
        shutil.copy2(twitter.SEED_PATH, backup)
        try:
            assert twitter.save_program_seed("genshin", "9.9", entry) is True
            assert twitter.program_seed("genshin", "9.9")["id"] == "9"
            assert twitter.save_program_seed("genshin", "9.9", entry) is False   # idempotent
        finally:
            shutil.copy2(backup, twitter.SEED_PATH)
            twitter._seeds = None
    assert twitter.program_seed("genshin", "9.9") == {}          # the seed file is back as it was


# =========================================================================== 1.3.0 (card cleanup + banner feed)
def test_no_x_button_and_no_key_art_or_source_line_on_the_schedule_card():
    """Card cleanup:
      * the 'x' button is gone (it only ever duplicated what's already in the card's title);
      * the '🖼️ key art: ...' footer line is gone;
      * the 'Source: ...' footer line is gone;
      * the legend remains: STC — Subject to Change • TBA — To be Announced."""
    s = settings()
    d = dict(SCHEDULE_SAMPLES["starrail"])
    d["source_url"] = "https://x.com/HonkaiStarRail/status/2099440781115211916"
    d["source_label"] = "X Post"
    d["media_from"] = "HoYoLAB"
    p = cards.schedule_payload(GAMES["starrail"], d, s, s.ping("schedule", "starrail"))
    flat = json.dumps(p, ensure_ascii=False)
    assert "https://x.com/HonkaiStarRail/status/" not in flat
    assert "🖼️" not in flat
    assert "Source:" not in flat
    assert "-# STC — Subject to Change • TBA — To be Announced" in flat


def test_a_non_x_source_still_gets_its_own_button():
    """Non-X sources (like HoYoLAB or the official news page) still get their own button when the
    title points elsewhere (e.g. YouTube stream), because they carry genuine extra information."""
    s = settings()
    d = dict(SCHEDULE_SAMPLES["starrail"])
    d["title_url"] = "https://www.youtube.com/watch?v=drFgtruoPe8"
    d["source_url"] = "https://www.hoyolab.com/article/46814308"
    d["source_label"] = "HoYoLAB"
    p = cards.schedule_payload(GAMES["starrail"], d, s, s.ping("schedule", "starrail"))
    btn_urls = [c["url"] for comp in p["components"][0]["components"] if comp["type"] == 1
                for c in comp.get("components", [])]
    assert "https://www.hoyolab.com/article/46814308" in btn_urls


def test_the_banner_feed_fills_the_lineup_no_official_post_ever_listed():
    """Banner feed (hub.json): fills empty 5★ phases at PRIORITY['bannerfeed'] = 5."""
    from gamexpress.sources import bannerfeed
    data = json.loads((FIX / "bannerfeed_hub_trimmed.json").read_text(encoding="utf-8"))
    feed = bannerfeed.parse_hub(data, now=1790452566)
    # HSR 4.6 (maint_end_ts: 1790564400)
    hsr_lineup = bannerfeed.banner_feed_for(feed["starrail"], 1790564400)
    assert set(hsr_lineup["phase1"]) == {"Evanescia", "Pearl"}
    assert hsr_lineup["phase2"] == ["Mortenax Blade"]

    # merge into HSR 4.6 card data
    prov: dict = {}
    record: dict = {"data": {"version": "4.6", "maint_end_ts": 1790564400}, "prov": prov}
    merged = schedule.merge(GAMES["starrail"], "4.6", [], record, {}, {}, 1790452566,
                            banner_feed=feed["starrail"])
    banners = merged.get("banners") or {}
    assert set(banners.get("phase1") or []) == {"Evanescia", "Pearl"}
    assert banners.get("phase2") == ["Mortenax Blade"]
    assert record["prov"]["b_phase1"][0] == 5
    assert record["prov"]["b_phase2"][0] == 5

    # Genshin 7.1 (maint_end_ts: 1790132400)
    gi_lineup = bannerfeed.banner_feed_for(feed["genshin"], 1790132400)
    assert set(gi_lineup["phase1"]) == {"Vodyanitsa", "Vesna"}
    assert "phase2" not in gi_lineup


def test_the_banner_feed_never_invents_a_lineup_for_the_wrong_version():
    """If the version has no release/maintenance timestamp, or one that doesn't match the feed,
    the banners stay empty / TBA."""
    from gamexpress.sources import bannerfeed
    data = json.loads((FIX / "bannerfeed_hub_trimmed.json").read_text(encoding="utf-8"))
    feed = bannerfeed.parse_hub(data, now=1790452566)
    lineup = bannerfeed.banner_feed_for(feed["starrail"], 1999999999)
    assert "phase1" not in lineup and "phase2" not in lineup   # titles may ride along

    record: dict = {"data": {"version": "9.9", "maint_end_ts": 1999999999}, "prov": {}}
    merged = schedule.merge(GAMES["starrail"], "9.9", [], record, {}, {}, 1790452566,
                            banner_feed=feed["starrail"])
    banners = merged.get("banners") or {}
    assert not banners.get("phase1") and not banners.get("phase2")


def test_a_stale_banner_feed_is_ignored_not_served():
    """A feed payload older than 14 days is refused rather than serving last month's lineup."""
    from gamexpress.sources import bannerfeed
    data = json.loads((FIX / "bannerfeed_hub_trimmed.json").read_text(encoding="utf-8"))
    stale = bannerfeed.parse_hub(data, now=1790452566 + 15 * 86400)
    assert stale == {}


def test_the_banner_feed_never_overrides_an_official_lineup():
    """PRIORITY['bannerfeed'] = 5 (lowest). An official post (priority 40/50) or override (100)
    beats the feed and is never overwritten."""
    from gamexpress.sources import bannerfeed
    data = json.loads((FIX / "bannerfeed_hub_trimmed.json").read_text(encoding="utf-8"))
    feed = bannerfeed.parse_hub(data, now=1790452566)
    prov = {"b_phase1": [50, 1790000000]}
    record = {"data": {"version": "4.6", "maint_end_ts": 1790564400, "banners": {"phase1": ["OfficialHero"]}},
              "prov": prov}
    merged = schedule.merge(GAMES["starrail"], "4.6", [], record, {}, {}, 1790452566,
                            banner_feed=feed["starrail"])
    assert merged["banners"]["phase1"] == ["OfficialHero"]


def test_banner_feed_can_be_switched_off():
    """BANNER_FEED=0 switches the fill-in off entirely."""
    s = settings(BANNER_FEED="0")
    assert s.banner_feed is False
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        ctx = make_ctx(sp, BANNER_FEED=0)
        assert ctx.settings.banner_feed is False


# ================================================== 1.5/1.6 (an unresolvable id + learned pre-install lead)
UNKNOWN_MESSAGE = '{"message": "Unknown Message", "code": 10008}'


class ScriptedWebhook:
    def __init__(self, edits: list[SendResult], sends: list[SendResult] | None = None):
        self.edit_results = list(edits)
        self.send_results = list(sends or [])
        self.edits: list[tuple[str, dict]] = []
        self.sends: list[dict] = []

    async def edit(self, webhook: str, message_id: str, payload: dict) -> SendResult:
        self.edits.append((message_id, payload))
        return self.edit_results.pop(0)

    async def send(self, webhook: str, payload: dict) -> SendResult:
        self.sends.append(payload)
        return self.send_results.pop(0)


# The real HSR 4.6 air time. At the now=1790450000 these tests use, that program aired six days
# earlier, so 4.6 is a SETTLED version -- exactly the case a live run must leave alone.
HSR_46_AIRED = 1789903800


def _posted_hsr_record(message_id: str = "dead-message",
                       program_ts: int = HSR_46_AIRED) -> dict:
    data = dict(SCHEDULE_SAMPLES["starrail"])
    data["program_ts"] = program_ts
    return {"status": "posted", "first_seen": 1790400000,
            "data": data, "prov": {},
            "message_id": message_id, "webhook_fp": webhook_fingerprint(HOOK),
            "payload_hash": "old-payload"}


def test_a_deleted_card_is_reposted_and_adopts_the_new_id():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790450000)
        ctx.webhook = ScriptedWebhook(
            [SendResult(False, 404, error=UNKNOWN_MESSAGE), SendResult(True, 200)],
            [SendResult(True, 200, message_id="replacement-message")])
        # program still inside the 36 h news window -> recovery must still repost
        records = {"4.6": _posted_hsr_record(program_ts=1790440000)}
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×301"}, {}, True))
        assert ctx.errors == [] and len(ctx.webhook.sends) == 1
        assert records["4.6"]["message_id"] == "replacement-message"
        assert any("10008" in line for line in ctx.report)

        # A later data change edits the adopted id; it does not PATCH the dead id forever or repost again.
        ctx.now += 600
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×302"}, {}, True))
        assert [message_id for message_id, _ in ctx.webhook.edits] == [
            "dead-message", "replacement-message"]
        assert len(ctx.webhook.sends) == 1 and ctx.errors == []


def test_a_deleted_card_whose_repost_fails_still_reports_an_error():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790450000)
        ctx.webhook = ScriptedWebhook(
            [SendResult(False, 404, error=UNKNOWN_MESSAGE)],
            [SendResult(False, 503, error="upstream unavailable")])
        records = {"4.6": _posted_hsr_record(program_ts=1790440000)}   # not settled yet
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×301"}, {}, True))
        assert len(ctx.webhook.sends) == 1 and len(ctx.errors) == 1
        assert "repost failed (503)" in ctx.errors[0] and "10008" in ctx.errors[0]
        assert records["4.6"]["message_id"] == "dead-message"   # retry recovery next run
        assert records["4.6"]["payload_hash"] == "old-payload"


def test_a_non_404_edit_failure_is_still_an_error_not_a_repost():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790450000)
        ctx.webhook = ScriptedWebhook([SendResult(False, 400, error="bad payload")])
        records = {"4.6": _posted_hsr_record()}
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×301"}, {}, True))
        assert ctx.webhook.sends == [] and ctx.errors == ["HSR 4.6: edit failed (400) bad payload"]
        assert records["4.6"]["message_id"] == "dead-message"


# ------------------------------------------------------- schedule card fan-out (game channel)
# The second channel a schedule card is copied into: #wuwa-news next to #schedule.
MIRROR = "https://discord.com/api/webhooks/987654321098765432/game-channel-TOK"


class FanOutWebhook:
    """Routes by destination so a test can assert WHICH channel received WHAT.

    The dry-run client records the fingerprint but always succeeds; ScriptedWebhook can fail
    on cue but ignores the URL. Fan-out needs both at once.
    """

    def __init__(self, fail_fp: str | None = None, status: int = 500):
        self.calls: list[tuple[str, str, dict]] = []      # (method, destination fp, payload)
        self.fail_fp, self.status, self._n = fail_fp, status, 0

    def to(self, url: str) -> list[tuple[str, dict]]:
        fp = webhook_fingerprint(url)
        return [(m, p) for m, f, p in self.calls if f == fp]

    def _result(self, fp: str, message_id: str | None) -> SendResult:
        if fp == self.fail_fp:
            return SendResult(False, self.status, error="channel is gone")
        return SendResult(True, 200, message_id=message_id)

    async def send(self, webhook: str, payload: dict) -> SendResult:
        fp = webhook_fingerprint(webhook)
        self.calls.append(("POST", fp, payload))
        self._n += 1
        return self._result(fp, f"msg-{self._n}")

    async def edit(self, webhook: str, message_id: str, payload: dict) -> SendResult:
        fp = webhook_fingerprint(webhook)
        self.calls.append(("PATCH", fp, payload))
        return self._result(fp, message_id)


def _wuwa_ctx(sp, webhook, **env):
    ctx = make_ctx(sp, items={"wuwa": [item_from_fx_json("wuwa", fx("fx_wuwa_3_7_broadcast.json"))]},
                   **env)
    ctx.webhook = webhook
    return ctx


def _card(payload: dict) -> dict:
    return payload["components"][0]          # the container — a card has no line above it


def _unpinged(payload: dict) -> dict:
    """The card with the mention stripped off its legend line — i.e. everything the two
    channels must still have in common now that the ping lives inside the container."""
    head = "-# STC — Subject to Change • TBA — To be Announced"
    card = json.loads(json.dumps(_card(payload)))            # deep copy, no extra import
    for c in card.get("components", []):
        if str(c.get("content", "")).startswith(head):
            c["content"] = head
    return card


def _legend(payload: dict) -> str:
    """The legend footer line. A schedule card carries its mention at the end of this line,
    inside the container, so the post is one block instead of a bare @role above a card."""
    return next(c["content"] for c in _card(payload)["components"]
                if str(c.get("content", "")).startswith("-# STC —"))


def test_a_schedule_card_is_delivered_to_both_channels_in_one_pass():
    """Same data, same run — the copy can never lag or differ, except for the ping."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _wuwa_ctx(Path(tmp) / "state.json", FanOutWebhook(), BOOTSTRAP_POST="1",
                        DISCORD_WEBHOOK_SCHEDULE_MIRROR_WUWA=MIRROR)
        asyncio.run(schedule.run(ctx))
        primary, copy = ctx.webhook.to(HOOK), ctx.webhook.to(MIRROR)
        assert [m for m, _ in primary] == ["POST"] and [m for m, _ in copy] == ["POST"]
        # Identical apart from the mention, which only the schedule channel carries.
        assert _unpinged(primary[0][1]) == _unpinged(copy[0][1])
        assert ctx.errors == []


def test_the_game_channel_copy_never_pings():
    """One announcement must not notify the role twice. The schedule channel is the only
    place the role is mentioned; the copy carries neither the mention nor the permission."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _wuwa_ctx(Path(tmp) / "state.json", FanOutWebhook(), BOOTSTRAP_POST="1",
                        DISCORD_WEBHOOK_SCHEDULE_MIRROR_WUWA=MIRROR)
        asyncio.run(schedule.run(ctx))
        primary, copy = ctx.webhook.to(HOOK)[0][1], ctx.webhook.to(MIRROR)[0][1]

        # The mention sits at the end of the legend line, inside the card.
        assert _legend(primary).endswith("To be Announced <@&1296268365593186426>")
        assert primary["allowed_mentions"] == {"parse": [], "roles": ["1296268365593186426"]}

        # No mention text at all, so the copy does not render a dead blue @role pill either.
        assert _legend(copy) == "-# STC — Subject to Change • TBA — To be Announced"
        assert "<@&" not in json.dumps(copy, ensure_ascii=False)
        assert copy["allowed_mentions"] == {"parse": []}


def test_without_the_mirror_secret_exactly_one_card_is_sent():
    """Guards the deliberate missing fallback: BASE_ENV sets DISCORD_WEBHOOK_URL, so a mirror
    that fell through to the catch-all like webhook_source() does would double-post here."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _wuwa_ctx(Path(tmp) / "state.json", FanOutWebhook(), BOOTSTRAP_POST="1")
        asyncio.run(schedule.run(ctx))
        assert len(ctx.webhook.calls) == 1 and ctx.webhook.to(HOOK)


def test_a_mirror_added_later_backfills_the_card_already_posted():
    """The card ZZZ 3.3 is in right now: posted days ago, unchanged since. Adding the secret
    has to copy it across without disturbing the original."""
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        ctx = _wuwa_ctx(sp, FanOutWebhook(), BOOTSTRAP_POST="1")
        asyncio.run(schedule.run(ctx))
        assert [m for m, _ in ctx.webhook.to(HOOK)] == ["POST"]
        ctx.state.save()

        ctx = _wuwa_ctx(sp, FanOutWebhook(), now=1789303600,
                        DISCORD_WEBHOOK_SCHEDULE_MIRROR_WUWA=MIRROR)
        asyncio.run(schedule.run(ctx))
        assert ctx.webhook.to(HOOK) == []                       # original left alone
        assert [m for m, _ in ctx.webhook.to(MIRROR)] == ["POST"]
        assert ctx.errors == []


def test_both_copies_are_edited_when_the_card_changes():
    """Without this the copy would sit in the game channel showing TBA and guessed times."""
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        env = {"DISCORD_WEBHOOK_SCHEDULE_MIRROR_WUWA": MIRROR}
        ctx = _wuwa_ctx(sp, FanOutWebhook(), BOOTSTRAP_POST="1", **env)
        asyncio.run(schedule.run(ctx))
        ctx.state.save()

        notice = Item("kuro", "wuwa", "9001",
                      "https://wutheringwaves.kurogames.com/en/main/news/detail/9001",
                      "Version 3.7 Update Maintenance Notice",
                      "Version 3.7 pre-download will begin at 2026/09/28 10:00 (UTC+8).\n"
                      "Maintenance Time: 2026/09/30 04:00 - 11:00 (UTC+8)\nCompensation: Astrite ×300",
                      1790000000)
        ctx = make_ctx(sp, items={"wuwa": [item_from_fx_json("wuwa", fx("fx_wuwa_3_7_broadcast.json")),
                                           notice]}, now=1790001000, **env)
        ctx.webhook = FanOutWebhook()
        asyncio.run(schedule.run(ctx))
        primary, copy = ctx.webhook.to(HOOK), ctx.webhook.to(MIRROR)
        assert [m for m, _ in primary] == ["PATCH"] and [m for m, _ in copy] == ["PATCH"]
        # Identical card bodies, including the "Updated …" footer an edit adds — the copy must
        # not be a footer-less near-miss of the real card.
        assert _unpinged(primary[0][1]) == _unpinged(copy[0][1])
        assert "Astrite ×300" in json.dumps(copy[0][1], ensure_ascii=False)
        assert "<@&" not in json.dumps(copy[0][1], ensure_ascii=False)   # still silent on edit
        assert ctx.errors == []


def test_a_mirror_pointed_at_the_schedule_channel_sends_one_card_not_two():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _wuwa_ctx(Path(tmp) / "state.json", FanOutWebhook(), BOOTSTRAP_POST="1",
                        DISCORD_WEBHOOK_SCHEDULE_MIRROR_WUWA=HOOK)
        asyncio.run(schedule.run(ctx))
        assert len(ctx.webhook.calls) == 1


def test_a_broken_game_channel_never_breaks_the_real_card():
    """The copy is a convenience. A dead game-channel webhook must not fail the run, must not
    land in ctx.errors, and must not stop the schedule channel from being served."""
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        ctx = _wuwa_ctx(sp, FanOutWebhook(fail_fp=webhook_fingerprint(MIRROR), status=403),
                        BOOTSTRAP_POST="1", DISCORD_WEBHOOK_SCHEDULE_MIRROR_WUWA=MIRROR)
        asyncio.run(schedule.run(ctx))
        assert [m for m, _ in ctx.webhook.to(HOOK)] == ["POST"]      # real card still posted
        assert ctx.errors == []
        assert any("copy failed (403)" in line for line in ctx.report)
        rec = ctx.state.data["schedule"]["wuwa"]["3.7"]
        assert rec["status"] == "posted" and "mirror_message_id" not in rec


def test_removing_the_mirror_secret_forgets_the_copy():
    """Stale ids would PATCH a message in a channel the run can no longer prove it owns."""
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        ctx = _wuwa_ctx(sp, FanOutWebhook(), BOOTSTRAP_POST="1",
                        DISCORD_WEBHOOK_SCHEDULE_MIRROR_WUWA=MIRROR)
        asyncio.run(schedule.run(ctx))
        assert ctx.state.data["schedule"]["wuwa"]["3.7"]["mirror_message_id"]
        ctx.state.save()

        ctx = _wuwa_ctx(sp, FanOutWebhook(), now=1789303600)         # secret removed
        asyncio.run(schedule.run(ctx))
        assert ctx.webhook.to(MIRROR) == []
        assert "mirror_message_id" not in ctx.state.data["schedule"]["wuwa"]["3.7"]


def test_force_webhook_keeps_the_fan_out_out_of_real_game_channels():
    """FORCE_WEBHOOK redirects the primary to a test channel; the copy must not escape."""
    with tempfile.TemporaryDirectory() as tmp:
        force = "https://discord.com/api/webhooks/111111111111111111/test-channel"
        ctx = _wuwa_ctx(Path(tmp) / "state.json", FanOutWebhook(), BOOTSTRAP_POST="1",
                        FORCE_WEBHOOK=force, DISCORD_WEBHOOK_SCHEDULE_MIRROR_WUWA=MIRROR)
        asyncio.run(schedule.run(ctx))
        assert [m for m, _ in ctx.webhook.to(force)] == ["POST"]
        assert ctx.webhook.to(MIRROR) == []


# =========================================== 2026-10-08 Genshin 7.1: a notice may fill, never open
# One live card showed three faults with a single cause. The record had been created by this
# *Update Details* notice on 2026-09-25, two days after 7.1's own maintenance, so it never got a
# program_ts: the card lost its air-time line, kept the notice's cover as key art and kept the
# notice's URL as its title link. Fifteen days after the version shipped, an edit of a message
# id that had never resolved returned 404 and the arm below published a brand-new card -- for a
# programme this bot had never carded at all. A schedule card announces a Special Program; the
# notice may only ever fill one in.
def _update_details_notice(ts: int) -> Item:
    """The real post that opened the Genshin 7.1 card — a maintenance notice, not an announcement."""
    return Item("hoyolab", "genshin", "46791577",
                "https://www.hoyolab.com/article/46791577",
                '"A Rekviem for the Underworld" Version 7.1 Update Details',
                "Dear Traveler,\nBelow are the details of the Version 7.1 update.\n"
                "Maintenance Time: 2026/09/23 06:00 - 11:00 (UTC+8)\n"
                "Compensation: Primogems ×300", ts)


def test_a_maintenance_notice_alone_never_opens_a_schedule_card():
    """The card announces a Special Program / Special Broadcast — nothing else may create one.

    Genshin 7.1 was opened by its Update Details notice on 2026-09-25, two days after its own
    maintenance, and so shipped with the notice's cover as key art, the notice's link in the
    title and no air-time line at all. A notice must only ever FILL a card the announcement
    opened."""
    now = 1790118000                                    # 1 h after the 2026-09-23 maintenance
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=now)
        ctx.webhook = FanOutWebhook()
        extracts = [e for e in [schedule.extract(GAMES["genshin"], _update_details_notice(now))] if e]
        assert [e.kind for e in extracts] == ["maintenance"]
        records: dict = {}
        asyncio.run(schedule._handle_version(ctx, GAMES["genshin"], "7.1", extracts, records,
                                             {}, {}, True))
        assert ctx.webhook.calls == []                   # nothing posted anywhere
        assert records["7.1"]["status"] == "tracked"
        assert not any("Special Program" in line for line in ctx.report)
        # but the notice's data IS recorded, ready for the announcement to adopt
        data = records["7.1"]["data"]
        assert data["maint_start_ts"] == 1790114400 and data["compensation"] == "Primogems ×300"
        assert "program_ts" not in data


def test_an_already_aired_programme_never_opens_a_new_card():
    """THE main rule. This bot was deployed after Genshin 7.1 and Wuthering Waves 3.7 had already
    aired their programmes; those versions were announced long ago, by somebody else. A version
    whose programme is in the past is history, not an announcement — it is tracked and kept
    current, never posted. Genshin 7.1 was announced 2026-09-07, aired 09-12 and shipped 09-23,
    and still got a brand-new card on 2026-10-08."""
    now = 1790118000                                    # 1 h after the 2026-09-23 maintenance
    aired = 1789214400                                  # 2026-09-12 20:00 +08 — 10 days earlier
    base = {"version": "7.1", "program_ts": aired, "program_seen": True}
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=now)
        ctx.webhook = FanOutWebhook()
        extracts = [e for e in [schedule.extract(GAMES["genshin"], _update_details_notice(now))] if e]
        records = {"7.1": {"status": "new", "first_seen": now, "prov": {}, "data": dict(base)}}
        asyncio.run(schedule._handle_version(ctx, GAMES["genshin"], "7.1", extracts, records,
                                             {}, {}, True))
        assert ctx.webhook.calls == []                   # the whole point: no new card
        assert records["7.1"]["status"] == "tracked"
        assert not any("schedule card posted" in line for line in ctx.report)
        # ...yet the maintenance it carried is still recorded, ready for whoever owns the card
        assert records["7.1"]["data"]["compensation"] == "Primogems ×300"

    # REPOST stays the deliberate override — the rule is a default, not a cage.
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=now, REPOST="genshin:7.1")
        ctx.webhook = FanOutWebhook()
        extracts = [e for e in [schedule.extract(GAMES["genshin"], _update_details_notice(now))] if e]
        records = {"7.1": {"status": "new", "first_seen": now, "prov": {}, "data": dict(base)}}
        asyncio.run(schedule._handle_version(ctx, GAMES["genshin"], "7.1", extracts, records,
                                             {}, {}, True))
        assert [m for m, _ in ctx.webhook.to(HOOK)] == ["POST"]
        assert records["7.1"]["status"] == "posted"


def test_a_genuine_upcoming_programme_still_opens_its_card():
    """The settled guard must not become a cage. This bot's first real card was the Zenless Zone
    Zero 3.3 Special Program — posted 2026-10-05 for a programme that airs on 10-09 — exactly
    what the schedule channel exists for. A programme that has not aired yet is never settled, so
    it posts normally, carrying its air time."""
    now = 1791446400                                    # 2026-10-08 16:00 +08
    item = Item("hoyolab", "zzz", "47100000", "https://www.hoyolab.com/article/47100000",
                "Zenless Zone Zero Version 3.3 Special Program Preview",
                "The Version 3.3 Special Program will premiere on 2026/10/09 at 19:30 (UTC+8)!",
                now - 3600)
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=now)
        ctx.webhook = FanOutWebhook()
        extracts = [e for e in [schedule.extract(GAMES["zzz"], item)] if e]
        assert [e.kind for e in extracts] == ["program"]
        assert not schedule.program_settled(extracts[0].fields, now)   # airs tomorrow
        records: dict = {}
        asyncio.run(schedule._handle_version(ctx, GAMES["zzz"], "3.3", extracts, records,
                                             {}, {}, True))
        sent = ctx.webhook.to(HOOK)
        assert [m for m, _ in sent] == ["POST"]
        assert "<t:1791545400:F>" in json.dumps(sent[0][1], ensure_ascii=False)
        assert records["3.3"]["status"] == "posted"


def test_no_game_opens_a_card_for_a_broadcast_that_already_aired():
    """The rule is not HoYoverse-specific. Wuthering Waves 3.7 broadcast on 2026-09-19, also
    before this bot existed. Kuro calls it a Special Broadcast and serves its notices from its own
    site, but the gate is one game-independent check, so both notice kinds that can still reach a
    settled version — the update maintenance notice and a convene notice — must leave it alone."""
    now = 1791446400                                    # 2026-10-08 16:00 +08
    aired = 1789815600                                  # 2026-09-19 19:00 +08 — the broadcast
    notices = {
        "maintenance": ('Version 3.7 "Tides of War" Update Maintenance Notice',
                        "Dear Rovers,\n\nVersion 3.7 update maintenance will begin on 2026/10/16 "
                        "at 10:00 (UTC+8) and is estimated to last 5 hours. Pre-installation will "
                        "be available from 2026/10/14 at 10:00 (UTC+8). Compensation: Astrite "
                        "×600."),
        "banner": ("Version 3.7 Convene Event Notice",
                   'The 5-star Resonator "Tidebreaker" Cartethyia (Aero) rate-up Convene will be '
                   "available from 2026/10/16 10:00 (UTC+8) to 2026/11/06 09:59 (UTC+8)."),
    }
    for kind, (title, text) in notices.items():
        item = Item("kuro", "wuwa", f"wuwa-3-7-{kind}",
                    "https://wutheringwaves.kurogames.com/en/main/news/detail/1",
                    title, text, now - 3600)
        extracts = [e for e in [schedule.extract(GAMES["wuwa"], item)] if e]
        assert [e.kind for e in extracts] == [kind]     # the notice itself parses as expected
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(Path(tmp) / "state.json", now=now)
            ctx.webhook = FanOutWebhook()
            records = {"3.7": {"status": "new", "first_seen": now, "prov": {},
                               "data": {"version": "3.7", "program_ts": aired,
                                        "program_seen": True}}}
            asyncio.run(schedule._handle_version(ctx, GAMES["wuwa"], "3.7", extracts, records,
                                                 {}, {}, True))
            assert ctx.webhook.calls == [], f"a {kind} notice opened a card for a settled broadcast"
            assert records["3.7"]["status"] == "tracked"
        # tracked, not ignored: whatever the notice carried is still written down
        if kind == "maintenance":
            stated = datetime(2026, 10, 16, 10, 0, tzinfo=timezone(timedelta(hours=8)))
            assert records["3.7"]["data"]["maint_start_ts"] == int(stated.timestamp())
            assert records["3.7"]["data"]["compensation"] == "Astrite ×600"


def test_a_maintenance_notice_still_fills_a_card_whose_program_was_seen():
    """The other direction: once the announcement is known, the notice must still post/fill it.
    Breaking this would stop every maintenance update the card exists to deliver."""
    now = 1790118000
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=now)
        ctx.webhook = FanOutWebhook()
        extracts = [e for e in [schedule.extract(GAMES["genshin"], _update_details_notice(now))] if e]
        records = {"7.1": {"status": "new", "first_seen": now, "prov": {},
                           "data": {"version": "7.1", "program_ts": now - 12 * 3600,
                                    "program_seen": True}}}
        asyncio.run(schedule._handle_version(ctx, GAMES["genshin"], "7.1", extracts, records,
                                             {}, {}, True))
        assert [m for m, _ in ctx.webhook.to(HOOK)] == ["POST"]
        assert records["7.1"]["status"] == "posted"
        assert records["7.1"]["data"]["compensation"] == "Primogems ×300"


def test_a_lookup_that_found_nothing_reaches_the_summary():
    """The announcement lookup is the repair; it used to report success to the job log only and
    failure nowhere at all. A card stuck without an air time and without a link is then
    indistinguishable from a rendering bug, which is how 7.1 stayed wrong for two weeks."""
    from gamexpress import runner

    async def _no_hit(*_a, **_k):
        return None

    now = 1791446400
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=now)
        ctx.state.schedule_records("genshin")["7.1"] = {
            "status": "posted", "data": {"version": "7.1", "maint_start_ts": 1790114400}}
        real, runner.find_program = runner.find_program, _no_hit
        try:
            asyncio.run(runner.gather_program_media(ctx))
        finally:
            runner.find_program = real
        said = [r for r in ctx.report if "no Special Program announcement found" in r]
        assert len(said) == 1 and "7.1" in said[0], ctx.report


def test_the_summary_says_why_a_notice_opened_no_card():
    """Both gates are deliberately silent in Discord, which leaves the operator reading a run
    that posted nothing and no way to tell "working as designed" from "broken". The step summary
    is where they are told apart, and the line is keyed on the notice so it is written once, not
    every ten minutes for the six hours the notice stays fresh."""
    now = 1790118000
    with tempfile.TemporaryDirectory() as tmp:
        def run(data: dict):
            ctx = make_ctx(Path(tmp) / "state.json", now=now)
            ctx.webhook = FanOutWebhook()
            extracts = [e for e in [schedule.extract(GAMES["genshin"], _update_details_notice(now))] if e]
            records = {"7.1": {"status": "tracked", "first_seen": now, "prov": {}, "data": data}}
            for _ in range(2):                       # a second run must not repeat the line
                asyncio.run(schedule._handle_version(ctx, GAMES["genshin"], "7.1", extracts,
                                                     records, {}, {}, False))
            return ctx, records["7.1"]

        ctx, rec = run({"version": "7.1"})                       # no announcement ever seen
        assert ctx.webhook.calls == []
        said = [r for r in ctx.report if "no Special Program announcement has been seen" in r]
        assert len(said) == 1, ctx.report
        assert rec["data"]["compensation"] == "Primogems ×300"   # the notice still landed

        ctx, rec = run({"version": "7.1", "program_ts": 1789214400})   # aired a month ago
        assert ctx.webhook.calls == []
        said = [r for r in ctx.report if "already out" in r]
        assert len(said) == 1, ctx.report
        assert rec["data"]["maint_start_ts"]                     # and its data is still current


def test_a_card_that_cannot_get_its_announcement_says_so_once():
    """A version the lookup can never help — no program_ts, past the 12 h window, and no cached
    tweet id to replay — is otherwise completely silent: the lookup never runs, so there is no
    🔍 line either, and the card simply keeps the notice's cover and link. The summary now names
    the one thing the operator can do, once per card rather than every five minutes."""
    from gamexpress import runner

    async def _no_hit(*_a, **_k):
        return None

    now = 1791446400
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=now)
        recs = ctx.state.schedule_records("genshin")
        stale = {"status": "posted", "message_id": "m", "webhook_fp": "fp", "payload_hash": "x"}
        recs["9.9"] = {**stale, "data": {"version": "9.9", "maint_start_ts": now - 20 * 86400}}
        recs["7.1"] = {**stale, "data": {"version": "7.1", "maint_start_ts": now - 20 * 86400}}
        real, runner.find_program = runner.find_program, _no_hit
        try:
            for _ in range(2):                       # the second run must not repeat the line
                asyncio.run(runner.gather_program_media(ctx))
        finally:
            runner.find_program = real
        said = [r for r in ctx.report if "no cached tweet id" in r]
        assert len(said) == 1 and "9.9" in said[0], ctx.report
        assert recs["9.9"]["announce_gap"]
        assert not any("7.1" in r and "no cached tweet id" in r for r in ctx.report)
        assert "announce_gap" not in recs["7.1"]     # cached -> the recovery will run instead


def test_the_cached_announcement_is_replayed_for_a_card_that_lost_its_air_time():
    """config/program_announcements.json is the pattern that stops this recurring: a live version
    with no program_ts gets exactly one more lookup, and only when the tweet id is already
    cached — so it is a single fxtwitter call that succeeds, not an open-ended retry."""
    now, gi = 1791446400, {"maint_start_ts": 1790114400, "version": "7.1"}
    look = schedule.needs_program_lookup
    assert look([], {"data": gi}, now, "genshin") is True            # seeded -> recover it
    assert look([], {"data": gi}, now) is False                      # no game key -> old behaviour
    assert look([], {"data": dict(gi, version="9.9")}, now, "genshin") is False      # not cached
    assert look([], {"data": dict(gi, program_ts=1789905600)}, now, "genshin") is False
    assert look([], {"data": gi}, now + 45 * 86400, "genshin") is False              # frozen
    assert look([], {"data": dict(gi, media_from="X Post")}, now, "genshin") is False  # ran already


def test_rechecking_confirmed_data_costs_nothing_and_edits_nothing():
    """'it only just reverify or re-check' — a second run over identical data must be silent:
    no PATCH, no POST, in either channel."""
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        env = {"DISCORD_WEBHOOK_SCHEDULE_MIRROR_WUWA": MIRROR}
        ctx = _wuwa_ctx(sp, FanOutWebhook(), BOOTSTRAP_POST="1", **env)
        asyncio.run(schedule.run(ctx))
        assert [m for m, _ in ctx.webhook.to(HOOK)] == ["POST"]
        assert [m for m, _ in ctx.webhook.to(MIRROR)] == ["POST"]
        ctx.state.save()
        for _ in range(3):                                # re-verify, repeatedly
            ctx = _wuwa_ctx(sp, FanOutWebhook(), now=1789303600, **env)
            asyncio.run(schedule.run(ctx))
            assert ctx.webhook.calls == [], ctx.webhook.calls
            assert ctx.errors == []
            ctx.state.save()


def test_no_game_channel_copy_is_created_for_a_version_already_out():
    """A settled version never gets a brand-new message in EITHER channel.

    The primary card had a settled check to skip; the fan-out's send() had none, and dropped a
    fifteen-day-old Genshin 7.1 card straight into #gi-news."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790450000,
                       DISCORD_WEBHOOK_SCHEDULE_MIRROR_STARRAIL=MIRROR)
        ctx.webhook = FanOutWebhook()
        records = {"4.6": _posted_hsr_record()}
        assert schedule.program_settled(records["4.6"]["data"], ctx.now)
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×301"}, {}, True))
        assert [m for m, _ in ctx.webhook.to(HOOK)] == ["PATCH"]     # the living card is edited
        assert ctx.webhook.to(MIRROR) == []                          # no copy is born
        assert records["4.6"]["mirror_retired"] == ctx.now
        assert "mirror_message_id" not in records["4.6"]
        assert any("copy not created" in line for line in ctx.report)
        assert ctx.errors == []


def test_a_deleted_game_channel_copy_says_which_message_vanished():
    """A copy that was deleted is a different story from a copy that was never made.

    Both refuse to create one for a settled version and both used to print the same sentence, so
    the only way to tell "the fan-out never worked" from "somebody removed the copy" was to go
    and look in the channel. Seen live on 2026-10-09: both the Genshin 7.1 card and its #gi-news
    copy had been deleted, and the run could only say that no copy was being created."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790450000,
                       DISCORD_WEBHOOK_SCHEDULE_MIRROR_STARRAIL=MIRROR)
        ctx.webhook = FanOutWebhook(fail_fp=webhook_fingerprint(MIRROR), status=404)
        rec = _posted_hsr_record()
        rec.update({"mirror_message_id": "gone-copy",
                    "mirror_webhook_fp": webhook_fingerprint(MIRROR), "mirror_hash": "old-copy"})
        records = {"4.6": rec}
        assert schedule.program_settled(rec["data"], ctx.now)
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×301"}, {}, True))
        assert [m for m, _ in ctx.webhook.to(MIRROR)] == ["PATCH"]   # the copy was tried first
        assert records["4.6"]["mirror_retired"] == ctx.now
        said = [r for r in ctx.report if "copy not created" in r]
        assert len(said) == 1 and "gone-copy" in said[0] and "10008" in said[0], ctx.report
        assert ctx.errors == []
        # once is once: the retirement guard keeps the second run quiet
        ctx.now += 600
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×302"}, {}, True))
        assert [r for r in ctx.report if "copy not created" in r] == said


def test_a_test_run_still_posts_the_game_channel_copy_of_a_retired_card():
    """The fan-out is half of what the bench is for, so a retired copy must not silence it.

    A live run that found a settled version retired the copy (`mirror_retired`, checked first
    thing in `_sync_mirror`). A bench run against that record would then post the primary TEST
    card and silently skip the game-channel copy — proving half the wiring and showing the
    operator a run that looks like a fan-out failure. Test mode drops the retirement in memory
    (it never saves state), so both halves are exercised and both copies are throwaways."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790450000, TEST_MODE="1",
                       DISCORD_WEBHOOK_SCHEDULE_MIRROR_STARRAIL=MIRROR)
        ctx.webhook = FanOutWebhook()
        records = {"4.6": _posted_hsr_record()}
        records["4.6"]["mirror_retired"] = 1790400000        # retired by an earlier live run
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×301"}, {}, True))
        assert [m for m, _ in ctx.webhook.to(HOOK)] == ["POST"]      # a fresh TEST card
        assert [m for m, _ in ctx.webhook.to(MIRROR)] == ["POST"]    # ...and its copy
        copy = json.dumps(ctx.webhook.to(MIRROR)[0][1], ensure_ascii=False)
        assert "🧪 TEST CARD" in copy                                # marked, so it is deletable
        assert "mirror_retired" not in records["4.6"]
        assert records["4.6"]["mirror_message_id"] and ctx.errors == []


def test_a_test_run_posts_a_fresh_copy_instead_of_editing_the_live_one():
    """The copy is where the bench hole was most visible.

    A posted version with a live copy in the game channel: dropping the copy's ids in memory
    turns that branch into a plain POST, so a test run leaves a throwaway beside the real
    message instead of PATCHing the production copy and stamping a 🧪 TEST banner on it."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790450000, TEST_MODE="1",
                       DISCORD_WEBHOOK_SCHEDULE_MIRROR_STARRAIL=MIRROR)
        ctx.webhook = FanOutWebhook()
        records = {"4.6": _posted_hsr_record()}
        records["4.6"].update({"mirror_message_id": "live-copy",
                               "mirror_webhook_fp": webhook_fingerprint(MIRROR),
                               "mirror_hash": "old-mirror-hash"})
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×301"}, {}, True))
        assert [m for m, _ in ctx.webhook.to(HOOK)] == ["POST"]      # a fresh TEST card
        assert [m for m, _ in ctx.webhook.to(MIRROR)] == ["POST"]    # and a fresh TEST copy
        assert not any(m == "PATCH" for m, _, _ in ctx.webhook.calls)   # nothing live was edited
        copy = json.dumps(ctx.webhook.to(MIRROR)[0][1], ensure_ascii=False)
        assert "🧪 TEST CARD" in copy
        assert records["4.6"]["mirror_message_id"].startswith("msg-")   # the throwaway's own id
        assert ctx.errors == []


def test_an_existing_game_channel_copy_is_still_edited_after_the_version_ships():
    """A settled program is not a finished version. The copy already sitting in the game channel
    keeps receiving the same silent corrections as the original — only CREATING one is barred."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790450000,
                       DISCORD_WEBHOOK_SCHEDULE_MIRROR_STARRAIL=MIRROR)
        ctx.webhook = FanOutWebhook()
        records = {"4.6": _posted_hsr_record()}
        records["4.6"].update({"mirror_message_id": "living-copy",
                               "mirror_webhook_fp": webhook_fingerprint(MIRROR),
                               "mirror_hash": "stale-copy"})
        assert schedule.program_settled(records["4.6"]["data"], ctx.now)
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×301"}, {}, True))
        assert [m for m, _ in ctx.webhook.to(HOOK)] == ["PATCH"]
        assert [m for m, _ in ctx.webhook.to(MIRROR)] == ["PATCH"]
        assert "mirror_retired" not in records["4.6"]
        assert records["4.6"]["mirror_message_id"] == "living-copy"
        assert ctx.errors == []


def test_repost_revives_a_retired_card_and_its_copy():
    """REPOST is the manual escape hatch. A retirement is a statement about a message that no
    longer exists, so publishing a replacement lifts it — otherwise the guard at the top of the
    edit path would return for the rest of the new card's life."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DISCORD_WEBHOOK_SCHEDULE_MIRROR_STARRAIL": MIRROR}
        ctx = make_ctx(Path(tmp) / "state.json", now=1790450000, REPOST="starrail:4.6", **env)
        ctx.webhook = FanOutWebhook()
        records = {"4.6": _posted_hsr_record()}
        records["4.6"].pop("message_id")                       # the user deleted both messages
        records["4.6"]["card_retired"] = 1790400000
        records["4.6"]["mirror_retired"] = 1790400000
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×301"}, {}, True))
        assert [m for m, _ in ctx.webhook.to(HOOK)] == ["POST"]
        assert [m for m, _ in ctx.webhook.to(MIRROR)] == ["POST"]
        assert "card_retired" not in records["4.6"] and "mirror_retired" not in records["4.6"]
        assert records["4.6"]["message_id"] and records["4.6"]["mirror_message_id"]

        # and the revived card keeps receiving its silent corrections on the next normal run
        ctx.state.save()
        nxt = make_ctx(Path(tmp) / "state.json", now=ctx.now + 600, **env)
        nxt.webhook = FanOutWebhook()
        nxt.state.schedule_records("starrail")["4.6"] = records["4.6"]
        asyncio.run(schedule._handle_version(nxt, GAMES["starrail"], "4.6", [],
                                             nxt.state.schedule_records("starrail"),
                                             {"compensation": "Stellar Jade ×302"}, {}, True))
        assert [m for m, _ in nxt.webhook.to(HOOK)] == ["PATCH"]
        assert [m for m, _ in nxt.webhook.to(MIRROR)] == ["PATCH"]
        assert nxt.errors == []


# The live Genshin 7.1 record on 2026-10-08, verbatim from state.json: images = the "Version 7.1
# Update Details" cover uploaded 2026-09-22, title_url = that same notice, maint_start_ts =
# 2026-09-23 06:00 +08 — and no program_ts, program_seen or media_from at all.
GI_71_NOW = 1791446400                   # 2026-10-08 16:00 +08 — the run that exposed all of it
GI_71_LIVESTREAM = 1789214400            # 2026-09-12 20:00 +08: the premiere the tweet names
GI_71_ANNOUNCEMENT = {
    "url": "https://x.com/GenshinImpact/status/2096810691021689205",
    "title": "Genshin Impact Version 7.1 Special Program",
    "images": ["https://pbs.twimg.com/media/HRlONCqXcAUhgGD.jpg?name=orig"],
    "program_ts": GI_71_LIVESTREAM,
    "source": "X Post",
}
GI_71_LIVE_DATA = {
    "version": "7.1",
    "images": ["https://upload-os-bbs.hoyolab.com/upload/2026/09/22/0/769afb25a1c8e9f3.jpeg"],
    "title_url": "https://www.hoyolab.com/article/46791577",
    "source_url": "https://www.hoyolab.com/article/46791577",
    "source_label": "HoYoLAB",
    "maint_start_ts": 1790114400,        # 2026-09-23 06:00 +08, fifteen days before GI_71_NOW
    "maint_end_ts": 1790132400,
}


def _gi_71_phase2_notice(ts: int = GI_71_NOW - 3600) -> Item:
    """HoYoLAB 47010361, the notice that made the run look at 7.1 on 2026-10-08 — and the very
    post whose banner titles the card was showing instead of the characters."""
    return Item("hoyolab", "genshin", "47010361",
                "https://www.hoyolab.com/article/47010361",
                "Version 7.1 Event Wishes Notice - Phase II",
                '〓Event Wish "La Chanson Cerise"〓\n'
                '● During this event wish, the event-exclusive 5-star character '
                '"Tasteful Excellence" Escoffier (Cryo) will receive a huge drop-rate boost!\n'
                '● During this event wish, the 4-star characters "Ode and Oblation" Dahlia '
                '(Hydro), "Golden Vow" Candace (Hydro), and "Coordinates of Clear Frost" Mika '
                '(Cryo) will receive a huge drop-rate boost!\n', ts)


def test_the_real_genshin_71_record_heals_in_place_with_no_new_message():
    """End to end on the real live record and the real cached tweet, replayed as the one
    fxtwitter call the recovery is gated on: the run swaps the notice's cover for the key art,
    points the title at the announcement, restores the air time — and posts no new message,
    because this card was only ever edited. The run after that has nothing left to do."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=GI_71_NOW,
                       items={"genshin": [_gi_71_phase2_notice()]})
        ctx.media = {"genshin": {"7.1": dict(GI_71_ANNOUNCEMENT)}}
        ctx.state.schedule_records("genshin")["7.1"] = {
            "status": "posted", "first_seen": 1790371200, "data": dict(GI_71_LIVE_DATA),
            # the live card id at the time of writing: the one the 404 arm published at
            # 2026-10-08 21:20 UTC+8 (state.json). The point is that the repair PATCHes
            # whatever id the record holds, so this only has to be a real one.
            "prov": {}, "message_id": "1557744484790304889",
            "webhook_fp": webhook_fingerprint(HOOK), "payload_hash": "the-broken-card"}
        ctx.webhook = FanOutWebhook()
        live = {"data": ctx.state.schedule_records("genshin")["7.1"]["data"]}
        assert schedule.needs_program_lookup([], live, GI_71_NOW, "genshin")   # cached -> one call
        asyncio.run(schedule.run(ctx))
        assert [m for m, _ in ctx.webhook.to(HOOK)] == ["PATCH"]
        assert len(ctx.webhook.calls) == 1                     # nothing posted anywhere
        flat = json.dumps(ctx.webhook.to(HOOK)[0][1], ensure_ascii=False)
        assert "<t:1789214400:F>" in flat                      # the air time the card had lost
        assert "HRlONCqXcAUhgGD.jpg?name=orig" in flat         # the programme's key art
        assert "x.com/GenshinImpact/status/2096810691021689205" in flat
        assert "hoyolab.com/article/46791577" not in flat      # never the notice's link
        assert "Escoffier" in flat                             # and the real phase-2 characters
        assert "Tasteful Excellence" not in flat
        data = ctx.state.schedule_records("genshin")["7.1"]["data"]
        assert data["program_ts"] == GI_71_LIVESTREAM and data["media_from"] == "X Post"
        assert "upload-os-bbs" not in flat                     # the notice's cover is gone

        # the fixed data is committed to state, and the next run reports "nothing new"
        ctx.state.save()
        ctx2 = make_ctx(Path(tmp) / "state.json", now=GI_71_NOW + 600,
                        items={"genshin": [_gi_71_phase2_notice()]})
        ctx2.media = {"genshin": {"7.1": dict(GI_71_ANNOUNCEMENT)}}
        ctx2.webhook = FanOutWebhook()
        asyncio.run(schedule.run(ctx2))
        assert ctx2.webhook.calls == []
        assert ctx2.errors == []
        healed = {"data": ctx2.state.schedule_records("genshin")["7.1"]["data"]}
        assert schedule.needs_program_lookup([], healed, GI_71_NOW + 600, "genshin") is False


def test_preinstall_cold_start_reproduces_four_real_notices():
    """Each shipped lead must reproduce a TYPICAL published notice, not the latest one.

    HSR is deliberately checked against 4.5 (Mon 14:00 -> Wed 06:00 = 40 h, the value 8 of its
    last 11 versions used) and not against 4.6, whose maintenance alone slipped to a Monday and
    stretched the lead to 88 h. Calibrating a default on a single outlier makes every cold start
    wrong by two days.
    """
    cases = {
        "genshin": (1790114400, 1789959600),       # 7.1: Mon 11:00 -> Wed 06:00 (43 h)
        "starrail": (1787695200, 1787551200),      # 4.5: Mon 14:00 -> Wed 06:00 (40 h)
        "zzz": (1788904800, 1788753600),           # 3.2: Mon 12:00 -> Wed 06:00 (42 h)
        "wuwa": (1790712000, 1790560800),          # 3.7: Tue 10:00 -> Thu 04:00 (42 h)
    }
    for game_key, (maintenance, want) in cases.items():
        record = {"data": {"maint_start_ts": maintenance},
                  "prov": {"maint_start_ts": [schedule.PRIORITY["hoyolab"], maintenance]}}
        got = schedule.merge(GAMES[game_key], "test", [], record, {}, {}, maintenance - 86400)
        assert got["preinstall_ts"] == want, (game_key, got["preinstall_ts"], want)


def test_preinstall_fallback_never_overwrites_a_real_time():
    real = 1790200000
    data = {"maint_start_ts": 1790546400, "preinstall_ts": real}
    prov = {"maint_start_ts": [50, 1], "preinstall_ts": [50, 1]}
    assert schedule.derive_preinstall(GAMES["starrail"], data, prov, 2, lead_h=12) is None
    assert data["preinstall_ts"] == real and "estimated" not in data


def test_preinstall_fallback_is_labelled_estimated():
    data, prov = {"maint_start_ts": 1790546400}, {"maint_start_ts": [50, 1]}
    assert schedule.derive_preinstall(GAMES["starrail"], data, prov, 2) == 1790402400
    assert data["estimated"] == ["preinstall_ts"]
    assert data["estimate_sources"] == ["version cadence"]
    assert prov["preinstall_ts"][0] == schedule.PRIORITY["pattern"]


def test_a_real_preinstall_replaces_the_fallback():
    record = {"data": {"maint_start_ts": 1790546400},
              "prov": {"maint_start_ts": [50, 1]}}
    derived = schedule.merge(GAMES["starrail"], "4.6", [], record, {}, {}, 2)
    assert "preinstall_ts" in derived.get("estimated", [])
    record["data"] = derived
    real = schedule.merge(GAMES["starrail"], "4.6", [], record,
                          {"preinstall_ts": 1790229660}, {}, 3)
    assert real["preinstall_ts"] == 1790229660 and "preinstall_ts" not in real.get("estimated", [])
    assert record["prov"]["preinstall_ts"][0] == schedule.PRIORITY["override"]


def test_a_real_notice_teaches_the_game_its_own_lead_time():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1789900000)
        item = Item("hoyolab", "genshin", "real-7.1", "https://www.hoyolab.com/article/real-7.1",
                    "Version 7.1 Update Details", "official notice", 1789900000)
        notice = schedule.Extract(item, "maintenance", "7.1", fields={
            "preinstall_ts": 1789959600, "maint_start_ts": 1790114400,
            "maint_end_ts": 1790132400})
        records: dict = {}
        asyncio.run(schedule._handle_version(ctx, GAMES["genshin"], "7.1", [notice], records,
                                             {}, {}, True))
        assert 42 <= records["7.1"]["preinstall_offset_h"] <= 44
        assert "preinstall_ts" not in (records["7.1"]["data"].get("estimated") or [])


def test_a_derived_preinstall_never_teaches_the_model():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790000000)
        item = Item("hoyolab", "starrail", "maint-4.7", "https://www.hoyolab.com/article/maint-4.7",
                    "Version 4.7 Update Details", "official notice", 1790000000)
        notice = schedule.Extract(item, "maintenance", "4.7", fields={
            "maint_start_ts": 1791000000, "maint_end_ts": 1791018000})
        records: dict = {}
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.7", [notice], records,
                                             {}, {}, True))
        record = records["4.7"]
        assert "preinstall_ts" in record["data"]["estimated"]
        assert "preinstall_offset_h" not in record


def test_observed_history_beats_the_shipped_default():
    records = {
        "4.4": {"preinstall_offset_h": 42},
        "4.5": {"preinstall_offset_h": 44},
        "bad-number": {"preinstall_offset_h": "not a number"},
        "bad-outlier": {"preinstall_offset_h": 700},
        "missing": {},
    }
    assert schedule.observed_lead_h(records) == 43                # even count -> average middle pair
    record = {"data": {"maint_start_ts": 2_000_000},
              "prov": {"maint_start_ts": [50, 1]}}
    got = schedule.merge(GAMES["starrail"], "4.6", [], record, {}, {}, 1,
                         lead_h=schedule.observed_lead_h(records))
    assert got["preinstall_ts"] == 2_000_000 - 43 * 3600         # not shipped HSR default (40 h)

    assert schedule.observed_lead_h({"x": {"preinstall_offset_h": "junk"}}) is None
    unknown = replace(GAMES["starrail"], key="unknown-game")
    data, prov = {"maint_start_ts": 2_000_000}, {"maint_start_ts": [50, 1]}
    assert schedule.derive_preinstall(unknown, data, prov, 1) is None
    assert "preinstall_ts" not in data


# --------------------------------------------------------------------- real notices, parser gaps
# Genshin 7.1 maintenance preview (HoYoLAB 46771203). The title never says "pre-install"; the body
# does, and HoYoverse publishes the post at the moment pre-installation opens.
GI_71_NOTICE = """Dear Travelers,

In order to provide a better gaming experience, the Genshin Impact team will be carrying out
Version 7.1 update maintenance, during which the game will be unavailable.

Update maintenance will begin on 2026-09-23 06:00 (UTC+8) and is estimated to take 5 hours.

At the same time, pre-installation for the Version 7.1 update is now available. Travelers on PC
and mobile devices can open the game and follow the prompts to start pre-installing the update.

Maintenance Compensation: Primogems ×300
"""

# ZZZ 3.2 update notice (HoYoLAB 46604333). Every number sits under a bracketed section header on
# the following line, and the window is spelled out in words instead of digits.
ZZZ_32_NOTICE = """Dear Proxies,

[Version Update Time]
2026-09-09 06:00 (UTC+8)
We estimate this will take five hours. Thank you for your patience.

[Pre-Download Period]
2026-09-07 12:00 (UTC+8) – 2026-09-09 06:00 (UTC+8)

[Pre-Installation Details]
Please make sure you have at least 32 GB of available storage space on your device.
"""


def test_genshin_preinstall_is_read_from_the_body_not_the_title():
    item = Item("hoyolab", "genshin", "46771203", "https://www.hoyolab.com/article/46771203",
                "Version 7.1 Update Maintenance Preview", GI_71_NOTICE, 1789960208)
    e = schedule.extract(GAMES["genshin"], item)
    assert e and e.kind == "maintenance" and e.version == "7.1"
    assert e.fields["maint_start_ts"] == 1790114400          # Wed 2026-09-23 06:00 (UTC+8)
    assert e.fields["maint_end_ts"] == 1790132400            # +5 h, == the banner feed's phase-1 start
    assert e.fields["preinstall_ts"] == 1789960208           # the post's own time: "is now available"


def test_zzz_labelled_sections_carry_the_times_the_sentence_scanner_dropped():
    item = Item("hoyolab", "zzz", "46604333", "https://www.hoyolab.com/article/46604333",
                "Version 3.2 Update Maintenance Notice", ZZZ_32_NOTICE, 1788700000)
    e = schedule.extract(GAMES["zzz"], item)
    assert e and e.kind == "maintenance" and e.version == "3.2"
    assert e.fields["maint_start_ts"] == 1788904800          # Wed 2026-09-09 06:00 (UTC+8)
    assert e.fields["maint_end_ts"] == 1788922800            # "take five hours" -> 11:00
    assert e.fields["preinstall_ts"] == 1788753600           # Mon 2026-09-07 12:00 (UTC+8)


def test_spelled_out_maintenance_duration_parses():
    assert find_duration_hours("We estimate this will take five hours.") == 5.0
    assert find_duration_hours("The maintenance is expected to take about three hours.") == 3.0
    assert find_duration_hours("is estimated to take 5 hours") == 5.0      # digits still win
    assert find_duration_hours("five hours of new story content await") is None


def test_the_storage_block_is_not_mistaken_for_a_preinstall_clock():
    text = ("[Pre-Download Period]\n"
            "Reserve at least 32 GB of storage space before 2026-09-01 00:00 (UTC+8).\n"
            "[Version Update Time]\n"
            "2026-09-09 06:00 (UTC+8)\n")
    f = schedule.extract_labelled(text, 1788700000)
    assert "preinstall_ts" not in f                            # a size line is not a schedule
    assert f["maint_start_ts"] == 1788904800


def test_the_title_links_the_announcement_not_the_youtube_stream():
    media = {
        "url": "https://x.com/honkaistarrail/status/2099440781115211916",
        "youtube": "https://www.youtube.com/watch?v=drFgtruoPe8",
        "source": "X Post",
    }
    data, prov = {}, {}
    schedule.apply_program_media(data, prov, media, 1790000000)
    assert data["title_url"] == "https://x.com/honkaistarrail/status/2099440781115211916"
    assert data["source_url"] == "https://x.com/honkaistarrail/status/2099440781115211916"
    assert data["youtube_video"] == "https://www.youtube.com/watch?v=drFgtruoPe8"


def test_an_official_announcement_replaces_a_countdown_estimate():
    data = {
        "program_ts": 1790341813,
        "estimated": ["program_ts"],
        "estimate_sources": ["hsr-countdown"],
    }
    prov = {"program_ts": [schedule.PRIORITY["countdown"], 1789000000]}
    media = {
        "url": "https://x.com/honkaistarrail/status/2099440781115211916",
        "program_ts": 1789903800,
    }
    changed = schedule.apply_program_media(data, prov, media, 1790000000)
    assert data["program_ts"] == 1789903800
    assert "program_ts" in changed
    assert "estimated" not in data
    assert "estimate_sources" not in data
    assert prov["program_ts"] == [schedule.PRIORITY["news"], 1790000000]

    data2 = {
        "program_ts": 1790341813,
        "preinstall_ts": 1790229600,
        "estimated": ["program_ts", "preinstall_ts"],
        "estimate_sources": ["hsr-countdown"],
    }
    prov2 = {"program_ts": [schedule.PRIORITY["countdown"], 1789000000]}
    schedule.apply_program_media(data2, prov2, media, 1790000000)
    assert data2["program_ts"] == 1789903800
    assert data2["estimated"] == ["preinstall_ts"]
    assert data2["estimate_sources"] == ["hsr-countdown"]


def test_a_real_official_air_time_is_never_replaced_by_media():
    data = {"program_ts": 1789903800}
    prov = {"program_ts": [schedule.PRIORITY["hoyolab"], 1789000000]}
    media = {
        "url": "https://x.com/honkaistarrail/status/2099440781115211916",
        "program_ts": 1789999999,
    }
    changed = schedule.apply_program_media(data, prov, media, 1790000000)
    assert data["program_ts"] == 1789903800
    assert "program_ts" not in changed
    assert prov["program_ts"] == [schedule.PRIORITY["hoyolab"], 1789000000]


def test_the_footer_puts_each_note_on_its_own_small_text_line():
    d = {
        "version": "4.6",
        "estimated": ["program_ts"],
        "estimate_sources": ["hsr-countdown"],
    }
    p = cards.schedule_payload(GAMES["starrail"], d, settings(show_legend=True), cards.Ping(), None)
    flat = json.dumps(p, ensure_ascii=False)
    assert "• 🕒" not in flat
    box = p["components"][-1]
    foot_lines = [c["content"] for c in box["components"] if c["type"] == 10 and c["content"].startswith("-# ")]
    assert len(foot_lines) == 2
    assert foot_lines[0] == "-# STC — Subject to Change • TBA — To be Announced"
    assert foot_lines[1] == "-# 🕒 program time estimated from hsr-countdown — the official notice replaces it automatically"


def test_a_version_whose_air_time_is_only_an_estimate_is_looked_up_again():
    now = 1790000000
    # Gate reopens while air time is estimated
    rec_est = {"data": {"media_from": "X", "estimated": ["program_ts"]}}
    assert schedule.needs_program_lookup([], rec_est, now) is True

    rec_seen_est = {"data": {"program_seen": True, "estimated": ["program_ts"]}}
    assert schedule.needs_program_lookup([], rec_seen_est, now) is True

    # Gate closes once settled (not in estimated)
    rec_settled = {"data": {"media_from": "X"}}
    assert schedule.needs_program_lookup([], rec_settled, now) is False

    rec_seen_settled = {"data": {"program_seen": True}}
    assert schedule.needs_program_lookup([], rec_seen_settled, now) is False

    # Still respects the maintenance cutoff (+12 h)
    rec_cutoff = {
        "data": {
            "media_from": "X",
            "estimated": ["program_ts"],
            "maint_start_ts": now - 13 * 3600,
        }
    }
    assert schedule.needs_program_lookup([], rec_cutoff, now) is False


# =========================================================================== banner line-ups from the game wikis
WIKI_FIX = FIX / "gachawiki"


def _wiki(name: str) -> str:
    return (WIKI_FIX / name).read_text(encoding="utf-8")


def test_gachawiki_reads_every_dialect():
    """One parser, four wiki dialects — checked against pages captured live on 2026-10-06."""
    gi = gachawiki.build_lineup("genshin", _wiki("version_genshin_7_1.wiki"), {
        "When Warm Winds Cavort/2026-09-23": _wiki("banner_genshin_warm_winds.wiki"),
        "Surging Ballad/2026-09-23": _wiki("banner_genshin_surging_ballad.wiki"),
    }, four_star_count=3)
    assert gi["phase1"] == ["Vesna", "Vodyanitsa"]
    assert gi["phase2"] == ["Skirk", "Escoffier"]                  # inline names, no page needed
    assert gi["phase1_4"] == ["Bennett", "Xingqiu", "Sucrose"]

    hsr = gachawiki.build_lineup("starrail", _wiki("version_starrail_4_6.wiki"),
                                 {"An Ocean in a Pearl/2026-09-28": _wiki("banner_starrail_pearl.wiki")},
                                 four_star_count=3)
    # the sibling 'Light Cone Event Warps:' list must not leak into the character line-up
    assert hsr["phase1"] == ["Pearl", "Evanescia"] and hsr["phase2"] == ["Mortenax Blade"]

    zzz = gachawiki.build_lineup("zzz", _wiki("version_zzz_3_2.wiki"), {
        "Bloodmoon Rising/2026-09-09": _wiki("banner_zzz_bloodmoon.wiki"),
        "Cindernight Respite/2026-09-30": _wiki("banner_zzz_cindernight.wiki"),
    }, four_star_count=2)
    # the Version page nicknames its own agents ('Claret'); the banner page's full name wins
    assert zzz["phase1"] == ["Claret Flint", "Nangong Yu"]
    assert zzz["phase2"] == ["Roxy Ifrita Pryce", "Promeia"]
    assert zzz["phase1_4"] == ["Anton Ivanov", "Nicole Demara"]

    ww = gachawiki.build_lineup("wuwa", _wiki("version_wuwa_3_7.wiki"),
                                {"As Full as Tonight, Forever/2026-09-30": _wiki("banner_wuwa_tonight.wiki")},
                                four_star_count=3, four_star_summary=True)
    # WuWa marks no phases at all: a dated banner page is phase 1, an undated one phase 2
    assert ww["phase1"] == ["Hsin", "Chisa", "Iuno"]
    assert ww["phase2"] == ["Suoming", "Lucilla", "Lynae"]
    assert ww["four_star"] == ["Danjin", "Mortefi", "Yuanwu"]      # summary line is WuWa-only


def test_gachawiki_reruns_are_a_set_difference_against_the_debut_roster():
    """Never 'the title was used before': HSR reused 'Indelible Coterie' 14 times."""
    gi = gachawiki.build_lineup("genshin", _wiki("version_genshin_7_1.wiki"), {}, four_star_count=3)
    assert gi["reruns"] == ["Skirk", "Escoffier"]                  # Vesna/Vodyanitsa debut here
    zzz = gachawiki.build_lineup("zzz", _wiki("version_zzz_3_2.wiki"), {}, four_star_count=2)
    # 'Claret' is the nickname of the debuting 'Claret Flint' -> prefix match, not a re-run
    assert zzz["reruns"] == ["Nangong Yu", "Promeia"]


def test_gachawiki_drops_a_four_star_list_of_the_wrong_length():
    pages = {"Bloodmoon Rising/2026-09-09": _wiki("banner_zzz_bloodmoon.wiki")}
    ok = gachawiki.build_lineup("zzz", _wiki("version_zzz_3_2.wiki"), pages, four_star_count=2)
    assert ok["phase1_4"] == ["Anton Ivanov", "Nicole Demara"]
    wrong = gachawiki.build_lineup("zzz", _wiki("version_zzz_3_2.wiki"), pages, four_star_count=3)
    assert "phase1_4" not in wrong                                 # published half-right: never
    # placeholders in an unannounced slot are not names (Genshin: 'Unknown Character' x3)
    placeholder = gachawiki.build_lineup(
        "genshin", _wiki("version_genshin_7_1.wiki"),
        {"Void Star's Advent/2026-10-14": _wiki("banner_genshin_unannounced.wiki")}, four_star_count=3)
    assert "phase2_4" not in placeholder


def test_gachawiki_early_tier_is_confirmed_names_only():
    early = gachawiki.build_lineup("zzz", _wiki("version_zzz_3_3_early.wiki"), {}, four_star_count=2)
    assert early == {"confirmed": ["Phoenix Reffaella", "Severian Lowell"]}
    game = load_games(ROOT / "config" / "games.json")["zzz"]
    data, prov = {}, {}
    schedule.apply_gacha_wiki(game, data, prov, early, 1790000000)
    assert data["banners"]["confirmed"] == ["Phoenix Reffaella", "Severian Lowell"]
    card = cards.banners_block(game, {"version": "3.3", **data})
    assert "※ Confirmed: Phoenix Reffaella, Severian Lowell" in card
    # real phase data lands -> the early line deletes itself on the same silent edit
    schedule.apply_gacha_wiki(game, data, prov, {"phase1": ["Phoenix Reffaella"]}, 1790000001)
    assert "confirmed" not in data["banners"] and "b_confirmed" not in prov


def test_gachawiki_fills_only_blanks_and_never_outranks_an_official_notice():
    game = load_games(ROOT / "config" / "games.json")["genshin"]
    assert schedule.PRIORITY["bannerfeed"] < schedule.PRIORITY["gachawiki"] < schedule.PRIORITY["pattern"]
    data = {"banners": {"phase1": ["Official Name"]}}
    prov = {"b_phase1": [schedule.PRIORITY["hoyolab"], 1]}
    schedule.apply_gacha_wiki(game, data, prov, {"phase1": ["Wiki Name"], "phase2": ["Wiki Two"]}, 10)
    assert data["banners"]["phase1"] == ["Official Name"]          # official wins
    assert data["banners"]["phase2"] == ["Wiki Two"]               # the blank is filled


def test_zzz_four_star_line_says_default():
    """ZZZ's A-Rank rate-ups are player-customisable, so the wiki list is a default."""
    games = load_games(ROOT / "config" / "games.json")
    assert "(Default)" in cards.banners_block(games["zzz"], {"version": "3.2"})
    assert "(Default)" not in cards.banners_block(games["genshin"], {"version": "7.1"})


def test_wiki_display_names_render_full_forms_without_touching_stored_data():
    """The official notice writes 'Phoenix', the wiki 'Phoenix Reffaella' — the card shows
    the full name and the stored banner data never changes (live ZZZ 3.3, run #1349)."""
    game = load_games(ROOT / "config" / "games.json")["zzz"]
    wiki = gachawiki.build_lineup("zzz", _wiki("version_zzz_3_3.wiki"), {}, four_star_count=2)
    # the line-up the monitor read that morning, exactly what run #1349 stored from it
    assert wiki["phase1"] == ["Phoenix", "Velina Airgid"]
    assert wiki["phase2"] == ["Severian", "Norma Hollowell"]
    assert wiki["reruns"] == ["Velina Airgid", "Norma Hollowell"]
    # the card holds the official notice's short forms, all locked — the wiki read may not
    # touch any of them; all it may do is contribute the display map
    data = {"banners": {"phase1": ["Phoenix", "Velina"], "phase2": ["Severian", "Norma"],
                        "phase1_4": ["Seth", "Manato"], "reruns": ["Velina", "Norma"]},
            "banners_settled": ["phase1", "phase1_4", "phase2", "phase2_4", "reruns"]}
    prov = {f"b_{k}": [schedule.PRIORITY["hoyolab"], 1]
            for k in ("phase1", "phase1_4", "phase2", "phase2_4", "reruns")}
    schedule.apply_gacha_wiki(game, data, prov, wiki, 1790000000)
    assert data["banners"]["phase1"] == ["Phoenix", "Velina"]           # locked data untouched
    assert data["banners"]["reruns"] == ["Velina", "Norma"]
    assert data["banners_display"] == {"Phoenix": "Phoenix Reffaella", "Velina": "Velina Airgid",
                                       "Severian": "Severian Lowell", "Norma": "Norma Hollowell"}
    card = cards.banners_block(game, {"version": "3.3", **data})
    assert "✦ First Half/Phase: Phoenix Reffaella, Velina Airgid" in card
    assert "✦ Second Half/Phase: Severian Lowell, Norma Hollowell" in card
    assert "※ Re-runs: Velina Airgid, Norma Hollowell" in card
    # names the map does not hold render exactly as stored (the 4★ line, from the calendar)
    assert "- 4 Star Characters (Default): Seth, Manato" in card


def test_wiki_display_names_are_data_gated_per_game():
    """A wiki that states single names (GI, HSR, WuWa) produces no map and nothing changes;
    ZZZ 3.2's page states the full forms where the official notice wrote the short ones."""
    cases = (("genshin", "version_genshin_7_1.wiki", 3, ["Vesna", "Vodyanitsa", "Skirk", "Escoffier"]),
             ("starrail", "version_starrail_4_6.wiki", 3, ["Pearl", "Evanescia", "Mortenax Blade"]),
             ("wuwa", "version_wuwa_3_7.wiki", 3, ["Hsin", "Chisa", "Iuno", "Suoming", "Lucilla", "Lynae"]))
    for key, fixture, count, card_names in cases:
        wiki = gachawiki.build_lineup(key, _wiki(fixture), {}, four_star_count=count)
        assert schedule._display_map({"phase1": card_names, "phase2": card_names},
                                     wiki.get("candidates") or []) == {}, key
    zzz = gachawiki.build_lineup("zzz", _wiki("version_zzz_3_2.wiki"), {}, four_star_count=2)
    disp = schedule._display_map({"phase1": ["Claret", "Nangong Yu"], "phase2": ["Roxy", "Promeia"]},
                                 zzz.get("candidates") or [])
    # the golden 3.2 card shows the full forms because the WIKI wrote those phases; the map
    # makes the official notice's short forms render the same way — one card style
    assert disp == {"Claret": "Claret Flint", "Roxy": "Roxy Ifrita Pryce"}


def test_wuwa_four_star_summary_hides_once_both_phases_carry_lists():
    """The `※ 4 Star Characters:` summary is the pre-notice 4★ signal (and the phase-2 leak);
    once both per-phase lists are filled it is their union — noise — and hides on the same
    silent edit that filled the second phase."""
    game = load_games(ROOT / "config" / "games.json")["wuwa"]
    b = {"phase1": ["Hsin", "Chisa", "Iuno"], "phase1_4": ["Buling", "Taoqi", "Youhu"],
         "phase2": ["Suoming", "Lucilla", "Lynae"],
         "four_star": ["Buling", "Taoqi", "Youhu", "Lumi", "Danjin", "Yangyang"]}
    half = cards.banners_block(game, {"version": "3.7", "banners": dict(b)})
    assert "※ 4 Star Characters: Buling, Taoqi, Youhu, Lumi, Danjin, Yangyang" in half
    full = cards.banners_block(game, {"version": "3.7",
                                      "banners": dict(b, phase2_4=["Lumi", "Danjin", "Yangyang"])})
    assert "※ 4 Star Characters:" not in full                     # the union is above, per phase
    assert "- 4 Star Characters: Buling, Taoqi, Youhu" in full
    assert "- 4 Star Characters: Lumi, Danjin, Yangyang" in full


def test_edit_summary_names_banner_keys_and_lock_witnesses():
    """"banners" alone answers nothing: the edit line says WHICH keys moved and who wrote them,
    and a lock (a witness settling a name) gets its own receipt line with its witnesses."""
    before = {"banners": {"phase1": ["A"], "reruns": ["X"]}, "banners_settled": ["phase1"]}
    after = {"banners": {"phase1": ["A"], "phase1_4": ["a", "b"], "reruns": []},
             "banners_settled": ["phase1", "phase1_4"]}
    prov = {"b_phase1_4_by": ["calendar", "wiki"]}
    # phase1_4 was written (writer: calendar + wiki); reruns was withdrawn (no writer -> bare)
    assert schedule.banner_change_detail(before["banners"], after["banners"], prov) == \
        "phase1_4 ← calendar + wiki, reruns"
    assert schedule.newly_locked_lines(before, after, prov) == ["phase1_4 locked (calendar + wiki)"]
    # nothing moved, nothing settled -> silence
    assert schedule.banner_change_detail(after["banners"], after["banners"], prov) == ""
    assert schedule.newly_locked_lines(after, after, prov) == []
    # a display-only change (the wiki's full-name map landing) names itself too: run #1386's
    # silent edit would have read "— display names" instead of saying nothing at all
    only_display = dict(after, banners_display={"Phoenix": "Phoenix Reffaella"})
    labels = [lbl for key, lbl in schedule.CARD_FIELDS if after.get(key) != only_display.get(key)]
    assert labels == ["display names"], labels


def test_banner_block_complete_short_circuits_the_whole_fetch():
    games = load_games(ROOT / "config" / "games.json")
    full = {"banners": {"phase1": ["A"], "phase2": ["B"], "phase1_4": ["a", "b", "c"],
                        "phase2_4": ["a", "b", "c"], "reruns": ["B"]}}
    assert schedule.banner_block_complete(games["genshin"], full)
    partial = {"banners": dict(full["banners"], reruns=[])}
    assert not schedule.banner_block_complete(games["genshin"], partial)
    # WuWa also needs its summary line before it counts as complete
    assert not schedule.banner_block_complete(games["wuwa"], full)
    # ANANTA has no banner block at all -> nothing to ask any wiki for
    assert schedule.banner_block_complete(games["ananta"], {})

    calls: list[str] = []

    class _Fetcher:
        async def get_json(self, url, **kw):
            calls.append(url)
            if "action=parse" in url:
                return {"parse": {"wikitext": _wiki("version_zzz_3_2.wiki")}}
            return {"query": {"pages": [
                {"ns": 0, "title": "Bloodmoon Rising/2026-09-09",
                 "revisions": [{"slots": {"main": {"content": _wiki("banner_zzz_bloodmoon.wiki")}}}]},
                {"ns": 0, "title": "Cindernight Respite/2026-09-30",
                 "revisions": [{"slots": {"main": {"content": _wiki("banner_zzz_cindernight.wiki")}}}]},
                {"ns": 14, "title": "Category:noise"},
            ]}}

    out = asyncio.run(gachawiki.fetch_lineup(_Fetcher(), "zzz", "3.2", 2))
    assert len(calls) == 2                                          # two requests, no more
    assert "Version%2F3.2" in calls[0] and "%7C" in calls[1]
    assert out["phase1"] == ["Claret Flint", "Nangong Yu"]
    assert out["reruns"] == ["Nangong Yu", "Promeia"]
    assert gachawiki.WIKI_UA == "Game-Express (banner monitor)"      # no version number, ever
    assert "ananta" not in gachawiki.WIKIS and "hna" not in gachawiki.WIKIS


def test_gachawiki_zzz_third_bucket_and_multi_name_annotations():
    """ZZZ 3.1, captured live: the trap that broke the first build.

    'Lasting the whole version:' is a THIRD bucket and belongs to phase 1 — which makes phase 1
    the re-run Aria alongside the debuting Remielle. One annotation can also hold several
    agents ('Exclusive Rescreening' runs three).
    """
    out = gachawiki.build_lineup("zzz", _wiki("version_zzz_3_1.wiki"), {}, four_star_count=2)
    assert out["phase1"] == ["Remielle", "Aria"]
    assert out["phase2"] == ["Sigrid", "Dialyn", "Ukinami Yuzuha", "Asaba Harumasa"]
    # 'Remielle' / 'Sigrid' are nicknames of the debuting 'Remielle Dan' / 'Sigrid de L'Azur'
    assert out["reruns"] == ["Aria", "Dialyn", "Ukinami Yuzuha", "Asaba Harumasa"]
    # the sibling '====W-Engine Channels====' section never leaks in
    assert not any("Signal Search" in n for n in out["phase1"] + out["phase2"])


def test_gachawiki_lineup_reaches_the_rendered_card():
    """End to end: wiki output -> merge() -> the banner block a channel would see."""
    games = load_games(ROOT / "config" / "games.json")
    game = games["genshin"]
    wiki = gachawiki.build_lineup("genshin", _wiki("version_genshin_7_1.wiki"), {
        "When Warm Winds Cavort/2026-09-23": _wiki("banner_genshin_warm_winds.wiki"),
        "Surging Ballad/2026-09-23": _wiki("banner_genshin_surging_ballad.wiki"),
    }, four_star_count=3)
    record = {"data": {"banners": {"phase2": ["Official Phase 2"]}},
              "prov": {"b_phase2": [schedule.PRIORITY["hoyolab"], 1]}}
    data = schedule.merge(game, "7.1", [], record, {}, {}, 1790000000, wiki=wiki)
    block_text = cards.banners_block(game, data)
    assert "✦ First Half/Phase: Vesna, Vodyanitsa" in block_text
    assert "- 4 Star Characters: Bennett, Xingqiu, Sucrose" in block_text
    assert "✦ Second Half/Phase: Official Phase 2" in block_text    # official is never replaced
    assert "※ Re-runs: Skirk, Escoffier" in block_text
    assert "※ Confirmed:" not in block_text                          # phases exist -> no early line


def test_gachawiki_is_not_asked_when_it_cannot_help():
    """Every path that must cost zero requests: toggle off, no wiki, complete block, dead card."""
    games = load_games(ROOT / "config" / "games.json")

    class _Ctx:
        now = 1790000000

        def __init__(self, **kw):
            self.settings = settings(**kw)
            self.fetcher = None            # any real attempt would raise -> None proves no call

    complete = {"data": {"banners": {"phase1": ["A"], "phase2": ["B"], "phase1_4": ["a", "b", "c"],
                                     "phase2_4": ["a", "b", "c"], "reruns": ["B"]}}}
    assert asyncio.run(schedule.gather_wiki_lineup(_Ctx(), games["genshin"], "7.1", complete)) is None
    assert asyncio.run(schedule.gather_wiki_lineup(_Ctx(GACHA_WIKI="0"), games["genshin"], "7.1", {})) is None
    assert asyncio.run(schedule.gather_wiki_lineup(_Ctx(), games["ananta"], "1.0", {})) is None
    frozen = {"data": {"maint_start_ts": _Ctx.now - 60 * 86400}}
    assert asyncio.run(schedule.gather_wiki_lineup(_Ctx(), games["genshin"], "6.0", frozen)) is None


def test_countdown_ignores_a_banner_countdown_and_a_server_rendered_zero():
    now = LIVE_NOW
    banner_page = ("<p>Genshin Impact 7.1 Banner Countdown</p>"
                   "<p>12 Days 03 Hours 00 Minutes 00 Seconds</p>")
    assert countdown.parse_page("update", banner_page, now) is None      # banner != version
    assert countdown.parse_page("program", banner_page, now)["ts"] > now  # a livestream page is fine

    zeros = "<p>Countdown to Version 7.2</p><p>0 Days 0 Hours 0 Minutes 0 Seconds</p>"
    assert countdown.parse_page("update", zeros, now) is None            # JS has not filled it in
    real = "<p>7.2 update is set to release</p><p>10 Days 00 Hours 00 Minutes 00 Seconds</p>"
    hit = countdown.parse_page("update", real, now)
    assert hit["version"] == "7.2" and hit["ts"] == now + 10 * 86400
    past = "<p>Countdown to Version 7.2</p><p>Release Date & Time: Friday, October 23, 2020 at 8:00 AM EDT</p>"
    assert countdown.parse_page("update", past, now) is None             # already gone by


def test_workflow_updates_are_attended_and_package_guard_is_rename_aware():
    workflow = ROOT / ".github" / "workflows"
    source = (workflow / "ci.yml").read_text(encoding="utf-8")
    import json
    import shutil
    import subprocess

    if shutil.which("node"):
        start = source.index("const requiresWorkflowReview =")
        helper = source[start:source.index(";", start) + 1]
        fixtures = [
            ([{"filename": "requirements.txt"}], False),
            ([{"filename": ".github/workflows/monitor.yml"}], True),
            ([{"filename": "renamed.txt", "previous_filename": ".github/workflows/old.yml"}], True),
            ([{"filename": ".github/workflows/deleted.yml", "status": "removed"}], True),
            ([{"filename": "README.md"}], False),
            ([{"filename": "requirements.txt"}] * 110 + [{"filename": ".github/workflows/ci.yml"}], True),
        ]
        code = helper + "\nconst cases = " + json.dumps(fixtures) + ";\n"
        code += "for (const [files, want] of cases) { if (requiresWorkflowReview(files) !== want) process.exit(1); }"
        subprocess.run(["node", "-e", code], check=True, capture_output=True, text=True)
    assert "github.paginate(github.rest.pulls.listFiles" in source
    assert "file.previous_filename" in source
    bump = (workflow / "python_version_bump.yml").read_text(encoding="utf-8")
    assert "gh pr merge" not in bump
    assert "vars.AUTO_MERGE_PYTHON_BUMP" not in bump


def test_python_patch_selection_is_numeric_stable_and_one_series_at_a_time():
    import importlib.util
    from unittest import mock

    path = ROOT / ".github" / "scripts" / "python_version_bump.py"
    spec = importlib.util.spec_from_file_location("bump_exact_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def entry(version, *, stable=True, arch="x64"):
        return {"version": version, "stable": stable, "files": [{"platform": "linux", "arch": arch}]}

    current = (3, 14, 8)
    assert len(module.current_pin()) == 3
    assert module.next_stable_version([entry("3.14.9"), entry("3.14.10"), entry("3.15.2")], current) == (3, 14, 10)
    assert module.next_stable_version([entry("3.15.2"), entry("3.15.10"), entry("3.16.0")], current) == (3, 15, 10)
    assert module.next_stable_version([entry("3.16.0"), entry("4.0.0"), entry("3.15.0rc1"), entry("3.15.0", stable=False)], current) is None
    assert module.next_stable_version([entry("3.14.8"), entry("3.14.10", arch="arm64")], current) is None
    target = module.RUFF_TARGET_RE.search(Path(module.RUFF_TOML).read_text(encoding="utf-8")).group(2)
    with mock.patch.object(module, "ruff_understands", side_effect=AssertionError("unchanged target must not probe")):
        assert module.bump_ruff_toml(int(target[0]), int(target[1:])) is False


def test_python_bump_is_safe_in_a_docless_production_repo():
    """The bump must run in a repo that has monitor.yml and nothing else.

    A production repo carries no ci.yml, no ruff.toml, no README and no docs/. Before this
    was fixed the script raised FileNotFoundError on ci.yml and the weekly run went red.
    It must now read the pin from whatever workflow exists, rewrite only that, and never
    open a Markdown file for writing.
    """
    import importlib.util
    import re
    import shutil
    import tempfile
    from unittest import mock

    fake = [{"version": "3.15.99", "stable": True, "files": [{"platform": "linux", "arch": "x64"}]}]
    script = ROOT / ".github" / "scripts" / "python_version_bump.py"

    for shape in ("prod", "dev"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "scripts").mkdir(parents=True)
            shutil.copy(script, root / ".github" / "scripts" / "python_version_bump.py")
            shutil.copy(ROOT / ".github" / "workflows" / "monitor.yml",
                        root / ".github" / "workflows" / "monitor.yml")
            (root / "README.md").write_text("pinned to 3.14\n", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "PYTHON_VERSION.md").write_text("3.14\n", encoding="utf-8")
            if shape == "dev":
                shutil.copy(ROOT / ".github" / "workflows" / "ci.yml",
                            root / ".github" / "workflows" / "ci.yml")
                shutil.copy(ROOT / "ruff.toml", root / "ruff.toml")

            # Fixed synthetic pins keep this fixture valid after future real bumps.
            for workflow in (root / ".github" / "workflows").glob("*.yml"):
                text = workflow.read_text(encoding="utf-8")
                workflow.write_text(re.sub(r"(python-version:\s*')\d+\.\d+\.\d+(')",
                                           r"\g<1>3.14.8\2", text), encoding="utf-8")
            ruff = root / "ruff.toml"
            if ruff.exists():
                text = ruff.read_text(encoding="utf-8")
                ruff.write_text(re.sub(r'(target-version\s*=\s*")py\d+(")',
                                       r'\g<1>py314\2', text), encoding="utf-8")

            spec = importlib.util.spec_from_file_location(
                f"bump_{shape}", root / ".github" / "scripts" / "python_version_bump.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.fetch_manifest = lambda: fake

            out = root / "out.txt"
            env = dict(GITHUB_OUTPUT=str(out), PR_BODY_PATH=str(root / "body.md"))
            old = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            output = io.StringIO()
            try:
                # Python 3.15.99 is synthetic: never consult the runner's Ruff.
                with (mock.patch.object(mod, "ruff_understands", return_value=False) as supports,
                      contextlib.redirect_stdout(output)):
                    assert mod.main() == 0
                if shape == "dev":
                    supports.assert_called_once_with("py315")
                    assert "ruff does not recognise --target-version py315" in output.getvalue()
                else:
                    supports.assert_not_called()
                    assert "::warning::" not in output.getvalue()
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

            text = out.read_text(encoding="utf-8")
            assert "old_version=3.14.8" in text               # read from whatever exists
            assert "changed=true" in text
            # the Markdown in the repo is byte-identical afterwards
            assert (root / "README.md").read_text(encoding="utf-8") == "pinned to 3.14\n"
            assert (root / "docs" / "PYTHON_VERSION.md").read_text(encoding="utf-8") == "3.14\n"
            # and the staged set never names one
            staged = [ln for ln in text.splitlines()
                      if ln.endswith((".yml", ".toml")) or ln.startswith("changed_files")]
            assert staged and not any(".md" in ln for ln in staged), staged
            assert "'3.15.99'" in (root / ".github" / "workflows" / "monitor.yml").read_text(
                encoding="utf-8")
            if shape == "prod":
                assert "changed_files=.github/workflows/monitor.yml" in text
                assert "ruff_bumped=false" in text

    src = script.read_text(encoding="utf-8")
    assert "ALLOWED_WRITES" in src and '"ci.yml", "monitor.yml", "python_version_bump.yml", "ruff.toml"' in src


def test_the_documented_nitter_fleet_size_matches_the_code():
    """Three docs quote a mirror count; the list they describe lives in config.py.

    Those numbers rotted before (SECURITY.md said 18 after xcancel.com was suspended, SOURCES.md
    still said 15). Pin them to the code so the next change to the fleet fails here instead of
    in a reader's head.
    """
    from gamexpress.config import DEFAULT_NITTER

    configured = len(DEFAULT_NITTER)
    suspended = [u for u in DEFAULT_NITTER if "xcancel" in u]      # kept last, cannot answer
    live = configured - len(suspended)
    assert (configured, live) == (21, 20)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    sources = (ROOT / "docs" / "SOURCES.md").read_text(encoding="utf-8")
    security = (ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    assert f"{configured} entries, {live} live" in readme
    assert f"{configured} entries, {live} live" in sources
    assert f"{live} nitter mirrors" in security


# =========================================================================== announcement lock (2026-10-09)
# ZZZ 3.3, live on 2026-10-09: the card's title link and key art switched to ZZZ_EN's 05:00 UTC
# "Share to Win Master Tape x10" giveaway, which repeats the air time and so classifies as a Special
# Program post. The announcement itself (2026-10-05 04:00 UTC) had aged out of the 72 h window.
# The texts below are the real fxtwitter bodies of both posts.
ZZZ33_ANN_URL = "https://x.com/ZZZ_EN/status/2106957553435312559"
ZZZ33_ANN_IMG = "https://pbs.twimg.com/media/HTxU4O3W4AAGolV.jpg?name=orig"
ZZZ33_GIVE_URL = "https://x.com/ZZZ_EN/status/2108422202974499088"
ZZZ33_GIVE_IMG = "https://pbs.twimg.com/media/HUGjhQZWUAAiOJd.jpg?name=orig"
ZZZ33_AIRS = 1791545400                      # 2026-10-09 11:30 UTC = 19:30 UTC+8
ZZZ33_ANN_TEXT = (
    "Zenless Zone Zero Version 3.3 \"Journey to the Sky\" Special Program Announcement\n\n"
    "Dear Proxies,\nThe Zenless Zone Zero Version 3.3 \"Journey to the Sky\" Special Program will begin on October 9 at 19:30 (UTC+8)!\n"
    "How will Phoenix, descending from the sky, rewrite the fate of the Proxies?\n"
    "Severian, acting on the mayor's orders, has suddenly appeared on Quietude Isle!? How will his presence shake up the ever-shifting situation in Roscaelifer?\n"
    "The story behind it all will be revealed by Mr. Z and these two new Agents!\n\n"
    "We will also be dropping an exclusive redemption code during the livestream, so make sure you tune in on time. Don't miss out, Proxies!\n\n"
    "Follow our official livestream channels:\nYouTube>> https://www.youtube.com/@ZZZ_Official/\nTwitch>> https://www.twitch.tv/zenlesszonezero\n"
    "TikTok>> https://www.tiktok.com/@zenlesszonezero\n\n#zzzero #zenlesszonezero\n#zzzSpecialProgram")
ZZZ33_GIVE_TEXT = (
    "🎁 #zzzSpecialProgram – Share to Win Master Tape ×10\n\n"
    "Dear Proxies, the Zenless Zone Zero Version 3.3 Special Program will air on October 9 at 19:30 (UTC+8)!\n"
    "This special program will unveil new game content and updates for the upcoming version. During the show, you'll also have the chance to receive redemption codes and other exciting gifts!\n\n"
    "Livestream Channels:\nTwitch: https://www.twitch.tv/zenlesszonezero\nYouTube: https://www.youtube.com/@ZZZ_Official\n\n"
    "▼ Event Rewards\nMaster Tape ×10: 50 Winners\n\n▼ How to Participate\n1. Follow our official account\n"
    "2. Quote this post along with the hashtags #zzzSpecialProgram #zzzero\n\n▼ Event Duration\nOctober 9 – October 11\n\n"
    "▼ Event Rules\nhttps://hoyo.link/5ADjFCAL\n\n#zzzero #zzzSpecialProgram")


def _zzz33_announcement() -> Item:
    return Item("x", "zzz", "2106957553435312559", ZZZ33_ANN_URL, "", ZZZ33_ANN_TEXT, 1791172800, [ZZZ33_ANN_IMG])


def _zzz33_giveaway() -> Item:
    return Item("x", "zzz", "2108422202974499088", ZZZ33_GIVE_URL, "", ZZZ33_GIVE_TEXT, 1791522000, [ZZZ33_GIVE_IMG])


def _merge_zzz33(record: dict, items: list[Item], now: int, override: dict | None = None) -> dict:
    """One run's merge for ZZZ 3.3, called the way _handle_version calls it."""
    extracts = [e for e in (schedule.extract(GAMES["zzz"], it) for it in items) if e]
    data = schedule.merge(GAMES["zzz"], "3.3", extracts, record, override or {}, {}, now, [])
    record["data"] = data
    return data


def test_the_announcement_that_opened_a_card_keeps_its_link_and_key_art():
    """The incident, step by step. Both posts classify as the same programme with the same air
    time, so the only thing that can keep the card on the announcement is remembering it."""
    assert schedule.extract(GAMES["zzz"], _zzz33_giveaway()).fields["program_ts"] == ZZZ33_AIRS
    record: dict = {}
    first = _merge_zzz33(record, [_zzz33_announcement()], now=1791172900)
    assert first["title_url"] == ZZZ33_ANN_URL and first["source_url"] == ZZZ33_ANN_URL
    assert first["images"] == [ZZZ33_ANN_IMG]
    assert first["program_ts"] == ZZZ33_AIRS
    assert first["announcement_locked"] is True
    # four days later the announcement has aged out of the window; only the giveaway is left
    later = _merge_zzz33(record, [_zzz33_giveaway()], now=1791522100)
    assert later["title_url"] == ZZZ33_ANN_URL and later["source_url"] == ZZZ33_ANN_URL
    assert later["images"] == [ZZZ33_ANN_IMG]
    assert later["source_links"] == [("X Post", ZZZ33_ANN_URL)]
    assert later["program_ts"] == ZZZ33_AIRS
    # and when both are in the window, the giveaway (newer) still does not take over
    both = _merge_zzz33(record, [_zzz33_giveaway(), _zzz33_announcement()], now=1791522200)
    assert both["title_url"] == ZZZ33_ANN_URL and both["images"] == [ZZZ33_ANN_IMG]
    assert not any(ZZZ33_GIVE_URL in u for _, u in both["source_links"])


def test_after_the_lock_only_banners_and_maintenance_details_move():
    record: dict = {}
    _merge_zzz33(record, [_zzz33_announcement()], now=1791172900)
    notice = Item("hoyolab", "zzz", "notice-3.3", "https://www.hoyolab.com/article/46970001",
                  "Version 3.3 Update and Maintenance Notice", "maintenance", 1791522000)
    banners = Item("hoyolab", "zzz", "banner-3.3", "https://www.hoyolab.com/article/46970002",
                   "Version 3.3 Banners", "banners", 1791522000)
    extracts = [schedule.Extract(item=notice, kind="maintenance", version="3.3",
                                 fields={"maint_start_ts": 1792533600, "maint_end_ts": 1792551600,
                                         "preinstall_ts": 1792382400}),
                schedule.Extract(item=banners, kind="banner", version="3.3",
                                 fields={"banner_phase": 1, "banner_five": ["Phoenix Reffaella"]}),
                schedule.Extract(item=_zzz33_giveaway(), kind="program", version="3.3",
                                 fields={"program_ts": ZZZ33_AIRS})]
    data = schedule.merge(GAMES["zzz"], "3.3", extracts, record, {}, {}, 1791522300, [])
    assert data["maint_start_ts"] == 1792533600 and data["maint_end_ts"] == 1792551600   # maintenance moved
    assert data["preinstall_ts"] == 1792382400
    assert data["banners"]["phase1"] == ["Phoenix Reffaella"]                              # banner moved
    assert data["title_url"] == ZZZ33_ANN_URL and data["images"] == [ZZZ33_ANN_IMG]       # link did not


def test_a_later_lookup_cannot_move_a_locked_card_but_may_fill_an_estimate():
    locked = {"version": "3.3", "title_url": ZZZ33_ANN_URL, "source_url": ZZZ33_ANN_URL,
              "source_label": "X Post", "source_links": [("X Post", ZZZ33_ANN_URL)],
              "images": [ZZZ33_ANN_IMG], "program_seen": True, "announcement_locked": True,
              "program_ts": ZZZ33_AIRS}
    other = {"url": "https://www.hoyolab.com/article/46970009", "source": "HoYoLAB",
             "images": ["https://img.example/other-key-art.jpg"], "program_ts": ZZZ33_AIRS + 3600}
    now = 1791522100
    data = dict(locked)
    assert schedule.apply_program_media(data, {}, other, now) == []        # official air time: kept
    assert data == locked
    # a countdown estimate of the air time IS replaced -- the link and key art still are not
    estimate = dict(locked, program_ts=1791500000, estimated=["program_ts"])
    changed = schedule.apply_program_media(estimate, {}, other, now)
    assert "program_ts" in changed and estimate["program_ts"] == ZZZ33_AIRS + 3600
    assert estimate["title_url"] == ZZZ33_ANN_URL and estimate["images"] == [ZZZ33_ANN_IMG]
    # a card written before the flag existed is locked by what it already shows
    legacy = {k: v for k, v in locked.items() if k != "announcement_locked"}
    schedule.apply_program_media(legacy, {}, other, now)
    assert legacy["title_url"] == ZZZ33_ANN_URL and legacy["images"] == [ZZZ33_ANN_IMG]
    # and an UNLOCKED card still adopts the lookup, which then locks it
    fresh: dict = {"version": "3.3"}
    schedule.apply_program_media(fresh, {}, other, now)
    assert fresh["title_url"] == other["url"] and fresh["announcement_locked"] is True


def test_a_teaser_without_an_air_time_does_not_lock_the_card():
    """A post that gives no air time cannot be the announcement the card is locked to. It may show
    until the real one arrives, and the real one (with the air time) then takes the card."""
    record: dict = {}
    teaser = Item("x", "zzz", "2100000000000000001", "https://x.com/ZZZ_EN/status/2100000000000000001",
                  "", "Version 3.3 is coming soon", 1791100000, [])
    first = schedule.merge(GAMES["zzz"], "3.3", [schedule.Extract(item=teaser, kind="program",
                                                                  version="3.3", fields={})],
                           record, {}, {}, 1791100100, [])
    assert first["announcement_locked"] is False and first["title_url"] == teaser.url
    record["data"] = first
    second = _merge_zzz33(record, [_zzz33_announcement()], now=1791172900)
    assert second["title_url"] == ZZZ33_ANN_URL and second["program_ts"] == ZZZ33_AIRS
    assert second["announcement_locked"] is True


def test_a_title_override_pins_the_link_and_the_source_button_together():
    record: dict = {}
    _merge_zzz33(record, [_zzz33_giveaway()], now=1791522100)           # the wrong post, locked
    fixed = _merge_zzz33(record, [_zzz33_giveaway()], now=1791522200,
                         override={"title_url": ZZZ33_ANN_URL, "image": ZZZ33_ANN_IMG})
    assert fixed["title_url"] == ZZZ33_ANN_URL and fixed["source_url"] == ZZZ33_ANN_URL
    assert fixed["source_links"] == [("X Post", ZZZ33_ANN_URL)]          # no stale second button
    assert fixed["images"] == [ZZZ33_ANN_IMG]


def test_the_shipped_zzz_33_override_repairs_the_card_that_was_switched():
    """The card as it stood after the 05:00 UTC run: a record from before the flag existed, locked
    to the giveaway. config/overrides.json must restore the announcement's own X post and its photo."""
    from gamexpress.sources.twitter import program_seed
    assert program_seed("zzz", "3.3").get("id") == "2106957553435312559"   # the seed agrees
    broken = {"data": {"version": "3.3", "title_url": ZZZ33_GIVE_URL, "source_url": ZZZ33_GIVE_URL,
                       "source_label": "X Post", "source_links": [("X Post", ZZZ33_GIVE_URL)],
                       "images": [ZZZ33_GIVE_IMG], "program_seen": True, "program_ts": ZZZ33_AIRS}}
    override = load_overrides()["zzz"]["3.3"]
    fixed = _merge_zzz33(broken, [_zzz33_giveaway()], now=1791522200, override=override)
    assert fixed["title_url"] == ZZZ33_ANN_URL and fixed["source_url"] == ZZZ33_ANN_URL
    assert fixed["source_links"] == [("X Post", ZZZ33_ANN_URL)]            # no stale giveaway button
    assert fixed["source_label"] == "X Post"
    assert fixed["images"] == [ZZZ33_ANN_IMG]
    assert fixed["program_ts"] == ZZZ33_AIRS                                # the air time was never wrong


def test_the_timeline_seed_is_the_first_announcement_not_a_later_reminder():
    """The timeline is newest-first. The seed must be the OLDEST match, or the reminder becomes the
    announcement that gets cached and then locked."""
    import asyncio

    from gamexpress.sources import twitter as tw

    saved: list[dict] = []
    ann = {"id": "2200000000000000001", "ts": 1000, "title": "", "links": [], "images": [],
           "text": "Zenless Zone Zero Version 3.4 Special Program Announcement\n\nThe Version 3.4 Special Program will begin on October 9 at 19:30 (UTC+8)!"}
    reminder = {"id": "2200000000000000002", "ts": 2000, "title": "", "links": [], "images": [],
                "text": "Dear Proxies, the Version 3.4 Special Program will air on October 9 at 19:30 (UTC+8)!"}

    client = tw.XClient(None, settings())

    async def timeline(account):
        return [reminder, ann]                                 # newest first, as in production

    async def tweet(tweet_id, account=None):
        return None                                            # fall back to the timeline entry

    client.timeline, client.tweet = timeline, tweet
    real_save = tw.save_program_seed
    tw.save_program_seed = lambda game_key, version, entry: saved.append(entry) or True
    try:
        item = asyncio.run(client.program_tweet(GAMES["zzz"], "3.4"))
    finally:
        tw.save_program_seed = real_save
    assert item is not None and item.id == ann["id"]
    assert saved and saved[0]["id"] == ann["id"]


# ---- nitter.cf first for single tweets (2026-10-09) -------------------------------------------
NIT_ID = "2091737263398862915"                         # ZZZ 3.2 announcement, 2026-08-24 04:00 UTC


def _nit_rss(description: str, status_id: str = NIT_ID) -> str:
    """A per-status feed in nitter's shape: the status link, pubDate, creator and HTML description."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
        "<title>Thread by @ZZZ_EN</title><item>"
        "<title>Zenless Zone Zero Version 3.2 announcement</title>"
        f"<link>https://nitter.cf/ZZZ_EN/status/{status_id}#m</link>"
        "<dc:creator>@ZZZ_EN</dc:creator>"
        "<pubDate>Mon, 24 Aug 2026 04:00:00 GMT</pubDate>"
        f"<guid>https://nitter.cf/ZZZ_EN/status/{status_id}#m</guid>"
        f"<description>{description}</description>"
        "</item></channel></rss>"
    )


NIT_PLAIN = (
    "&lt;p&gt;Zenless Zone Zero Version 3.2 &quot;Their Secret Histories&quot; Special Program Announcement"
    "&lt;/p&gt;&lt;p&gt;The Version 3.2 Special Program will begin on August 28 at 19:30 (UTC+8)!&lt;/p&gt;"
    "&lt;p&gt;#zzzSpecialProgram&lt;/p&gt;"
    '&lt;img src="https://nitter.cf/pic/https%3A%2F%2Fpbs%2Etwimg%2Ecom%2Fmedia%2FHQZWs%2D3WAAEj32y%2Ejpg"/&gt;'
)
NIT_TCO = NIT_PLAIN.replace(
    "#zzzSpecialProgram&lt;/p&gt;",
    '#zzzSpecialProgram&lt;/p&gt;&lt;p&gt;YouTube&amp;gt;&amp;gt; '
    '&lt;a href="https://nitter.cf/t.co/x1mTuVPngd"&gt;https://nitter.cf/t.co/x1mTuVPngd&lt;/a&gt;&lt;/p&gt;')


class _NitterThenFx:
    """Fetcher stand-in: nitter.cf serves `nitter_body` (xitter.cf serves nothing), FxTwitter serves
    `fx`. It records the source of every request in order, which is what these tests check."""
    def __init__(self, nitter_body=None, fx=None):
        self.nitter_body, self.fx, self.calls = nitter_body, fx, []

    async def get_text(self, url, source="", **k):
        self.calls.append(source)
        return self.nitter_body if "nitter.cf/" in url else None

    async def get_json(self, url, source="", **k):
        self.calls.append(source)
        return self.fx if source == "fxtwitter" else None


def _fx_body(links=()):
    return {"tweet": {"id": NIT_ID, "text": "fxtwitter's text", "created_timestamp": 1787544000,
                      "author": {"screen_name": "ZZZ_EN"}, "url": f"https://x.com/ZZZ_EN/status/{NIT_ID}",
                      "raw_text": {"facets": [{"type": "url", "replacement": u} for u in links]}}}


def test_a_single_tweet_is_read_from_nitter_before_fxtwitter():
    """With the account known, nitter.cf answers first and FxTwitter is not asked at all."""
    from gamexpress.sources import twitter as tw

    f = _NitterThenFx(nitter_body=_nit_rss(NIT_PLAIN), fx=_fx_body())
    c = tw.XClient(f, settings())
    t = asyncio.run(c.tweet(NIT_ID, "ZZZ_EN"))
    assert f.calls == ["nitter"]
    assert c.source_used == {"nitter": 1}
    assert t["text"].startswith("Zenless Zone Zero Version 3.2") and t["ts"] == 1787544000
    assert t["author"] == "ZZZ_EN" and t["url"] == f"https://x.com/ZZZ_EN/status/{NIT_ID}"
    assert t["photos"] == ["https://pbs.twimg.com/media/HQZWs-3WAAEj32y.jpg"]


def test_a_t_co_link_nitter_cannot_expand_is_expanded_by_fxtwitter():
    """nitter's RSS leaves t.co wrapped. The YouTube link the card reads must come from FxTwitter's
    expanded facets, so the nitter answer alone is not final when such a link is present."""
    from gamexpress.sources import twitter as tw

    stream = "https://www.youtube.com/@ZZZ_Official/"
    f = _NitterThenFx(nitter_body=_nit_rss(NIT_TCO), fx=_fx_body([stream]))
    c = tw.XClient(f, settings())
    t = asyncio.run(c.tweet(NIT_ID, "ZZZ_EN"))
    assert f.calls == ["nitter", "fxtwitter"]
    assert stream in t["links"]
    assert c.source_used == {"fxtwitter": 1}


def test_when_nitter_is_silent_fxtwitter_still_answers():
    from gamexpress.sources import twitter as tw

    f = _NitterThenFx(nitter_body=None, fx=_fx_body())
    c = tw.XClient(f, settings())
    t = asyncio.run(c.tweet(NIT_ID, "ZZZ_EN"))
    assert f.calls == ["nitter", "nitter", "fxtwitter"]                 # nitter.cf, then xitter.cf
    assert t["text"] == "fxtwitter's text" and c.source_used == {"fxtwitter": 1}


def test_a_nitter_feed_for_another_status_is_never_taken_as_this_tweet():
    from gamexpress.sources import twitter as tw

    f = _NitterThenFx(nitter_body=_nit_rss(NIT_PLAIN, status_id="2106957553435312559"), fx=_fx_body())
    c = tw.XClient(f, settings())
    t = asyncio.run(c.tweet(NIT_ID, "ZZZ_EN"))
    assert t["text"] == "fxtwitter's text"
    assert c.source_used == {"fxtwitter": 1}


def test_without_an_account_nitter_is_not_asked():
    from gamexpress.sources import twitter as tw

    f = _NitterThenFx(nitter_body=_nit_rss(NIT_PLAIN), fx=_fx_body())
    c = tw.XClient(f, settings())
    asyncio.run(c.tweet(NIT_ID))
    assert f.calls == ["fxtwitter"]


# ---- source preference and the lock (2026-10-09, the X-first rule) ------------------------------
HOY_ANN_URL = "https://www.hoyolab.com/article/46972907"        # the fallback announcement


def _zzz33_hoyolab_announcement() -> Item:
    """The same announcement from HoYoLAB, published three seconds after the X post."""
    return Item("hoyolab", "zzz", "46972907", HOY_ANN_URL, "Zenless Zone Zero Version 3.3 Announcement",
                ZZZ33_ANN_TEXT, 1791172803, ["https://upload-os-bbs.hoyolab.com/upload/2026/09/30/"
                                              "c32ff7216961efbcd3fc83ef9f256f36_7613960778498299818.jpg"])


def test_when_x_and_hoyolab_both_carry_the_announcement_the_lock_goes_to_x():
    """X is the preferred source, so when both posts are visible in the same run, the card's link
    and key art are X's post and its photo, and HoYoLAB is only the second button."""
    record: dict = {}
    data = _merge_zzz33(record, [_zzz33_hoyolab_announcement(), _zzz33_announcement()], now=1791172900)
    assert data["title_url"] == ZZZ33_ANN_URL and data["images"] == [ZZZ33_ANN_IMG]
    assert data["announcement_source"] == "x"
    assert [lbl for lbl, _ in data["source_links"]][0] == "X Post"


def test_a_hoyolab_lock_taken_while_x_was_down_is_upgraded_once_to_the_same_x_announcement():
    """X was unreadable when the card was first seen, so HoYoLAB held it. Once X can be read again,
    its announcement (published first, same air time) takes the link and key art -- once."""
    record: dict = {}
    first = _merge_zzz33(record, [_zzz33_hoyolab_announcement()], now=1791172900)
    assert first["title_url"] == HOY_ANN_URL and first["announcement_source"] == "hoyolab"
    later = _merge_zzz33(record, [_zzz33_hoyolab_announcement(), _zzz33_announcement()], now=1791180000)
    assert later["title_url"] == ZZZ33_ANN_URL and later["images"] == [ZZZ33_ANN_IMG]
    assert later["announcement_source"] == "x"
    # the giveaway appears afterwards with the same air time: it is published later, so it never upgrades
    # and never re-pins -- the X announcement stays
    after = _merge_zzz33(record, [_zzz33_hoyolab_announcement(), _zzz33_announcement(), _zzz33_giveaway()],
                         now=1791530000)
    assert after["title_url"] == ZZZ33_ANN_URL and after["images"] == [ZZZ33_ANN_IMG]


def test_a_giveaway_never_upgrades_a_hoyolab_lock():
    """The incident in the other direction: a later X post repeating the air time cannot upgrade a
    card that is locked to HoYoLAB, because it was published after that lock."""
    record: dict = {}
    _merge_zzz33(record, [_zzz33_hoyolab_announcement()], now=1791172900)
    after = _merge_zzz33(record, [_zzz33_hoyolab_announcement(), _zzz33_giveaway()], now=1791530000)
    assert after["title_url"] == HOY_ANN_URL and after["announcement_source"] == "hoyolab"
    assert after["images"] != [ZZZ33_GIVE_IMG]


def test_the_lock_holds_in_every_game_the_incident_can_reach():
    """The lock lives in merge(), shared by all games. For each game with a programme pattern, an
    announcement with an air time locks the card, and a same-air-time giveaway that arrives after the
    announcement has aged out cannot move the link or the key art."""
    for key, game in GAMES.items():
        if not game.enabled:
            continue
        word = game.program_patterns[0].title()
        ann_text = (f"{game.short} Version 9.9 {word} Announcement\n\nThe Version 9.9 {word} will begin on "
                    "October 20 at 19:30 (UTC+8)!")
        gw_text = (f"Share to win! The Version 9.9 {word} begins on October 20 at 19:30 (UTC+8). "
                   "Retweet and reply to join the giveaway.")
        now0 = 1792000000 - 3 * 86400
        ann = Item("x", key, "1000000000000000001", "https://x.com/A/status/1000000000000000001", "",
                   ann_text, now0 - 3600, ["https://pbs.twimg.com/media/ANN.jpg?name=orig"])
        gw = Item("x", key, "1000000000000000002", "https://x.com/A/status/1000000000000000002", "",
                  gw_text, now0 + 5 * 86400, ["https://pbs.twimg.com/media/GIVE.jpg?name=orig"])
        record: dict = {}
        for items, now in (([ann], now0), ([gw], now0 + 5 * 86400)):
            extracts = [e for e in (schedule.extract(game, it) for it in items) if e]
            data = schedule.merge(game, "9.9", extracts, record, {}, {}, now, [])
            record["data"] = data
        assert data["title_url"] == ann.url and data["images"] == ann.images, key


def test_a_dead_nitter_status_mirror_is_not_waited_on_for_every_tweet():
    """With nitter.cf and xitter.cf both down, each tweet used to wait on both before FxTwitter.
    After STATUS_FAIL_LIMIT failures the mirrors are skipped for the run."""
    from gamexpress.sources import twitter as tw

    class Dead:
        def __init__(self):
            self.calls = []

        async def get_text(self, url, source="", **k):
            self.calls.append(source)
            return None                                      # every status mirror is down

        async def get_json(self, url, source="", **k):
            self.calls.append(source)
            return _fx_body() if source == "fxtwitter" else None

    f = Dead()
    c = tw.XClient(f, settings())
    for n in range(5):
        asyncio.run(c.tweet(str(1000 + n), "ZZZ_EN"))
    assert f.calls.count("nitter") == tw.STATUS_FAIL_LIMIT * len(tw.STATUS_MIRRORS)
    assert f.calls.count("fxtwitter") == 5


def test_a_countdown_estimate_is_replaced_by_the_official_notice_in_every_game():
    """The speculation / countdown mechanism is native and game-independent: a maintenance start that is
    only a countdown estimate is labelled as one, and an official notice replaces it and clears the
    label, whichever game the card belongs to."""
    now = 1791522000
    for key, game in GAMES.items():
        if not game.enabled:
            continue
        estimate = 1792300000
        official = 1792380000
        record = {"data": {"version": "9.9", "maint_start_ts": estimate, "estimated": ["maint_start_ts"],
                           "estimate_sources": ["version cadence"]},
                  "prov": {"maint_start_ts": [schedule.PRIORITY["countdown"], now - 3600]}}
        notice = schedule.Extract(
            Item("hoyolab", key, "notice-9.9", f"https://www.hoyolab.com/article/{key}-9.9-notice",
                 "Version 9.9 Update Maintenance Notice", "official", now),
            "maintenance", "9.9", fields={"maint_start_ts": official})
        data = schedule.merge(game, "9.9", [notice], record, {}, {}, now, [])
        assert data["maint_start_ts"] == official, key
        assert "maint_start_ts" not in (data.get("estimated") or []), key


# ---- banner confirmation and lock (2026-10-09) -------------------------------------------------
# A banner name is confirmed when two different groups name it (official / hub / wiki); an override
# confirms alone. A confirmed name is locked, so a later notice or wiki reading cannot change it.
def _banner_notice(game_key: str, source: str, url: str, phase: int, five: list[str], ts: int,
                   four: list[str] | None = None, unsure: bool = False) -> schedule.Extract:
    item = Item(source, game_key, url.rsplit("/", 1)[-1], url, f"Version 9.9 Banner Phase {phase}", "", ts)
    fields = {"banner_five": five, "banner_phase": phase, "banner_four": four or [],
              "banner_four_unsure": unsure}
    return schedule.Extract(item, "banner", "9.9", fields=fields)


def test_an_official_notice_locks_on_its_own_and_a_later_different_one_is_logged_not_applied():
    game, now, rec = GAMES["zzz"], 1791522000, {}
    first = _banner_notice("zzz", "x", "https://x.com/ZZZ_EN/status/1", 1, ["Phoenix Reffaella"], now - 3600)
    rec["data"] = schedule.merge(game, "9.9", [first], rec, {}, {}, now)
    assert rec["data"]["banners_settled"] == ["phase1"]          # one OFFICIAL notice is the confirmation
    later = _banner_notice("zzz", "hoyolab", "https://www.hoyolab.com/article/2", 1, ["Severian Lowell"], now - 60)
    notes: list[str] = []
    rec["data"] = schedule.merge(game, "9.9", [later], rec, {}, {}, now + 600, notes)
    assert rec["data"]["banners"]["phase1"] == ["Phoenix Reffaella"]    # final: the conflict is logged
    assert any("locked official name is kept" in n for n in notes)
    rec["data"] = schedule.merge(game, "9.9", [later], rec, {}, {}, now + 1200, notes)
    assert sum("locked official name is kept" in n for n in notes) == 1  # logged once, not every run


def test_a_community_lock_is_corrected_by_a_later_official_notice():
    """Feed and wiki agreement locks a name, but an official notice outranks both and replaces it."""
    game, now, rec = GAMES["zzz"], 1791522000, {}
    rec["data"] = schedule.merge(game, "9.9", [], rec, {}, {}, now)
    schedule.apply_banner_feed(rec["data"], rec["prov"],
                               {"phase1": ["Lycaon"], "phase2": [], "titles": []}, now)
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"], {"phase1": ["Lycaon"]}, now + 60)
    assert rec["data"]["banners_settled"] == ["phase1"]          # feed + wiki agree -> locked
    official = _banner_notice("zzz", "hoyolab", "https://www.hoyolab.com/article/9", 1, ["Soukaku"], now + 120)
    rec["data"] = schedule.merge(game, "9.9", [official], rec, {}, {}, now + 600)
    assert rec["data"]["banners"]["phase1"] == ["Soukaku"]
    assert rec["data"]["banners_settled"] == ["phase1"]


def test_an_official_banner_locks_when_the_hub_confirms_it_and_never_changes_after():
    game, now, rec = GAMES["zzz"], 1791522000, {}
    notice = _banner_notice("zzz", "x", "https://x.com/ZZZ_EN/status/1", 1, ["Phoenix Reffaella"], now - 3600)
    rec["data"] = schedule.merge(game, "9.9", [notice], rec, {}, {}, now)
    schedule.apply_banner_feed(rec["data"], rec["prov"],
                               {"phase1": ["Phoenix Reffaella"], "phase2": [], "titles": []}, now)
    assert rec["data"]["banners_settled"] == ["phase1"]
    newer = _banner_notice("zzz", "hoyolab", "https://www.hoyolab.com/article/3", 1, ["Severian Lowell"], now + 60)
    rec["data"] = schedule.merge(game, "9.9", [newer], rec, {}, {}, now + 600)
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"], {"phase1": ["Someone Else"]}, now + 900)
    assert rec["data"]["banners"]["phase1"] == ["Phoenix Reffaella"]
    assert rec["data"]["banners_settled"] == ["phase1"]


def test_the_wiki_confirming_an_official_banner_locks_it_too():
    game, now, rec = GAMES["zzz"], 1791522000, {}
    notice = _banner_notice("zzz", "hoyolab", "https://www.hoyolab.com/article/4", 2, ["Ellen Joe"], now - 3600)
    rec["data"] = schedule.merge(game, "9.9", [notice], rec, {}, {}, now)
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"], {"phase2": ["Ellen Joe"]}, now + 60)
    assert "phase2" in rec["data"]["banners_settled"]
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"], {}, now + 120)   # a later, empty read
    assert rec["data"]["banners"]["phase2"] == ["Ellen Joe"]


def test_a_wiki_only_early_tier_line_never_locks():
    game, now, rec = GAMES["zzz"], 1791522000, {}
    rec["data"], rec["prov"] = {"version": "9.9"}, {}
    for i in range(3):
        schedule.apply_gacha_wiki(game, rec["data"], rec["prov"],
                                  {"confirmed": ["Phoenix Reffaella", "Severian Lowell"]}, now + i * 300)
    assert rec["data"]["banners"]["confirmed"] == ["Phoenix Reffaella", "Severian Lowell"]
    assert "confirmed" not in (rec["data"].get("banners_settled") or [])


def test_reruns_and_the_four_star_summary_lock_only_after_their_phase_lists_do():
    """The wiki-derived fields: `reruns` is a subset of the phase names, and `four_star` is the list
    both phases share. Each locks only when the wiki names it AND its inputs are already locked."""
    game, now = GAMES["wuwa"], 1791522000
    four1 = ["Calcharo", "Encore", "Jiyan"][:game.four_star_count]
    four2 = ["Yangyang", "Lingyang", "Taoqi"][:game.four_star_count]

    def card(phase2_4):
        rec = {}
        notices = [_banner_notice("wuwa", "kuro", "https://wutheringwaves.kurogames.com/en/main/news/1", 1,
                                  ["Verina"], now - 7200, four1),
                   _banner_notice("wuwa", "kuro", "https://wutheringwaves.kurogames.com/en/main/news/2", 2,
                                  ["Lupa"], now - 3600, phase2_4)]
        rec["data"] = schedule.merge(game, "2.5", notices, rec, {}, {}, now)
        return rec

    # 1. the wiki disagrees on phase 1: the official name is final (it stays and locks), and a re-run
    #    the phase lists do not contain never locks
    rec = card(four2)
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"],
                              {"phase1": ["Someone Else"], "phase2": ["Lupa"], "phase1_4": four1,
                               "phase2_4": four2, "reruns": ["Someone Else"]}, now + 60)
    assert rec["data"]["banners"]["phase1"] == ["Verina"]
    assert "phase1" in rec["data"]["banners_settled"]
    assert "reruns" not in rec["data"]["banners_settled"]

    # 2. the wiki agrees on the re-runs: they lock now that their phase lists are settled
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"],
                              {"phase1": ["Verina"], "phase2": ["Lupa"], "phase1_4": four1,
                               "phase2_4": four2, "reruns": ["Lupa"]}, now + 120)
    assert {"phase1", "phase2", "phase1_4", "phase2_4", "reruns"} <= set(rec["data"]["banners_settled"])
    assert "four_star" not in rec["data"]["banners_settled"]    # the two 4★ lists differ: no summary

    # 3. the summary is the shared list when both phases have the same 4★: it locks the same way
    rec = card(four1)
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"],
                              {"phase1": ["Verina"], "phase2": ["Lupa"], "phase1_4": four1,
                               "phase2_4": four1, "four_star": four1}, now + 60)
    assert "four_star" in rec["data"]["banners_settled"]

    # 4. a summary that disagrees with a phase list is never locked, even when the wiki names it
    rec = card(four2)
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"],
                              {"phase1": ["Verina"], "phase2": ["Lupa"], "phase1_4": four1,
                               "phase2_4": four2, "four_star": four1}, now + 60)
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"],
                              {"phase1": ["Verina"], "phase2": ["Lupa"], "phase1_4": four1,
                               "phase2_4": four2, "four_star": four1}, now + 120)
    assert "four_star" not in rec["data"]["banners_settled"]


def test_a_locked_four_star_list_is_not_turned_into_tba_by_a_later_doubtful_notice():
    game, now, rec = GAMES["genshin"], 1791522000, {}
    names = ["Furina", "Lyney", "Kaveh"]
    notice = _banner_notice("genshin", "hoyolab", "https://www.hoyolab.com/article/5", 1, ["Neuvillette"],
                            now - 3600, names)
    rec["data"] = schedule.merge(game, "9.9", [notice], rec, {}, {}, now)
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"],
                              {"phase1": ["Neuvillette"], "phase1_4": names}, now + 60)
    assert "phase1_4" in rec["data"]["banners_settled"]
    doubt = _banner_notice("genshin", "x", "https://x.com/GenshinImpact/status/9", 1, [], now + 120,
                           [], unsure=True)
    rec["data"] = schedule.merge(game, "9.9", [doubt], rec, {}, {}, now + 600)
    assert rec["data"]["banners"]["phase1_4"] == names


def test_an_override_locks_the_name_it_sets():
    game, now, rec = GAMES["zzz"], 1791522000, {}
    notice = _banner_notice("zzz", "x", "https://x.com/ZZZ_EN/status/1", 1, ["Wrong Name"], now - 3600)
    rec["data"] = schedule.merge(game, "9.9", [notice], rec, {}, {}, now,
                                 )
    rec["data"] = schedule.merge(game, "9.9", [notice], rec, {"banners": {"phase1": ["Phoenix Reffaella"]}},
                                 {}, now + 60)
    assert "phase1" in rec["data"]["banners_settled"]
    newer = _banner_notice("zzz", "hoyolab", "https://www.hoyolab.com/article/6", 1, ["Severian Lowell"], now + 120)
    rec["data"] = schedule.merge(game, "9.9", [newer], rec, {}, {}, now + 600)
    assert rec["data"]["banners"]["phase1"] == ["Phoenix Reffaella"]


def test_a_settled_block_stops_the_wiki_and_an_unconfirmed_one_does_not():
    games = load_games(ROOT / "config" / "games.json")
    full = {"banners": {"phase1": ["A"], "phase2": ["B"], "phase1_4": ["a", "b", "c"],
                        "phase2_4": ["a", "b", "c"], "reruns": ["B"]}}
    keys = list(schedule.BANNER_KEYS)
    assert not schedule.banner_block_settled(games["genshin"], full)     # complete, nothing confirmed
    settled = dict(full, banners_settled=keys)
    assert schedule.banner_block_settled(games["genshin"], settled)
    assert not schedule.banner_block_settled(games["wuwa"], settled)     # WuWa also needs its summary
    assert schedule.banner_block_settled(games["ananta"], {})            # no banner block at all


def test_a_complete_but_unconfirmed_block_is_rechecked_every_six_hours_and_a_settled_one_never():
    games = load_games(ROOT / "config" / "games.json")
    now = 1791522000
    game = games["genshin"]
    full = {"banners": {"phase1": ["A"], "phase2": ["B"], "phase1_4": ["a", "b", "c"],
                        "phase2_4": ["a", "b", "c"], "reruns": ["B"]}, "banners_checked_ts": now - 3600}
    assert schedule.wiki_recheck_due(game, dict(full, banners_checked_ts=now - 7 * 3600), now)
    assert not schedule.wiki_recheck_due(game, full, now)                       # read an hour ago
    incomplete = {"banners": dict(full["banners"], reruns=[]), "banners_checked_ts": now - 60}
    assert not schedule.wiki_recheck_due(game, incomplete, now)                 # still TBA: read hourly, not per run
    assert schedule.wiki_recheck_due(game, dict(incomplete, banners_checked_ts=now - schedule.WIKI_INCOMPLETE_H * 3600), now)
    settled = dict(full, banners_settled=list(schedule.BANNER_KEYS))
    assert not schedule.wiki_recheck_due(game, dict(settled, banners_checked_ts=0), now)


def test_an_unchanged_derived_preinstall_is_not_re_stamped_every_run():
    """A derived pre-install that did not move must not get a new provenance clock on every run:
    the state file would change, and commit, every five minutes for nothing."""
    game = GAMES["genshin"]
    data = {"version": "9.9", "maint_start_ts": 1792000000}
    prov: dict = {}
    assert schedule.derive_preinstall(game, data, prov, 1791522000, 43) is not None
    stamp = list(prov["preinstall_ts"])
    assert schedule.derive_preinstall(game, data, prov, 1791522300, 43) is None
    assert prov["preinstall_ts"] == stamp
    data["maint_start_ts"] = 1792010000                     # a real change of the maintenance date does move it
    assert schedule.derive_preinstall(game, data, prov, 1791522600, 43) is not None
    assert data["preinstall_ts"] == 1792010000 - 43 * 3600


def test_a_failed_wiki_read_is_not_retried_every_run_for_a_complete_block():
    """When the wiki is down, a complete block must not hammer it on every five-minute run."""
    games = load_games(ROOT / "config" / "games.json")
    game = games["genshin"]
    complete = {"version": "7.1", "maint_start_ts": 1790114400,
                "banners": {"phase1": ["A"], "phase2": ["B"], "phase1_4": ["a", "b", "c"],
                            "phase2_4": ["a", "b", "c"], "reruns": ["B"]}}
    calls = []

    async def failing_fetch(fetcher, key, ver, count, summary):
        calls.append(ver)
        return None                                          # the page could not be read

    class _Ctx:
        settings = types.SimpleNamespace(gacha_wiki=True)
        fetcher = object()
        now = 1791522000

    old = gachawiki.fetch_lineup
    gachawiki.fetch_lineup = failing_fetch
    try:
        record = {"data": complete}
        assert asyncio.run(schedule.gather_wiki_lineup(_Ctx(), game, "7.1", record)) is None
        assert len(calls) == 1 and complete["banners_checked_ts"] == _Ctx.now
        assert asyncio.run(schedule.gather_wiki_lineup(_Ctx(), game, "7.1", record)) is None
        assert len(calls) == 1                               # not asked again straight away
    finally:
        gachawiki.fetch_lineup = old


def test_an_incomplete_block_is_stamped_and_read_at_most_hourly_so_the_state_changes_once_an_hour():
    """An incomplete block (still TBA) is read at most every WIKI_INCOMPLETE_H hours. Each read stamps
    the record, so the state file changes once an hour at most, never on every five-minute run."""
    game, now, rec = GAMES["genshin"], 1791522000, {}
    rec["data"], rec["prov"] = {"version": "9.9", "banners": {"phase1": ["A"]}}, {}
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"], {"phase2": ["B"]}, now)
    assert rec["data"]["banners_checked_ts"] == now
    assert rec["data"]["banners"]["phase2"] == ["B"]
    assert not schedule.wiki_recheck_due(game, rec["data"], now + 300)     # the next five-minute run: no read
    assert schedule.wiki_recheck_due(game, rec["data"], now + schedule.WIKI_INCOMPLETE_H * 3600)


def test_a_banner_lock_holds_in_every_game_and_maintenance_still_moves():
    now = 1791522000
    for key, game in GAMES.items():
        if not game.enabled or not game.card.show_banners:
            continue
        name = ["Lyra Vale"]
        rec = {}
        first = _banner_notice(key, "hoyolab", f"https://www.hoyolab.com/article/{key}-1", 1, name, now - 3600)
        rec["data"] = schedule.merge(game, "9.9", [first], rec, {}, {}, now)
        schedule.apply_gacha_wiki(game, rec["data"], rec["prov"], {"phase1": name}, now + 60)
        assert "phase1" in rec["data"]["banners_settled"], key
        other = _banner_notice(key, "x", f"https://x.com/{key}/status/2", 1, ["Someone Else"], now + 120)
        rec["data"] = schedule.merge(game, "9.9", [other], rec, {}, {}, now + 600)
        assert rec["data"]["banners"]["phase1"] == name, key
        notice = Item("hoyolab", key, "maint", f"https://www.hoyolab.com/article/{key}-m",
                      "Version 9.9 Maintenance", "", now + 300)
        maint = schedule.Extract(notice, "maintenance", "9.9", fields={"maint_start_ts": now + 86400})
        rec["data"] = schedule.merge(game, "9.9", [maint], rec, {}, {}, now + 900)
        assert rec["data"]["maint_start_ts"] == now + 86400, key


def test_the_feed_follows_its_own_correction_and_a_silent_wiki_never_withdraws_it():
    """The hub is a single source: its own unlocked value follows a later hub reading. The wiki says
    nothing about this phase, so the wiki must not erase the hub's value."""
    game, now, rec = GAMES["starrail"], 1791522000, {}
    rec["data"] = schedule.merge(game, "9.9", [], rec, {}, {}, now)
    schedule.apply_banner_feed(rec["data"], rec["prov"], {"phase1": ["Pela"], "phase2": [], "titles": []}, now)
    schedule.apply_banner_feed(rec["data"], rec["prov"], {"phase1": ["Pearl"], "phase2": [], "titles": []}, now + 600)
    assert rec["data"]["banners"]["phase1"] == ["Pearl"]              # feed-owned, unlocked: follows the hub
    assert "banners_settled" not in rec["data"]
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"], {"phase2": []}, now + 1200)
    assert rec["data"]["banners"]["phase1"] == ["Pearl"]              # silent wiki: the feed's value stays


def test_the_wiki_corrects_a_feed_value_but_never_an_official_one():
    """Priority order: official (50) > wiki (6) > hub (5). The wiki may correct the hub, never HoYoverse."""
    game, now, rec = GAMES["starrail"], 1791522000, {}
    rec["data"] = schedule.merge(game, "9.9", [], rec, {}, {}, now)
    schedule.apply_banner_feed(rec["data"], rec["prov"], {"phase1": ["Pela"], "phase2": [], "titles": []}, now)
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"], {"phase1": ["Pearl"]}, now + 60)
    assert rec["data"]["banners"]["phase1"] == ["Pearl"]              # wiki outranks the hub
    assert rec["prov"]["b_phase1"][0] == schedule.PRIORITY["gachawiki"]
    official = _banner_notice("starrail", "hoyolab", "https://www.hoyolab.com/article/77", 1, ["Seele"], now + 120)
    rec["data"] = schedule.merge(game, "9.9", [official], rec, {}, {}, now + 600)
    assert rec["data"]["banners"]["phase1"] == ["Seele"]              # official replaces the wiki value
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"], {"phase1": ["Pearl"]}, now + 900)
    assert rec["data"]["banners"]["phase1"] == ["Seele"]              # and the wiki never overrides official


# =========================================================================== banner search by title
# Discovery by TITLE, for any version (sources/banner_search.py). A notice older than the lookback
# window used to be invisible to the card; these tests pin the matching, the official-account rule,
# the fallbacks and the budget. Fixtures are the verbatim HoYoLAB / Kuro bodies checked on 2026-10-09.
class _RouteFetch:
    """Serves canned JSON by predicate; a text route serves the Atom mirror. Records every call."""

    def __init__(self, json_routes=(), text_routes=()):
        self.json_routes, self.text_routes, self.calls = list(json_routes), list(text_routes), []

    async def get_json(self, url, source="", params=None, **kw):
        self.calls.append((url, dict(params or {})))
        for pred, body in self.json_routes:
            if pred(url, params or {}):
                return body(url, params or {}) if callable(body) else body
        return None

    async def get_text(self, url, source="", **kw):
        self.calls.append((url, {}))
        for pred, body in self.text_routes:
            if pred(url):
                return body
        return None


def _hoyolab_row(pid, subject, uid, created, content=""):
    return {"post": {"post_id": pid, "subject": subject, "uid": uid, "created_at": created,
                     "content": content, "desc": content},
            "user": {"uid": uid}}


def _hoyolab_full(body):
    return {"retcode": 0, "data": {"post": {"post": {"content": body, "structured_content": ""},
                                            "image_list": [], "cover_list": []}}}


def _search_route(by_phrase):
    """Search endpoint: the rows whose keyword contains the given phrase; anything else is empty."""
    def pred(url, params):
        return url == banner_search.SEARCH_API
    def body_for(url, params):
        kw = params.get("keyword", "")
        for phrase, rows in by_phrase.items():
            if phrase in kw:
                return {"retcode": 0, "data": {"list": rows}}
        return {"retcode": 0, "data": {"list": []}}
    return pred, body_for


def test_banner_title_match_is_exact_on_version_phase_and_prefix():
    gi = "Version {v} Event Wishes Notice - Phase {p}"
    assert banner_search.phase_of("Version 7.1 Event Wishes Notice - Phase I", gi, "7.1") == 1
    assert banner_search.phase_of("Version 7.1 Event Wishes Notice - Phase II", gi, "7.1") == 2
    assert banner_search.phase_of("Version 7.10 Event Wishes Notice - Phase I", gi, "7.1") is None   # 7.1 != 7.10
    assert banner_search.phase_of("Version 7.1 Event Wishes Notice - Phase I", gi, "7.10") is None
    assert banner_search.phase_of("Version 7.1 Event Wishes Notice - Phase III", gi, "7.1") is None
    assert banner_search.phase_of("Version 7.1 Event Wishes Notice", gi, "7.1") is None            # no phase
    zzz = "V{v} Limited-Time Channels (Phase {p})"
    assert banner_search.phase_of("V3.2 Limited-Time Channels (Phase I)", zzz, "3.2") == 1
    assert banner_search.phase_of("V3.2 Limited-Time Channels (Phase II)", zzz, "3.2") == 2
    assert banner_search.phase_of("V3.2 Limited-Time Channels (Phase II)", zzz, "3.3") is None
    hsr = "Version {v} Event Warp: Phase {p}"
    assert banner_search.phase_of("<hoyolab>Version</hoyolab> 4.6 <hoyolab>Event</hoyolab> <hoyolab>Warp</hoyolab>: <hoyolab>Phase</hoyolab> I", hsr, "4.6") == 1
    wuwa = "Version {v} Featured Resonator/Weapon Convene: Phase {p}"
    assert banner_search.phase_of("[Version 3.6 Featured Resonator/Weapon Convene: Phase I]", wuwa, "3.6") == 1
    assert banner_search.phase_of("Version 3.6 Featured Weapon Convene: Phase I", wuwa, "3.6") is None   # weapon-only title
    assert banner_search.keyword_for(zzz, "3.2", 2) == "V3.2 Limited-Time Channels (Phase II)"


def test_banner_search_finds_hsr_phase1_outside_the_lookback_by_title():
    game = GAMES["starrail"]
    row = _hoyolab_row("46851682", "<hoyolab>Version</hoyolab> 4.6 <hoyolab>Event</hoyolab> <hoyolab>Warp</hoyolab>: <hoyolab>Phase</hoyolab> I",
                       "172534910", 1790488804)                                 # 2026-09-27, 12 days old
    pred, body = _search_route({"Phase I": [row], "Phase II": []})
    fetch = _RouteFetch(json_routes=[(pred, body),
                                     (lambda u, p: "getPostFull" in u, _hoyolab_full(HSR_46_WARP_PHASE1))])
    items = asyncio.run(banner_search.find(fetch, game, "4.6"))
    assert [i.id for i in items] == ["46851682"], items
    assert items[0].title == "Version 4.6 Event Warp: Phase I" and items[0].published_ts == 1790488804
    b = schedule.extract_banner(items[0])
    assert b["banner_five"] == ["Pearl", "Evanescia"] and b["banner_phase"] == 1
    # Phase II is not out yet: it was asked for by title, found nothing, and nothing is invented.
    asked = {p["keyword"] for u, p in fetch.calls if u == banner_search.SEARCH_API}
    assert asked == {"Version 4.6 Event Warp: Phase I", "Version 4.6 Event Warp: Phase II"}, asked
    assert [i.kind if hasattr(i, "kind") else i.source for i in items] == ["hoyolab"]


def test_wuwa_banner_search_warning_only_when_the_menu_was_unreachable():
    """A Kuro menu that answers with nothing published yet is the pre-notice normal (INFO,
    with that context); the WARNING is reserved for the menu being unreachable with the
    mirror also empty — it fired on every run for WW 3.7 while the menu was answering fine."""
    game = load_games(ROOT / "config" / "games.json")["wuwa"]
    recs: list[logging.LogRecord] = []

    class _Capt(logging.Handler):
        def emit(self, r):
            recs.append(r)

    handler = _Capt(level=logging.DEBUG)
    banner_search.log.addHandler(handler)
    saved_level = banner_search.log.level      # the module logger defaults to WARNING: let INFO through
    banner_search.log.setLevel(logging.DEBUG)
    try:
        def _msgs():
            return [(r.levelname, r.getMessage()) for r in recs]
        menu = (lambda u, p: "ArticleMenu.json" in u or "MainMenu.json" in u)

        # 1) menu answered, no notice under the exact title yet, mirror empty -> INFO, never WARNING
        recs.clear()
        out = asyncio.run(banner_search.find(_RouteFetch(json_routes=[(menu, {"article": []})]),
                                             game, "3.7"))
        assert out == []
        assert all(lvl == "INFO" for lvl, _ in _msgs()), _msgs()
        assert any("menu answered — nothing published yet" in m for _, m in _msgs()), _msgs()

        # 2) the menu itself unreachable, mirror empty -> the WARNING, and it says why
        recs.clear()

        def _boom(url, params):
            raise RuntimeError("kuro down")
        out = asyncio.run(banner_search.find(_RouteFetch(json_routes=[(menu, _boom)]),
                                             game, "3.7"))
        assert out == []
        assert any(lvl == "WARNING" and "menu unreachable" in m for lvl, m in _msgs()), _msgs()
    finally:
        banner_search.log.removeHandler(handler)
        banner_search.log.setLevel(saved_level)


def test_banner_search_ignores_fan_reposts_and_other_versions():
    game = GAMES["genshin"]
    fan = _hoyolab_row("900001", "Version 7.1 Event Wishes Notice - Phase I", "555555", 1790000000)
    other_version = _hoyolab_row("900002", "Version 7.10 Event Wishes Notice - Phase I", "1015537", 1790000000)
    pred, body = _search_route({"Phase I": [fan, other_version], "Phase II": []})
    fetch = _RouteFetch(json_routes=[(pred, body)])
    assert asyncio.run(banner_search.find(fetch, game, "7.1")) == []        # a repost is not an announcement


def test_banner_search_falls_back_to_the_official_news_list_when_search_is_down():
    game = GAMES["zzz"]
    notice = _hoyolab_row("46604530", "V3.2 <hoyolab>Limited</hoyolab>-<hoyolab>Time</hoyolab> <hoyolab>Channels</hoyolab> (Phase I)",
                          "219270333", 1788755408)
    news_page = {"retcode": 0, "data": {"list": [{"post": notice["post"]}]}}

    def search_down(url, params):
        return url == banner_search.SEARCH_API

    def news(url, params):
        return url.endswith("getNewsList") and params.get("type") == 1

    fetch = _RouteFetch(json_routes=[
        (search_down, {"retcode": 1, "message": "down"}),
        (news, news_page),
        (lambda u, p: "getPostFull" in u, _hoyolab_full("Dear Proxies, ..."))])
    items = asyncio.run(banner_search.find(fetch, game, "3.2"))
    assert [i.id for i in items] == ["46604530"]
    assert any(u.endswith("getNewsList") for u, _ in fetch.calls)           # the fallback really ran


def test_banner_search_budget_settled_never_frozen_never_and_two_clocks():
    game = GAMES["starrail"]
    now = 1790500000
    keys = list(schedule.BANNER_KEYS)
    full = {k: ["Name"] for k in keys}
    full.update({"phase1_4": ["A", "B", "C"], "phase2_4": ["A", "B", "C"]})
    settled = {"banners": full, "banners_settled": keys, "maint_start_ts": now - 86400}
    assert schedule.banner_search_due(game, settled, now + 999999) is False       # settled: never
    partial = {"banners": {"phase1": ["Pearl"]}, "maint_start_ts": now - 86400}
    assert schedule.banner_search_due(game, partial, now) is True                  # incomplete, never searched
    partial["banners_search_ts"] = now
    assert schedule.banner_search_due(game, partial, now + 2 * 3600) is False      # incomplete: 3 h clock
    assert schedule.banner_search_due(game, partial, now + 3 * 3600) is True
    unconfirmed = {"banners": full, "maint_start_ts": now - 86400, "banners_search_ts": now}
    assert schedule.banner_search_due(game, unconfirmed, now + 5 * 3600) is False  # complete: 6 h clock
    assert schedule.banner_search_due(game, unconfirmed, now + 6 * 3600) is True
    frozen = {"banners": {}, "maint_start_ts": now - (schedule.CARD_FREEZE_D + 1) * 86400}
    assert schedule.banner_search_due(game, frozen, now) is False                  # frozen card: never
    assert schedule.banner_search_due(GAMES["wuwa"], {}, now) is True              # WW has templates too


def test_gather_banner_search_feeds_the_merge_once_and_stamps_the_clock():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790500000, versions={"starrail": {"live": "4.6"}})
        row = _hoyolab_row("46851682", "Version 4.6 Event Warp: Phase I", "172534910", 1790488804)
        pred, body = _search_route({"Phase I": [row], "Phase II": []})
        ctx.fetcher = _RouteFetch(json_routes=[(pred, body),
                                               (lambda u, p: "getPostFull" in u, _hoyolab_full(HSR_46_WARP_PHASE1))])
        records = {"4.6": {"status": "posted", "data": {"maint_start_ts": ctx.now - 2 * 86400}}}
        by_version: dict = {}
        asyncio.run(schedule.gather_banner_search(ctx, GAMES["starrail"], records, by_version))
        extracts = by_version.get("4.6") or []
        assert [e.kind for e in extracts] == ["banner"] and extracts[0].fields["banner_five"] == ["Pearl", "Evanescia"]
        assert records["4.6"]["data"]["banners_search_ts"] == ctx.now
        calls = len(ctx.fetcher.calls)
        asyncio.run(schedule.gather_banner_search(ctx, GAMES["starrail"], records, by_version))   # 5 min later
        assert len(ctx.fetcher.calls) == calls                                  # throttled: no request
        # With the banner search switched off nothing is asked and nothing is stamped.
        ctx2 = make_ctx(Path(tmp) / "state2.json", now=1790500000, BANNER_SEARCH="0")
        ctx2.fetcher = _RouteFetch()
        recs2 = {"4.6": {"status": "posted", "data": {"maint_start_ts": ctx2.now - 86400}}}
        asyncio.run(schedule.gather_banner_search(ctx2, GAMES["starrail"], recs2, {}))
        assert ctx2.fetcher.calls == [] and "banners_search_ts" not in recs2["4.6"]["data"]


def test_wuwa_banner_comes_from_the_kuro_menu_by_title_with_its_body():
    game = GAMES["wuwa"]
    menu = [{"articleId": 5318, "articleTitle": "[Version 3.6 Featured Resonator/Weapon Convene: Phase I]",
             "createTime": "2026-08-17 17:35:40", "suggestCover": ""},
            {"articleId": 5220, "articleTitle": "[Version 3.5 Featured Resonator/Weapon Convene: Phase II]",
             "createTime": "2026-07-28 15:50:35", "suggestCover": ""}]
    detail = {"articleContent": WUWA_36_PHASE1_BODY}
    fetch = _RouteFetch(json_routes=[
        (lambda u, p: u.endswith("ArticleMenu.json"), menu),
        (lambda u, p: u.endswith("article/5318.json"), detail)])
    items = asyncio.run(banner_search.find(fetch, game, "3.6"))
    assert [i.id for i in items] == ["5318"], items           # the 3.5 notice is another version
    b = schedule.extract_banner(items[0])
    assert b["banner_five"] == ["Denia"]
    assert b["banner_four"] == ["Yangyang", "Baizhi", "Sanhua"] and b["banner_four_unsure"] is False


WUWA_36_PHASE1_BODY = (
    "[False Promise for Tomorrow] Featured Resonator Convene\n"
    "During the event, 5-Star Resonator: Denia, 4-Star Resonators: Yangyang, Baizhi, and Sanhua receive "
    "boosted drop rates!✦Duration✦Version 3.6 update - 2026-09-10 09:59 (server time)✦Convene Rules✦"
    "- [False Promise for Tomorrow] is a Featured Resonator Convene event banner.\n"
    "[Forged Dwarf Star] Featured Weapon Convene\n"
    "During the event, 5-Star Weapon: Forged Dwarf Star, 4-Star Weapons: Commando of Conviction, Fusion "
    "Accretion, and Lunar Cutter receive boosted drop rates!✦Duration✦Version 3.6 update✦"
)


def test_zzz_and_wuwa_four_star_lists_are_read_whole():
    zzz = Item("hoyolab", "zenless", "46604530", "u", "V3.2 Limited-Time Channels (Phase I)",
               ZZZ_32_PHASE1_TEXT, 1788755408)
    b = schedule.extract_banner(zzz)
    assert b["banner_five"] == ["Claret", "Nangong Yu"]
    assert b["banner_four"] == ["Anton", "Nicole"] and b["banner_four_unsure"] is False
    ww = Item("kuro", "wuwa", "5318", "u", "Version 3.6 Featured Resonator/Weapon Convene: Phase I",
              WUWA_36_PHASE1_BODY, 1787000000)
    assert schedule.extract_banner(ww)["banner_four"] == ["Yangyang", "Baizhi", "Sanhua"]
    # "Topaz & Numby" (no bracket before the ampersand) stays one name.
    assert schedule.bare_names(" Topaz & Numby (Destruction: Fire), Bailu will receive") == ["Topaz & Numby", "Bailu"]


ZZZ_32_PHASE1_TEXT = (
    '"Bloodmoon Rising" Signal Search Details\n'
    "During the event, the limited S-Rank Agent Claret (Electric - Armorer) and default A-Rank Agents "
    "Anton (Electric - Attack) & Nicole (Ether - Support) have significantly boosted reception rates!\n"
    '"Axiom of Captivation" Signal Search Details\n'
    "During the event, the limited S-Rank Agent Nangong Yu (Ether - Stun) and default A-Rank Agents "
    "Anton (Electric - Attack) & Nicole (Ether - Support) have significantly boosted reception rates!\n"
)


def test_zzz_bare_phase_one_title_matches_once_and_never_phase_two():
    """ZZZ 3.1 Phase I (HoYoLAB 46015688) is titled 'V3.1 Limited-Time Channels' with no phase suffix;
    Phase II (46329735) carries '(Phase II)'. The bare template identifies Phase I only."""
    zzz = GAMES["zzz"]
    bare = "V{v} Limited-Time Channels"
    assert banner_search.phases_for(bare) == (1,)
    assert banner_search.phases_for("V{v} Limited-Time Channels (Phase {p})") == (1, 2)
    assert banner_search.phase_of("V3.1 Limited-Time Channels", bare, "3.1") == 1
    assert banner_search.phase_of("V3.1 Limited-Time Channels (Phase II)", bare, "3.1") is None
    assert banner_search.phase_of("V3.10 Limited-Time Channels", bare, "3.1") is None
    assert banner_search.phase_of("V3.1 Limited-Time Channels Preview", bare, "3.1") is None
    assert "V{v} Limited-Time Channels" in zzz.banner_titles              # shipped with the config


def test_zzz_bare_phase_one_is_found_and_only_phase_one_is_asked_for():
    game = GAMES["zzz"]
    bare_row = _hoyolab_row("46015688", "V3.1 Limited-Time Channels", "219270333", 1785140000)
    phase2_row = _hoyolab_row("46329735", "V3.1 Limited-Time Channels (Phase II)", "219270333", 1787000000)
    fan_row = _hoyolab_row("900003", "V3.1 Limited-Time Channels", "555555", 1789000000)  # a repost, not official

    def search(url, params):
        kw = params.get("keyword", "")
        rows = [bare_row, phase2_row, fan_row] if kw == "V3.1 Limited-Time Channels" else []
        if kw == "V3.1 Limited-Time Channels (Phase II)":
            rows = [phase2_row]
        if kw == "V3.1 Limited-Time Channels (Phase I)":
            rows = [bare_row]
        return {"retcode": 0, "data": {"list": rows}}

    fetch = _RouteFetch(json_routes=[(lambda u, p: u == banner_search.SEARCH_API, search),
                                     (lambda u, p: "getPostFull" in u, _hoyolab_full("Dear Proxies ..."))])
    items = asyncio.run(banner_search.find(fetch, game, "3.1"))
    assert sorted(i.id for i in items) == ["46015688", "46329735"], [i.id for i in items]
    asked = [p["keyword"] for u, p in fetch.calls if u == banner_search.SEARCH_API]
    assert "V3.1 Limited-Time Channels" in asked and "V3.1 Limited-Time Channels (Phase II)" in asked
    assert "V3.1 Limited-Time Channels (Phase I)" in asked                  # the templated Phase I query
    assert set(asked) == {"V3.1 Limited-Time Channels", "V3.1 Limited-Time Channels (Phase I)",
                          "V3.1 Limited-Time Channels (Phase II)"}, asked    # 3 queries, no phase-two bare query


def test_the_calendar_keeps_only_five_star_characters_in_each_games_shape():
    """api.ennead.cc: Genshin and Star Rail list `characters` with integer rarity, Zenless lists `agents`
    with S/A. Weapons, light cones and W-Engines are never banner characters, and 4★ names are not read."""
    gi = {"banners": [
        {"id": 231, "name": "Character Event Wish", "version": "7.1",
         "characters": [{"name": "Vesna", "rarity": 5}, {"name": "Diona", "rarity": 4}], "weapons": [],
         "start_time": 1790118000, "end_time": 1791885540},
        {"id": 233, "name": "Weapon Event Wish", "version": "7.1", "characters": [],
         "weapons": [{"name": "Hymn of the Maelstrom", "rarity": 5}], "start_time": 1790118000, "end_time": 1791885540}]}
    assert calendarfeed.parse_banners(gi) == [
        {"version": "7.1", "featured": ["Vesna"], "featured4": ["Diona"],
         "startsAt": 1790118000, "endsAt": 1791885540, "title": ""}]
    sr = {"banners": [
        {"id": 97, "name": "", "version": "4.6",
         "characters": [{"name": "Mortenax Blade", "rarity": 5}, {"name": "Qingque", "rarity": 4}],
         "light_cones": [], "start_time": 1792580400, "end_time": 1794319200},
        {"id": 99, "name": "", "version": "4.6", "characters": [],
         "light_cones": [{"name": "Until the Flowers Bloom Again", "rarity": 5}],
         "start_time": 1790560800, "end_time": 1792580340}]}
    assert [(b["featured"], b["featured4"]) for b in calendarfeed.parse_banners(sr)] == [
        (["Mortenax Blade"], ["Qingque"])]
    zz = {"banners": [
        {"banner_type": "GACHA_TYPE_CHARACTER_UP", "version": "3.2",
         "agents": [{"name": "Roxy", "rarity": "S"}, {"name": "Promeia", "rarity": "S"}, {"name": "Corin", "rarity": "A"}],
         "w_engines": [{"name": "Sleepless Eternal Night", "rarity": "S"}],
         "start_time": 1790740800, "end_time": 1792479599}]}
    assert calendarfeed.parse_banners(zz)[0]["featured"] == ["Roxy", "Promeia"]
    assert calendarfeed.parse_banners(zz)[0]["featured4"] == ["Corin"]
    assert calendarfeed.parse_banners({}) == [] and calendarfeed.parse_banners({"banners": [None]}) == []


def test_the_calendar_lineup_writes_four_star_lists_only_at_the_rate_up_count():
    """A-rank and 4★ lists follow the wiki's rule: exactly four_star_count names per phase, else TBA."""
    sr = calendarfeed.parse_banners({"banners": [
        {"version": "4.6", "start_time": 1790560800, "end_time": 1792580340,
         "characters": [{"name": "Evanescia", "rarity": 5}, {"name": "Qingque", "rarity": 4},
                        {"name": "Xueyi", "rarity": 4}, {"name": "Misha", "rarity": 4}]}]})
    assert calendarfeed.lineup(sr, 1790560800, 3) == {
        "phase1": ["Evanescia"], "phase1_4": ["Qingque", "Xueyi", "Misha"]}
    assert calendarfeed.lineup(sr, 1790560800, 2) == {"phase1": ["Evanescia"]}   # 3 != 2 -> TBA, never half-right
    zz = calendarfeed.parse_banners({"banners": [
        {"version": "3.2", "start_time": 1790740800, "end_time": 1792479599,
         "agents": [{"name": "Roxy", "rarity": "S"}, {"name": "Promeia", "rarity": "S"},
                    {"name": "Corin", "rarity": "A"}, {"name": "Billy", "rarity": "A"}]}]})
    assert calendarfeed.lineup(zz, 1790740800, 2) == {"phase1": ["Roxy", "Promeia"],
                                                      "phase1_4": ["Corin", "Billy"]}


def test_a_calendar_four_star_name_locks_only_with_a_second_source_and_fills_otherwise_blank():
    """The same rule for 4★ / A-rank: the calendar fills an empty 4★ list, and a second source confirms it."""
    game, now, rec = GAMES["zzz"], 1791522000, {}
    rec["data"] = schedule.merge(game, "3.2", [], rec, {}, {}, now)
    schedule.apply_banner_feed(rec["data"], rec["prov"], {}, now, calendar={"phase2_4": ["Corin", "Billy"]})
    assert rec["data"]["banners"]["phase2_4"] == ["Corin", "Billy"]
    assert "banners_settled" not in rec["data"]                                  # one source: not locked
    schedule.apply_gacha_wiki(game, rec["data"], rec["prov"], {"phase2_4": ["Corin", "Billy"]}, now + 60)
    assert rec["data"]["banners_settled"] == ["phase2_4"]                        # wiki agrees -> locked
    schedule.apply_banner_feed(rec["data"], rec["prov"], {}, now + 600, calendar={"phase2_4": ["Anby", "Ben"]})
    assert rec["data"]["banners"]["phase2_4"] == ["Corin", "Billy"]              # locked: unchanged


def test_a_calendar_name_alone_fills_an_empty_phase_and_never_locks_it():
    """One group is not confirmation: the calendar fills the empty phase, unlocked, like the hub does."""
    game, now, rec = GAMES["starrail"], 1791522000, {}
    rec["data"] = schedule.merge(game, "4.6", [], rec, {}, {}, now)
    schedule.apply_banner_feed(rec["data"], rec["prov"], {}, now, calendar={"phase1": ["Pearl", "Evanescia"]})
    assert rec["data"]["banners"]["phase1"] == ["Pearl", "Evanescia"]
    assert rec["prov"]["b_phase1_by"] == ["calendar"]
    assert "banners_settled" not in rec["data"]


def test_a_calendar_name_that_the_hub_agrees_with_locks_the_phase_for_every_game():
    """Any two sources agreeing lock a name: calendar + hub is that pair (here Star Rail 4.6 Phase II)."""
    game, now, rec = GAMES["starrail"], 1791522000, {}
    rec["data"] = schedule.merge(game, "4.6", [], rec, {}, {}, now)
    schedule.apply_banner_feed(rec["data"], rec["prov"],
                               {"phase1": [], "phase2": ["Mortenax Blade"], "titles": []}, now,
                               calendar={"phase2": ["Mortenax Blade"]})
    assert rec["data"]["banners"]["phase2"] == ["Mortenax Blade"]
    assert rec["data"]["banners_settled"] == ["phase2"]
    assert rec["prov"]["b_phase2_by"] == ["calendar", "feed"]
    later = {"phase2": ["Someone Else"], "titles": []}
    schedule.apply_banner_feed(rec["data"], rec["prov"], later, now + 600, calendar={"phase2": ["Someone Else"]})
    assert rec["data"]["banners"]["phase2"] == ["Mortenax Blade"]          # locked: neither source may move it


def test_a_calendar_that_disagrees_with_the_hub_neither_locks_nor_overwrites():
    game, now, rec = GAMES["zzz"], 1791522000, {}
    rec["data"] = schedule.merge(game, "3.2", [], rec, {}, {}, now)
    schedule.apply_banner_feed(rec["data"], rec["prov"], {"phase1": ["Roxy"], "phase2": [], "titles": []}, now)
    schedule.apply_banner_feed(rec["data"], rec["prov"], {"phase1": ["Roxy"], "phase2": [], "titles": []}, now + 60,
                               calendar={"phase1": ["Promeia"]})
    assert rec["data"]["banners"]["phase1"] == ["Roxy"]                    # held by the hub: calendar may not take it
    assert "banners_settled" not in rec["data"]
    assert rec["prov"]["b_phase1_by"] == ["feed"]


def test_the_calendar_follows_its_own_correction_and_the_hub_never_takes_it_over():
    game, now, rec = GAMES["starrail"], 1791522000, {}
    rec["data"] = schedule.merge(game, "4.6", [], rec, {}, {}, now)
    schedule.apply_banner_feed(rec["data"], rec["prov"], {}, now, calendar={"phase1": ["Pela"]})
    schedule.apply_banner_feed(rec["data"], rec["prov"], {"phase1": ["Pearl"], "phase2": [], "titles": []}, now + 600)
    assert rec["data"]["banners"]["phase1"] == ["Pela"]                    # the hub does not take a calendar name
    schedule.apply_banner_feed(rec["data"], rec["prov"], {}, now + 1200, calendar={"phase1": ["Pearl"]})
    assert rec["data"]["banners"]["phase1"] == ["Pearl"]                   # calendar follows its own change
    assert "banners_settled" not in rec["data"]                            # one group so far: not locked
    schedule.apply_banner_feed(rec["data"], rec["prov"], {"phase1": ["Pearl"], "phase2": [], "titles": []}, now + 1800)
    assert rec["data"]["banners_settled"] == ["phase1"]                    # the hub now agrees with it -> locked


def test_the_calendar_never_changes_a_name_an_official_notice_wrote():
    game, now, rec = GAMES["zzz"], 1791522000, {}
    notice = _banner_notice("zzz", "hoyolab", "https://www.hoyolab.com/article/55", 1, ["Soukaku"], now - 3600)
    rec["data"] = schedule.merge(game, "9.9", [notice], rec, {}, {}, now)
    schedule.apply_banner_feed(rec["data"], rec["prov"], {}, now + 60, calendar={"phase1": ["Lycaon"]})
    assert rec["data"]["banners"]["phase1"] == ["Soukaku"]
    assert rec["data"]["banners_settled"] == ["phase1"]                    # the official notice locks alone, as before


def test_the_calendar_reaches_merge_and_only_fills_the_phases_it_names():
    """merge(calendar=...) splits the calendar's banners by the card's own release timestamp, as the hub's."""
    game, now, rec = GAMES["starrail"], 1791522000, {}
    maint = Item("hoyolab", "starrail", "maint", "https://www.hoyolab.com/article/sr-m",
                 "Version 4.6 Maintenance", "", now)
    extract = schedule.Extract(maint, "maintenance", "4.6", fields={"maint_start_ts": now})
    cal = [{"version": "4.6", "featured": ["Pearl"], "startsAt": now + 3600, "endsAt": now + 90 * 86400, "title": ""},
           {"version": "4.6", "featured": ["Mortenax Blade"], "startsAt": now + 23 * 86400,
            "endsAt": now + 60 * 86400, "title": ""}]
    rec["data"] = schedule.merge(game, "4.6", [extract], rec, {}, {}, now, calendar=cal)
    assert rec["data"]["banners"]["phase1"] == ["Pearl"]
    assert rec["data"]["banners"]["phase2"] == ["Mortenax Blade"]
    assert rec["prov"]["b_phase2_by"] == ["calendar"]


def test_the_calendar_is_one_request_per_game_per_run_and_only_when_due():
    """gather_calendar: asked only for a block that still has a TBA or unconfirmed name, once per run,
    stamped so the next run does not ask again; a settled block and a switched-off setting ask nothing."""
    from types import SimpleNamespace
    game, now = GAMES["starrail"], 1791522000
    calls = []

    async def fake_fetch(fetcher, key):
        calls.append(key)
        return [{"version": "4.6", "featured": ["Pearl"], "startsAt": now, "endsAt": now + 9999, "title": ""},
                {"version": "4.5", "featured": ["Old"], "startsAt": now, "endsAt": now + 9999, "title": ""}]

    orig = calendarfeed.fetch_banners
    calendarfeed.fetch_banners = fake_fetch
    try:
        records = {"4.6": {"status": "tracked", "data": {"version": "4.6", "maint_start_ts": now}}}
        ctx = SimpleNamespace(settings=SimpleNamespace(banner_calendar=True), fetcher=object(), versions={},
                              now=now)
        by_version = {}
        asyncio.run(schedule.gather_calendar(ctx, game, records, by_version))
        assert calls == ["starrail"] and by_version == {"4.6": []}
        assert records["4.6"]["calendar"]["banners"] == [
            {"version": "4.6", "featured": ["Pearl"], "startsAt": now, "endsAt": now + 9999, "title": ""}]
        ctx.now = now + 600
        asyncio.run(schedule.gather_calendar(ctx, game, records, {}))
        assert calls == ["starrail"]                                       # stamped: not due again this soon
        full = {k: ["Pearl"] for k in schedule.BANNER_KEYS}
        keys = list(schedule.BANNER_KEYS) + (["four_star"] if game.card.four_star_summary else [])
        settled = {"4.6": {"status": "tracked", "data": {"version": "4.6", "maint_start_ts": now,
                                                          "banners": full, "banners_settled": keys}}}
        assert schedule.banner_block_settled(game, settled["4.6"]["data"])
        asyncio.run(schedule.gather_calendar(ctx, game, settled, {}))
        assert calls == ["starrail"]                                       # a settled block asks nothing
        ctx.settings.banner_calendar = False
        asyncio.run(schedule.gather_calendar(ctx, game, {"4.6": {"status": "tracked", "data": {}}}, {}))
        assert calls == ["starrail"]                                       # BANNER_CALENDAR=0
    finally:
        calendarfeed.fetch_banners = orig


def test_once_both_phases_lock_with_their_four_star_lists_the_card_stops_being_watched():
    """The stop rule: a phase is final when its 5★ and 4★ lists are locked. Phase 2 has to be final too,
    so a card with no phase-2 names yet is still watched. Re-runs stay on the wiki's own schedule."""
    game, now = GAMES["starrail"], 1791522000
    base = {"version": "4.6", "maint_start_ts": now, "banners_search_ts": now, "calendar_ts": now}
    phase1 = {"phase1": ["Evanescia", "Pearl"], "phase1_4": ["Qingque", "Xueyi", "Misha"]}
    phase2 = {"phase2": ["Mortenax Blade"], "phase2_4": ["Qingque", "Xueyi", "Misha"]}
    locked = ["phase1", "phase1_4", "phase2", "phase2_4"]
    done = dict(base, banners={**phase1, **phase2}, banners_settled=locked)          # reruns still TBA
    assert schedule.banner_phases_done(game, done)
    later = now + 10 * 86400
    assert not schedule.banner_search_due(game, dict(done, banners_search_ts=0), later)
    assert not schedule.calendar_due(game, dict(done, calendar_ts=0), later)
    assert schedule.wiki_recheck_due(game, dict(done, banners_checked_ts=0), later)   # wiki keeps its schedule
    no_phase2 = dict(base, banners=dict(phase1), banners_settled=["phase1", "phase1_4"])
    assert not schedule.banner_phases_done(game, no_phase2)
    assert schedule.calendar_due(game, dict(no_phase2, calendar_ts=0), later)         # phase 2 still watched
    unlocked_2 = dict(base, banners={**phase1, **phase2}, banners_settled=["phase1", "phase1_4"])
    assert not schedule.banner_phases_done(game, unlocked_2)
    assert schedule.banner_search_due(game, dict(unlocked_2, banners_search_ts=0), later)
    four_open = dict(base, banners={**phase1, **phase2}, banners_settled=["phase1", "phase2", "phase2_4"])
    assert not schedule.banner_phases_done(game, four_open)                           # 4★ of phase 1 still open


def test_the_wuwa_37_pin_gives_phase_one_all_three_names_and_locks_it():
    """Phase 1 was locked on two names (Chisa, Iuno) without Hsin, and a locked phase
    never grows. The human-verified pin (hub + game8 + your screenshot) carries all three names,
    locked, and a later feed cannot move them."""
    game, now, rec = GAMES["wuwa"], 1790762400, {}
    pin = json.loads((ROOT / "config" / "overrides.json").read_text(encoding="utf-8"))["wuwa"]["3.7"]
    data = schedule.merge(game, "3.7", [], rec, pin, {}, now)
    assert data["banners"]["phase1"] == ["Hsin", "Chisa", "Iuno"]
    assert "phase1" in data["banners_settled"]
    rec["data"], rec["prov"] = data, rec.get("prov", {})
    schedule.apply_banner_feed(rec["data"], rec["prov"], {"phase1": ["Someone"], "titles": []}, now + 600,
                               calendar=None)
    assert rec["data"]["banners"]["phase1"] == ["Hsin", "Chisa", "Iuno"]


def test_mortenax_blade_locks_when_the_calendar_and_the_hub_agree_for_star_rail_4_6():
    """HSR 4.6 Phase II Mortenax Blade (re-run) is read from the calendar's own shape, split by the
    release timestamp, and locks once the hub names the same character. Either source alone only fills."""
    game, now = GAMES["starrail"], 1791522000
    cal = calendarfeed.parse_banners({"banners": [
        {"version": "4.6", "start_time": 1790564400, "end_time": 1792580399,
         "characters": [{"name": "Evanescia", "rarity": 5}]},
        {"version": "4.6", "start_time": 1792580400, "end_time": 1794319199,
         "characters": [{"name": "Mortenax Blade", "rarity": 5}]}]})
    lineup = calendarfeed.lineup(cal, 1790546400, 3)
    assert lineup["phase2"] == ["Mortenax Blade"] and lineup["phase1"] == ["Evanescia"]
    rec: dict = {}
    rec["data"] = schedule.merge(game, "4.6", [], rec, {}, {}, now)
    schedule.apply_banner_feed(rec["data"], rec["prov"], {"phase2": ["Mortenax Blade"], "titles": []}, now,
                               calendar=lineup)
    assert rec["data"]["banners"]["phase2"] == ["Mortenax Blade"]
    assert "phase2" in rec["data"]["banners_settled"]
    assert set(rec["prov"]["b_phase2_by"]) == {"calendar", "feed"}
    alone: dict = {}
    alone["data"] = schedule.merge(game, "4.6", [], alone, {}, {}, now)
    schedule.apply_banner_feed(alone["data"], alone["prov"], {}, now, calendar=lineup)
    assert alone["data"]["banners"]["phase2"] == ["Mortenax Blade"]
    assert "phase2" not in alone["data"].get("banners_settled", [])


def test_a_card_locked_by_the_legacy_markers_writes_the_lock_down():
    """A record from before announcement_locked existed is locked by program_seen. Its lock is written
    down on the next run, so the lock no longer depends on a marker that could be cleared, and the link
    still stays with the announcement when a giveaway appears."""
    record: dict = {}
    _merge_zzz33(record, [_zzz33_announcement()], now=1791172900)
    data = record["data"]
    assert data["announcement_locked"] is True
    data.pop("announcement_locked")                       # the record as it was before the flag existed
    later = _merge_zzz33(record, [_zzz33_giveaway()], now=1791530000)
    assert later["title_url"] == ZZZ33_ANN_URL and later["images"] == [ZZZ33_ANN_IMG]
    assert later["announcement_locked"] is True


def test_a_legacy_locked_card_is_written_down_even_on_a_run_with_no_program_post():
    """The write-down must not depend on a program post being in the lookback window: a run with none
    takes the other branch, and the legacy lock still has to be stored."""
    record = {"data": {"program_seen": True, "title_url": ZZZ33_ANN_URL, "images": [ZZZ33_ANN_IMG]}}
    data = schedule.merge(GAMES["zzz"], "3.3", [], record, {}, {}, 1791530000, [])
    assert data["announcement_locked"] is True
    assert data["title_url"] == ZZZ33_ANN_URL and data["images"] == [ZZZ33_ANN_IMG]


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


# =========================================================================== settled programs
# HSR 4.6's special program aired 2026-09-20; the version itself went live 2026-09-28. A live run
# must stop announcing it, while a test run must keep rendering it so the fetch stays observable.
def test_a_settled_program_is_never_re_created_on_a_live_run():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790450000)
        ctx.webhook = ScriptedWebhook([SendResult(False, 404, error=UNKNOWN_MESSAGE)],
                                      [SendResult(True, 200, message_id="should-not-happen")])
        records = {"4.6": _posted_hsr_record()}                 # air time 6 days ago
        assert schedule.program_settled(records["4.6"]["data"], ctx.now)
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×301"}, {}, True))
        assert ctx.webhook.sends == [] and ctx.errors == []      # nothing reappears in the channel
        assert records["4.6"].get("message_id") is None          # dead id dropped, no retry loop
        assert records["4.6"]["card_retired"] == ctx.now
        # ...and the line names the message that stopped resolving, because the id is dropped
        # from the record immediately afterwards and the state alone would never say which one
        # it was. An operator who did not delete that card themselves has nothing else to go on.
        assert any("its card is not re-created" in line and "dead-message" in line
                   for line in ctx.report)

        # and it stays gone: the later data change adds no PATCH and no POST (the single edit
        # above is the one that 404'd and triggered the retirement)
        ctx.now += 600
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×302"}, {}, True))
        assert ctx.webhook.sends == [] and len(ctx.webhook.edits) == 1 and ctx.errors == []


def test_a_test_run_renders_a_card_instead_of_touching_the_live_one():
    """One rule, both sides of it, because they are the same fault seen from two directions.

    LIVE: an id that stopped resolving is never republished for a version that is already out.
    Republishing it is what put a fifteen-day-old Genshin 7.1 card into #announcements on
    2026-10-08 -- and again on run #1031 the same day -- for a programme this bot had never
    carded at all. The dead id is dropped, the card is retired, and the correction still
    reaches state. REPOST=<game>:<version> remains the deliberate override.

    TEST: `mode=test` promises "the REAL schedule card for the version that is out now ...
    exactly what a live run would post", as a throwaway to delete afterwards. So it must not
    enter the edit path at all. Every version worth checking has already been posted, and for
    those the edit path would PATCH the LIVE card (unlabelled -- only the repost arm marks it),
    stamp a 🧪 TEST banner onto the production copy in the game's channel, and then return
    without ever posting the card the operator asked to see. It renders one new marked message
    instead, and writes neither a retirement nor the state file.
    """
    def run(tmp, **env):
        ctx = make_ctx(Path(tmp) / f"state-{len(env)}.json", now=1790450000, **env)
        ctx.webhook = ScriptedWebhook([SendResult(False, 404, error=UNKNOWN_MESSAGE)],
                                      [SendResult(True, 200, message_id="fresh-id")])
        records = {"4.6": _posted_hsr_record()}
        assert schedule.program_settled(records["4.6"]["data"], ctx.now)
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×301"}, {}, True))
        return ctx, records["4.6"]

    with tempfile.TemporaryDirectory() as tmp:
        ctx, rec = run(tmp)
        assert ctx.webhook.sends == [] and ctx.errors == []
        assert rec.get("message_id") is None
        assert rec["card_retired"] == ctx.now
        assert rec["data"]["compensation"] == "Stellar Jade ×301"   # nothing posted, nothing lost

        ctx, rec = run(tmp, TEST_MODE="1")
        assert ctx.webhook.edits == []                              # the live card is untouched
        assert len(ctx.webhook.sends) == 1
        assert ctx.webhook.sends[0]["components"][0]["components"][0]["content"].startswith(
            "-# 🧪 TEST CARD")
        assert "card_retired" not in rec and rec["message_id"] == "fresh-id"
        assert any("TEST card posted" in line for line in ctx.report)
        assert rec["data"]["compensation"] == "Stellar Jade ×301"


def test_a_frozen_card_says_why_it_stopped_updating():
    """CARD_FREEZE_D ends the edits, and the version then vanishes from the summary entirely —
    which is precisely what "why has my card stopped updating?" looks like from outside. One
    line, once, keyed on the maintenance date the freeze is measured from."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790450000)
        ctx.webhook = ScriptedWebhook([SendResult(True, 200)])
        record = _posted_hsr_record()
        record["data"]["maint_start_ts"] = ctx.now - 60 * 86400     # frozen long ago
        records = {"4.6": record}
        for _ in range(2):                           # a second run must not repeat the line
            asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                                 {"compensation": "Stellar Jade ×301"}, {}, True))
        assert ctx.webhook.edits == [] and ctx.webhook.sends == []   # frozen means frozen
        said = [r for r in ctx.report if "card frozen" in r]
        assert len(said) == 1 and "45 days past maintenance" in said[0], ctx.report
        assert records["4.6"]["frozen_noted"] == records["4.6"]["data"]["maint_start_ts"]


def test_a_settled_program_does_not_freeze_the_card_that_is_already_posted():
    """Settled program != finished version: HSR 4.6's banners and maintenance landed AFTER the
    stream, so in-place edits must keep flowing. Only NEW messages are suppressed."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790450000)
        ctx.webhook = ScriptedWebhook([SendResult(True, 200)])
        records = {"4.6": _posted_hsr_record()}
        assert schedule.program_settled(records["4.6"]["data"], ctx.now)
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {"compensation": "Stellar Jade ×301"}, {}, True))
        assert len(ctx.webhook.edits) == 1 and ctx.webhook.sends == []
        assert any("schedule card updated — compensation" in line for line in ctx.report)
        assert records["4.6"]["message_id"] == "dead-message"


def test_a_settled_version_is_never_announced_anew_on_a_live_run():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = make_ctx(Path(tmp) / "state.json", now=1790450000)
        records = {"4.6": {"status": "new", "first_seen": ctx.now,
                           "data": dict(SCHEDULE_SAMPLES["starrail"]), "prov": {}}}
        asyncio.run(schedule._handle_version(ctx, GAMES["starrail"], "4.6", [], records,
                                             {}, {}, False))          # not bootstrapped
        assert ctx.webhook.sent == [] and records["4.6"]["status"] == "tracked"
        assert any("nothing to announce" in line for line in ctx.report)

        # the same version on a test run IS rendered, from an empty state
        ctx2 = make_ctx(Path(tmp) / "s2.json", now=1790450000, TEST_MODE="1")
        records2 = {"4.6": {"status": "new", "first_seen": ctx2.now,
                            "data": dict(SCHEDULE_SAMPLES["starrail"]), "prov": {}}}
        asyncio.run(schedule._handle_version(ctx2, GAMES["starrail"], "4.6", [], records2,
                                             {}, {}, True))
        assert len(ctx2.webhook.sent) == 1


def test_program_settled_shares_the_window_with_the_announce_decision():
    now = 1790450000
    assert schedule.program_settled({"program_ts": now - int(36 * 3600) - 1}, now) is True
    assert schedule.program_settled({"program_ts": now - int(36 * 3600) + 60}, now) is False
    assert schedule.program_settled({"program_ts": now + 86400}, now) is False   # not aired yet
    assert schedule.program_settled({}, now) is False                            # air time unknown


# =========================================================================== banner accuracy
# Verbatim body of HoYoLAB post 46851682, "Version 4.6 Event Warp: Phase I" (created_at
# 1790488804), fetched 2026-09-27 from
#   bbs-api-os.hoyolab.com/community/post/wapi/getPostFull?post_id=46851682
# This is the post that produced the wrong live card, and it must be the WHOLE body: the
# first two paragraphs alone read correctly, and an earlier fixture stopped there -- which is
# exactly why the two banner TITLES that leak out of the "※ Event Warp Details" section
# ("Celestial Invitation", "An Ocean in a Pearl") survived into production. With them present
# the reader has to prove it ignores a banner name in every clause of the notice.
HSR_46_WARP_PHASE1 = """\
Hello, Trailblazers!
The drop rates for the limited 5-star character Pearl (Elation: Ice) and the limited 5-star Light Cone "Colors for Tomorrow (Elation)" will be boosted for a limited time. The drop rates for the 4-star characters Qingque (Erudition: Quantum), Xueyi (Destruction: Quantum), and Misha (Destruction: Ice), as well as the 4-star Light Cones "Post-Op Conversation (Abundance)," "Planetary Rendezvous (Harmony)," and "Boundless Choreo (Nihility)" will be boosted for a limited time. The Warp period is from after the Version 4.6 update on 2026-09-28 – 2026-11-10 15:00 (server time).
The limited 5-star character Evanescia (Elation: Physical) and the limited 5-star Light Cone "Until the Flowers Bloom Again (Elation)" will return. The Warp period is from after the Version 4.6 update on 2026-09-28 – 2026-10-21 11:59 (server time).

▌"An Ocean in a Pearl" and "Brilliant Fixation: Colors for Tomorrow" Event Warps
● During the "An Ocean in a Pearl" Character Event Warp, the drop rates of the limited 5-star character Pearl (Elation: Ice) and 4-star characters Qingque (Erudition: Quantum), Xueyi (Destruction: Quantum), and Misha (Destruction: Ice) will be boosted for a limited time.
● During the "Brilliant Fixation: Colors for Tomorrow" Light Cone Event Warp, the drop rates of the limited 5-star Light Cone "Colors for Tomorrow (Elation)" and the 4-star Light Cones "Post-Op Conversation (Abundance)," "Planetary Rendezvous (Harmony)," and "Boundless Choreo (Nihility)" will be boosted for a limited time.

▌"The Demoiselle in Charge" and "Bygone Reminiscence: Until the Flowers Bloom Again" Event Warps
● During "The Demoiselle in Charge" Character Event Warp, the drop rates of the limited 5-star character Evanescia (Elation: Physical) and 4-star characters Qingque (Erudition: Quantum), Xueyi (Destruction: Quantum), and Misha (Destruction: Ice) will be boosted for a limited time.
● During the "Bygone Reminiscence: Until the Flowers Bloom Again" Light Cone Event Warp, the drop rates of the limited 5-star Light Cone "Until the Flowers Bloom Again (Elation)" and the 4-star Light Cones "Post-Op Conversation (Abundance)," "Planetary Rendezvous (Harmony)," and "Boundless Choreo (Nihility)" will be boosted for a limited time.

▌ Event Warp Details
※ Among the above characters and Light Cones, limited characters and limited Light Cones will not be available in the Stellar Warp event.
※ In this phase of Warp, obtainable 5-star characters include the featured 5-star characters and the custom-selected characters from "Celestial Invitation."
※ During the Event Warp period, the limited 5-star character Pearl (Elation: Ice) can only be obtained from the "An Ocean in a Pearl" Character Event Warp, and the limited 5-star character Evanescia (Elation: Physical) can only be obtained from the "The Demoiselle in Charge" Character Event Warp.
※ During the Event Warp period, the limited 5-star Light Cone "Colors for Tomorrow (Elation)" can only be obtained from the "Brilliant Fixation: Colors for Tomorrow" Light Cone Event Warp, and the limited 5-star Light Cone "Until the Flowers Bloom Again (Elation)" can only be obtained from the "Bygone Reminiscence: Until the Flowers Bloom Again" Light Cone Event Warp.
※ "An Ocean in a Pearl" and "The Demoiselle in Charge" are Character Event Warps that share the same guaranteed drop counter. The cumulative Warp count for a guaranteed 5-star character in any Character Event Warp will always be carried over to other Character Event Warps, but is independent of and unaffected by other types of Warps.
※ "Brilliant Fixation: Colors for Tomorrow" and "Bygone Reminiscence: Until the Flowers Bloom Again" are considered Light Cone Event Warps, and share the same guaranteed drop counter. The cumulative Warp count for a guaranteed 5-star Light Cone in any Light Cone Event Warp will always be carried over to other Light Cone Event Warps, but is independent of and unaffected by other types of Warps.
※ For more information, please head to the Warp screen.

▌ "Aptitude Showcase" Character Trial Event
● Requirement: Unlock Travel Log
Event Details: After the Version 4.6 update on 2026-09-28 – 2026-11-10 15:00 (server time), you can experience the trial stages for characters Pearl (Elation: Ice), Qingque (Erudition: Quantum), Xueyi (Destruction: Quantum), and Misha (Destruction: Ice). After the Version 4.6 update on 2026-09-28 – 2026-10-21 11:59 (server time), you can experience the trial stage for the character Evanescia (Elation: Physical). Completing the challenges awards Stellar Jades, Adventure Logs, Universal Enhancement Materials, and Credits.
"""
HSR_46_NOT_CHARACTERS = {
    "Celestial Invitation", "An Ocean in a Pearl", "The Demoiselle in Charge",   # banner titles
    "Post-Op Conversation", "Planetary Rendezvous", "Boundless Choreo",          # 4* light cones
    "Colors for Tomorrow", "Until the Flowers Bloom Again",                      # 5* light cones
}


def test_the_real_warp_notice_yields_characters_not_light_cones():
    item = Item("hoyolab", "starrail", "46851682", "https://www.hoyolab.com/article/46851682",
                "Version 4.6 Event Warp: Phase I", HSR_46_WARP_PHASE1, 1790488804)
    b = schedule.extract_banner(item)
    assert b["banner_five"] == ["Pearl", "Evanescia"]
    assert b["banner_four"] == ["Qingque", "Xueyi", "Misha"]
    assert b["banner_four_unsure"] is False and b["banner_phase"] == 1
    leaked = (set(b["banner_five"]) | set(b["banner_four"])) & HSR_46_NOT_CHARACTERS
    assert not leaked, leaked


def test_re_reading_the_same_post_heals_a_wrong_four_star_list():
    """phase1_4 held three LIGHT CONES, written into the state by post 46851682 itself. The
    corrected reader re-reads THAT VERY POST, so _four_star_problem() must not treat the stored
    list as a second official source disagreeing -- doing so latches the wrong list into a
    permanent TBA (the flag is sticky) and the 4-stars never come back."""
    item = Item("hoyolab", "starrail", "46851682", "https://www.hoyolab.com/article/46851682",
                "Version 4.6 Event Warp: Phase I", HSR_46_WARP_PHASE1, 1790488804)
    ex = schedule.extract(GAMES["starrail"], item)
    record = {"data": {"version": "4.6", "banners": {
                  "phase1": ["Celestial Invitation", "An Ocean in a Pearl"],
                  "phase1_4": ["Post-Op Conversation", "Planetary Rendezvous", "Boundless Choreo"]}},
              "prov": {"b_phase1": [50, 1790488804], "b_phase1_4": [50, 1790488804]}}
    for i in (1, 2):                                    # and it stays healed on every later run
        data = schedule.merge(GAMES["starrail"], "4.6", [ex], record, {}, {}, 1790510400 + i * 600)
        record["data"] = data
        assert data["banners"]["phase1"] == ["Pearl", "Evanescia"]
        assert data["banners"]["phase1_4"] == ["Qingque", "Xueyi", "Misha"]
        assert "b_phase1_4_tba" not in record["prov"]


def test_two_different_posts_that_disagree_still_fall_back_to_tba():
    """The re-read exemption is narrow: a DIFFERENT post contradicting the stored 4-star list is
    still a real disagreement and must still show TBA."""
    item = Item("hoyolab", "starrail", "46851682", "u", "Version 4.6 Event Warp: Phase I",
                HSR_46_WARP_PHASE1, 1790488804)
    ex = schedule.extract(GAMES["starrail"], item)
    record = {"data": {"version": "4.6",
                       "banners": {"phase1_4": ["Hanya", "Sampo", "Natasha"]}},
              "prov": {"b_phase1_4": [50, 1790400000]}}   # an EARLIER, different post
    data = schedule.merge(GAMES["starrail"], "4.6", [ex], record, {}, {}, 1790510400)
    assert data["banners"]["phase1_4"] == []
    assert record["prov"]["b_phase1_4_tba"] == "official sources disagree"


def test_a_quoted_character_notice_still_reads_the_quotes():
    """The other official style quotes its characters. Both must work."""
    text = ('Event Wish "Ballad" Phase II: the event-exclusive 5-star character "Vodyanitsa (Hydro)" '
            'and the 4-star characters "Bennett (Pyro)", "Xingqiu (Hydro)" and "Sucrose (Anemo)" '
            'will get a huge drop-rate boost!')
    b = schedule.extract_banner(Item("hoyolab", "genshin", "1", "u", "Event Wish", text, 1))
    assert b["banner_five"] == ["Vodyanitsa"]
    assert b["banner_four"] == ["Bennett", "Xingqiu", "Sucrose"]


def test_a_dangling_article_is_never_read_as_a_character_name():
    b = schedule.bare_names(' "Vodyanitsa (Hydro)" and the ')
    assert "the" not in b and "The" not in b


def test_a_prose_tail_is_never_read_as_a_character_name():
    """A bare run is raw prose. When the sentence ends with a clause TIER_STOP does not know,
    the leftover must not be posted as a character -- the quoted-only reader posted NOTHING
    here, so absorbing the tail would be a new way to be wrong."""
    for tail in (" Pearl debuts soon.", " Pearl is here", " Pearl arrives in the new update"):
        assert schedule.bare_names(tail) == [], tail
    item = Item("hoyolab", "starrail", "2", "u", "Warp",
                "The limited 5-star character Pearl debuts soon.", 1)
    assert "Pearl debuts soon" not in (schedule.extract_banner(item).get("banner_five") or [])
    # ...while real multi-word names, which look like prose to a word counter, still pass.
    assert schedule.bare_names(" Dan Heng • Imbibitor Lunae, Topaz & Numby, and March 7th ") == [
        "Dan Heng • Imbibitor Lunae", "Topaz & Numby", "March 7th"]


def test_the_banner_feed_replaces_a_phase_that_holds_a_banner_title():
    """The hub is the only source that separates a banner's NAME from its featured character."""
    data = {"banners": {"phase1": ["An Ocean in a Pearl", "The Demoiselle in Charge"]}}
    prov = {"b_phase1": [50, 1790488804]}          # hoyolab -- higher priority than the feed
    schedule.apply_banner_feed(data, prov,
                               {"phase1": ["Evanescia", "Pearl"],
                                "titles": ["An Ocean in a Pearl", "The Demoiselle in Charge",
                                           "Now I Am Become Blade"]}, 1790500000)
    assert data["banners"]["phase1"] == ["Evanescia", "Pearl"]
    assert prov["b_phase1"][0] == 5                # now attributed to the feed


def test_a_locked_phase_holding_a_banner_title_is_corrected_and_unlocked():
    """An official misread (a title, not a character) is corrected by the hub, and the correction is
    unconfirmed until another source agrees, so the phase is no longer locked."""
    data = {"banners": {"phase1": ["An Ocean in a Pearl"]}, "banners_settled": ["phase1"]}
    prov = {"b_phase1": [50, 1790488804], "b_phase1_by": ["official"]}
    schedule.apply_banner_feed(data, prov,
                               {"phase1": ["Pearl"], "titles": ["An Ocean in a Pearl"]}, 1790500000)
    assert data["banners"]["phase1"] == ["Pearl"]
    assert "phase1" not in data["banners_settled"]
    schedule._sync_official_locks(data, prov)           # owner is the feed now: no official lock comes back
    assert "phase1" not in data["banners_settled"]


def test_the_banner_feed_still_never_overwrites_real_characters():
    data = {"banners": {"phase1": ["Evanescia", "Pearl"]}}
    prov = {"b_phase1": [50, 1790488804]}
    schedule.apply_banner_feed(data, prov,
                               {"phase1": ["Evanescia", "Pearl"], "titles": ["An Ocean in a Pearl"]},
                               1790500000)
    assert data["banners"]["phase1"] == ["Evanescia", "Pearl"]
    assert prov["b_phase1"][0] == 50               # untouched


# =========================================================================== nitter fleet
def test_the_nitter_fleet_leads_with_nitter_cf_and_demotes_the_flaky_mirror():
    """nitter.cf leads: it is the only mirror with live production evidence. Nothing is deleted:
    x.n0g.xyz answered HTTP 404 then 429 on every handle in runs 36296323488 / 36298632006 /
    36299586254, so it is demoted to dead last and kept as a backup rather than removed."""
    from gamexpress.config import DEFAULT_NITTER
    from gamexpress.sources.twitter import NITTER_BATCH
    assert DEFAULT_NITTER[0] == "https://nitter.cf"
    assert len(DEFAULT_NITTER) == len(set(DEFAULT_NITTER))

    idx = DEFAULT_NITTER.index("https://x.n0g.xyz")
    assert idx >= NITTER_BATCH, "a mirror that answers nothing must never sit in the hot path"
    assert idx == len(DEFAULT_NITTER) - 1, "demoted means dead last"
    for inst in ("https://nitter.cf", "https://xitter.cf", "https://nitter.miningtcup.me",
                 "https://nitter.jaydenha.uk", "https://shitter.thepixora.com"):
        assert DEFAULT_NITTER.index(inst) < idx, inst


# A mirror only earns a hot-path slot by serving a real RSS BODY, not by scoring well on a
# status page that probes the homepage. Each of these was fetched by hand (the 2026-10-03
# audit, then each dated rotation since) and answered with a parseable feed; each of the
# walled ones answered with an interstitial.
#
# This is a NECESSARY condition, not a sufficient one -- serving a clean body to a VPS says
# nothing about what an Actions runner gets. nitter.kareem.one is in this tuple and still
# banned from the hot path, because run 2026-10-03 04:25 answered it with 403 six times out of
# six. A hot-path slot requires BOTH gates: a verified body here, and no runner-measured
# failure in NITTER_CANNOT_ANSWER.
NITTER_SERVES_REAL_RSS = (
    "https://nitter.cf", "https://nitter.kareem.one", "https://tw.eir-nya.gay",
    "https://shitter.thepixora.com", "https://nitter.meowing.monster",
    "https://xitter.cf",   # answered from the Actions runners across its whole pre-2026-10-03
                           # hot-path tenure (its feed's links were read then); demoted for
                           # being nitter.cf's second domain, never for failing a probe
    "https://xcopy.uk",   # rotation #5 (2026-10-14): every monitored handle's /rss body
                          # hand-read that day, nitter.cf parity on top tweet ids; on the
                          # shitter fork's working list. TRIAL until a runner probe says
                          # otherwise.
    "https://nitter.freedit.eu",   # rotation #6 (2026-10-15): 8/8 union handles across
                                   # two days -- 5/5 News-Express accounts (2026-10-14)
                                   # plus GenshinImpact, honkaistarrail and ZZZ_EN this
                                   # round, all with nitter.cf parity on top tweet ids.
                                   # Zero refusals on record, from any vantage. TRIAL.
    "https://nitter.gravitywell.xyz",   # rotation #6 (2026-10-15): 5/8 union handles --
                                   # honkaistarrail, ZZZ_EN, TYPEII_EN, PomPom_HonkaiSR,
                                   # Ananta_EN current with identical top ids;
                                   # deterministic `User not found` on GenshinImpact
                                   # (new this round), Wuthering_Waves and HonkaiNA --
                                   # kareem-style gaps, not downtime. TRIAL.
)
# Reversible tier (demotion only, not NITTER_CANNOT_ANSWER): tw.eir-nya.gay lost the NITTER_GRACE
# race on 4 of 6 handles in run 2026-10-03 04:25 (0 entries on the other 2), and when rotation #3
# gave it a one-run slot-4 trial after thepixora's 403, run #2323 (2026-10-13) returned HTTP 200
# with 0 entries on all 6 handles. Rotation #6 (2026-10-15) seats it back ahead of thepixora by
# the no-block rule (it answers the runners HTTP 200; that round it 429'd every GE-handle probe,
# retry included -- throttled, never a hard refusal) but behind the two zero-history trials.
NITTER_LOSES_THE_RACE = {
    "https://tw.eir-nya.gay": "silent past NITTER_GRACE on 4/6 handles (2026-10-03 04:25); "
                              "slot-4 trial: HTTP 200 with 0 entries on 6/6 handles "
                              "(run #2323, 2026-10-13)",
}
NITTER_WALLED = {
    "https://nitter.jaydenha.uk": '/rss serves a "click anywhere to enter" splash page',
    "https://nitter.click": "browser check (__gandalf) in front of /rss",
    "https://nitter.tiekoetter.com": "Anubis proof-of-work wall",
    "https://nitter.xitter.cc": "Cloudflare 502 Bad gateway",
    "https://nitter.netbub.com": "__goaway_challenge redirect, and HTTP 403 to GH runners",
}


def test_a_walled_mirror_never_sits_in_the_hot_path():
    """The 2026-10-03 audit: three of the four hot-path slots were dead weight. Every mirror
    that answers /rss with an interstitial instead of a feed is behind the hot path, and every
    hot-path slot is a host whose feed body was read by hand."""
    from gamexpress.config import DEFAULT_NITTER
    from gamexpress.sources.twitter import NITTER_BATCH, TOKEN_GATED
    hot = DEFAULT_NITTER[:NITTER_BATCH]
    for inst, why in NITTER_WALLED.items():
        assert inst in DEFAULT_NITTER, f"{inst} is demoted, not deleted"
        assert inst not in hot, f"{inst} is probed every run but {why}"
    for inst in hot:
        assert inst in NITTER_SERVES_REAL_RSS or inst in TOKEN_GATED, inst


def test_a_hot_path_slot_needs_both_a_verified_body_and_a_clean_live_run():
    """Rotation #2 (run 2026-10-03 04:25) + Rotation #4 (run #2323, 2026-10-13). A clean hand
    probe is necessary but NOT sufficient: nitter.kareem.one and shitter.thepixora.com served
    clean feeds off-runner and answered GitHub's runners with 403 six times out of six
    (NITTER_CANNOT_ANSWER). Losing the NITTER_GRACE race or returning HTTP 200 with 0 entries
    (NITTER_LOSES_THE_RACE) is a reversible penalty that still keeps tw.eir-nya.gay out of the
    every-run hot path (rotation #6 seats xitter.cf at slot 2 and meowing.monster at slot 3,
    both runner-proven)."""
    from gamexpress.config import DEFAULT_NITTER
    from gamexpress.sources.twitter import NITTER_BATCH
    hot = DEFAULT_NITTER[:NITTER_BATCH]
    for inst, why in {**NITTER_CANNOT_ANSWER, **NITTER_LOSES_THE_RACE}.items():
        assert inst in DEFAULT_NITTER, f"{inst} is demoted, not deleted"
        assert inst not in hot, f"{inst} costs a request every run but {why}"

    # The quorum is two answers, and the two 6/6-every-run answerers are in the hot path, so
    # the other two slots can never cost an announcement -- only wasted requests.
    proven = ("https://nitter.cf", "https://nitter.miningtcup.me")
    for inst in proven:
        assert inst in hot, f"{inst} answered 6/6 handles live and must stay in the hot path"
    assert len(hot) - len(proven) == 2, "exactly two non-proven slots, no more"
    assert "https://xitter.cf" in hot and "https://tw.eir-nya.gay" not in hot


def test_the_duplicate_domain_never_displaces_an_independent_answerer():
    """xitter.cf answers, and every link in its feed points back at nitter.cf -- it is the same
    backend behind a second domain. It never displaces an independent answerer out of the hot
    path: rotation #4 (run #2323) warmed slot 4 with it after eir-nya's empty trial, and
    rotation #6 (2026-10-15) seats it at slot 2 by operator direction with both independent
    answerers (meowing.monster, miningtcup.me) still in the batch."""
    from gamexpress.config import DEFAULT_NITTER
    from gamexpress.sources.twitter import NITTER_BATCH
    hot = DEFAULT_NITTER[:NITTER_BATCH]
    assert "https://nitter.cf" in hot and "https://xitter.cf" in hot
    assert hot.index("https://nitter.cf") < hot.index("https://xitter.cf")
    assert len([i for i in hot if i in NITTER_SERVES_REAL_RSS]) >= 3


def test_rotation_4_xitter_cf_warms_slot_4_after_eir_nyas_empty_trial():
    """Rotation #4 (run #2323, 2026-10-13): the eir-nya promotion answered HTTP 200 with 0
    entries on all six handles -- a body that parses to nothing, the same uselessness as the
    grace-race loss that benched it on 2026-10-03, now measured twice. xitter.cf takes slot 4:
    it answered from the runners across its whole pre-2026-10-03 hot-path tenure (demoted then
    for being nitter.cf's second domain, never for failing a probe). eir-nya is demoted, not
    deleted; it moves back up the day it delivers a real feed."""
    from gamexpress.config import DEFAULT_NITTER
    from gamexpress.sources.twitter import NITTER_BATCH
    hot = DEFAULT_NITTER[:NITTER_BATCH]
    assert "https://xitter.cf" in hot, "slot 4 must hold a mirror that answers"
    assert "https://tw.eir-nya.gay" not in hot, "two measured failures: grace race + 0 entries"
    assert "https://tw.eir-nya.gay" in DEFAULT_NITTER, "demoted, not deleted"


def test_rotation_6_runner_proven_hot_path_xcopy_verdict_demotes_it():
    """Rotation #6 (2026-10-15): the rotation-5 trial got its verdict -- xcopy.uk
    returned HTTP 403 to the runners on all 6 handles in 4 consecutive post-merge
    runs (#2662-#2665, westus/westus3/eastus2/centralus, 24/24 fetches), the
    nitter.kareem.one signature. The operator then set this round's rule -- no
    member that blocks GitHub Actions / Azure datacenter IPs sits above one that
    doesn't -- and directed the hot path to the four mirrors with positive runner
    evidence: nitter.cf, xitter.cf (back at slot 2, an answerer its whole tenure),
    meowing.monster (6/6 run #2323, 5/6 run #2621; IPv6-slow, so it may lose a
    NITTER_GRACE race on a handle, never the quorum) and the token-gated
    miningtcup.me. The union-coverage test fills the bench: freedit.eu (8/8 union
    handles, nitter.cf parity, zero refusals anywhere) heads the fallbacks,
    gravitywell.xyz (5/8, deterministic not-found gaps: GenshinImpact new this
    round plus Wuthering_Waves and HonkaiNA, complementary to kareem.one) behind
    it, eir-nya (no block, but the only measured runner non-deliveries) and
    thepixora (flapping runner 403) behind the trials. xcopy records its verdict
    in the runner-refused tier. Fleet 19 -> 21; nothing deleted."""
    from gamexpress.config import DEFAULT_NITTER
    from gamexpress.sources.twitter import NITTER_BATCH
    hot = DEFAULT_NITTER[:NITTER_BATCH]
    assert hot == ["https://nitter.cf", "https://xitter.cf",
                   "https://nitter.meowing.monster", "https://nitter.miningtcup.me"], \
        "operator-directed: the four mirrors with positive runner evidence"
    assert "https://xcopy.uk" not in hot, "the trial's verdict: 403 to the runners, 24/24"
    assert DEFAULT_NITTER.index("https://xcopy.uk") > DEFAULT_NITTER.index("https://nitter.kareem.one"), \
        "demoted into the runner-refused tier, beside its own signature"
    assert DEFAULT_NITTER[4] == "https://nitter.freedit.eu", "8/8 union handles -- the fallback head"
    assert DEFAULT_NITTER[5] == "https://nitter.gravitywell.xyz", "5/8 union, deterministic gaps"
    assert DEFAULT_NITTER.index("https://tw.eir-nya.gay") < DEFAULT_NITTER.index("https://shitter.thepixora.com"), \
        "no-block (slow, empty) seats above flapping-403 by this round's rule"
    assert len(DEFAULT_NITTER) == 21 and "https://xcancel.com" in DEFAULT_NITTER
    for kept in ("https://nitter.cf", "https://xitter.cf", "https://xcopy.uk",
                 "https://nitter.meowing.monster", "https://nitter.miningtcup.me",
                 "https://shitter.thepixora.com", "https://tw.eir-nya.gay",
                 "https://nitter.jaydenha.uk", "https://nitter.click",
                 "https://nitter.tiekoetter.com", "https://nitter.xitter.cc",
                 "https://nitter.perennialte.ch", "https://nitter.privacydev.net",
                 "https://nitter.net", "https://nitter.kareem.one",
                 "https://nitter.netbub.com", "https://xcancel.com",
                 "https://x.yuuki.sh", "https://x.n0g.xyz"):
        assert kept in DEFAULT_NITTER, "nothing is ever deleted"


def test_the_token_gated_instance_is_in_the_fleet():
    from gamexpress.config import DEFAULT_NITTER
    from gamexpress.sources.twitter import TOKEN_GATED
    for inst in TOKEN_GATED:
        assert inst in DEFAULT_NITTER, inst


# =========================================================================== nitter hot path
# twitter.py probes NITTER_BATCH instances in parallel on EVERY run, for EVERY handle, whether
# or not the ones ahead of them answered. A mirror in that first batch is therefore a cost on
# every run -- which is why x.n0g.xyz was demoted out of it. These are the instances measured
# as unable to answer GitHub's runners, each with the run that proved it; none of them may sit
# in the hot path again, whatever a public uptime table claims -- or whatever a hand probe from
# somewhere else returns, which is how nitter.kareem.one earned its entry. They all stay in the
# fleet.
#
# An entry leaves this dict ONLY when the measurement that created it has been invalidated, not
# because a mirror looks healthy again. That has happened exactly once: nitter.meowing.monster
# was listed for "HTTP 200 but 0 entries" in run 36305123457, which is the signature of the
# response-truncation bug fixed later the same week -- the client was mis-reading a feed that
# was being served correctly. The verdict was the client's fault, so it was withdrawn and the
# mirror is back on trial in the hot path.
NITTER_CANNOT_ANSWER = {
    "https://nitter.netbub.com": "HTTP 403 on all 4 handles (live run 36305123457)",
    "https://nitter.kareem.one": "HTTP 403 on all 6 handles (live run 2026-10-03 04:25)",
    "https://shitter.thepixora.com": "HTTP 403 on all 6 handles (live run #2312, 2026-10-13)",
    "https://xcopy.uk": "HTTP 403 on all 6 handles in 4 consecutive live runs (#2662-#2665, 2026-10-14)",
                                   # rotation #5 trial verdict (2026-10-15): HTTP 403 on
                                   # all 6 handles in 4 consecutive runs (#2662-#2665)
                                   # across 4 Azure regions -- 24/24 refused. Clean body
                                   # off-runner (stays in NITTER_SERVES_REAL_RSS, exactly
                                   # like kareem.one); flat refusal to Actions IPs.
    "https://xcancel.com": "suspended 2026-09-14",
    "https://x.yuuki.sh": "HTTP 403 to GitHub runners (2026-09-25)",
    "https://x.n0g.xyz": "404 then 429 in runs 36296323488 / 36298632006 / 36299586254",
}


def test_the_first_nitter_batch_holds_only_instances_that_answer():
    from gamexpress.config import DEFAULT_NITTER
    from gamexpress.sources.twitter import NITTER_BATCH
    hot = DEFAULT_NITTER[:NITTER_BATCH]
    for inst, why in NITTER_CANNOT_ANSWER.items():
        assert inst not in hot, f"{inst} is probed on every run but {why}"
    assert hot[0] == "https://nitter.cf"


def test_a_demoted_instance_is_kept_as_a_fallback_rather_than_deleted():
    """Demoting is cheap and reversible; deleting throws away a mirror that may work from a VPS.
    Nothing in the fleet is removed -- a mirror that stopped answering is pushed down instead."""
    from gamexpress.config import DEFAULT_NITTER
    for inst in ("https://nitter.netbub.com", "https://nitter.meowing.monster",
                 "https://x.n0g.xyz"):
        assert inst in DEFAULT_NITTER, inst
    assert len(DEFAULT_NITTER) == len(set(DEFAULT_NITTER))


def test_a_demoted_tail_mirror_costs_no_request_while_the_fleet_answers():
    """The backup at the end of the fleet is free until it is needed: XClient.timeline() breaks
    out of its batch loop the moment TWO instances have answered, so a demoted mirror behind
    that point is never even requested -- and is a real backup again once the fleet degrades."""
    from gamexpress.sources.twitter import XClient
    calls = []

    class F:
        async def get_text(self, url, **k):
            calls.append(url)
            if "n0g" in url:
                return None                        # the 404/429 the live runs recorded
            if "nitter.cf" in url or "xitter.cf" in url:
                return RSS.format(id="3333333333")
            return None

    fleet = ["https://nitter.cf", "https://xitter.cf", "https://nitter.miningtcup.me",
             "https://nitter.jaydenha.uk", "https://x.n0g.xyz"]
    x = XClient(F(), settings(NITTER_INSTANCES=",".join(fleet)))
    tl = asyncio.run(x.timeline("Wuthering_Waves"))
    assert {e["id"] for e in tl} == {"3333333333"}
    assert not [u for u in calls if "n0g" in u], calls



def test_the_notice_carries_phase_one_so_the_hub_cross_check_stays_quiet():
    """Live HSR 4.6 held `prov.b_phase1 = [5, ...]` -- the community banner feed, the LOWEST
    priority in the bot -- and the log said "banner phase1 held a banner TITLE, not a character
    -- replaced from the feed" on every single run. The notice itself was the source of those
    titles. Read correctly, phase1 belongs to the notice at priority 50, in the notice's own
    order (Pearl is the new 5-star, Evanescia the rerun), and the cross-check has nothing left to
    replace -- so the card no longer depends on hub.json being up to date."""
    item = Item("hoyolab", "starrail", "46851682", "https://www.hoyolab.com/article/46851682",
                "Version 4.6 Event Warp: Phase I", HSR_46_WARP_PHASE1, 1790488804)
    ex = schedule.extract(GAMES["starrail"], item)
    record = {"data": {"version": "4.6", "banners": {"phase1": ["Evanescia", "Pearl"]}},
              "prov": {"b_phase1": [5, 1790504763]}}      # exactly what live state holds
    data = schedule.merge(GAMES["starrail"], "4.6", [ex], record, {}, {}, 1790510400)
    assert data["banners"]["phase1"] == ["Pearl", "Evanescia"]     # the notice's own order
    assert record["prov"]["b_phase1"][0] == 50                     # official notice, not the feed

    # With the notice carrying it, a feed that knows every banner TITLE has nothing to replace.
    prov = dict(record["prov"])
    schedule.apply_banner_feed(
        data, prov,
        {"phase1": ["Evanescia", "Pearl"],
         "titles": ["An Ocean in a Pearl", "The Demoiselle in Charge", "Celestial Invitation"]},
        1790510400)
    assert data["banners"]["phase1"] == ["Pearl", "Evanescia"]
    assert prov["b_phase1"][0] == 50


def test_a_quoted_name_after_prose_is_a_banner_not_a_character():
    """The two clauses that broke HSR 4.6 phase1, isolated: a quoted span is only a character
    when a NAME can sit between the star-tier phrase and the quote."""
    text = ('The drop rates for the limited 5-star character Pearl (Elation: Ice) will be '
            'boosted for a limited time. In this phase of Warp, obtainable 5-star characters '
            'include the featured 5-star characters and the custom-selected characters from '
            '"Celestial Invitation." The limited 5-star character Evanescia (Elation: Physical) '
            'can only be obtained from the "The Demoiselle in Charge" Character Event Warp.')
    b = schedule.extract_banner(Item("hoyolab", "starrail", "1", "u",
                                     "Version 4.6 Event Warp: Phase I", text, 1790488804))
    # the name that FOLLOWS the tier phrase is a character; the two quoted spans that sit behind
    # prose are banner names, and must not be read as 5-stars
    assert b["banner_five"] == ["Pearl"], b


def test_a_live_run_pings_even_with_a_stale_no_ping_variable():
    """monitor.yml sets NO_PING for EVERY step, so the live branch has to emit a value that is
    not blank. config._merge_json_blobs() only fills keys whose env value is empty, so
    `NO_PING=''` leaves a hole: the day a NO_PING repo variable is added it arrives in
    GE_VARS_JSON, re-fills the blank, and settings.ping() short-circuits again -- PING_SCHEDULE
    back to dead weight. No such variable exists today (the 2026-09-27 run logs show
    GE_VARS_JSON = {AUTO_MERGE_DEPENDABOT, PING_SCHEDULE}), so this is a guard, not a fix."""
    role = "1296268365593186426"
    stale = '{"NO_PING": "1", "PING_SCHEDULE": "'+ role + '"}'

    live = settings(NO_PING="0", GE_VARS_JSON=stale)          # what a cron/live run now sends
    assert live.no_ping is False
    assert live.ping("schedule", "starrail").roles == [role]

    blank = settings(NO_PING="", GE_VARS_JSON=stale)          # the bug '0' exists to avoid
    assert blank.no_ping is True
    assert not blank.ping("schedule", "starrail")

    test_run = settings(NO_PING="1", PING_SCHEDULE=role)      # ⑥ off -> a test card never pings
    assert not test_run.ping("schedule", "starrail")


def test_a_body_that_arrives_in_several_chunks_is_read_whole():
    """A real feed is answered over several TCP reads. `content.read(n)` returns only what is
    buffered at that instant, so the body used to come back TRUNCATED and a perfectly valid
    feed failed as 'invalid JSON' (c3kay, kuro, fandom, raw.githubusercontent — all of them
    HTTP 200 in the 2026-10-03 run). get_text must loop until EOF, and must still refuse a
    body over the cap. Loopback only — no internet.
    """
    import aiohttp
    from aiohttp import web

    from gamexpress import http as ghttp

    payload = json.dumps({"items": [{"id": i, "pad": "x" * 200} for i in range(8000)]})
    assert len(payload) > 1_500_000, "the point of this test is a body bigger than one read"

    async def handler(request):
        resp = web.StreamResponse(headers={"Content-Type": "application/json"})
        await resp.prepare(request)
        data = payload.encode()
        for i in range(0, len(data), 50_000):
            await resp.write(data[i:i + 50_000])
            await asyncio.sleep(0)        # hand control back: several separate feeds, like the wire
        await resp.write_eof()
        return resp

    async def run():
        app = web.Application()
        app.router.add_get("/feed.json", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/feed.json"
        try:
            async with aiohttp.ClientSession() as session:
                f = ghttp.Fetcher(session, timeout=15)
                body = await f.get_text(url, source="chunked", retries=0)
                got = await f.get_json(url, source="chunked", retries=0)

                cap = ghttp.MAX_BYTES
                ghttp.MAX_BYTES = 100_000          # same body, now over the cap
                try:
                    refused = await f.get_text(url, source="capped", retries=0)
                finally:
                    ghttp.MAX_BYTES = cap
                return body, got, refused, f.health
        finally:
            await runner.cleanup()

    body, got, refused, health = asyncio.run(run())

    assert body is not None and len(body) == len(payload), \
        f"truncated: got {0 if body is None else len(body)} of {len(payload)} chars"
    assert isinstance(got, dict) and len(got["items"]) == 8000
    assert health["chunked"].fail == 0, health["chunked"].last_error

    assert refused is None, "a body over MAX_BYTES must be refused, not truncated and parsed"
    assert "larger than" in health["capped"].last_error



# ===================================================================== native cycle speculation
# The bot predicts a version's livestream / maintenance / pre-install times from the published
# rhythm when no live source has spoken yet, labels them estimated, and lets any real source
# overwrite them. These tests pin the maths against the real history of all four games.
TZ8 = timezone(timedelta(hours=8))


def at8(text: str) -> int:
    """'2026-11-04 06:00' in the publisher's clock (UTC+8) -> unix seconds."""
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ8).timestamp())


# Real maintenance start dates as published by HoYoverse and Kuro, oldest first.
PUBLISHED_MAINTENANCE = {
    "genshin": ["2024-07-17", "2024-08-28", "2024-10-09", "2024-11-20", "2025-01-01", "2025-02-12",
                "2025-03-26", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-10", "2025-10-22",
                "2025-12-03", "2026-01-14", "2026-02-25", "2026-04-08", "2026-05-20", "2026-07-01",
                "2026-08-12", "2026-09-23"],
    "starrail": ["2024-06-19", "2024-07-31", "2024-09-10", "2024-10-23", "2024-12-04", "2025-01-15",
                 "2025-02-26", "2025-04-09", "2025-05-21", "2025-07-02", "2025-08-13", "2025-09-24",
                 "2025-11-05", "2025-12-17", "2026-02-13", "2026-03-25", "2026-04-22", "2026-06-01",
                 "2026-07-15", "2026-08-26", "2026-09-28"],
    "zzz": ["2025-01-22", "2025-03-12", "2025-04-23", "2025-06-06", "2025-07-16", "2025-09-04",
            "2025-10-15", "2025-11-26", "2025-12-30", "2026-02-06", "2026-03-24", "2026-05-06",
            "2026-06-17", "2026-07-29", "2026-09-09"],
    "wuwa": ["2024-11-14", "2025-01-02", "2025-02-13", "2025-03-27", "2025-04-29", "2025-06-12",
             "2025-07-24", "2025-08-28", "2025-10-09", "2025-11-20", "2025-12-25", "2026-02-05",
             "2026-03-19", "2026-04-30", "2026-06-08", "2026-07-10", "2026-08-20", "2026-09-30"],
}


def _history_records(game_key: str, count: int) -> dict:
    """The first `count` published maintenance dates as real (non-estimated) version records."""
    hhmm = "04:00" if game_key == "wuwa" else "06:00"
    records = {}
    for i, day in enumerate(PUBLISHED_MAINTENANCE[game_key][:count]):
        records[f"0.{i}"] = {"data": {"maint_start_ts": at8(f"{day} {hhmm}")},
                             "prov": {"maint_start_ts": [schedule.PRIORITY["hoyolab"], 1]}}
    return records


def test_discord_timestamp_tags_decode_to_the_published_wall_clock():
    """<t:EPOCH:F> carries plain unix seconds; the suffix only picks a rendering."""
    cases = [
        (1716552000, "Fri 2024-05-24 20:00"),      # GI 4.7 Special Program
        (1717759800, "Fri 2024-06-07 19:30"),      # HSR 2.3 Special Program
        (1718748000, "Wed 2024-06-19 06:00"),      # HSR 2.3 maintenance start
        (1718766000, "Wed 2024-06-19 11:00"),      # HSR 2.3 maintenance end
        (1721167200, "Wed 2024-07-17 06:00"),      # GI 4.8 maintenance start
        (1790114400, "Wed 2026-09-23 06:00"),      # GI 7.1 maintenance start
    ]
    for epoch, want in cases:
        assert datetime.fromtimestamp(epoch, TZ8).strftime("%a %Y-%m-%d %H:%M") == want
        for style in ("F", "R", "d", "t"):         # the style never changes the instant
            assert discord_ts(epoch, style) == f"<t:{epoch}:{style}>"


def test_snap_weekday_moves_to_the_nearest_matching_day():
    wed = datetime(2026, 11, 4, 6, 0, tzinfo=TZ8)
    assert wed.weekday() == 2
    assert schedule._snap_weekday(wed, 2) == wed                       # already right -> untouched
    assert schedule._snap_weekday(wed, 3).day == 5                     # Thu: forward one
    assert schedule._snap_weekday(wed, 1).day == 3                     # Tue: back one
    assert schedule._snap_weekday(wed, 5).day == 7                     # Sat: forward three
    assert schedule._snap_weekday(wed, 6).day == 1                     # Sun: back three, not +4
    assert schedule._snap_weekday(wed, 2).hour == 6                    # time of day preserved


def test_observed_starts_never_learns_from_its_own_guess():
    older, newer = at8("2026-08-12 06:00"), at8("2026-09-23 06:00")
    records = {
        "1.0": {"data": {"maint_start_ts": newer}, "prov": {}},
        "1.1": {"data": {"maint_start_ts": at8("2026-11-04 06:00"),
                         "estimated": ["maint_start_ts"]}},            # speculated -> ignored
        "1.2": {"data": {"maint_start_ts": "junk"}},
        "1.3": {"data": {"maint_start_ts": 0}},
        "1.4": {"data": None},
        "1.5": "not a record",
        "1.6": {"data": {"maint_start_ts": older}},
        "1.7": {"data": {"maint_start_ts": 1000}},                     # before TS_MIN -> ignored
    }
    assert schedule.observed_starts(records) == [("1.6", older), ("1.0", newer)]
    assert schedule.observed_starts({}) == [] and schedule.observed_starts(None) == []


def test_observed_cadence_prefers_full_history_over_a_short_window():
    starts = [("v", at8(f"{d} 06:00")) for d in PUBLISHED_MAINTENANCE["starrail"]]
    # HSR's recent run is 58, 40, 28, 40, 44, 42, 33 days; the full history still reads 42.
    assert schedule.observed_cadence_days(starts) == 42
    assert schedule.observed_cadence_days(starts[-3:]) == 37.5      # a short window is distracted
    # implausible gaps are discarded rather than averaged in
    assert schedule.observed_cadence_days([("a", 0), ("b", 10 ** 9)]) is None
    assert schedule.observed_cadence_days([("a", 0)]) is None
    assert schedule.observed_cadence_days([]) is None


def test_next_version_flags_the_major_rollover():
    assert schedule.next_version("7.1") == ("7.2", "")
    assert schedule.next_version("3.2") == ("3.3", "")
    # past .6 the series has rolled at both .7 (GI 6.7 -> 7.0) and .8 (GI 4.8 -> 5.0)
    assert schedule.next_version("3.7") == ("3.8", "4.0")
    assert schedule.next_version("4.8") == ("4.9", "5.0")
    assert schedule.next_version("") == ("", "")
    assert schedule.next_version("not-a-version") == ("", "")


def test_predict_cycle_backtests_the_published_history():
    """Replay every game: predict version N knowing only versions before it.

    The floors are the accuracy actually measured over this corpus. Genshin has never missed a
    42-day beat in 20 versions and must be predicted to the minute every single time.
    """
    # Measured over this corpus: GI 18/18 exact, HSR 14/19, ZZZ 6/13, WW 9/16. The floors sit
    # just below so a real regression fails while a harmless tie-break change does not.
    floors = {"genshin": (1.00, 1.00), "starrail": (0.70, 0.80),
              "zzz": (0.45, 0.50), "wuwa": (0.55, 0.65)}          # (exact, within 3 days)
    for game_key, (exact_floor, near_floor) in floors.items():
        days = PUBLISHED_MAINTENANCE[game_key]
        hhmm = "04:00" if game_key == "wuwa" else "06:00"
        exact = near = total = 0
        for i in range(schedule.SPECULATE_MIN_HISTORY, len(days)):
            records = _history_records(game_key, i)
            truth = at8(f"{days[i]} {hhmm}")
            now = records[f"0.{i - 1}"]["data"]["maint_start_ts"] + 86400
            guess = schedule.predict_cycle(GAMES[game_key], records, now)
            assert guess, (game_key, days[i])
            off = abs(guess["maint_start_ts"] - truth)
            total += 1
            exact += off == 0
            near += off <= 3 * 86400
        assert exact / total >= exact_floor, f"{game_key}: {exact}/{total} exact"
        assert near / total >= near_floor, f"{game_key}: {near}/{total} within 3 days"


def test_predict_cycle_matches_the_documented_forecast():
    """The four headline predictions in docs/TIMESTAMP-PATTERNS.md, from a cold start."""
    now = at8("2026-10-03 12:00")
    want = {
        "genshin": ("7.2", "", "2026-10-23 20:00", "2026-11-02 11:00", "2026-11-04 06:00", "2026-11-04 11:00"),
        "starrail": ("4.7", "", "2026-10-30 19:30", "2026-11-09 14:00", "2026-11-11 06:00", "2026-11-11 11:00"),
        "zzz": ("3.3", "", "2026-10-09 19:30", "2026-10-19 12:00", "2026-10-21 06:00", "2026-10-21 11:00"),
        "wuwa": ("3.8", "4.0", "2026-10-30 19:00", "2026-11-10 10:00", "2026-11-12 04:00", "2026-11-12 11:00"),
    }
    for game_key, (ver, alt, program, pre, start, end) in want.items():
        got = schedule.predict_cycle(GAMES[game_key], {}, now)       # no history -> shipped anchor
        assert got, game_key
        assert (got["version"], got["version_alt"]) == (ver, alt), game_key
        assert got["program_ts"] == at8(program), (game_key, "program")
        assert got["preinstall_ts"] == at8(pre), (game_key, "pre-install")
        assert got["maint_start_ts"] == at8(start), (game_key, "maintenance start")
        assert got["maint_end_ts"] == at8(end), (game_key, "maintenance end")
        assert got["learned"] is False and got["observed"] == 0


def test_speculation_never_outranks_or_overwrites_a_real_source():
    records = _history_records("genshin", 4)
    now = at8("2026-10-03 12:00")
    real_start, real_program = at8("2026-11-05 06:00"), at8("2026-10-24 20:00")
    data = {"maint_start_ts": real_start, "program_ts": real_program}
    prov = {"maint_start_ts": [schedule.PRIORITY["hoyolab"], 1],
            "program_ts": [schedule.PRIORITY["news"], 1]}
    assert schedule.derive_cycle(GAMES["genshin"], "7.2", data, prov, now, records) == []
    assert data["maint_start_ts"] == real_start and data["program_ts"] == real_program
    assert "estimated" not in data
    assert schedule.PRIORITY["pattern"] < min(schedule.PRIORITY[k] for k in
                                              ("countdown", "launcher", "x", "news", "hoyolab",
                                               "kuro", "override"))


def test_speculation_is_labelled_estimated_and_shown_as_such_on_the_card():
    records = _history_records("genshin", 4)
    now = at8("2026-10-03 12:00")
    data, prov = {"version": "7.2"}, {}
    written = schedule.derive_cycle(GAMES["genshin"], "7.2", data, prov, now, records)
    assert set(written) == {"program_ts", "maint_start_ts", "maint_end_ts"}
    assert data["estimated"] == ["program_ts", "maint_start_ts", "maint_end_ts"]
    assert data["estimate_sources"] == ["version cadence"]
    for key in written:
        assert prov[key][0] == schedule.PRIORITY["pattern"]
    # merge() also chains the pre-install derivation onto the speculated maintenance date
    record = {"data": {}, "prov": {}}
    merged = schedule.merge(GAMES["genshin"], "7.2", [], record, {}, {}, now, records=records)
    assert merged["maint_start_ts"] == at8("2026-11-04 06:00")
    assert merged["preinstall_ts"] == at8("2026-11-02 11:00")
    assert "preinstall_ts" in merged["estimated"]
    rendered = json.dumps(cards.schedule_payload(GAMES["genshin"], merged, settings(),
                                                 cards.Ping(), now), ensure_ascii=False)
    assert "estimated from version cadence" in rendered
    assert "the official notice replaces it automatically" in rendered


def test_speculation_never_dates_the_anchor_version_itself():
    """A livestream post for the newest version must not inherit its successor's dates."""
    records = _history_records("genshin", 4)
    anchor = max(records, key=lambda v: records[v]["data"]["maint_start_ts"])
    now = at8("2026-10-03 12:00")
    data, prov = {"version": anchor}, {}
    assert schedule.derive_cycle(GAMES["genshin"], anchor, data, prov, now, records) == []
    assert data == {"version": anchor} and prov == {}
    assert schedule.derive_cycle(GAMES["genshin"], "0.1", data, prov, now, records) == []


def test_a_corrupt_timestamp_in_state_can_never_abort_the_run():
    """state.json is long-lived and hand-editable, so it can hold anything.

    datetime.fromtimestamp() raises ValueError past year 9999, and predict_cycle runs inside
    merge() -- an exception here would abort the whole schedule run and post nothing at all.
    Implausible values are therefore dropped, exactly as observed_lead_h already drops them.
    """
    now = at8("2026-10-03 12:00")
    hostile = [
        None, {}, {"x": None}, {"x": "not a record"},
        {"1.0": {"data": {"maint_start_ts": -5}}},
        {"1.0": {"data": {"maint_start_ts": -(10 ** 15)}}},
        {"1.0": {"data": {"maint_start_ts": 10 ** 15}}},        # year 31690708 -> ValueError
        {"1.0": {"data": {"maint_start_ts": 10 ** 18}}},
        {"1.0": {"data": {"maint_start_ts": float("nan")}}},
        {"1.0": {"data": {"maint_start_ts": float("inf")}}},
        {"1.0": {"data": {"maint_start_ts": True}}},
        {"1.0": {"data": {"maint_start_ts": 1790114400, "estimated": "not a list"}}},
    ]
    for records in hostile:
        schedule.predict_cycle(GAMES["genshin"], records, now)       # must not raise
        schedule.derive_cycle(GAMES["genshin"], "7.2", {}, {}, now, records or {})
        schedule.observed_starts(records)
    # out-of-range values are dropped, not clamped into the history
    assert schedule.observed_starts({"1.0": {"data": {"maint_start_ts": 10 ** 15}},
                                     "1.1": {"data": {"maint_start_ts": 1790114400}}}) \
        == [("1.1", 1790114400)]
    # a hand-edited games.json anchor is bounded the same way
    broken = replace(GAMES["genshin"],
                     cadence=replace(GAMES["genshin"].cadence, anchor_ts=10 ** 15))
    assert schedule.predict_cycle(broken, {}, now) is None


def test_speculation_is_refused_outside_the_believable_horizon():
    records = _history_records("genshin", 4)
    far_past = at8("2020-01-01 00:00")
    # an anchor many cycles stale still yields a FUTURE date, never one already behind us
    guess = schedule.predict_cycle(GAMES["genshin"], records, at8("2026-10-03 12:00"))
    assert guess["maint_start_ts"] > at8("2026-10-03 12:00")
    # nothing to anchor on, and a game that declares no cadence at all
    assert schedule.predict_cycle(GAMES["hna"], {}, far_past) is None
    no_cadence = replace(GAMES["genshin"], cadence=None)
    assert schedule.predict_cycle(no_cadence, records, far_past) is None
    assert schedule.derive_cycle(no_cadence, "7.2", {}, {}, far_past, records) == []
    assert schedule.derive_cycle(GAMES["genshin"], "", {}, {}, far_past, records) == []
    # An anchor so old that the roll-forward gives up: the loop advances at most 24 cycles
    # (~2.8 years at 42 days), so a 2002 anchor never reaches the present and no date is shown.
    # Plausible timestamps, so they survive the TS_MIN/TS_MAX filter and really exercise the loop.
    stale = {"0.0": {"data": {"maint_start_ts": at8("2002-01-02 06:00")}, "prov": {}},
             "0.1": {"data": {"maint_start_ts": at8("2002-02-13 06:00")}, "prov": {}}}
    assert schedule.observed_starts(stale), "these must survive the plausibility filter"
    assert schedule.predict_cycle(GAMES["genshin"], stale, at8("2026-10-03 12:00")) is None


def test_cadence_config_survives_malformed_values():
    assert gconfig._cadence(None) is None and gconfig._cadence({}) is None
    assert gconfig._cadence({"days": "soon"}) is None
    assert gconfig._cadence({"days": 0}) is None and gconfig._cadence({"days": 9999}) is None
    assert gconfig._cadence({"maint_hours": 0}) is None
    assert gconfig._cadence({"tz_offset_h": 99}) is None
    loose = gconfig._cadence({"days": 42, "maint_weekday": "garbage", "maint_time": "99:99",
                              "program_weekday": 11, "program_time": "no", "anchor_ts": "soon"})
    assert loose.maint_weekday == 2 and loose.maint_time == (6, 0)    # defaults, never a crash
    assert loose.program_weekday == 4 and loose.program_time == (20, 0) and loose.anchor_ts == 0
    assert gconfig._cadence({"days": 42, "maint_weekday": "thursday"}).maint_weekday == 3
    assert gconfig._cadence({"days": 42, "maint_weekday": 6}).maint_weekday == 6
    for g in GAMES.values():                                          # shipped config is valid
        if g.cadence is not None:
            assert 0 <= g.cadence.maint_weekday <= 6
            assert g.cadence.confidence in ("high", "medium", "low")
            assert g.cadence.anchor_ts > 0 and g.cadence.anchor_version


def test_a_real_notice_replaces_the_speculation_and_edits_the_card():
    """Speculate -> post, then the official notice lands on a different day -> ONE silent edit."""
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        now = at8("2026-10-03 12:00")
        records = _history_records("genshin", 4)
        for rec in records.values():                     # history only, never posted
            rec["status"] = "posted"
            rec["card_retired"] = True

        ctx = make_ctx(sp, now=now)
        ctx.webhook = ScriptedWebhook([], [SendResult(True, 200, message_id="card-1")])
        item = Item("hoyolab", "genshin", "teaser-7.2", "https://www.hoyolab.com/article/teaser-7.2",
                    "Version 7.2 Special Program announcement", "coming soon", now)
        teaser = schedule.Extract(item, "program", "7.2", fields={})
        asyncio.run(schedule._handle_version(ctx, GAMES["genshin"], "7.2", [teaser], records,
                                             {}, {}, True))
        posted = records["7.2"]
        assert posted["data"]["maint_start_ts"] == at8("2026-11-04 06:00")
        assert "maint_start_ts" in posted["data"]["estimated"]
        assert posted["message_id"] == "card-1"
        first_hash = posted["payload_hash"]

        # the official notice says the 5th, not the 4th
        real_start, real_end = at8("2026-11-05 06:00"), at8("2026-11-05 11:00")
        notice_item = Item("hoyolab", "genshin", "notice-7.2",
                           "https://www.hoyolab.com/article/notice-7.2",
                           "Version 7.2 Update Details", "official notice", now + 3600)
        notice = schedule.Extract(notice_item, "maintenance", "7.2",
                                  fields={"maint_start_ts": real_start, "maint_end_ts": real_end})
        ctx2 = make_ctx(sp, now=now + 3600)
        ctx2.webhook = ScriptedWebhook([SendResult(True, 200, message_id="card-1")])
        asyncio.run(schedule._handle_version(ctx2, GAMES["genshin"], "7.2", [notice], records,
                                             {}, {}, True))
        updated = records["7.2"]
        assert updated["data"]["maint_start_ts"] == real_start
        assert "maint_start_ts" not in (updated["data"].get("estimated") or [])
        assert updated["prov"]["maint_start_ts"][0] == schedule.PRIORITY["hoyolab"]
        assert updated["payload_hash"] != first_hash
        assert updated["message_id"] == "card-1", "the same card must be edited, not reposted"
        assert len(ctx2.webhook.edits) == 1 and ctx2.webhook.sends == []


def test_a_correct_speculation_moves_nothing_but_the_estimated_caveat():
    """When the official notice confirms the guess, no timestamp on the card may move.

    The card is still edited exactly once, because it must stop telling readers the times are
    estimated — but that one edit is silent, changes no date, and is the ONLY difference. A
    further run with the same notice costs nothing at all.
    """
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "state.json"
        now = at8("2026-10-03 12:00")
        records = _history_records("genshin", 4)
        for rec in records.values():
            rec["status"] = "posted"
            rec["card_retired"] = True

        ctx = make_ctx(sp, now=now)
        ctx.webhook = ScriptedWebhook([], [SendResult(True, 200, message_id="card-1")])
        item = Item("hoyolab", "genshin", "teaser-7.2", "https://www.hoyolab.com/article/teaser-7.2",
                    "Version 7.2 Special Program announcement", "coming soon", now)
        asyncio.run(schedule._handle_version(ctx, GAMES["genshin"], "7.2",
                                             [schedule.Extract(item, "program", "7.2", fields={})],
                                             records, {}, {}, True))
        guessed = dict(records["7.2"]["data"])
        speculated_card = json.dumps(ctx.webhook.sends[0], ensure_ascii=False)
        assert "estimated from version cadence" in speculated_card

        # the official notice confirms exactly what was predicted
        notice_item = Item("hoyolab", "genshin", "notice-7.2",
                           "https://www.hoyolab.com/article/notice-7.2",
                           "Version 7.2 Update Details", "official notice", now + 3600)
        fields = {k: guessed[k] for k in ("maint_start_ts", "maint_end_ts", "preinstall_ts",
                                          "program_ts")}
        # a maintenance notice never announces the livestream, so the air time arrives on the
        # program post — both are confirming exactly what was already predicted
        notice = schedule.Extract(notice_item, "maintenance", "7.2",
                                  fields={k: v for k, v in fields.items() if k != "program_ts"})
        announce = schedule.Extract(item, "program", "7.2",
                                    fields={"program_ts": fields["program_ts"]})
        ctx2 = make_ctx(sp, now=now + 3600)
        ctx2.webhook = ScriptedWebhook([SendResult(True, 200, message_id="card-1")])
        asyncio.run(schedule._handle_version(ctx2, GAMES["genshin"], "7.2", [notice, announce],
                                             records, {}, {}, True))
        after = records["7.2"]
        assert ctx2.webhook.sends == [], "a confirmed prediction must never be re-posted or re-pinged"
        assert len(ctx2.webhook.edits) == 1, "exactly one silent edit"
        # not one timestamp moved, and the card no longer calls them estimates
        for key, value in fields.items():
            assert after["data"][key] == value, key
        assert not after["data"].get("estimated")
        confirmed_card = json.dumps(ctx2.webhook.edits[0][1], ensure_ascii=False)
        assert "estimated from version cadence" not in confirmed_card
        for stamp in fields.values():                       # the same <t:...> tags, untouched
            assert f"<t:{stamp}:" in speculated_card and f"<t:{stamp}:" in confirmed_card

        # third run, same notice, nothing new to say -> zero Discord calls
        ctx3 = make_ctx(sp, now=now + 7200)
        ctx3.webhook = ScriptedWebhook([])
        asyncio.run(schedule._handle_version(ctx3, GAMES["genshin"], "7.2", [notice, announce],
                                             records, {}, {}, True))
        assert ctx3.webhook.edits == [] and ctx3.webhook.sends == []


def test_speculation_does_not_churn_the_state_file():
    """Re-running with no new information must not re-stamp provenance with the new clock."""
    records = _history_records("genshin", 4)
    data, prov = {"version": "7.2"}, {}
    first = schedule.derive_cycle(GAMES["genshin"], "7.2", data, prov, at8("2026-10-03 12:00"), records)
    assert first
    snapshot, prov_snapshot = dict(data), {k: list(v) for k, v in prov.items()}
    again = schedule.derive_cycle(GAMES["genshin"], "7.2", data, prov, at8("2026-10-04 12:00"), records)
    assert again == [], "an unchanged prediction must rewrite nothing"
    assert data == snapshot and {k: list(v) for k, v in prov.items()} == prov_snapshot


def _speculate(*argv) -> tuple[int, str]:
    """Run the CLI against an EMPTY state file, never the repo's committed one.

    state/state.json is rewritten by the monitor workflow on every live run. A test that reads
    it asserts on data that changes under it: the moment a real 7.2 notice lands, the anchor
    becomes 7.2, the prediction becomes 7.3, and CI fails on a pull request that touched none of
    this. Pinning STATE_PATH to an empty temp file makes the cold-start path deterministic.
    """
    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("STATE_PATH")
        os.environ["STATE_PATH"] = str(Path(tmp) / "state.json")
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = gxmain.main(list(argv))
            return rc, out.getvalue()
        finally:
            if previous is None:
                os.environ.pop("STATE_PATH", None)
            else:
                os.environ["STATE_PATH"] = previous


def test_speculate_command_reports_every_game_without_posting():
    rc, text = _speculate("speculate", "--now", str(at8("2026-10-03 12:00")), "--verbose")
    assert rc == 0
    for short in ("GI", "HSR", "ZZZ", "WW"):
        assert f"\n{short:5}" in f"\n{text}", short
    assert "Genshin Impact 7.2" in text
    assert "Wuthering Waves 3.8 (or 4.0 if the major rolls over)" in text
    assert "estimates only" in text
    assert "real maintenance dates on file: none" in text       # --verbose, empty state
    # a cold start must say so rather than claiming it learned anything
    assert "shipped cold-start value" in text and "learned from" not in text

    rc, one = _speculate("speculate", "--game", "zzz")
    assert rc == 0 and "Zenless" in one and "Genshin" not in one


def test_speculate_command_survives_the_real_state_file():
    """Same command against whatever state/state.json currently holds: must never crash.

    Deliberately asserts nothing about the predicted versions -- that file is rewritten by the
    monitor on every run and is not a fixture.
    """
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert gxmain.main(["speculate"]) == 0
    assert "estimates only" in out.getvalue()

def test_build_id_reports_the_commit_in_actions_and_degrades_quietly_outside():
    """The run summary's build line replaced a hand-maintained __version__.

    Guards the swap: Actions must get a real commit (the whole point -- it names the code
    that posted the card), and anywhere without GITHUB_SHA must fall back rather than
    print an empty string or raise. Also pins the short form: a 40-char SHA in a summary
    heading is noise.
    """
    import gamexpress

    saved = os.environ.get("GITHUB_SHA")
    try:
        os.environ["GITHUB_SHA"] = "702c9bd1f4e8a3c05d6b9271ae4c8f0b3d5e7a19"
        assert gamexpress.build_id() == "702c9bd"

        for blank in ("", "   "[:0]):          # unset and empty both mean "not in Actions"
            os.environ["GITHUB_SHA"] = blank
            assert gamexpress.build_id() == "local"
        os.environ.pop("GITHUB_SHA", None)
        assert gamexpress.build_id() == "local"
    finally:
        os.environ.pop("GITHUB_SHA", None)
        if saved is not None:
            os.environ["GITHUB_SHA"] = saved

    # and nothing anywhere still expects the retired attribute
    assert not hasattr(gamexpress, "__version__")



def test_existing_summary_sanitization_truthful_empty_report_and_failed_output():
    from dataclasses import replace

    from gamexpress import runner as module

    old_values = {name: os.environ.get(name) for name in ("GITHUB_STEP_SUMMARY", "SUMMARY_FIXTURE_SECRET")}
    try:
        os.environ["SUMMARY_FIXTURE_SECRET"] = "private_fixture_token"
        clean = module._summary_text("private_fixture_token https://discord.com/api/webhooks/123/abc "
                                     "https://example.test/rss?token=hiddenfeed Bearer hiddenbearer")
        assert not any(value in clean for value in ("private_fixture_token", "webhooks/123/abc", "hiddenfeed", "hiddenbearer"))
        assert "%25" in module._annotation_text("100%")
        assert "\n" not in module._annotation_text("upstream\n::error::injected")
        with tempfile.TemporaryDirectory() as directory:
            ctx = make_ctx(Path(directory) / "state.json", now=1790450000)
            ctx.fetcher = types.SimpleNamespace(summary=lambda: "fixture: 1 ok / 0 fail; " + HOOK)
            ctx.report = []
            ctx.warnings = ["upstream\n::error::injected " + HOOK]
            ctx.errors = []
            ctx.elapsed = 4.2
            path = Path(directory) / "summary.md"
            path.write_text("existing summary\n", encoding="utf-8")
            os.environ["GITHUB_STEP_SUMMARY"] = str(path)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                assert module.write_summary(ctx) is None
            assert "\n::error::injected" not in output.getvalue()
            assert HOOK not in output.getvalue()
            text = path.read_text(encoding="utf-8")
            assert text.startswith("existing summary\n")
            assert HOOK not in text
            assert "No report messages recorded" in text
            assert "repository commit/push: not observed here" in text
            assert "run took 4.2s" in text and "1 ok / 0 fail" in text
            ctx.settings = replace(ctx.settings, dry_run=True)
            with contextlib.redirect_stdout(io.StringIO()):
                module.write_summary(ctx)
            assert "DRY RUN" in path.read_text(encoding="utf-8")
            os.environ["GITHUB_STEP_SUMMARY"] = directory  # directory is not a writable file
            with contextlib.redirect_stdout(io.StringIO()):
                assert module.write_summary(ctx) is None
    finally:
        for name, value in old_values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# =========================================================================== opt-in official news
class NewsFetcher:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def news_item(ident="100", ts=1789300010, body="Article body", title="Character introduction"):
    return Item("hoyolab", "genshin", ident, f"https://www.hoyolab.com/article/{ident}", title, "", ts,
                article_blocks=[{"kind": "text", "text": body}], author="Official Author")


def news_ctx(path, now=1789300000, **env):
    return make_ctx(path, now=now, NEWS_ENABLED="1", NEWS_GAMES="genshin",
                    DISCORD_WEBHOOK_NEWS_GENSHIN=HOOK, **env)


def test_news_opt_in_global_disable_and_no_routing_fallback():
    assert "news" not in settings(ENABLED_FEATURES="news").features
    assert "news" not in settings(NEWS_ENABLED="0", ONLY="news").features
    assert settings(NEWS_ENABLED="1", ONLY="news").features == {"news"}
    for disabled in ("none", "off", "standby", "0"):
        assert not settings(NEWS_ENABLED="1", ENABLED_FEATURES=disabled).features
    s = settings(NEWS_ENABLED="1", DISCORD_WEBHOOK_NEWS=HOOK, FORCE_WEBHOOK=HOOK)
    for game in GAMES:
        assert s.webhook("news", game) is None
    s = settings(DISCORD_WEBHOOK_NEWS_HSR=HOOK)
    assert s.webhook("news", "starrail") == HOOK
    assert s.webhook("news", "genshin") is None
    assert settings().webhook("schedule", "genshin") == HOOK
    assert settings().webhook("codes", "genshin") == HOOK
    assert settings(NEWS_MAX_POSTS_PER_RUN=100).news_max_posts == 20
    assert settings(NEWS_MAX_POSTS_PER_RUN=-3).news_max_posts == 1


def test_news_disabled_passive_missing_destination_make_no_requests_or_state():
    from gamexpress.runner import run_news
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / "state.json")
        ctx.settings = settings()
        asyncio.run(run_news(ctx))
        assert "news" not in ctx.state.data
        ctx = news_ctx(Path(directory) / "state.json")
        ctx.active = False
        asyncio.run(run_news(ctx))
        assert "news" not in ctx.state.data
        ctx = news_ctx(Path(directory) / "state.json")
        ctx.settings = settings(NEWS_ENABLED=1, ONLY="news")
        asyncio.run(run_news(ctx))  # fetcher=None fails if anything tries to fetch
        assert "news" not in ctx.state.data and ctx.warnings


def test_news_five_destinations_ananta_inactive_wangyue_absent():
    s = settings(NEWS_ENABLED=1, **{f"DISCORD_WEBHOOK_NEWS_{key.upper()}": HOOK + key
                                  for key in ("genshin", "starrail", "hna", "zzz", "wuwa")})
    assert s.news_games == {"genshin", "starrail", "hna", "zzz", "wuwa"}
    assert "ananta" not in s.news_games and "wangyue" not in GAMES
    for key in s.news_games:
        assert s.webhook("news", key) == HOOK + key
    assert s.webhook("news", "ananta") is None


def test_news_hoyolab_discovery_all_tabs_dedup_and_failure():
    page = {"retcode": 0, "data": {"list": [{"post": {"post_id": "100", "created_at": 120, "game_id": 2}},
                                                {"post": {"post_id": "101", "created_at": 120, "game_id": 6}},
                                                {"post": {"post_id": "102", "created_at": 90}}]}}
    f = NewsFetcher([page, page, page])
    rows = asyncio.run(hoyolab.discover_news(f, GAMES["genshin"], 100))
    assert rows == [{"id": "100", "ts": 120}]
    assert {kw["params"]["type"] for _, kw in f.calls} == {1, 2, 3}
    assert all(kw["params"]["gids"] == 2 for _, kw in f.calls)
    for invalid in (None, {}, {"retcode": -1}, {"retcode": 0, "data": {}}, {"retcode": 0, "data": {"list": "bad"}}):
        assert asyncio.run(hoyolab.discover_news(NewsFetcher([invalid]), GAMES["hna"], 100)) is None


def test_news_hoyolab_pagination_and_repeated_cursor_fail_closed():
    page = {"retcode": 0, "data": {"list": [{"post": {"post_id": str(i), "created_at": 120}}
                                                for i in range(100, 120)]}}
    empty = {"retcode": 0, "data": {"list": []}}
    f = NewsFetcher([page, empty, empty, empty])
    assert len(asyncio.run(hoyolab.discover_news(f, GAMES["genshin"], 100))) == 20
    assert f.calls[1][1]["params"]["last_id"] == "119"
    assert asyncio.run(hoyolab.discover_news(NewsFetcher([page, page]), GAMES["genshin"], 100)) is None


def test_news_full_hoyolab_real_fixture_and_no_preview_delivery():
    full = fx("hoyolab_genshin_46604275_full.json")
    post = full["data"]["post"]["post"]
    row = {"id": post["post_id"], "ts": post["created_at"]}
    item = asyncio.run(hoyolab.news_article(NewsFetcher([full]), GAMES["genshin"], row))
    assert item.title == post["subject"] and item.published_ts == row["ts"]
    assert item.article_blocks[0]["kind"] == "image"
    assert any("Dear Traveler" in block.get("text", "") for block in item.article_blocks)
    assert asyncio.run(hoyolab.news_article(NewsFetcher([None]), GAMES["genshin"], row)) is None
    wrong = json.loads(json.dumps(full))
    wrong["data"]["post"]["post"]["game_id"] = 6
    assert asyncio.run(hoyolab.news_article(NewsFetcher([wrong]), GAMES["genshin"], row)) is None
    empty = json.loads(json.dumps(full))
    empty["data"]["post"]["post"]["content"] = ""
    assert asyncio.run(hoyolab.news_article(NewsFetcher([empty]), GAMES["genshin"], row)) is None


def test_news_kuro_menu_detail_and_no_truncated_preview_fallback():
    from gamexpress.sources import kuro
    article = {"articleId": 5000, "articleTitle": "New character", "createTime": "2026-10-18 08:00:00",
               "articleType": 1, "articleDesc": "TRUNCATED PREVIEW"}
    f = NewsFetcher([{"article": [article]}])
    rows = asyncio.run(kuro.discover_news(f, GAMES["wuwa"], 0))
    assert rows[0]["ts"] == 1792281600
    assert f.calls[0][0].endswith("/en/MainMenu.json")
    assert asyncio.run(kuro.discover_news(NewsFetcher([{}]), GAMES["wuwa"], 0)) is None
    assert asyncio.run(kuro.discover_news(NewsFetcher([{"article": []}]), GAMES["wuwa"], 0)) == []
    assert asyncio.run(kuro.news_article(NewsFetcher([{}]), GAMES["wuwa"], rows[0])) is None
    detail = {"articleId": 5000, "articleContent": '<p>Full body</p><img src="https://example.com/a.gif"><p>After</p>'}
    item = asyncio.run(kuro.news_article(NewsFetcher([detail]), GAMES["wuwa"], rows[0]))
    assert [b["kind"] for b in item.article_blocks] == ["text", "image", "text"]
    assert item.author == "Kuro Games" and item.url.endswith("5000")
    assert "TRUNCATED PREVIEW" not in json.dumps(item.article_blocks)


def test_news_ordered_html_quill_gifs_videos_links_and_unknown_embeds():
    from gamexpress.textutil import news_blocks
    body = ('<p>Before <a href="https://example.com/source">link</a></p>'
            '<img src="https://example.com/a.gif"><p>Between</p>'
            '<video><source src="https://example.com/movie.mp4"></video>'
            '<iframe src="https://youtube.com/embed/abcdefghijk"></iframe><p>After</p>')
    blocks = news_blocks(body)
    assert [b["kind"] for b in blocks] == ["text", "image", "text", "video", "link", "text"]
    assert "https://example.com/source" in blocks[0]["text"]
    assert "After" in blocks[-1]["text"]
    quill = news_blocks("en-us", json.dumps([{"insert": "Before\n"},
                                            {"insert": {"image": "https://example.com/a.gif"}},
                                            {"insert": "After\n"}, {"insert": {"mystery": "unknown"}}]))
    assert [b["kind"] for b in quill] == ["text", "image", "text", "text"]
    assert "Unsupported" in quill[-1]["text"]
    assert news_blocks("<script>secret</script><p>visible</p>")[0]["text"] == "visible"


def test_news_overflow_one_message_one_container_lead_only_and_emoji_buttons():
    item = news_item(body="Complete source sentence. " * 500)
    item.article_blocks += [{"kind": "image", "url": "https://example.com/lead.gif"},
                            {"kind": "image", "url": "https://example.com/second.png"}]
    payloads = cards.news_payloads(GAMES["genshin"], item)
    assert len(payloads) == 1 and len(payloads[0]["components"]) == 1
    p = payloads[0]
    assert not cards.validate_payload(p)
    kids = p["components"][0]["components"]
    body = "\n".join(c.get("content", "") for c in kids)
    assert "Excerpt" in body and "remaining media" in body and "Official Author" in body
    assert f"<t:{item.published_ts}:f>" in body and cards.text_length(p["components"]) <= 4000
    assert [c["items"][0]["media"]["url"] for c in kids if c["type"] == 12] == ["https://example.com/lead.gif"]
    assert [b["label"] for b in kids[-1]["components"]] == ["Read Post", "Citlali News", "Donate"]
    assert all(b.get("emoji", {}).get("id") for b in kids[-1]["components"])


def test_news_media_count_and_order_not_aspect_ranked_or_truncated():
    item = news_item()
    urls = [f"https://example.com/{i}.gif" for i in range(22)]
    # Intentional repeat: article order/count must not be deduplicated by the renderer.
    urls.append(urls[0])
    item.article_blocks = [{"kind": "image", "url": url} for url in urls]
    payloads = cards.news_payloads(GAMES["genshin"], item)
    rendered = [media["media"]["url"] for p in payloads for co in p["components"] for c in co["components"]
                if c["type"] == 12 for media in c["items"]]
    assert rendered == urls
    assert all(not cards.validate_payload(p) for p in payloads)


def test_news_unsafe_media_defers_and_long_tokens_use_labelled_excerpt():
    item = news_item()
    item.article_blocks = [{"kind": "image", "url": "javascript:alert(1)"}]
    try:
        cards.news_payloads(GAMES["genshin"], item)
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe media accepted")
    item.article_blocks = [{"kind": "text", "text": "x" * 5000}]
    payload = cards.news_payloads(GAMES["genshin"], item)[0]
    assert len(payload["components"]) == 1 and not cards.validate_payload(payload)
    assert "Excerpt" in json.dumps(payload)


def test_news_failed_discovery_does_not_bootstrap_then_silent_baseline_and_new_only():
    from unittest.mock import patch

    from gamexpress.runner import run_news
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / "state.json")
        with patch.object(hoyolab, "discover_news", return_value=None):
            asyncio.run(run_news(ctx))
        assert "news" not in ctx.state.data
        with patch.object(hoyolab, "discover_news", return_value=[{"id": "old", "ts": ctx.now - 10}]):
            asyncio.run(run_news(ctx))
        assert ctx.state.news_record("genshin")["floor"] == ctx.now
        assert ctx.webhook.sent == []
        ctx.now += 20
        rows = [{"id": "old", "ts": ctx.now - 30}, {"id": "100", "ts": ctx.now - 10}]
        with patch.object(hoyolab, "discover_news", return_value=rows), patch.object(hoyolab, "news_article", return_value=news_item()):
            asyncio.run(run_news(ctx))
            asyncio.run(run_news(ctx))
        assert len(ctx.webhook.sent) == 1
        assert ctx.state.news_record("genshin")["records"]["100"]["status"] == "posted"
        assert State.load(ctx.state.path).news_record("genshin")["records"]["100"]["status"] == "posted"


def test_news_single_message_failed_send_retries_without_delivery_checkpoint():
    from unittest.mock import patch

    from gamexpress.runner import run_news
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / "state.json")
        with patch.object(hoyolab, "discover_news", return_value=[]):
            asyncio.run(run_news(ctx))
        ctx.now += 20
        row = {"id": "100", "ts": ctx.now - 10}
        item = news_item(body="A full article sentence. " * 300)
        ctx.webhook = ScriptedWebhook([], [SendResult(False, 503)])
        with patch.object(hoyolab, "discover_news", return_value=[row]), patch.object(hoyolab, "news_article", return_value=item):
            asyncio.run(run_news(ctx))
        assert "100" not in ctx.state.news_record("genshin")["records"]
        ctx.webhook = ScriptedWebhook([], [SendResult(True, 200, "only-message")])
        with patch.object(hoyolab, "discover_news", return_value=[row]), patch.object(hoyolab, "news_article", return_value=item):
            asyncio.run(run_news(ctx))
        rec = ctx.state.news_record("genshin")["records"]["100"]
        assert rec["status"] == "posted" and rec["messages"] == ["only-message"]
        assert len(ctx.webhook.sends) == 1


def test_news_full_fetch_failure_does_not_mark_or_block_later_candidate():
    from unittest.mock import patch

    from gamexpress.runner import run_news
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / "state.json")
        with patch.object(hoyolab, "discover_news", return_value=[]):
            asyncio.run(run_news(ctx))
        ctx.now += 20
        rows = [{"id": "bad", "ts": ctx.now - 15}, {"id": "100", "ts": ctx.now - 10}]
        with patch.object(hoyolab, "discover_news", return_value=rows), patch.object(hoyolab, "news_article", side_effect=[None, news_item()]):
            asyncio.run(run_news(ctx))
        records = ctx.state.news_record("genshin")["records"]
        assert "bad" not in records and records["100"]["status"] == "posted"
        assert len(ctx.webhook.sent) == 1 and any("bad" in line for line in ctx.warnings)


def test_news_schedule_announcements_are_excluded_from_news_without_moving_codes():
    from unittest.mock import patch

    from gamexpress.runner import run_news
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / "state.json")
        with patch.object(hoyolab, "discover_news", return_value=[]):
            asyncio.run(run_news(ctx))
        ctx.now += 20
        for i, title in enumerate(("Version 7.1 Special Program Preview", "Version 7.1 Update and Maintenance Notice")):
            row = {"id": str(i), "ts": ctx.now - 10}
            with patch.object(hoyolab, "discover_news", return_value=[row]), patch.object(hoyolab, "news_article", return_value=news_item(str(i), title=title)):
                asyncio.run(run_news(ctx))
        assert not ctx.webhook.sent
        assert all(rec["excluded"] == "schedule" for rec in ctx.state.news_record("genshin")["records"].values())
        assert ctx.state.data["schedule"] == {} and ctx.state.data["codes"] == {}


def test_news_dry_run_test_mode_and_changed_route_are_safe():
    from unittest.mock import patch

    from gamexpress.runner import run_news
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        ctx = news_ctx(path, DRY_RUN="1")
        with patch.object(hoyolab, "discover_news", return_value=[]):
            asyncio.run(run_news(ctx))
        ctx.now += 20
        with patch.object(hoyolab, "discover_news", return_value=[{"id": "100", "ts": ctx.now - 10}]), patch.object(hoyolab, "news_article", return_value=news_item()):
            asyncio.run(run_news(ctx))
        assert len(ctx.webhook.sent) == 1 and not path.exists()
        ctx.settings = replace(ctx.settings, test_mode=True)
        ctx.fetcher = None
        asyncio.run(run_news(ctx))
        assert any("test-mode" in line for line in ctx.warnings)
        ctx.settings = replace(ctx.settings, test_mode=False)
        ctx.state.news_record("genshin")["route"] = "different-route"
        asyncio.run(run_news(ctx))  # no request: no fetcher
        assert any("destination changed" in line for line in ctx.warnings)


def test_news_off_on_rebaseline_and_partials_remain_protected():
    from unittest.mock import patch

    from gamexpress.runner import run_news
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / "state.json")
        with patch.object(hoyolab, "discover_news", return_value=[]):
            asyncio.run(run_news(ctx))
        rec = ctx.state.news_record("genshin")
        rec["paused"] = True
        ctx.now += 1000
        with patch.object(hoyolab, "discover_news", return_value=[{"id": "100", "ts": ctx.now - 10}]):
            asyncio.run(run_news(ctx))
        assert rec["floor"] == ctx.now and not rec["paused"] and not ctx.webhook.sent


def test_news_cleanup_bounded_safe_floor_and_partial_protection():
    now = 1800000000
    state = State(Path("unused"))
    game = state.news_record("genshin")
    game.update(floor=1, records={str(i): {"status": "posted", "ts": now - 3 * 86400 + i} for i in range(500)})
    game["records"]["partial"] = {"status": "partial", "ts": now - 60 * 86400, "messages": ["one"]}
    state.prune_news(now)
    assert len(game["records"]) == 499 and "partial" in game["records"]
    assert game["floor"] >= now - 3 * 86400
    recent = state.news_record("starrail")
    recent.update(floor=1, records={str(i): {"status": "posted", "ts": now - 500 + i} for i in range(500)})
    state.prune_news(now)
    assert len(recent["records"]) == 500 and recent["floor"] == 1
    old = state.news_record("wuwa")
    old.update(floor=1, records={"old": {"status": "posted", "ts": now - 31 * 86400}})
    state.prune_news(now)
    assert not old["records"] and old["floor"] == now - 31 * 86400


def test_news_peer_merge_deduplicates_completed_and_partial_progress():
    peer = State(Path("unused-peer"))
    rec = peer.news_record("genshin")
    rec.update(floor=100, route="same", records={"p": {"status": "partial", "ts": 110, "signature": "sig", "messages": ["one"]},
                                               "done": {"status": "posted", "ts": 120, "messages": ["two"]}})
    own = State(Path("unused"))
    assert own.merge_peer(peer.data) == 0 and "news" not in own.data
    assert own.merge_peer(peer.data, include_news=True) == 2
    assert own.news_record("genshin")["records"]["p"]["messages"] == ["one"]
    rec["records"]["p"].update(status="posted", messages=["one", "three"])
    assert own.merge_peer(peer.data, include_news=True) == 1
    assert own.news_record("genshin")["records"]["p"]["status"] == "posted"
    assert own.merge_peer(peer.data, include_news=True) == 0


def test_news_preview_samples_are_explicit_and_valid_for_five_games():
    samples = gxmain.sample_payloads("news")
    assert len(samples) == 5
    for feature, name, payload in samples:
        assert feature == "news" and name.startswith("news_")
        assert "OFFLINE SAMPLE" in json.dumps(payload) and not cards.validate_payload(payload)
    assert all(feature != "news" for feature, _, _ in gxmain.sample_payloads("all"))



def test_news_hna_native_vod_highest_resolution_source_order_and_no_fake_stats():
    full = {"retcode": 0, "data": {"post": {
        "post": {"game_id": 9, "post_id": "900", "subject": "New Nexus Anima trailer", "created_at": 120,
                 "content": "en-us", "structured_content": json.dumps([
                     {"insert": "Before\n"}, {"insert": {"vod": "v1"}}, {"insert": "After\n"}])},
        "user": {"nickname": "Honkai: Nexus Anima"},
        "vod_list": [{"id": "v1", "resolutions": [
            {"height": 720, "bitrate": 1000, "url": "https://example.com/720.mp4"},
            {"height": 1080, "bitrate": 2000, "url": "https://example.com/1080.mp4"}]}]}}}
    item = asyncio.run(hoyolab.news_article(NewsFetcher([full]), GAMES["hna"], {"id": "900", "ts": 120}))
    assert [b["kind"] for b in item.article_blocks] == ["text", "video", "text"]
    assert item.article_blocks[1]["url"] == "https://example.com/1080.mp4"
    assert item.author == "Honkai: Nexus Anima" and item.published_ts == 120
    assert all(not cards.validate_payload(p) for p in cards.news_payloads(GAMES["hna"], item))
    full["data"]["post"]["post"].update(view_type=5, content='{"describe":"Video description"}', structured_content="")
    item = asyncio.run(hoyolab.news_article(NewsFetcher([full]), GAMES["hna"], {"id": "900", "ts": 120}))
    assert item.article_blocks[0]["text"] == "Video description"
    assert item.article_blocks[1]["url"].endswith("1080.mp4")


def test_news_run_once_isolated_fetching_and_disabled_state_stability():
    from unittest.mock import AsyncMock, patch

    from gamexpress import runner
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        on = settings(NEWS_ENABLED=1, ONLY="news", NEWS_GAMES="genshin", DISCORD_WEBHOOK_NEWS_GENSHIN=HOOK)
        on.state_path = path
        off = settings(NEWS_ENABLED=0, ENABLED_FEATURES="none")
        off.state_path = path
        fetcher = types.SimpleNamespace(summary=lambda: {})
        with patch.object(runner, "Fetcher", return_value=fetcher), patch.object(runner, "XClient"), \
                patch.object(runner, "gather_versions", new_callable=AsyncMock) as versions, \
                patch.object(runner, "gather_items", new_callable=AsyncMock) as items, \
                patch.object(runner, "write_summary"), \
                patch.object(hoyolab, "discover_news", return_value=[]) as discovery, \
                patch.object(runner.time, "time", return_value=1789300000):
            asyncio.run(runner.run_once(on, {"genshin": GAMES["genshin"]}, {}, session=object()))
            assert discovery.await_count == 1 and not versions.await_count and not items.await_count
            asyncio.run(runner.run_once(off, {"genshin": GAMES["genshin"]}, {}, session=object()))
            disabled = path.read_bytes()
            asyncio.run(runner.run_once(off, {"genshin": GAMES["genshin"]}, {}, session=object()))
            assert path.read_bytes() == disabled and discovery.await_count == 1
            assert State.load(path).news_record("genshin")["paused"]
            asyncio.run(runner.run_once(on, {"genshin": GAMES["genshin"]}, {}, session=object()))
            assert not State.load(path).news_record("genshin")["paused"] and discovery.await_count == 2


def test_news_capacity_defers_admission_and_keeps_active_dedup_protection():
    from unittest.mock import patch

    from gamexpress.runner import run_news
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / "state.json")
        with patch.object(hoyolab, "discover_news", return_value=[]):
            asyncio.run(run_news(ctx))
        ctx.now += 1000
        game = ctx.state.news_record("genshin")
        game["records"] = {str(i): {"status": "posted", "ts": ctx.now - 10} for i in range(500)}
        floor = game["floor"]
        with patch.object(hoyolab, "discover_news", return_value=[{"id": "new", "ts": ctx.now - 5}]), \
                patch.object(hoyolab, "news_article") as full:
            asyncio.run(run_news(ctx))
        assert len(game["records"]) == 500 and game["floor"] == floor and not full.called
        assert any("capacity" in line for line in ctx.warnings)


def test_news_legacy_partial_layout_change_is_held_without_replaying():
    from unittest.mock import patch

    from gamexpress.runner import run_news
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / "state.json")
        with patch.object(hoyolab, "discover_news", return_value=[]):
            asyncio.run(run_news(ctx))
        row = {"id": "100", "ts": ctx.now - 3 * 86400}
        rec = {"status": "partial", "ts": row["ts"], "row": row,
               "signature": "legacy-multipart-layout", "messages": ["one"]}
        ctx.state.news_record("genshin")["records"]["100"] = rec
        with patch.object(hoyolab, "discover_news", return_value=[]), patch.object(hoyolab, "news_article", return_value=news_item()):
            asyncio.run(run_news(ctx))
        assert not ctx.webhook.sent and rec["status"] == "partial" and rec["messages"] == ["one"]
        assert any("changed" in line for line in ctx.warnings)


def test_news_full_discovery_page_stops_after_publication_window_not_historical_archive():
    page = {"retcode": 0, "data": {"list": [{"post": {"post_id": str(i), "created_at": 120 - i}}
                                                for i in range(20)]}}
    f = NewsFetcher([page, page, page])
    rows = asyncio.run(hoyolab.discover_news(f, GAMES["genshin"], 110))
    assert len(rows) == 11 and len(f.calls) == 3
    assert all("last_id" not in kwargs["params"] for _, kwargs in f.calls)


def test_news_malformed_discovery_rows_never_establish_baseline():
    for data in ([1], {"retcode": 0, "data": [1]}, {"retcode": 0, "data": {"list": ["bad"]}},
                 {"retcode": 0, "data": {"list": [{"post": {"post_id": "100"}}]}}):
        assert asyncio.run(hoyolab.discover_news(NewsFetcher([data]), GAMES["genshin"], 0)) is None



def test_news_attempt_rotation_does_not_starve_valid_articles_behind_retry_holes():
    from unittest.mock import patch

    from gamexpress.runner import run_news
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / "state.json")
        with patch.object(hoyolab, "discover_news", return_value=[]):
            asyncio.run(run_news(ctx))
        ctx.now += 20
        rows = [{"id": f"bad{i}", "ts": ctx.now - 10} for i in range(10)]
        rows.append({"id": "zzz", "ts": ctx.now - 10})

        async def article(_fetcher, _game, row):
            return news_item("zzz", ts=row["ts"]) if row["id"] == "zzz" else None

        with patch.object(hoyolab, "discover_news", return_value=rows), patch.object(hoyolab, "news_article", side_effect=article):
            asyncio.run(run_news(ctx))
            assert not ctx.webhook.sent
            asyncio.run(run_news(ctx))
        assert len(ctx.webhook.sent) == 1
        game = ctx.state.news_record("genshin")
        assert game["records"]["zzz"]["status"] == "posted"
        assert game["floor"] == ctx.now - 20



def test_news_selecting_two_games_does_not_fetch_or_bootstrap_the_other_three():
    from unittest.mock import patch

    from gamexpress.runner import run_news
    from gamexpress.sources import kuro
    with tempfile.TemporaryDirectory() as directory:
        ctx = make_ctx(Path(directory) / "state.json")
        ctx.settings = settings(NEWS_ENABLED=1, NEWS_GAMES="genshin,wuwa",
                                DISCORD_WEBHOOK_NEWS_GENSHIN=HOOK, DISCORD_WEBHOOK_NEWS_WUWA=HOOK)
        with patch.object(hoyolab, "discover_news", return_value=[]) as hoyo, \
                patch.object(kuro, "discover_news", return_value=[]) as ww:
            asyncio.run(run_news(ctx))
        assert hoyo.await_count == 1 and hoyo.await_args.args[1].key == "genshin"
        assert ww.await_count == 1 and ww.await_args.args[1].key == "wuwa"
        assert set(ctx.state.data["news"]) == {"genshin", "wuwa"}
        assert not ctx.webhook.sent


def test_news_general_payment_maintenance_and_version_events_are_not_schedule_announcements():
    from unittest.mock import patch

    from gamexpress.runner import run_news
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / "state.json")
        with patch.object(hoyolab, "discover_news", return_value=[]):
            asyncio.run(run_news(ctx))
        ctx.now += 20
        titles = ["PlayStation®Store Payment System Maintenance Notice for the United Arab Emirates and Saudi Arabia",
                  "Version 7.1 Share HoYoverse Top-Up Center Event: Win Primogems, Phase 2",
                  "V7.1 Known Issues & Update"]
        for index, title in enumerate(titles):
            row = {"id": str(index), "ts": ctx.now - 10}
            item = news_item(str(index), title=title)
            with patch.object(hoyolab, "discover_news", return_value=[row]), patch.object(hoyolab, "news_article", return_value=item):
                asyncio.run(run_news(ctx))
        assert len(ctx.webhook.sent) == len(titles)
        assert all(not rec.get("excluded") for rec in ctx.state.news_record("genshin")["records"].values())


def test_news_user_supplied_5644_media_urls_preserve_three_jpgs_seven_gifs_and_order():
    # Supplied URL fixture only: article 5644 itself was not fetched in this sandbox.
    names = ["pschl2zbs6d2meg84m-1792137929039.jpg", "f3ci7wtfxus6zl1lbe-1792137955242.jpg",
             "0xyrumpv62m78ml2mn-1792138024135.gif", "okv7ggh4djaxdke86w-1792138130984.gif",
             "qz1a3fkcac9as1oceb-1792138150407.gif", "tk9vkmghyeici95l42-1792138277420.gif",
             "ch5jwzs4ssa0fojxff-1792138444444.gif", "g30vhq05jjs5plx75l-1792138467380.gif",
             "5regub0in3y7l2abc5-1792138486258.gif", "0pci0q1quwuo5rsifn-1792138636832.jpg"]
    urls = ["https://hw-media-cdn-mingchao.kurogame.com/object/1792080000000/" + name for name in names]
    item = Item("kuro", "wuwa", "5644", "https://wutheringwaves.kurogames.com/en/main/news/detail/5644",
                "Provided media URL layout — not an article-fetch fixture", "", 0,
                article_blocks=[{"kind": "image", "url": url} for url in urls], author="Kuro Games")
    payloads = cards.news_payloads(GAMES["wuwa"], item)
    rendered = [entry["media"]["url"] for p in payloads for co in p["components"] for c in co["components"]
                if c["type"] == 12 for entry in c["items"]]
    assert rendered == urls
    assert sum(url.endswith(".jpg") for url in rendered) == 3
    assert sum(url.endswith(".gif") for url in rendered) == 7
    assert all(not cards.validate_payload(p) for p in payloads)


def test_news_short_articles_across_all_games_keep_order_and_one_button_row():
    for game in GAMES.values():
        if game.key == "ananta":
            continue
        item = news_item(body="Dear players.\n• First\n• Second\n※ One note\n※ Another note")
        item.published_ts = 0
        item.images = ["https://example.com/cover.png"]
        item.article_blocks += [{"kind": "image", "url": f"https://example.com/{i}.gif"} for i in range(10)]
        item.article_blocks += [{"kind": "text", "text": "Final source paragraph."}]
        ps = cards.news_payloads(game, item)
        assert len(ps) == 1 and len(ps[0]["components"]) > 1
        cs = ps[0]["components"]
        assert not cards.validate_payload(ps[0])
        assert sum(c["type"] == 1 for co in cs for c in co["components"]) == 1
        assert cs[-1]["components"][-1]["type"] == 1
        rendered = "\n".join(c.get("content", "") for co in cs for c in co["components"])
        assert "- First" in rendered and "\n\n※ Another" in rendered
        assert "<t:" not in rendered and "Excerpt" not in rendered and "Final source paragraph." in rendered
        assert "Media unavailable" not in rendered
        assert cs[0]["components"][1]["items"][0]["media"]["url"] == item.images[0]


def test_news_media_preflight_status_signature_redirect_and_query_preservation():
    from gamexpress.http import Fetcher

    class Response:
        def __init__(self, status=200, raw=b"\x89PNG\r\n\x1a\n" + b"a" * 80, headers=None):
            self.status = status
            self.raw = raw
            self.headers = headers or {"Content-Type": "image/png"}
            self.content = self
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def iter_chunked(self, size):
            for i in range(0, len(self.raw), size):
                yield self.raw[i:i + size]

    class Session:
        def __init__(self, responses):
            self.responses, self.calls = responses, []
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)

    async def run():
        url = "https://upload-os-bbs.hoyolab.com/asset.jpeg?x-oss-process=quality%2Cq_90"
        session = Session([Response()])
        assert await Fetcher(session).verify_media(url)
        assert session.calls[0][0] == url and not session.calls[0][1]["allow_redirects"]
        for response in [Response(403), Response(raw=b"<Error><Code>AccessDenied</Code></Error>"),
                         Response(headers={"Content-Type": "text/html"}), Response(raw=b"")]:
            assert not await Fetcher(Session([response])).verify_media(url)
        session = Session([Response(302, headers={"Location": "http://127.0.0.1/private"})])
        assert not await Fetcher(session).verify_media(url) and len(session.calls) == 1
        session = Session([])
        assert not await Fetcher(session).verify_media("https://untrusted.example/image.png")
        assert not session.calls
    asyncio.run(run())


def test_news_media_failure_refreshes_official_source_once_then_holds_without_posting():
    from unittest.mock import AsyncMock, patch

    from gamexpress.runner import run_news
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / "state.json")
        with patch.object(hoyolab, "discover_news", return_value=[]):
            asyncio.run(run_news(ctx))
        ctx.now += 20
        row = {"id": "100", "ts": ctx.now - 10}
        item = news_item()
        item.article_blocks.append({"kind": "image", "url": "https://upload-os-bbs.hoyolab.com/expired.png"})
        fresh = __import__("copy").deepcopy(item)
        fresh.article_blocks[-1]["url"] = "https://upload-os-bbs.hoyolab.com/current.png?source=official"
        ctx.fetcher = types.SimpleNamespace(verify_media=AsyncMock(side_effect=[False, True]))
        with patch.object(hoyolab, "discover_news", return_value=[row]), patch.object(hoyolab, "news_article", side_effect=[item, fresh]) as source:
            asyncio.run(run_news(ctx))
            assert source.await_count == 2
        assert len(ctx.webhook.sent) == 1 and "current.png?source=official" in json.dumps(ctx.webhook.sent)
        ctx.fetcher = types.SimpleNamespace(verify_media=AsyncMock(return_value=False))
        row["id"] = "bad"
        item.id = "bad"
        before = len(ctx.webhook.sent)
        with patch.object(hoyolab, "discover_news", return_value=[row]), patch.object(hoyolab, "news_article", return_value=item) as source:
            asyncio.run(run_news(ctx))
            assert source.await_count == 2
        assert len(ctx.webhook.sent) == before and "bad" not in ctx.state.news_record("genshin")["records"]
        assert any("held for retry" in w for w in ctx.warnings)


def test_news_text_only_short_and_overflow_component_budget_are_explicit():
    item = news_item(body="Notice for all players.\n" * 80)
    ps = cards.news_payloads(GAMES["wuwa"], item)
    assert len(ps) == 1 and len(ps[0]["components"]) == 1
    assert "Excerpt" not in json.dumps(ps)
    item.article_blocks = [{"kind": "image", "url": f"https://example.com/{i}.png"} for i in range(100)]
    ps = cards.news_payloads(GAMES["wuwa"], item)
    assert len(ps) == 1 and len(ps[0]["components"]) == 1
    assert not cards.validate_payload(ps[0]) and "Excerpt" in json.dumps(ps)
    assert sum(c["type"] == 12 for c in ps[0]["components"][0]["components"]) == 1


def test_news_source_heading_and_list_formatting_without_media_reordering():
    from gamexpress.textutil import news_blocks

    blocks = news_blocks("<h3>Heading</h3><p><strong>Bold</strong> words</p><ul><li>First</li><li>Second</li></ul>")
    value = "".join(b.get("text", "") for b in blocks)
    assert "**Heading**" in value and "**Bold**" in value and "- First" in value
    blocks = news_blocks("en-us", json.dumps([{"insert": "Title"}, {"insert": "\n", "attributes": {"header": 3}},
                                             {"insert": "Item"}, {"insert": "\n", "attributes": {"list": "bullet"}}]))
    assert "".join(b.get("text", "") for b in blocks) == "**Title**\n- Item\n"


def test_news_fitting_excerpt_word_count_halved_only_for_overflow():
    import re

    item = news_item(body="word " * 5000)
    p = cards.news_payloads(GAMES["wuwa"], item)[0]
    kids = p["components"][0]["components"]
    body = kids[3]["content"] if kids[3]["type"] == 10 else kids[4]["content"]
    # Reconstruct the preceding fitting-budget step independently.
    notice_index = next(i for i, c in enumerate(kids) if "**Excerpt" in c.get("content", ""))
    remaining = 4000 - cards.text_length(kids[:notice_index + 2]) - 4
    full = item.article_blocks[0]["text"].strip()
    cut = max(full.rfind("\n", 0, remaining), full.rfind(" ", 0, remaining))
    fitting = full[:cut if cut > remaining // 2 else remaining].rstrip()
    assert len(re.findall(r"\S+", body)) == len(re.findall(r"\S+", fitting)) // 2
    assert len(p["components"]) == 1 and not cards.validate_payload(p)
    item.article_blocks = [{"kind": "text", "text": "●No space\n● With space\n**●Bold item**\n• Old bullet"}]
    p = cards.news_payloads(GAMES["wuwa"], item)[0]
    value = p["components"][0]["components"][1]["content"]
    assert value == "- No space\n- With space\n- **Bold item**\n- Old bullet"
    assert "Excerpt" not in str(p)
    item.article_blocks = [{"kind": "text", "text": "**bold words " * 700}]
    p = cards.news_payloads(GAMES["wuwa"], item)[0]
    value = next(c["content"] for c in p["components"][0]["components"]
                 if c["type"] == 10 and c["content"].startswith("**bold"))
    assert value.count("**") % 2 == 0


def test_news_masked_links_and_heading_boundary_do_not_capture_next_section():
    from gamexpress.textutil import news_blocks, news_link

    url = "https://example.com/path(a)?first=1&second=2"
    assert news_link(">> GO <<", url) == "[>> GO <<](https://example.com/path%28a%29?first=1&second=2)"
    assert news_link("Bad", "javascript:alert(1)") == "Bad"
    assert news_link("https://example.com/path", "https://example.com/path") == "https://example.com/path"
    html_blocks = news_blocks('<p><strong><a href="https://twitch.tv/genshinimpact">Twitch</a></strong>'
                             ' <a href="https://youtube.com/@GenshinImpact">YouTube</a></p>')
    value = "".join(b.get("text", "") for b in html_blocks)
    assert "**[Twitch](https://twitch.tv/genshinimpact)**" in value
    assert "[YouTube](https://youtube.com/@GenshinImpact)" in value
    raw = fx("hoyolab_starrail_46814308_full.json")
    post = raw["data"]["post"]["post"]
    item = asyncio.run(hoyolab.news_article(NewsFetcher([raw]), GAMES["starrail"],
                                          {"id": post["post_id"], "ts": post["created_at"]}))
    p = cards.news_payloads(GAMES["starrail"], item)[0]
    rendered = "\n".join(c.get("content", "") for co in p["components"] for c in co["components"])
    target = "https://act.hoyoverse.com/puzzle/hkrpg/e20260913version_fasf13434_77d1c0e99b/index.html?game_biz=hkrpg_global"
    assert f"[>> GO <<]({target})\n\n**▌Compensation Details**\n" in rendered
    assert "]([https://" not in rendered and "&gt;" not in rendered
    assert all(b["url"].startswith("https://") for b in p["components"][0]["components"][-1]["components"])


def test_news_publisher_fallback_is_game_name_but_real_nickname_is_preserved():
    import copy

    raw = fx("hoyolab_zzz_46604530_full.json")
    post = raw["data"]["post"]["post"]
    row = {"id": post["post_id"], "ts": post["created_at"]}
    without = copy.deepcopy(raw)
    without["data"]["post"].pop("user", None)
    item = asyncio.run(hoyolab.news_article(NewsFetcher([without]), GAMES["zzz"], row))
    assert item.author == GAMES["zzz"].name
    without["data"]["post"]["user"] = {"nickname": "Official publisher nickname"}
    item = asyncio.run(hoyolab.news_article(NewsFetcher([without]), GAMES["zzz"], row))
    assert item.author == "Official publisher nickname"
    item.author = "HoYoLAB official news"
    p = cards.news_payloads(GAMES["zzz"], item)[0]
    assert "HoYoLAB official news" not in str(p) and GAMES["zzz"].name in str(p)


def test_news_marker_spacing_is_idempotent_and_only_note_runs_gain_blank_lines():
    item = news_item(body="Paragraph\n※One note\nNormal text\n✦Maintenance Time:\n**✦Already bold**\n"
                    "※ First in run\n※Second in run\n- One\n\n- Two\n\n- Three")
    p = cards.news_payloads(GAMES["wuwa"], item)[0]
    body = p["components"][0]["components"][1]["content"]
    assert "Paragraph\n※ One note\nNormal text" in body
    assert "✦ Maintenance Time:" in body and "**✦ Already bold**" in body
    assert "※ First in run\n\n※ Second in run" in body
    assert "- One\n- Two\n- Three" in body
    item.article_blocks[0]["text"] = body
    again = cards.news_payloads(GAMES["wuwa"], item)[0]
    assert again["components"][0]["components"][1]["content"] == body


def test_news_uninterrupted_nine_media_run_fits_one_container_without_regrouping():
    item = news_item()
    urls = [f"https://example.com/{i}.gif" for i in range(9)]
    item.article_blocks += [{"kind": "image", "url": u} for u in urls]
    p = cards.news_payloads(GAMES["wuwa"], item)[0]
    assert len(p["components"]) == 1 and not cards.validate_payload(p)
    galleries = [c for c in p["components"][0]["components"] if c["type"] == 12]
    assert len(galleries) == 9 and all(len(c["items"]) == 1 for c in galleries)
    assert [c["items"][0]["media"]["url"] for c in galleries] == urls


def test_news_html_adjacent_nested_bold_and_media_boundaries_have_no_stray_markers():
    from gamexpress.textutil import news_blocks

    value = "\n".join(b.get("text", "") for b in news_blocks(
        '<div><strong>✦Maintenance Time: </strong><span><strong>2026-09-30 (UTC+8)</strong></span></div>'
        '<p><strong>A <strong>B</strong> C</strong></p>'
        '<p><strong>Before<img src="https://example.com/a.png">After</strong></p>'))
    assert "**✦Maintenance Time: 2026-09-30 (UTC+8)**" in value
    assert "**A B C**" in value and "****" not in value
    assert "**Before**" in value and "**After**" in value
    assert value.count("**") % 2 == 0


def test_news_decorative_star_gets_space_on_both_sides_idempotently():
    item = news_item(body="**✦Any Text Example✦**\n**✦ Already Spaced ✦**\n※Obtainable")
    p = cards.news_payloads(GAMES["wuwa"], item)[0]
    body = p["components"][0]["components"][1]["content"]
    assert body == "**✦ Any Text Example ✦**\n**✦ Already Spaced ✦**\n※ Obtainable"
    item.article_blocks[0]["text"] = body
    assert cards.news_payloads(GAMES["wuwa"], item)[0] == p



def test_news_enumerated_entries_compact_without_rewriting_or_crossing_paragraphs():
    value = '1. First\n\n2. Second\n\n3. Third\n\nSection\n\n① Enter\n\n② Redeem\n\nNotes\n\na. One\n\nb. Two'
    item = news_item(body=value)
    p = cards.news_payloads(GAMES["wuwa"], item)[0]
    body = p["components"][0]["components"][1]["content"]
    assert body == '1. First\n2. Second\n3. Third\n\nSection\n\n① Enter\n② Redeem\n\nNotes\n\na. One\nb. Two'
    item.article_blocks[0]["text"] = body
    assert cards.news_payloads(GAMES["wuwa"], item)[0] == p



def test_news_moderate_overflow_two_pages_all_games_and_long_excerpt():
    body = ('One source paragraph with an unchanged https://hoyo.link/example URL.\n\n' * 70).rstrip()
    for key in ("genshin", "starrail", "hna", "zzz", "wuwa"):
        item = news_item(body=body)
        pages = cards.news_payloads(GAMES[key], item)
        assert len(pages) == 2 and all(not cards.validate_payload(p) for p in pages)
        first, last = [p["components"][0]["components"] for p in pages]
        assert first[0]["content"].startswith("## ")
        assert last[0]["content"] == "-# Page 2"
        assert not any(c["type"] == 1 for c in first)
        assert last[-1]["type"] == 1 and len(last[-1]["components"]) == 3
        texts = ''.join(c["content"] for cs in (first, last) for c in cs
                        if c["type"] == 10 and not c["content"].startswith(("## ", "-# Page")))
        assert texts == body
        assert all(p["allowed_mentions"] == {"parse": []} for p in pages)
        item.article_blocks[0]["text"] = body * 3
        long = cards.news_payloads(GAMES[key], item)
        assert len(long) == 1 and len(long[0]["components"]) == 1
        assert 'Excerpt' in str(long) and not cards.validate_payload(long[0])


def test_news_moderate_ceiling_and_unsplittable_fallback():
    for wanted in (7000, 7001):
        item = news_item(body="probe")
        header = cards.news_payloads(GAMES["wuwa"], item)[0]["components"][0]["components"][0]
        n = wanted - len(header["content"])
        item.article_blocks[0]["text"] = 'a' * 3400 + '\n\n' + 'b' * (n - 3402)
        pages = cards.news_payloads(GAMES["wuwa"], item)
        assert len(pages) == (2 if wanted == 7000 else 1)
        assert all(not cards.validate_payload(p) for p in pages)
    item = news_item(body='a' * 4500)  # no safe paragraph boundary
    pages = cards.news_payloads(GAMES["wuwa"], item)
    assert len(pages) == 1 and 'Excerpt' in str(pages)



def test_news_nested_list_paragraph_keeps_marker_attached_and_square_entries_compact():
    from gamexpress.textutil import news_blocks
    source = '<h1>Eligibility</h1><ul>\n<li>\n<p>Reach Union Level 5.</p>\n</li>\n<li><p><strong>Or complete</strong> the trial.</p></li></ul><p>■ First</p><p><br></p><p>■ Second</p>'
    blocks = news_blocks(source)
    raw = '\n'.join(b.get('text', '') for b in blocks)
    assert '- Reach Union Level 5.' in raw and '- **Or complete** the trial.' in raw
    assert not __import__('re').search(r'(?m)^-\s*$', raw)
    item = news_item(body='unused')
    item.article_blocks = blocks
    p = cards.news_payloads(GAMES['wuwa'], item)[0]
    body = p['components'][0]['components'][1]['content']
    assert '■ First\n■ Second' in body
    assert '- Reach Union Level 5.\n- **Or complete** the trial.' in body
    assert '<li>' in source  # original input string was never edited


def test_news_single_message_multicontainer_buttons_only_final_container_all_games():
    for key in ('genshin', 'starrail', 'hna', 'zzz', 'wuwa'):
        item = news_item(body='intro')
        item.article_blocks += [{'kind': 'image', 'url': f'https://example.com/{i}.jpg'} for i in range(7)]
        item.article_blocks.append({'kind': 'text', 'text': 'last section'})
        pages = cards.news_payloads(GAMES[key], item)
        assert len(pages) == 1 and len(pages[0]['components']) == 2
        first, last = pages[0]['components']
        assert not any(c['type'] == 1 for c in first['components'])
        assert last['components'][-1]['type'] == 1
        assert len(last['components'][-1]['components']) == 3
        assert not cards.validate_payload(pages[0])




def test_news_group_setting_default_and_explicit_values():
    assert settings().news_group_long_strips is False
    for value in ('1', 'true', 'yes', 'on'):
        assert settings(NEWS_GROUP_LONG_STRIPS=value).news_group_long_strips is True
    for value in ('0', 'false', 'off', ''):
        assert settings(NEWS_GROUP_LONG_STRIPS=value).news_group_long_strips is False
    s = settings(NEWS_GROUP_LONG_STRIPS=1)
    assert not s.news_enabled and 'news' not in s.features  # grouping never opts in


def test_news_group_header_dimensions_supported_and_unknown_formats():
    import struct

    from gamexpress.media import image_dimensions
    png = b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', 100, 900)
    gif = b'GIF89a' + struct.pack('<HH', 100, 900)
    jpeg = b'\xff\xd8\xff\xe0\x00\x04ab\xff\xc2\x00\x0b\x08' + struct.pack('>HH', 900, 100) + b'\x01\x01\x11\x00'
    webpx = b'RIFF' + b'\0' * 4 + b'WEBPVP8X' + b'\0' * 8 + (99).to_bytes(3, 'little') + (899).to_bytes(3, 'little')
    webp = b'RIFF' + b'\0' * 4 + b'WEBPVP8 ' + b'\0' * 7 + b'\x9d\x01\x2a' + struct.pack('<HH', 100, 900)
    bits = 99 | (899 << 14)
    webpl = b'RIFF' + b'\0' * 4 + b'WEBPVP8L' + b'\0' * 4 + b'\x2f' + bits.to_bytes(4, 'little') + b'\0' * 5
    for raw in (png, gif, jpeg, webpx, webp, webpl):
        assert image_dimensions(raw) == (100, 900)
        assert image_dimensions(raw[:8]) is None
    for raw in (b'', b'<html>error</html>', b'\0\0\0\x18ftypavif',
                b'GIF89a\0\0\0\0', b'\xff\xd8\xff\xe0\0\0', b'\xff\xd8\xff\xff'):
        assert image_dimensions(raw) is None
    # Corrupt/random data cannot throw or generate URL-based guesses.
    for n in range(256):
        assert image_dimensions(bytes([n]) * n) is None


def _group_fixture(count=4):
    item = news_item(body='Original wording. No invented note marker.')
    item.images = ['https://upload-os-bbs.hoyolab.com/lead.png?original=1']
    item.article_blocks += [{'kind': 'image', 'url': f'https://upload-os-bbs.hoyolab.com/{i}.gif'}
                            for i in range(count)]
    return item


def _group_urls(payloads):
    return [it['media']['url'] for p in payloads for co in p['components']
            for c in co['components'] if c['type'] == 12 for it in c['items']]


def test_news_group_all_games_switch_off_is_identical_no_measurement():
    from gamexpress.media import group_news_media
    for key in ('genshin', 'starrail', 'hna', 'zzz', 'wuwa'):
        original = cards.news_payloads(GAMES[key], _group_fixture())
        result = asyncio.run(group_news_media(None, original, enabled=False))
        assert result is original and all(not cards.validate_payload(p) for p in result)
        assert all(len(c['items']) == 1 for p in result for co in p['components']
                   for c in co['components'] if c['type'] == 12)


def test_news_group_all_games_preserves_lead_text_metadata_controls_and_inputs():
    from copy import deepcopy

    from gamexpress.media import group_measured_news
    for key in ('genshin', 'starrail', 'hna', 'zzz', 'wuwa'):
        original = cards.news_payloads(GAMES[key], _group_fixture())
        snapshot = deepcopy(original)
        urls = _group_urls(original)
        dims = {u: (100, 900) for u in urls}
        dims[urls[0]] = (1920, 1080)  # normal lead remains separate
        result = group_measured_news(original, dims)
        assert result != original and original == snapshot and _group_urls(result) == urls
        kids = result[0]['components'][0]['components']
        galleries = [c for c in kids if c['type'] == 12]
        assert [len(c['items']) for c in galleries] == [1, 4]
        grouped = next(i for i, c in enumerate(kids) if c['type'] == 12 and len(c['items']) == 4)
        assert kids[grouped - 1]['type'] == 14
        assert [c for c in kids if c['type'] in (1, 10)] == [
            c for c in original[0]['components'][0]['components'] if c['type'] in (1, 10)]
        assert '※' not in json.dumps(result, ensure_ascii=False)
        assert not cards.validate_payload(result[0])


def test_news_group_long_lead_can_join_run_normal_final_separate():
    from gamexpress.media import group_measured_news
    item = _group_fixture()
    item.article_blocks = item.article_blocks[1:]  # truly consecutive cover/body galleries
    original = cards.news_payloads(GAMES['wuwa'], item)
    urls = _group_urls(original)
    dims = {u: (100, 900) for u in urls}
    dims[urls[-1]] = (100, 100)
    result = group_measured_news(original, dims)
    galleries = [c for c in result[0]['components'][0]['components'] if c['type'] == 12]
    assert [len(c['items']) for c in galleries] == [4, 1]
    assert galleries[0]['items'][0]['media']['url'] == urls[0]
    assert _group_urls(result) == urls


def test_news_group_requires_all_dimensions_and_strict_height_ratio():
    from gamexpress.media import group_measured_news
    original = cards.news_payloads(GAMES['wuwa'], _group_fixture(2))
    urls = _group_urls(original)
    for bad in (None, (0, 100), (100, -1), (float('nan'), 100), (100, float('inf')), '100x900'):
        dims = {u: (100, 900) for u in urls}
        dims[urls[0]] = bad
        assert group_measured_news(original, dims) is original
    for size in ((100, 350), (900, 100)):
        assert group_measured_news(original, dict.fromkeys(urls, size)) is original
    assert group_measured_news(original, dict.fromkeys(urls, (100, 351))) != original


def test_news_group_never_crosses_text_separator_container_page_or_existing_gallery():
    from gamexpress.media import group_measured_news
    url = lambda n: f'https://upload-os-bbs.hoyolab.com/{n}.png'
    g = lambda n: cards.gallery([url(n)])
    barriers = [cards.text('Section'), cards.sep(), cards.gallery([url(8), url(9)])]
    for barrier in barriers:
        original = [{'flags': cards.IS_COMPONENTS_V2, 'allowed_mentions': {'parse': []},
                     'components': [cards.container([g(1), barrier, g(2)], GAMES['wuwa'].color)]}]
        dims = dict.fromkeys(_group_urls(original), (100, 900))
        assert group_measured_news(original, dims) is original
    original = [{'flags': cards.IS_COMPONENTS_V2, 'components': [cards.container([g(n)], 0)]} for n in (1, 2)]
    assert group_measured_news(original, dict.fromkeys(_group_urls(original), (100, 900))) is original
    original = [{'flags': cards.IS_COMPONENTS_V2, 'components': [cards.container([g(n)], 0) for n in (1, 2)]}]
    assert group_measured_news(original, dict.fromkeys(_group_urls(original), (100, 900))) is original


def test_news_group_ten_item_limit_retains_item_fields_order_duplicates_and_bytes():
    from copy import deepcopy

    from gamexpress.media import group_measured_news
    item = _group_fixture(23)
    item.images = []
    item.article_blocks = item.article_blocks[1:]
    original = cards.news_payloads(GAMES['wuwa'], item)
    for p in original:
        for co in p['components']:
            for c in co['components']:
                if c['type'] == 12:
                    c['items'][0]['description'] = 'Original description'
                    c['items'][0]['spoiler'] = True
    snapshot = deepcopy(original)
    result = group_measured_news(original, dict.fromkeys(_group_urls(original), (100, 900)))
    galleries = [c for p in result for co in p['components'] for c in co['components'] if c['type'] == 12]
    assert [len(c['items']) for c in galleries] == [10, 10, 3]
    assert [i for c in galleries for i in c['items']] == [i for p in original for co in p['components']
                                                       for c in co['components'] if c['type'] == 12 for i in c['items']]
    assert original == snapshot and all(not cards.validate_payload(p) for p in result)
    # Repeated source URLs still remain repeated items; only measurement I/O deduplicates.
    galleries[0]['items'][1]['media']['url'] = galleries[0]['items'][0]['media']['url']
    assert len(_group_urls(result)) == 23
    assert group_measured_news(result, dict.fromkeys(_group_urls(result), (100, 900))) is result


def test_news_group_budget_rejection_rolls_back_without_mutation():
    from unittest.mock import patch

    from gamexpress.media import group_measured_news
    original = cards.news_payloads(GAMES['wuwa'], _group_fixture())
    dims = dict.fromkeys(_group_urls(original), (100, 900))
    with patch.object(cards, 'validate_payload', return_value=['budget exceeded']):
        assert group_measured_news(original, dims) is original
    # Exact original limits remain independently valid after grouping.
    for count in (4, 30):
        original = cards.news_payloads(GAMES['wuwa'], _group_fixture(count))
        result = group_measured_news(original, dict.fromkeys(_group_urls(original), (100, 900)))
        assert all(not cards.validate_payload(p) for p in result)
        assert _group_urls(result) == _group_urls(original)


def test_news_group_excerpt_unchanged_no_measurement_and_two_pages_keep_controls():
    from unittest.mock import AsyncMock

    from gamexpress.media import group_measured_news, group_news_media
    item = _group_fixture()
    item.article_blocks[0]['text'] = 'word ' * 5000
    original = cards.news_payloads(GAMES['wuwa'], item)
    f = types.SimpleNamespace(media_dimensions=AsyncMock(return_value=(100, 900)))
    assert 'Excerpt' in str(original)
    assert asyncio.run(group_news_media(f, original, enabled=True)) is original
    f.media_dimensions.assert_not_awaited()
    item.article_blocks[0]['text'] = ('Paragraph ' * 80 + '\n\n') * 6
    original = cards.news_payloads(GAMES['wuwa'], item)
    assert len(original) == 2
    result = group_measured_news(original, dict.fromkeys(_group_urls(original), (100, 900)))
    assert len(result) == 2 and _group_urls(result) == _group_urls(original)
    assert all(not cards.validate_payload(p) for p in result)
    rows = [(pi, ci) for pi, p in enumerate(result) for ci, co in enumerate(p['components'])
            for c in co['components'] if c['type'] == 1]
    assert rows == [(1, len(result[-1]['components']) - 1)]


def test_news_group_adapter_failures_timeout_and_deduplicated_measurements():
    from unittest.mock import AsyncMock, patch

    from gamexpress.media import group_news_media
    original = cards.news_payloads(GAMES['wuwa'], _group_fixture())
    for error in (None, OSError('network'), ValueError('bad header'), asyncio.TimeoutError()):
        f = types.SimpleNamespace(media_dimensions=AsyncMock(return_value=error, side_effect=error if isinstance(error, Exception) else None))
        assert asyncio.run(group_news_media(f, original, enabled=True)) is original
    item = _group_fixture()
    item.article_blocks.append(item.article_blocks[-1].copy())
    original = cards.news_payloads(GAMES['wuwa'], item)
    f = types.SimpleNamespace(media_dimensions=AsyncMock(return_value=(100, 900)))
    result = asyncio.run(group_news_media(f, original, enabled=True))
    assert f.media_dimensions.await_count == len(set(_group_urls(original)))
    assert _group_urls(result) == _group_urls(original)

    async def timeout(awaitable, *, timeout):
        assert timeout == 20
        awaitable.close()
        raise asyncio.TimeoutError
    with patch('asyncio.wait_for', side_effect=timeout):
        assert asyncio.run(group_news_media(f, original, enabled=True)) is original


def test_news_group_http_dimension_reads_bounded_allowlisted_redirect_safe():
    import struct

    from gamexpress.http import Fetcher
    class Response:
        def __init__(self, raw=b'', status=200, mime='image/png', location=None):
            self.raw, self.status, self.content, self.read = raw, status, self, 0
            self.headers = {'Content-Type': mime}
            if location is not None:
                self.headers['Location'] = location
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def iter_chunked(self, size):
            for i in range(0, len(self.raw), size):
                self.read += len(self.raw[i:i + size])
                yield self.raw[i:i + size]
    class Session:
        def __init__(self, *responses):
            self.responses, self.calls = list(responses), []
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)
    url = 'https://hw-media-cdn-mingchao.kurogame.com/a.png?original=1'
    raw = b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', 100, 900)
    async def check():
        response = Response(raw + b'\0' * 1_000_000)
        session = Session(response)
        assert await Fetcher(session).media_dimensions(url) == (100, 900)
        assert response.read == 2048 and session.calls[0][0] == url
        assert session.calls[0][1]['allow_redirects'] is False
        assert session.calls[0][1]['timeout'].total == 8
        response = Response(b'x' * 1_000_000)
        assert await Fetcher(Session(response)).media_dimensions(url) is None
        assert response.read == 256 * 1024
        for bad in ('https://evil.example/a.png', 'http://127.0.0.1/a',
                    'https://kurogame.com:444/a', 'https://u:p@kurogame.com/a'):
            s = Session()
            assert await Fetcher(s).media_dimensions(bad) is None and not s.calls
            s = Session(Response(status=302, location=bad))
            assert await Fetcher(s).media_dimensions(url) is None and len(s.calls) == 1
        s = Session(*[Response(status=302, location=url) for _ in range(4)])
        assert await Fetcher(s).media_dimensions(url) is None and len(s.calls) == 4
        for response in (Response(raw, mime='text/html'), Response(raw, status=403),
                         Response(raw[:12]), Response(status=302)):
            assert await Fetcher(Session(response)).media_dimensions(url) is None
    asyncio.run(check())


def test_news_group_runner_switch_actual_delivery_all_five_games():
    from unittest.mock import AsyncMock, patch

    from gamexpress.runner import run_news
    from gamexpress.sources import kuro
    for key in ('genshin', 'starrail', 'hna', 'zzz', 'wuwa'):
        for enabled in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                ctx = make_ctx(Path(directory) / 'state.json')
                ctx.settings = settings(NEWS_ENABLED=1, NEWS_GAMES=key, NEWS_GROUP_LONG_STRIPS=int(enabled),
                                        **{f'DISCORD_WEBHOOK_NEWS_{key.upper()}': HOOK})
                source = kuro if key == 'wuwa' else hoyolab
                with patch.object(source, 'discover_news', return_value=[]):
                    asyncio.run(run_news(ctx))
                ctx.now += 20
                row = {'id': '100', 'ts': ctx.now - 10}
                ctx.fetcher = types.SimpleNamespace(verify_media=AsyncMock(return_value=True),
                                                    media_dimensions=AsyncMock(return_value=(100, 900)))
                item = _group_fixture()
                item.game, item.source = key, 'kuro' if key == 'wuwa' else 'hoyolab'
                with patch.object(source, 'discover_news', return_value=[row]), patch.object(source, 'news_article', return_value=item):
                    asyncio.run(run_news(ctx))
                assert len(ctx.webhook.sent) == 1, ctx.warnings
                payload = ctx.webhook.sent[0]["payload"]
                assert _group_urls([payload]) == _group_urls(cards.news_payloads(GAMES[key], item))
                assert any(len(c['items']) > 1 for co in payload['components']
                           for c in co['components'] if c['type'] == 12) is enabled
                assert bool(ctx.fetcher.media_dimensions.await_count) is enabled
                assert ctx.state.news_record(key)['records']['100']['status'] == 'posted'


def test_news_group_runner_measurement_failure_delivers_default_availability_failure_holds():
    from unittest.mock import AsyncMock, patch

    from gamexpress.runner import run_news
    for available in (True, False):
        with tempfile.TemporaryDirectory() as directory:
            ctx = news_ctx(Path(directory) / 'state.json', NEWS_GROUP_LONG_STRIPS=1)
            with patch.object(hoyolab, 'discover_news', return_value=[]):
                asyncio.run(run_news(ctx))
            ctx.now += 20
            ctx.fetcher = types.SimpleNamespace(verify_media=AsyncMock(return_value=available),
                                                media_dimensions=AsyncMock(return_value=None))
            item = _group_fixture()
            row = {'id': '100', 'ts': ctx.now - 10}
            with patch.object(hoyolab, 'discover_news', return_value=[row]), patch.object(hoyolab, 'news_article', return_value=item) as full:
                asyncio.run(run_news(ctx))
            assert len(ctx.webhook.sent) == int(available)
            if available:
                assert ctx.webhook.sent[0]["payload"] == cards.news_payloads(GAMES['genshin'], item)[0]
                assert full.await_count == 1
            else:
                ctx.fetcher.media_dimensions.assert_not_awaited()
                assert full.await_count == 2


def test_news_group_partial_signature_change_holds_no_repost():
    from unittest.mock import AsyncMock, patch

    from gamexpress.runner import run_news
    from gamexpress.state import stable_hash
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / 'state.json', NEWS_GROUP_LONG_STRIPS=1)
        with patch.object(hoyolab, 'discover_news', return_value=[]):
            asyncio.run(run_news(ctx))
        ctx.now += 20
        item = _group_fixture()
        row = {'id': '100', 'ts': ctx.now - 10}
        rec = {'status': 'partial', 'ts': row['ts'], 'row': row,
               'signature': stable_hash(cards.news_payloads(GAMES['genshin'], item)), 'messages': ['old']}
        ctx.state.news_record('genshin')['records']['100'] = rec
        ctx.fetcher = types.SimpleNamespace(verify_media=AsyncMock(return_value=True),
                                            media_dimensions=AsyncMock(return_value=(100, 900)))
        with patch.object(hoyolab, 'discover_news', return_value=[]), patch.object(hoyolab, 'news_article', return_value=item):
            asyncio.run(run_news(ctx))
        assert not ctx.webhook.sent and rec['messages'] == ['old'] and rec['status'] == 'partial'
        assert any('changed' in w for w in ctx.warnings)




def test_news_group_workflow_variable_blob_and_no_optin_side_effect():
    s = settings(GE_VARS_JSON=json.dumps({'NEWS_GROUP_LONG_STRIPS': '1'}))
    assert s.news_group_long_strips and not s.news_enabled
    assert not s.features.intersection({'news'})


def test_news_group_exact_message_budgets_remain_valid():
    from gamexpress.media import group_measured_news
    item = _group_fixture()
    header = cards.news_payloads(GAMES['wuwa'], item)[0]['components'][0]['components'][0]
    item.article_blocks[0]['text'] = 'a' * (4000 - len(header['content']))
    original = cards.news_payloads(GAMES['wuwa'], item)
    assert cards.text_length(original[0]['components']) == 4000
    result = group_measured_news(original, dict.fromkeys(_group_urls(original), (100, 900)))
    assert result != original and not cards.validate_payload(result[0])
    item = _group_fixture(32)
    item.images = []
    original = cards.news_payloads(GAMES['wuwa'], item)
    assert len(original) == 1 and cards.count_components(original[0]['components']) == 40
    result = group_measured_news(original, dict.fromkeys(_group_urls(original), (100, 900)))
    assert result != original and not cards.validate_payload(result[0])


def test_news_group_runner_measures_only_refreshed_source_urls():
    from copy import deepcopy
    from unittest.mock import AsyncMock, patch

    from gamexpress.runner import run_news
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / 'state.json', NEWS_GROUP_LONG_STRIPS=1)
        with patch.object(hoyolab, 'discover_news', return_value=[]):
            asyncio.run(run_news(ctx))
        ctx.now += 20
        old = _group_fixture()
        fresh = deepcopy(old)
        fresh.images = ['https://upload-os-bbs.hoyolab.com/renewed.png?source=official']
        row = {'id': '100', 'ts': ctx.now - 10}
        ctx.fetcher = types.SimpleNamespace(verify_media=AsyncMock(side_effect=[False] + [True] * 5),
                                            media_dimensions=AsyncMock(return_value=(100, 900)))
        with patch.object(hoyolab, 'discover_news', return_value=[row]), patch.object(hoyolab, 'news_article', side_effect=[old, fresh]):
            asyncio.run(run_news(ctx))
        assert len(ctx.webhook.sent) == 1
        measured = [a.args[0] for a in ctx.fetcher.media_dimensions.await_args_list]
        assert fresh.images[0] in measured and old.images[0] not in measured
        assert _group_urls([ctx.webhook.sent[0]['payload']]) == _group_urls(cards.news_payloads(GAMES['genshin'], fresh))


def test_news_group_stable_partial_delivery_resumes_second_page_only():
    from unittest.mock import AsyncMock, patch

    from gamexpress.media import group_measured_news
    from gamexpress.runner import run_news
    from gamexpress.state import stable_hash
    with tempfile.TemporaryDirectory() as directory:
        ctx = news_ctx(Path(directory) / 'state.json', NEWS_GROUP_LONG_STRIPS=1)
        with patch.object(hoyolab, 'discover_news', return_value=[]):
            asyncio.run(run_news(ctx))
        ctx.now += 20
        item = _group_fixture()
        item.article_blocks[0]['text'] = ('Paragraph ' * 80 + '\n\n') * 6
        original = cards.news_payloads(GAMES['genshin'], item)
        assert len(original) == 2
        grouped = group_measured_news(original, dict.fromkeys(_group_urls(original), (100, 900)))
        row = {'id': '100', 'ts': ctx.now - 10}
        rec = {'status': 'partial', 'ts': row['ts'], 'row': row,
               'signature': stable_hash(grouped), 'messages': ['confirmed-first-page']}
        ctx.state.news_record('genshin')['records']['100'] = rec
        ctx.fetcher = types.SimpleNamespace(verify_media=AsyncMock(return_value=True),
                                            media_dimensions=AsyncMock(return_value=(100, 900)))
        with patch.object(hoyolab, 'discover_news', return_value=[]), patch.object(hoyolab, 'news_article', return_value=item):
            asyncio.run(run_news(ctx))
        assert len(ctx.webhook.sent) == 1 and ctx.webhook.sent[0]['payload'] == grouped[1]
        assert rec['messages'][0] == 'confirmed-first-page' and rec['status'] == 'posted'


if __name__ == "__main__":
    sys.exit(main())
