"""Feature 4, third card — "Servers Live" (2026-11-04). Offline, no secrets.

Covers the two signals and every guard around them:

  * HoYoPlay games: `getGlobalApplicationMaintenance` flips `object -> null` after it was seen set
    for the version this install announced -> one Servers Live card (observed), the Maintenance
    Started card's End Time corrected to the actual reopening, nothing twice.
  * Every game: the publisher's completion notice / tweet (`maint_completed_ts`) -> Servers Live
    (official); it also upgrades an observed card silently, never reposting.
  * Never: before `maint_start_ts + 20 min`, after +24 h, without a posted Maintenance Started
    card, for a version joined midway (seeded), on an unknown gateway answer, or on a flag that
    was never seen set. Completion phrases quoted in future tense by the maintenance PREVIEW
    ("compensation ... after the update maintenance is finished") are not completions.

Run:  python tests/test_maintenance_live.py      (CI runs it next to the other offline suites)
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_smoke import GAMES, HOOK, ScriptedWebhook, fx, make_ctx  # noqa: E402

from gamexpress import maintenance, schedule  # noqa: E402
from gamexpress.cards import maintenance_payload, validate_payload  # noqa: E402
from gamexpress.discord import SendResult  # noqa: E402
from gamexpress.models import Item  # noqa: E402
from gamexpress.sources import launcher as launcher_mod  # noqa: E402
from gamexpress.sources.hoyolab import _post_text  # noqa: E402

# GI 7.2 (real): pre-load 2026-11-02 11:00 UTC+8, maintenance 2026-11-04 06:00-11:00 UTC+8.
GI_PRE, GI_START, GI_END = 1793588400, 1793743200, 1793761200
GI_VIDEO = "https://fastcdn.hoyoverse.com/static-resource-v2/2026/11/03/launch_7_2.mp4"
GI_MAINT_URL = launcher_mod.MAINT_STATUS["gopR6Cufr3"][0]
PING = "1296268365593186426"
IN_MAINT = {"retcode": 0, "message": "OK", "data": {"application_maintenance": {
    "start_time": str(GI_START), "end_time": str(GI_END), "msg": "Version update in progress"}}}
OPEN = {"retcode": 0, "message": "OK", "data": {"application_maintenance": None}}


class _Fetcher:
    """`get_json` routed by URL; counts the maintenance-status reads so idle runs prove 0."""

    def __init__(self, *, maint=None, available=()):
        self.maint = maint
        self.available = set(available)
        self.calls: list[tuple[str, dict]] = []

    async def verify_media(self, url):
        return url in self.available

    async def get_text(self, url, *, source, headers=None, params=None, retries=2, timeout=None):
        return None

    async def get_json(self, url, *, source, headers=None, params=None, retries=2, timeout=None):
        if url == GI_MAINT_URL:
            self.calls.append((source, dict(headers or {})))
            return self.maint() if callable(self.maint) else self.maint
        return None

    @property
    def maint_reads(self) -> int:
        return sum(1 for src, _ in self.calls if src == "hoyo-maint")


def _gi_ctx(path, now, **extra):
    ctx = make_ctx(path, now=now, MAINTENANCE_ENABLED="1", ONLY="maintenance", MAINTENANCE_GAMES="genshin",
                   DISCORD_WEBHOOK_MAINTENANCE_GENSHIN=HOOK, PING_MAINTENANCE_GENSHIN=PING, **extra)
    ctx.state.schedule_records("genshin")["7.2"] = {
        "status": "posted",
        "data": {"version": "7.2", "preinstall_ts": GI_PRE, "maint_start_ts": GI_START, "maint_end_ts": GI_END},
        "prov": {"preinstall_ts": [50, 1], "maint_start_ts": [50, 1], "maint_end_ts": [50, 1]},
    }
    ctx.versions["genshin"] = {"live": "7.1", "pre": "7.2", "live_tag": "7.1.0"}
    ctx.items["genshin"] = []
    return ctx


def _run(ctx, binfo=None):
    with patch.object(launcher_mod, "hoyoplay_basic_info", new=AsyncMock(return_value=binfo or {})):
        asyncio.run(maintenance.run(ctx))


def _flat(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _gallery_urls(payload) -> list[str]:
    return [it["media"]["url"] for c in payload["components"][0]["components"] if c["type"] == 12
            for it in c["items"]]


def _vstate(ctx, game="genshin", ver="7.2") -> dict:
    return ctx.state.maintenance_record(game)["versions"][ver]


def _post_maintenance_card(path, *, binfo):
    """Seed before pre-load, then post the Maintenance Started card at start + 60 s."""
    ctx = _gi_ctx(path, now=GI_PRE - 3600)
    ctx.fetcher = _Fetcher()
    _run(ctx)                                                    # silent baseline
    assert ctx.webhook.sent == [] and "live" not in _vstate(ctx)
    ctx.state.save()

    ctx = _gi_ctx(path, now=GI_PRE + 60)
    ctx.fetcher = _Fetcher()
    ctx.webhook = ScriptedWebhook([], [SendResult(True, 200, "pre-msg")])
    _run(ctx)
    assert ["Pre-Load Started" in _flat(p) for p in ctx.webhook.sends] == [True]
    ctx.state.save()

    ctx = _gi_ctx(path, now=GI_START + 60)
    ctx.versions["genshin"] = {"live": "7.2", "pre": None, "live_tag": "7.2.0"}
    ctx.fetcher = _Fetcher(available=[GI_VIDEO])
    ctx.webhook = ScriptedWebhook([], [SendResult(True, 200, "maint-msg")])
    _run(ctx, binfo)
    assert ["Maintenance Started" in _flat(p) for p in ctx.webhook.sends] == [True]
    assert _vstate(ctx)["maintenance"]["status"] == "posted"
    assert ctx.fetcher.maint_reads == 0                          # first 20 minutes: not asked
    ctx.state.save()


# ------------------------------------------------------------------------------------ patterns
def test_completion_phrases_match_and_future_tense_quotes_do_not():
    positives = [
        "Version 7.2 Update Maintenance Completed",
        "The Version 7.2 update maintenance has been completed. Travelers can now log in to the game.",
        "Version 4.7 Update Maintenance Complete Notice",
        "Version 3.8 Update Completion Notice",
        "Dear Trailblazers, the Version 4.7 update maintenance has concluded.",
        "The Version 3.8 update has been completed and Rovers can now log in.",
        "Servers are now online. Thank you for your patience, Proxies!",
        "Maintenance is over!",
    ]
    negatives = [
        "Version 7.2 Update and Maintenance Notice",
        "Version 7.2 Update Maintenance Preview",
        "Update maintenance begins 2026/11/04 06:00 (UTC+8) and is estimated to take 5 hours.",
        "Our developers will distribute compensation via in-game mail within 5 hours after the update "
        "maintenance is finished.",
        "Once the maintenance is complete, Travelers can log in and claim the compensation.",
        "Travelers will be able to log in after the Version 7.2 update maintenance is completed.",
        "The maintenance is expected to be completed at 11:00 (UTC+8).",
        "If the maintenance is completed early, the servers will open early.",
        "Maintenance has been extended until 2026/11/04 12:00 (UTC+8).",
        "Pre-installation is now available.",
        "Version 7.1 Update Details and Compensation",
    ]
    for text in positives:
        assert schedule._completion_match(text), text
    for text in negatives:
        assert not schedule._completion_match(text), text


def test_real_preview_and_update_details_notices_never_yield_a_completion():
    # The GI 7.1 Update Details article quotes "after the update maintenance is finished".
    for row in fx("hoyolab_genshin_list_update_details.json")["data"]["list"]:
        post = row["post"]
        text, links, imgs = _post_text(post)
        item = Item("hoyolab", "genshin", post["post_id"], f"https://www.hoyolab.com/article/{post['post_id']}",
                    post["subject"], text, int(post["created_at"]), imgs, links)
        assert not schedule.maintenance_completed(item), item.title
        ex = schedule.extract(GAMES["genshin"], item)
        assert ex is None or "maint_completed_ts" not in ex.fields
    preview = Item("hoyolab", "genshin", "p1", "https://www.hoyolab.com/article/1", "Version 7.2 Update Maintenance Preview",
                   "Update maintenance begins 2026/11/04 06:00 (UTC+8) and is estimated to take 5 hours. "
                   "Compensation will be sent within 5 hours after the update maintenance is finished.", GI_PRE)
    ex = schedule.extract(GAMES["genshin"], preview)
    assert ex is not None and ex.fields.get("maint_start_ts") == GI_START and "maint_completed_ts" not in ex.fields


def test_completion_extract_uses_the_stated_clock_else_the_post_time():
    stated = Item("hoyolab", "genshin", "c1", "https://www.hoyolab.com/article/2", "Version 7.2 Update Maintenance Completed",
                  "The Version 7.2 update maintenance was completed at 2026/11/04 10:42 (UTC+8). Travelers can now log in.",
                  GI_START + 5 * 3600)
    ex = schedule.extract(GAMES["genshin"], stated)
    assert ex is not None and ex.kind == "maintenance" and ex.version == "7.2"
    assert ex.fields == {"maint_completed_ts": GI_START + 4 * 3600 + 42 * 60}
    tweet = Item("x", "genshin", "t1", "https://x.com/GenshinImpact/status/1", "",
                 "The Version 7.2 update maintenance has been completed. Travelers can now log in. "
                 "Maintenance Time: 2026/11/04 06:00 - 11:00 (UTC+8)", GI_START + 4 * 3600 + 50 * 60)
    ex = schedule.extract(GAMES["genshin"], tweet)
    assert ex is not None and ex.fields == {"maint_completed_ts": GI_START + 4 * 3600 + 50 * 60}


def test_parse_maintenance_status_is_strict():
    parse = launcher_mod.parse_maintenance_status
    assert parse(IN_MAINT) is True
    assert parse(OPEN) is False
    assert parse(None) is None and parse({}) is None and parse([]) is None
    assert parse({"retcode": -1, "data": {"application_maintenance": None}}) is None   # error, not "open"
    assert parse({"retcode": 0, "data": {}}) is None                                   # key missing
    assert parse({"retcode": 0, "data": {"application_maintenance": []}}) is None      # wrong shape
    assert parse({"retcode": 0, "data": {"application_maintenance": "yes"}}) is None
    branches = launcher_mod.parse_branches({"data": {"game_branches": [
        {"game": {"id": "gopR6Cufr3"}, "main": {"tag": "7.2.0"}, "pre_download": None}]}})
    assert branches["gopR6Cufr3"] == {"live": "7.2", "pre": None, "live_tag": "7.2.0"}


# ------------------------------------------------------------------------------- HoYo gateway
def test_gateway_flip_posts_servers_live_once_and_corrects_the_end_time():
    binfo = {"gopR6Cufr3": [{"video": GI_VIDEO, "background": "", "theme": "", "links": [],
                            "id": "", "type": "", "video_md5": "", "background_md5": ""}]}
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        _post_maintenance_card(path, binfo=binfo)

        def at(now, answer, sends=(), edits=()):
            ctx = _gi_ctx(path, now=now)
            ctx.versions["genshin"] = {"live": "7.2", "pre": None, "live_tag": "7.2.0"}
            ctx.fetcher = _Fetcher(maint=answer, available=[GI_VIDEO])
            ctx.webhook = ScriptedWebhook(list(edits), list(sends))
            _run(ctx, binfo)
            ctx.state.save()
            return ctx

        # +10 min: inside the settling period -> the gateway is not even asked.
        ctx = at(GI_START + 600, OPEN)
        assert ctx.fetcher.maint_reads == 0 and ctx.webhook.sends == []

        # +30 min: flag set -> remembered for 7.2, nothing posted. Headers follow hoyo-update-notifier.
        ctx = at(GI_START + 1800, IN_MAINT)
        assert ctx.fetcher.maint_reads == 1 and ctx.webhook.sends == []
        src, headers = ctx.fetcher.calls[0]
        assert headers == {"x-rpc-app_version": "7.2.0", "x-rpc-client_type": "1", "x-rpc-cg_game_biz": "hk4e_global"}
        api = ctx.state.maintenance_record("genshin")["maint_api"]
        assert api["version"] == "7.2" and api["seen_set_at"] == GI_START + 1800

        # +4 h: unknown answer (gateway error) -> nothing changes, no card.
        ctx = at(GI_START + 4 * 3600, {"retcode": -100, "message": "err"})
        assert ctx.webhook.sends == [] and "live" not in _vstate(ctx)

        # +4 h 42 min: null -> ONE silent Servers Live card (tier-1 video reused, observed note),
        # and the Maintenance Started card's End Time is rewritten to the actual moment.
        live_at = GI_START + 4 * 3600 + 42 * 60
        ctx = at(live_at, OPEN, sends=[SendResult(True, 200, "live-msg")], edits=[SendResult(True, 200)])
        assert len(ctx.webhook.sends) == 1 and len(ctx.webhook.edits) == 1
        card = ctx.webhook.sends[0]
        flat = _flat(card)
        assert "[GI - 7.2] Servers Live" in flat and f"<@&{PING}>" not in flat
        assert card["allowed_mentions"] == {"parse": []}
        assert f"<t:{live_at}:f>" in flat and f"<t:{live_at}:R>" in flat
        assert "Detected from the game's maintenance status" in flat
        assert _gallery_urls(card) == [GI_VIDEO]
        assert not validate_payload(card)
        msg_id, edit = ctx.webhook.edits[0]
        assert msg_id == "maint-msg"
        assert f"※ **End Time:** <t:{live_at}:t>" in _flat(edit) and "Maintenance Started" in _flat(edit)
        assert edit["allowed_mentions"] == {"parse": [], "roles": [PING]}
        live = _vstate(ctx)["live"]
        assert live["status"] == "posted" and live["provenance"] == "observed" and live["ts"] == live_at
        maint = _vstate(ctx)["maintenance"]
        assert maint["end_ts"] == live_at and maint["end_actual"] is True and maint["timing_edit_count"] == 1
        assert any("Servers Live" in r or "live: posted" in r for r in ctx.report)
        assert any("End Time corrected" in r for r in ctx.report)

        # Later runs: no second card, no gateway read, and the scheduled 11:00 end is NOT restored.
        for later in (live_at + 300, live_at + 3600, GI_START + 23 * 3600):
            ctx = at(later, OPEN)
            assert ctx.webhook.sends == [] and ctx.webhook.edits == [] and ctx.fetcher.maint_reads == 0
            assert _vstate(ctx)["maintenance"]["end_ts"] == live_at
        # Far beyond the window (next day) the gateway is still never asked.
        ctx = at(GI_START + 2 * 86400, IN_MAINT)
        assert ctx.fetcher.maint_reads == 0 and ctx.webhook.sends == []


def test_flag_never_seen_set_or_still_set_never_posts_and_window_closes_at_24h():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        _post_maintenance_card(path, binfo={})

        def at(now, answer):
            ctx = _gi_ctx(path, now=now)
            ctx.versions["genshin"] = {"live": "7.2", "pre": None, "live_tag": "7.2.0"}
            ctx.fetcher = _Fetcher(maint=answer)
            ctx.webhook = ScriptedWebhook([], [SendResult(True, 200, "never")])
            _run(ctx)
            ctx.state.save()
            return ctx

        # `null` from the first read on (flag never observed set) is not a transition.
        for offset in (1800, 3600, 5 * 3600, 12 * 3600):
            ctx = at(GI_START + offset, OPEN)
            assert ctx.fetcher.maint_reads == 1 and ctx.webhook.sends == [] and "live" not in _vstate(ctx)
        # Set at +13 h and STILL set at +23 h 50 min: no card; at +24 h 10 min the window is closed.
        ctx = at(GI_START + 13 * 3600, IN_MAINT)
        assert ctx.webhook.sends == []
        ctx = at(GI_START + 23 * 3600 + 50 * 60, IN_MAINT)
        assert ctx.fetcher.maint_reads == 1 and ctx.webhook.sends == []
        ctx = at(GI_START + 24 * 3600 + 600, OPEN)
        assert ctx.fetcher.maint_reads == 0 and ctx.webhook.sends == [] and "live" not in _vstate(ctx)


def test_no_posted_maintenance_card_means_no_gateway_read_and_no_live():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        ctx = _gi_ctx(path, now=GI_PRE - 3600)
        ctx.fetcher = _Fetcher()
        _run(ctx)
        ctx.state.save()
        ctx = _gi_ctx(path, now=GI_PRE + 60)
        ctx.fetcher = _Fetcher()
        ctx.webhook = ScriptedWebhook([], [SendResult(True, 200, "pre-msg")])
        _run(ctx)
        ctx.state.save()
        # Maintenance Started send FAILS (e.g. Discord 5xx) -> nothing is "posted".
        ctx = _gi_ctx(path, now=GI_START + 60)
        ctx.versions["genshin"] = {"live": "7.2", "pre": None, "live_tag": "7.2.0"}
        ctx.fetcher = _Fetcher(maint=IN_MAINT)
        ctx.webhook = ScriptedWebhook([], [SendResult(False, 502)])
        _run(ctx)
        assert _vstate(ctx).get("maintenance") is None
        ctx.state.save()
        ctx = _gi_ctx(path, now=GI_START + 5 * 3600)
        ctx.versions["genshin"] = {"live": "7.2", "pre": None, "live_tag": "7.2.0"}
        ctx.fetcher = _Fetcher(maint=OPEN)
        ctx.webhook = ScriptedWebhook([], [SendResult(True, 200, "maint-late"), SendResult(True, 200, "x")])
        _run(ctx)
        # The retry inside the active window posts Maintenance Started; the gateway is only read
        # for a card that is already posted, so no Servers Live in the same run.
        assert [("Maintenance Started" in _flat(p)) for p in ctx.webhook.sends] == [True]
        assert ctx.fetcher.maint_reads == 0 and "live" not in _vstate(ctx)


def test_first_enable_during_maintenance_seeds_live_and_never_backfills():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        ctx = _gi_ctx(path, now=GI_START + 3600)            # enabled one hour into the maintenance
        ctx.versions["genshin"] = {"live": "7.2", "pre": None, "live_tag": "7.2.0"}
        ctx.fetcher = _Fetcher(maint=IN_MAINT)
        _run(ctx)
        vs = _vstate(ctx)
        assert vs["maintenance"]["status"] == "seeded" and vs["live"]["status"] == "seeded"
        assert ctx.fetcher.maint_reads == 0 and ctx.webhook.sent == []
        ctx.state.save()
        ctx = _gi_ctx(path, now=GI_START + 5 * 3600)
        ctx.versions["genshin"] = {"live": "7.2", "pre": None, "live_tag": "7.2.0"}
        ctx.fetcher = _Fetcher(maint=OPEN)
        ctx.webhook = ScriptedWebhook([], [SendResult(True, 200, "never")])
        _run(ctx)
        assert ctx.webhook.sends == [] and ctx.fetcher.maint_reads == 0


# ------------------------------------------------------------------ official completion notices
def _kuro_notice(ts, title="Version 3.8 Update Completion Notice", text=None):
    return Item("kuro", "wuwa", "5901", "https://wutheringwaves.kurogames.com/en/main/news/detail/5901", title,
                text or "Dear Rovers, the Version 3.8 update has been completed. Rovers can now log in and "
                        "claim the maintenance compensation.", ts)


def test_kuro_completion_notice_posts_servers_live_for_wuthering_waves():
    ww_start, ww_end = 1794427200, 1794452400          # 2026-11-12 04:00-11:00 UTC+8
    notice = Item("kuro", "wuwa", "5890", "https://wutheringwaves.kurogames.com/en/main/news/detail/5890",
                  "Version 3.8 Update Maintenance Notice",
                  "Version 3.8 pre-download will begin at 2026/11/10 10:00 (UTC+8).\n"
                  "Maintenance Time: 2026/11/12 04:00 - 11:00 (UTC+8)\nCompensation: Astrite ×300", ww_start - 2 * 86400)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"

        def ctx_at(now, items):
            c = make_ctx(path, now=now, MAINTENANCE_ENABLED="1", ONLY="maintenance", MAINTENANCE_GAMES="wuwa",
                         DISCORD_WEBHOOK_MAINTENANCE_WUWA=HOOK, PING_MAINTENANCE_WUWA=PING)
            # The Kuro launcher lists the pre-download from 3.8's pre-load moment and 3.8 as live
            # from maintenance start, exactly as the real index does.
            pre_open, live_now = now >= ww_start - 2 * 86400 + 6 * 3600, now >= ww_start
            c.versions["wuwa"] = {"live": "3.8" if live_now else "3.7",
                                  "pre": None if (live_now or not pre_open) else "3.8"}
            c.items["wuwa"] = list(items)
            c.fetcher = _Fetcher()
            return c

        ctx = ctx_at(ww_start - 3 * 86400, [])
        _run(ctx)
        ctx.state.save()                                                   # silent baseline
        ctx = ctx_at(ww_start - 2 * 86400 + 6 * 3600 + 120, [notice])      # pre-download 10:00 UTC+8
        ctx.webhook = ScriptedWebhook([], [SendResult(True, 200, "ww-pre")])
        _run(ctx)
        assert ["Pre-Load Started" in _flat(p) for p in ctx.webhook.sends] == [True]
        ctx.state.save()
        ctx = ctx_at(ww_start + 120, [notice])
        ctx.webhook = ScriptedWebhook([], [SendResult(True, 200, "ww-maint")])
        _run(ctx)
        assert any("Maintenance Started" in _flat(p) for p in ctx.webhook.sends)
        ctx.state.save()
        # The completion notice published 5 min before the scheduled end -> official Servers Live,
        # no "detected" note; End Time on the maintenance card corrected to the notice time.
        done = ww_end - 300
        ctx = ctx_at(done + 240, [notice, _kuro_notice(done)])
        ctx.webhook = ScriptedWebhook([SendResult(True, 200)], [SendResult(True, 200, "ww-live")])
        _run(ctx)
        assert len(ctx.webhook.sends) == 1 and len(ctx.webhook.edits) == 1
        flat = _flat(ctx.webhook.sends[0])
        assert "[WW - 3.8] Servers Live" in flat and f"<t:{done}:f>" in flat and "Detected from" not in flat
        assert _vstate(ctx, "wuwa", "3.8")["live"] == {
            **_vstate(ctx, "wuwa", "3.8")["live"], "status": "posted", "provenance": "official", "ts": done}
        assert ctx.webhook.edits[0][0] == "ww-maint" and f"<t:{done}:t>" in _flat(ctx.webhook.edits[0][1])
        ctx.state.save()
        ctx = ctx_at(done + 900, [notice, _kuro_notice(done)])
        ctx.webhook = ScriptedWebhook([], [])
        _run(ctx)
        assert ctx.webhook.sends == [] and ctx.webhook.edits == []


def test_completion_notice_before_start_or_after_24h_is_ignored():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        _post_maintenance_card(path, binfo={})
        for ts in (GI_START - 3600, GI_START + 600, GI_START + 25 * 3600):
            ctx = _gi_ctx(path, now=max(ts, GI_START + 60) + 60)
            ctx.versions["genshin"] = {"live": "7.2", "pre": None, "live_tag": "7.2.0"}
            ctx.items["genshin"] = [Item("hoyolab", "genshin", f"c{ts}", "https://www.hoyolab.com/article/9",
                                         "Version 7.2 Update Maintenance Completed",
                                         "The Version 7.2 update maintenance has been completed.", ts)]
            ctx.fetcher = _Fetcher(maint=None)
            ctx.webhook = ScriptedWebhook([], [SendResult(True, 200, "never")])
            _run(ctx)
            assert ctx.webhook.sends == [] and "live" not in _vstate(ctx), ts


def test_official_notice_upgrades_an_observed_live_card_silently():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        _post_maintenance_card(path, binfo={})
        live_at = GI_START + 4 * 3600 + 40 * 60
        for now, answer in ((GI_START + 1800, IN_MAINT), (live_at, OPEN)):
            ctx = _gi_ctx(path, now=now)
            ctx.versions["genshin"] = {"live": "7.2", "pre": None, "live_tag": "7.2.0"}
            ctx.fetcher = _Fetcher(maint=answer)
            ctx.webhook = ScriptedWebhook([SendResult(True, 200)], [SendResult(True, 200, "live-msg")])
            _run(ctx)
            ctx.state.save()
        assert _vstate(ctx)["live"]["provenance"] == "observed" and len(ctx.webhook.sends) == 1
        # HoYoLAB publishes the completion notice 7 minutes later, stating 10:38 -> silent PATCH only.
        official = GI_START + 4 * 3600 + 38 * 60
        ctx = _gi_ctx(path, now=live_at + 420)
        ctx.versions["genshin"] = {"live": "7.2", "pre": None, "live_tag": "7.2.0"}
        ctx.items["genshin"] = [Item("hoyolab", "genshin", "c7", "https://www.hoyolab.com/article/7",
                                     "Version 7.2 Update Maintenance Completed",
                                     "The Version 7.2 update maintenance was completed at 2026/11/04 10:38 (UTC+8).",
                                     live_at + 300)]
        ctx.fetcher = _Fetcher(maint=OPEN)
        ctx.webhook = ScriptedWebhook([SendResult(True, 200)], [])
        _run(ctx)
        assert ctx.webhook.sends == [] and len(ctx.webhook.edits) == 1
        msg_id, edit = ctx.webhook.edits[0]
        assert msg_id == "live-msg" and f"<t:{official}:f>" in _flat(edit) and "Detected from" not in _flat(edit)
        live = _vstate(ctx)["live"]
        assert live["provenance"] == "official" and live["ts"] == official and live["timing_edit_count"] == 1
        assert ctx.fetcher.maint_reads == 0                     # an official answer needs no gateway read


# --------------------------------------------------------------------------------------- card
def test_servers_live_card_shape_and_preview_samples():
    card = maintenance_payload(GAMES["genshin"], "7.2", "live", start_ts=GI_END - 1080, media_url=GI_VIDEO)
    flat = _flat(card)
    assert "## [GI - 7.2] Servers Live" in flat and f"<t:{GI_END - 1080}:R>" in flat and "End Time" not in flat
    assert card["flags"] == 32768 and not validate_payload(card) and _gallery_urls(card) == [GI_VIDEO]
    observed = maintenance_payload(GAMES["wuwa"], "3.8", "live", start_ts=GI_END, observed=True)
    assert "Detected from the game's maintenance status" in _flat(observed)
    try:
        maintenance_payload(GAMES["genshin"], "7.2", "reopened", start_ts=GI_END)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown kinds must be rejected")
    from gamexpress.__main__ import sample_payloads
    names = [name for _, name, _ in sample_payloads("maintenance", None)]
    for key in ("genshin", "starrail", "zzz", "wuwa", "hna", "ananta"):
        assert f"maintenance_{key}_live" in names, key
    live_sample = next(p for _, n, p in sample_payloads("maintenance", "genshin") if n == "maintenance_genshin_live")
    assert "Servers Live" in _flat(live_sample) and not validate_payload(live_sample)


def test_repost_target_accepts_the_live_kind():
    assert maintenance._repost_matches("genshin:7.2:live", "genshin", "7.2", "live")
    assert not maintenance._repost_matches("genshin:7.2:preload", "genshin", "7.2", "live")
    assert maintenance.MILESTONE_KINDS == ("preload", "maintenance", "live")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc!r}")
                import traceback
                traceback.print_exc()
    print("ALL PASS" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)
