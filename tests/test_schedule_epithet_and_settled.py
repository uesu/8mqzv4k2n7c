"""Regression tests for the 2026-10-08 Genshin 7.1 schedule-card faults.

Run:   python tests/test_schedule_epithet_and_settled.py     (plain runner)
   or: python -m pytest -q tests/test_schedule_epithet_and_settled.py

Four defects were visible on one live card (Game-Express 64f0101, 2026-10-08T08:00Z). The last
three are a single root cause: the record was opened by a maintenance notice, so the Special
Program announcement was never attached to it.

  A. extract_banner() returned the BANNER TITLE instead of the character for notices written as
     `"Epithet" Name (Element)`. Genshin 7.1 shipped
     phase2   = ["Tasteful Excellence"]                                    (should be Escoffier)
     phase2_4 = ["Ode and Oblation", "Golden Vow", "Coordinates of Clear Frost"]
                                                            (should be Dahlia, Candace, Mika)

  B. A card was opened at all for a version announced a month earlier. Genshin 7.1 was announced
     2026-09-07, aired 09-12 and shipped 09-23. This bot was deployed afterwards and had never
     posted a 7.1 card -- the one players actually had came from a different bot entirely. Two
     things let it publish one anyway: program_settled() read only program_ts, which a record
     built from an *Update Details* notice never has, so 7.1 never counted as settled; and the
     creation gate never consulted program_settled() at all. A Phase II notice on 2026-10-08
     made the bot PATCH a message id that no longer resolved, the edit returned 404, and it
     published a brand-new card and a brand-new mirror copy.

  C. That same missing program_ts left the card with no air time at all (cards.py prints nothing
     rather than a misleading TBA), while `data = dict(prev)` kept the notice's cover as key art
     and the notice's URL as the title link -- for fifteen days, because needs_program_lookup()
     switched the archived-announcement lookup off 12 h after maintenance and nothing else could
     ever supply the announcement. The fix replays the tweet already cached in
     config/program_announcements.json: one call, which fixes picture, link and date together.

  D. The card was opened by the maintenance notice in the first place. A schedule card announces
     a Special Program / Special Broadcast; a notice may fill one in but must never create one.
     (Covered in tests/test_smoke.py, which has the async run harness.)

None of these is Genshin-specific: every game uses the same extractor and the same ladder.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Everything exercised below is a pure function: no sockets, no Discord, no event loop. Importing
# gamexpress.schedule still drags in gamexpress.discord -> aiohttp, so a checkout without the
# runtime dependencies installed could not run this regression at all. Stub it only when it is
# genuinely absent; CI installs the real package and takes this branch never.
try:  # pragma: no cover - environment dependent
    import aiohttp  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - offline checkout
    _stub = types.ModuleType("aiohttp")
    for _name in ("ClientSession", "ClientTimeout", "ClientError", "TCPConnector"):
        setattr(_stub, _name, type(_name, (), {}))
    sys.modules["aiohttp"] = _stub

try:  # pragma: no cover - environment dependent
    import feedparser  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - offline checkout
    _fp = types.ModuleType("feedparser")
    _fp.parse = lambda *a, **kw: types.SimpleNamespace(entries=[], bozo=1)
    sys.modules["feedparser"] = _fp

from gamexpress import schedule  # noqa: E402
from gamexpress.models import Item  # noqa: E402

NOW = 1791446400          # 2026-10-08 16:00 +08 — the run that exposed both defects
DAY = 86400


def _item(title: str, text: str, ts: int = NOW) -> Item:
    return Item(source="hoyolab", game="genshin", id="47010361",
                url="https://www.hoyolab.com/article/47010361",
                title=title, text=text, published_ts=ts)


# --------------------------------------------------------------------------- A. epithet parsing
# Wording reproduced from HoYoLAB 47010361, "Version 7.1 Event Wishes Notice - Phase II".
GI_71_PHASE_II = _item(
    "Version 7.1 Event Wishes Notice - Phase II",
    '〓Event Wish Duration〓\n'
    '2026/10/13 18:00:00 - 2026/11/03 14:59:59 (UTC+8)\n'
    '\n'
    '〓Event Wish "Void Star\'s Advent"〓\n'
    '● During this event wish, the event-exclusive 5-star character "Void Star" Skirk (Cryo) '
    'will receive a huge drop-rate boost!\n'
    '● During this event wish, the 4-star characters "Ode and Oblation" Dahlia (Hydro), '
    '"Golden Vow" Candace (Hydro), and "Coordinates of Clear Frost" Mika (Cryo) will receive '
    'a huge drop-rate boost!\n'
    '\n'
    '〓Event Wish "La Chanson Cerise"〓\n'
    '● During this event wish, the event-exclusive 5-star character "Tasteful Excellence" '
    'Escoffier (Cryo) will receive a huge drop-rate boost!\n'
    '● During this event wish, the 4-star characters "Ode and Oblation" Dahlia (Hydro), '
    '"Golden Vow" Candace (Hydro), and "Coordinates of Clear Frost" Mika (Cryo) will receive '
    'a huge drop-rate boost!\n')


def test_epithet_notice_yields_characters_not_banner_titles():
    got = schedule.extract_banner(GI_71_PHASE_II)
    assert got["banner_five"] == ["Skirk", "Escoffier"], got["banner_five"]
    assert got["banner_four"] == ["Dahlia", "Candace", "Mika"], got["banner_four"]
    assert got["banner_four_unsure"] is False
    assert got["banner_phase"] == 2
    # the exact strings the live card was showing must be gone
    for wrong in ("Tasteful Excellence", "Void Star", "Ode and Oblation",
                  "Golden Vow", "Coordinates of Clear Frost"):
        assert wrong not in got["banner_five"] + got["banner_four"], wrong


def test_epithet_handles_compound_names():
    """Names with spaces, bullets, ampersands and digits survive the epithet reader."""
    got = schedule.extract_banner(_item(
        "Phase I",
        'the 5-star character "Lightless Feast" Dan Heng • Imbibitor Lunae (Imaginary) will '
        'receive a boost. During this warp, the 4-star characters "Gem" Topaz & Numby (Fire), '
        '"Seven" March 7th (Ice), and "Vow" Candace (Hydro) will receive a boost.'))
    assert got["banner_five"] == ["Dan Heng • Imbibitor Lunae"], got["banner_five"]
    assert got["banner_four"] == ["Topaz & Numby", "March 7th", "Candace"], got["banner_four"]


def test_bare_name_notices_are_untouched():
    """HSR 4.6 style: characters bare, weapons quoted. Must keep working exactly as before."""
    got = schedule.extract_banner(_item(
        "Version 4.6 Event Warp: Phase I",
        'the limited 5-star character Pearl (Elation: Ice) and the limited 5-star Light Cone '
        '"Colors for Tomorrow (Elation)" will be boosted. Meanwhile the 4-star characters '
        'Qingque (Erudition: Quantum), Xueyi (Destruction: Quantum), and Misha '
        '(Destruction: Ice), as well as the 4-star Light Cones "Post-Op Conversation" will be '
        'boosted.'))
    assert got["banner_five"] == ["Pearl"], got["banner_five"]
    assert got["banner_four"] == ["Qingque", "Xueyi", "Misha"], got["banner_four"]


def test_quoted_character_notices_are_untouched():
    """Notices that really do quote the character keep reading the quote."""
    got = schedule.extract_banner(_item(
        "Signal Search: Phase I",
        'the S-Rank Agent "Hugo Vlad" will receive a drop-rate boost. '
        'the A-Rank Agents "Lucy" and "Piper" will receive a drop-rate boost.'))
    assert got["banner_five"] == ["Hugo Vlad"], got["banner_five"]
    assert got["banner_four"] == ["Lucy", "Piper"], got["banner_four"]


def test_banner_title_trap_still_yields_nothing():
    """The HSR 4.6 trap sentence must still refuse to publish a banner title as a 5-star."""
    got = schedule.extract_banner(_item(
        "Version 4.6 Event Warp",
        '※ the limited 5-star character Pearl (Elation: Ice) can only be obtained from the '
        '"An Ocean in a Pearl" Character Event Warp'))
    assert "An Ocean in a Pearl" not in (got.get("banner_five") or [])


# --------------------------------------------------------------------------- B. settled versions
def test_settled_from_program_ts_unchanged():
    assert schedule.program_settled({"program_ts": NOW - 18 * DAY}, NOW) is True
    assert schedule.program_settled({"program_ts": NOW - 5 * 3600}, NOW) is False


def test_settled_from_maintenance_when_program_ts_is_missing():
    """The real Genshin 7.1 record: no program_ts, maintenance 15 days ago."""
    gi_71 = {"maint_start_ts": 1790114400, "maint_end_ts": 1790132400}   # 2026-09-23 06:00 +08
    assert schedule.program_settled(gi_71, NOW) is True


def test_not_settled_before_maintenance():
    assert schedule.program_settled({"maint_start_ts": NOW + 9 * DAY}, NOW) is False
    assert schedule.program_settled({"maint_start_ts": NOW - 3600}, NOW) is False   # inside 12 h
    assert schedule.program_settled({}, NOW) is False


def test_corrupt_timestamps_cannot_settle_a_version():
    """State is hand-editable; nonsense must not make a version historical."""
    for bad in (0, -1, 1, 10 ** 12, "", None):
        assert schedule.program_settled({"program_ts": bad, "maint_start_ts": bad}, NOW) is False


# --------------------------------------------------------------------------------------------
# C. The card built from the wrong post: no air time, the notice's cover, the notice's link.
#
# The live Genshin 7.1 record on 2026-10-08 (verbatim from state.json): no program_ts, no
# program_seen, no media_from, images = the "Version 7.1 Update Details" cover uploaded
# 2026-09-22, title_url = that same notice. Three visible faults, one cause — the record was
# opened by a maintenance notice, so the announcement was never looked up, and `data = dict(prev)`
# carried the notice's picture and link forward untouched for fifteen days.
# --------------------------------------------------------------------------------------------

LIVE_71_DATA = {
    "version": "7.1",
    "images": ["https://upload-os-bbs.hoyolab.com/upload/2026/09/22/0/769afb25a1c8e9f3.jpeg"],
    "title_url": "https://www.hoyolab.com/article/46791577",
    "source_url": "https://www.hoyolab.com/article/46791577",
    "source_label": "HoYoLAB",
    "maint_start_ts": 1790114400,            # 2026-09-23 06:00 +08 — fifteen days before NOW
    "maint_end_ts": 1790132400,
}

# What config/program_announcements.json already knows, replayed through one fxtwitter call.
# 2026-09-12 20:00 +08 is the LIVESTREAM, not the 2026-09-07 announcement: the tweet says
# "premiere ... 09/12/2026 at 08:00 AM (UTC-4)", which is 12:00 UTC.
LIVESTREAM_TS = 1789214400
CACHED_HIT = {
    "url": "https://x.com/GenshinImpact/status/2096810691021689205",
    "title": "Genshin Impact Version 7.1 Special Program",
    "images": ["https://pbs.twimg.com/media/HRlONCqXcAUhgGD.jpg?name=orig"],
    "program_ts": LIVESTREAM_TS,
    "source": "X Post",
}


def test_livestream_timestamp_is_the_premiere_not_the_announcement():
    import datetime as _dt
    aired = _dt.datetime.fromtimestamp(LIVESTREAM_TS, _dt.timezone.utc)
    assert aired == _dt.datetime(2026, 9, 12, 12, 0, tzinfo=_dt.timezone.utc)   # 20:00 +08
    assert aired != _dt.datetime(2026, 9, 7, 4, 0, tzinfo=_dt.timezone.utc)     # not the tweet date


def test_a_live_version_with_no_air_time_is_looked_up_once_more_when_it_is_cached():
    """The 12 h cutoff is right for a finished version — except one that never got its
    announcement at all, which is the only way a card can end up with no air time to show."""
    look = schedule.needs_program_lookup
    rec = {"data": dict(LIVE_71_DATA)}
    assert look([], rec, NOW, "genshin") is True                 # cached -> worth one call
    assert look([], rec, NOW) is False                           # no game key -> old behaviour
    assert look([], {"data": dict(LIVE_71_DATA, version="9.9")}, NOW, "genshin") is False
    assert look([], {"data": dict(LIVE_71_DATA, program_ts=LIVESTREAM_TS)}, NOW, "genshin") is False
    assert look([], rec, NOW + 45 * 86400, "genshin") is False    # frozen card: never again
    # and once the single call has happened it stops, whatever it managed to find
    assert look([], {"data": dict(LIVE_71_DATA, media_from="X Post")}, NOW, "genshin") is False


def test_the_notice_cover_and_link_are_replaced_by_the_announcement():
    """All three visible faults are one assignment apart: apply_program_media overwrites the
    picture, the title link and the air time together, so the single recovered call fixes the
    whole card."""
    data, prov = dict(LIVE_71_DATA), {}
    changed = schedule.apply_program_media(data, prov, CACHED_HIT, NOW)
    assert "program_ts" in changed
    assert data["program_ts"] == LIVESTREAM_TS                               # the missing date
    assert data["images"] == ["https://pbs.twimg.com/media/HRlONCqXcAUhgGD.jpg?name=orig"]
    assert "upload-os-bbs" not in data["images"][0]                          # not the notice cover
    assert data["title_url"] == CACHED_HIT["url"]                            # not article 46791577
    assert "hoyolab.com/article/46791577" not in data["title_url"]
    assert data["media_from"] == "X Post"
    # re-running the identical recovery changes nothing further: re-verify, never re-edit
    again = dict(data)
    schedule.apply_program_media(again, dict(prov), CACHED_HIT, NOW)
    assert again == data


def test_every_live_version_has_a_cached_announcement():
    """The cache is the pattern that stops this recurring, so it has to actually be populated —
    an empty or malformed entry silently disables the recovery for that game."""
    from gamexpress.sources.twitter import program_seed
    for game_key, version in (("genshin", "7.1"), ("starrail", "4.6"),
                              ("zzz", "3.2"), ("wuwa", "3.7")):
        seed = program_seed(game_key, version)
        assert seed.get("id", "").isdigit(), f"{game_key} {version} has no cached tweet id"
        assert seed.get("url", "").startswith("https://x.com/"), f"{game_key} {version} url"
        assert seed.get("image", "").startswith("https://pbs.twimg.com/"), f"{game_key} {version} art"
    assert program_seed("genshin", "9.9") == {}          # an unknown version is simply absent
    assert program_seed("nosuchgame", "1.0") == {}


def test_every_game_treats_an_aired_programme_the_same_way():
    """One rule, four games. program_settled takes no game key and the creation gate has no
    per-game branch, so the four reference records must all answer identically: history once the
    programme is over, announceable while it is still ahead. Driven off the real sample data so
    a future version cannot drift away from the rule unnoticed."""
    from gamexpress.samples import SCHEDULE_SAMPLES
    assert set(SCHEDULE_SAMPLES) == {"genshin", "starrail", "zzz", "wuwa"}
    for key, sample in SCHEDULE_SAMPLES.items():
        aired, starts = int(sample["program_ts"]), int(sample["maint_start_ts"])
        assert schedule.program_settled(sample, aired - 3600) is False, f"{key}: airs in an hour"
        assert schedule.program_settled(sample, aired + 3600) is False, f"{key}: just aired"
        assert schedule.program_settled(sample, aired + 37 * 3600) is True, f"{key}: old news"
        # and the record a notice would have built instead — no program_ts, the 7.1 shape
        notice_built = {k: v for k, v in sample.items() if k != "program_ts"}
        assert schedule.program_settled(notice_built, starts - DAY) is False, f"{key}: pre-patch"
        assert schedule.program_settled(notice_built, starts + 13 * 3600) is True, f"{key}: shipped"


def test_a_card_with_no_recoverable_announcement_says_so():
    """The one unreachable state, in words: a notice-built record whose tweet id was never
    cached. Nothing runs for it, the lookup never fires, and before this the summary was silent
    — the card just kept the wrong cover and the wrong link for ever."""
    gap = schedule.announcement_gap
    live = {"data": dict(LIVE_71_DATA)}                      # no program_ts, maint 15 days past
    assert "no cached tweet id" in gap(live, "nosuchgame", NOW)     # nothing cached for it
    assert gap(live, "genshin", NOW) == ""                   # 7.1 IS cached -> the recovery runs
    assert gap({"data": dict(LIVE_71_DATA, program_ts=LIVESTREAM_TS)}, "nosuchgame", NOW) == ""
    assert gap({"data": dict(LIVE_71_DATA, program_seen=True)}, "nosuchgame", NOW) == ""
    assert gap({"data": dict(LIVE_71_DATA, media_from="X Post")}, "nosuchgame", NOW) == ""
    early = NOW - 20 * DAY + 6 * 3600                        # maintenance 6 h ago: window open
    assert gap({"data": dict(LIVE_71_DATA, maint_start_ts=early)}, "nosuchgame", early) == ""
    frozen = NOW - 60 * DAY                                  # frozen: nothing to show any more
    assert gap({"data": dict(LIVE_71_DATA, maint_start_ts=frozen)}, "nosuchgame", NOW) == ""
    assert gap({"data": {"version": "9.9"}}, "nogame", NOW) == ""   # no maintenance at all


# --------------------------------------------------------------------------- E. ZZZ 3.3 X channels (2026-10-09)
#
# The fourth banner fault, found the same day on the live ZZZ 3.3 card: the two channel tweets
# posted right after the Special Program read
#
#     V3.3 Limited-Time Channels (Phase I)
#     S-Rank Agent Phoenix Signal Search "Into the Flames of Life"
#     S-Rank Agent Velina Signal Search "Graceful Gale"
#
# In this dialect the quoted string is the CHANNEL's name and the bare word is the AGENT — the
# opposite of the Genshin epithet shape ("\"Epithet\" Name"). The bare-name reader did not know
# that "Signal Search" is prose that names the channel, so it rejected the whole run as too long
# and the quoted fallback published the channel title as the 5-star: the card showed
# "Into the Flames of Life, Graceful Gale" (Phase I) and "Answers in the Wind, Outlier of
# Prodigies" (Phase II), both 4★ lines TBA. The fix is the CHANNEL_MODE cut in bare_names() plus
# the same cut in the quoted fallback — for every game, not just ZZZ: the vocabulary is each
# game's own banner-mode phrase (event wish, event warp, signal search, convene, …).
#
# The fixtures are the real api.fxtwitter.com responses captured 2026-10-09 (Phase I:
# 2108524400761061839, Phase II: 2108524541022818638), plus the real HoYoLAB getPostFull bodies of
# the V3.2 channels notices (46604530 / 46863847) that use the SAME dialect on the news page and
# must keep reading exactly as before.

import json as _json  # noqa: E402

from gamexpress.config import load_games as _load_games  # noqa: E402
from gamexpress.sources.hoyolab import _post_text as _post_text  # noqa: E402
from gamexpress.sources.twitter import item_from_fx_json as _item_from_fx  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def _fx(name: str) -> dict:
    return _json.loads((FIX / name).read_text(encoding="utf-8"))


def _zzz33_tweet(name: str):
    return _item_from_fx("zzz", _fx(name))


def _hoyolab_item(name: str, pid: str):
    post = _fx(name)["data"]["post"]
    text, links, imgs = _post_text(post["post"])
    return Item("hoyolab", "zzz", pid, f"https://www.hoyolab.com/article/{pid}",
                post["post"]["subject"], text, int(post["post"]["created_at"]), imgs, links)


def test_zzz_x_channels_tweets_yield_agents_not_channel_titles():
    """The two real V3.3 channel tweets: Phoenix + Velina (Phase I), Severian + Norma (Phase II)."""
    game = _load_games()["zzz"]
    one = schedule.extract(game, _zzz33_tweet("fx_zzz_3_3_channels_phase1.json"))
    two = schedule.extract(game, _zzz33_tweet("fx_zzz_3_3_channels_phase2.json"))
    assert one.kind == "banner" and one.fields["banner_phase"] == 1
    assert one.fields["banner_five"] == ["Phoenix", "Velina"], one.fields["banner_five"]
    assert two.kind == "banner" and two.fields["banner_phase"] == 2
    assert two.fields["banner_five"] == ["Severian", "Norma"], two.fields["banner_five"]
    # the exact strings the live card was showing must be gone
    for wrong in ("Into the Flames of Life", "Graceful Gale",
                  "Answers in the Wind", "Outlier of Prodigies"):
        assert wrong not in one.fields["banner_five"] + two.fields["banner_five"], wrong


def test_zzz_hoyolab_channels_notices_keep_reading_bare_agents():
    """The same dialect on the HoYoLAB news page (V3.2, posts 46604530 / 46863847): the quoted
    string is still the channel and the bare word the agent — quoted channel titles must never
    be published, and the A-rank defaults must survive the new cuts."""
    one = schedule.extract_banner(_hoyolab_item("hoyolab_zzz_46604530_full.json", "46604530"))
    assert one["banner_five"] == ["Claret", "Nangong Yu"], one["banner_five"]
    assert one["banner_four"] == ["Anton", "Nicole"], one["banner_four"]
    assert one["banner_four_unsure"] is False and one["banner_phase"] == 1
    two = schedule.extract_banner(_hoyolab_item("hoyolab_zzz_46863847_full.json", "46863847"))
    assert two["banner_five"] == ["Roxy", "Promeia"], two["banner_five"]
    assert two["banner_four"] == ["Corin", "Billy"], two["banner_four"]
    assert two["banner_four_unsure"] is False and two["banner_phase"] == 2
    for wrong in ("Bloodmoon Rising", "Axiom of Captivation", "Cindernight Respite",
                  "Cold Rain Wanes in the Night", "Crimson Thirst", "Neon Fantasies"):
        assert wrong not in one["banner_five"] + one["banner_four"]
        assert wrong not in two["banner_five"] + two["banner_four"]


def test_channel_mode_phrase_never_publishes_a_channel_title():
    """Every game's mode phrase, in the shape its own posts use. None of these may publish the
    quoted channel title; the bare agent (or nothing, when the name is not readable) is all
    that may come out."""
    zzz = _load_games()["zzz"]
    cases = [
        # ZZZ X dialect: bare agent, then the mode phrase, then the quoted channel title
        ("S-Rank Agent Phoenix Signal Search \"Into the Flames of Life\"",
         ["Phoenix"]),
        # the same dialect with the agent quoted — the quote right after the tier phrase is the agent
        ("the S-Rank Agent \"Phoenix\" Signal Search \"Into the Flames of Life\" will receive a boost",
         ["Phoenix"]),
        # a name the bare reader cannot parse: the fallback must refuse the channel title too
        ("the S-Rank Agent phoenix signal search \"Into the Flames of Life\" will receive a boost",
         []),
        # Genshin: the channel title quoted after prose that names the mode
        ("the event-exclusive 5-star character Escoffier (Cryo) can be obtained from the "
         "\"Tasteful Excellence\" Event Wish", []),
        # Star Rail: the HSR 4.6 trap sentence, with the mode phrase spelled out
        ("the limited 5-star character Pearl (Elation: Ice) can only be obtained from the "
         "\"An Ocean in a Pearl\" Character Event Warp", []),
        # WuWa: the convene notice shape — bare names, no quotes at all
        ("During the event, 5-Star Resonator: Denia, 4-Star Resonators: Yangyang, Baizhi, "
         "and Sanhua receive boosted drop rates!", ["Denia"]),
    ]
    for text, want in cases:
        got = schedule.extract_banner(Item("x", zzz.key, "1", "u", "V3.3 Limited-Time Channels", text, 1))
        assert (got.get("banner_five") or []) == want, (text, got.get("banner_five"))


def test_zzz_and_wuwa_bare_lists_survive_the_channel_cut():
    """The channel-mode cut must not eat real bare lists: ZZZ's two default A-rank agents and
    WuWa's comma list read whole, and 'Topaz & Numby' stays one name."""
    assert schedule.bare_names(" Anton (Electric - Attack) & Nicole (Ether - Support) have "
                               "significantly boosted") == ["Anton", "Nicole"]
    assert schedule.bare_names(" Yangyang, Baizhi, and Sanhua receive boosted") == \
        ["Yangyang", "Baizhi", "Sanhua"]
    assert schedule.bare_names(" Topaz & Numby (Destruction: Fire), Bailu will receive") == \
        ["Topaz & Numby", "Bailu"]


def _zzz33_live_record() -> dict:
    """The record as the 2026-10-09 11:50 UTC run left it (state/state.json, shape verbatim):
    the two channel tweets had written their quoted CHANNEL TITLES as the 5-star agents."""
    return {
        "status": "posted",
        "message_id": "1556516427786096744",
        "payload_hash": "a3e77b5ac895951c",
        "posted_at": 1791172831,
        "first_seen": 1791172831,
        "updated_at": 1791546620,
        "prov": {
            "b_phase1": [40, 1791546366],
            "b_phase1_by": ["official"],
            "b_phase2": [40, 1791546399],
            "b_phase2_by": ["official"],
        },
        "data": {
            "version": "3.3",
            "banners": {
                "phase1": ["Into the Flames of Life", "Graceful Gale"],
                "phase2": ["Answers in the Wind", "Outlier of Prodigies"],
            },
            "banners_settled": ["phase1", "phase2"],
        },
    }


def test_the_wrong_zzz_33_banners_heal_from_the_same_posts():
    """The 11:50 UTC run wrote channel titles onto the live card. Re-reading the SAME two tweets
    (same source, same timestamps — still inside the 72 h lookback) through the fixed parser must
    rewrite both phases in place, while the settled lock stays exactly where it was."""
    game = _load_games()["zzz"]
    rec = _zzz33_live_record()
    extracts = [e for e in (schedule.extract(game, _zzz33_tweet(f)) for f in
                            ("fx_zzz_3_3_channels_phase1.json", "fx_zzz_3_3_channels_phase2.json"))
                if e]
    assert [e.kind for e in extracts] == ["banner", "banner"]
    notes: list[str] = []
    data = schedule.merge(game, "3.3", extracts, rec, {}, {}, 1791550000, notes)
    banners = data["banners"]
    assert banners["phase1"] == ["Phoenix", "Velina"], banners
    assert banners["phase2"] == ["Severian", "Norma"], banners
    assert data["banners_settled"] == ["phase1", "phase2"]          # the lock never moved
    assert rec["prov"]["b_phase1"] == [40, 1791546366]              # same post, re-read, not replaced
    assert rec["prov"]["b_phase2"] == [40, 1791546399]
    assert not notes                                                # the heal is silent, like any edit


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:                                  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)
