"""Devvit relay intake reader (2026-11-02) — offline, no network, no secrets.

Run:  python tests/test_reddit_intake.py     (CI runs it next to test_smoke.py)

Records below are the first live relay records (owner screenshots, 2026-11-02 PHT):
1xlhem9 (gallery, x.com link with `\\_` escapes), 1xli6di (video + poster, truncated body),
1xlh3fw (text post with inline preview.redd.it images), 1xlkfrm (YouTube link post),
1xljyzj (.gif) and the leak-sub record 1xliq84 the old chain posted at 08:20 after the relay had
it at 08:15.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamexpress.config import Settings, load_games, load_settings  # noqa: E402
from gamexpress.http import Fetcher, SourceHealth  # noqa: E402
from gamexpress.sources import reddit_intake as ri  # noqa: E402
from gamexpress.sources.reddit import RedditClient, _is_official_subreddit_post  # noqa: E402

NOW = 1_793_583_400.0  # 2026-11-02 00:16:40Z, right after the screenshots


def _fence(rec: dict) -> str:
    return "```json\n" + json.dumps(rec, ensure_ascii=False) + "\n```"


def _rec(**kw) -> dict:
    base = {"v": 1, "id": "1xliq84", "sub": "Zenlesszonezeroleaks_",
            "title": "[3.4.2 Beta] Sunbringers' Form Selection Menu",
            "permalink": "/r/Zenlesszonezeroleaks_/comments/1xliq84/342_beta_sunbringers_form_selection_menu/",
            "url": "https://i.redd.it/rd8d9f59d3zh1.png", "created": 1793578392, "author": "adrigamer2950",
            "flair": "Reliable", "distinguished": None, "spoiler": False, "nsfw": False, "score": 20,
            "comments": 3, "removed": None, "xpost": None,
            "media": ["https://preview.redd.it/rd8d9f59d3zh1.png?auto=webp&s=75101ea0f3f82e170c0b2c483a92efd9160720dc"],
            "body": "yea, we'll be able to switch to any sunbringer in the overworld.", "bodyTruncated": False}
    base.update(kw)
    return base


def _msgs(records: list[dict], ages: list[int], noise: int = 0) -> list[dict]:
    out = [{"id": str(ri.snowflake_from_epoch(NOW - age)), "content": _fence(r)} for r, age in zip(records, ages)]
    out += [{"id": str(ri.snowflake_from_epoch(NOW - 3 - i)), "content": f"wick: member joined #{i}"}
            for i in range(noise)]
    return out


class _Fetcher:
    """Stands in for `Fetcher`: serves Discord pages newest-first, honours `before`."""

    def __init__(self, messages: list[dict] | None = None, fail: str | None = None,
                 files: dict[str, object] | None = None) -> None:
        self.messages = messages or []
        self.fail = fail
        self.files = files or {}          # 0.0.7: record.json attachments by CDN URL
        self.calls: list[tuple[str, dict, dict]] = []
        self.cdn_calls: list[str] = []
        self.health: dict[str, SourceHealth] = {}

    async def get_json(self, url, *, source, headers=None, params=None, retries=2, timeout=None):
        if source == "intake-cdn":
            self.cdn_calls.append(url)
            assert "Authorization" not in (headers or {}), "the bot token never goes to the CDN"
            return self.files.get(url)
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if self.fail:
            self.health[source] = SourceHealth(fail=1, last_error=self.fail)
            return None
        before = (params or {}).get("before")
        page = [m for m in self.messages if before is None or int(m["id"]) < int(before)]
        page.sort(key=lambda m: int(m["id"]), reverse=True)
        return page[:100]

    async def get_text(self, url, **kw):  # the old chain must never be reached in relay mode
        self.calls.append((url, {}, {}))
        return None


def _settings(**kw) -> Settings:
    s = load_settings({"DISCORD_BOT_TOKEN": "tok", "INTAKE_CHANNEL_ID": "1296308518470549606", **kw})
    return s


def _run(coro):
    return asyncio.run(coro)


def test_settings_defaults_and_env_wiring():
    s = load_settings({})
    assert (s.intake_reader, s.discord_bot_token, s.intake_channel_id) == ("primary", "", "")
    assert (s.intake_stale_seconds, s.intake_lookback_seconds) == (1800, 21600)
    s = load_settings({"INTAKE_READER": "Shadow", "INTAKE_CHANNEL_ID": " 1296308518470549606 ",
                       "INTAKE_STALE_SECONDS": "600", "INTAKE_LOOKBACK_SECONDS": "7200"})
    assert (s.intake_reader, s.intake_channel_id, s.intake_stale_seconds, s.intake_lookback_seconds) == \
        ("shadow", "1296308518470549606", 600, 7200)
    assert load_settings({"INTAKE_READER": "bogus"}).intake_reader == "primary"
    # GE_SECRETS_JSON / GE_VARS_JSON carry the two settings without any workflow edit
    s = load_settings({"GE_SECRETS_JSON": json.dumps({"DISCORD_BOT_TOKEN": "t"}),
                       "GE_VARS_JSON": json.dumps({"INTAKE_CHANNEL_ID": "1296308518470549606"})})
    assert s.discord_bot_token == "t" and s.intake_channel_id == "1296308518470549606"


def test_record_parsing_and_listing_shape():
    assert ri.parse_intake_record("hello") is None
    assert ri.parse_intake_record(_fence({"v": 2, "id": "abc"})) is None
    assert ri.parse_intake_record(_fence(_rec()))["id"] == "1xliq84"
    e = ri.record_to_entry(_rec())
    assert e["subreddit"] == "Zenlesszonezeroleaks_" and e["author"] == "adrigamer2950" and e["ts"] == 1793578392
    assert e["url"] == "https://www.reddit.com/r/Zenlesszonezeroleaks_/comments/1xliq84/342_beta_sunbringers_form_selection_menu/"
    assert e["images"] == ["https://i.redd.it/rd8d9f59d3zh1.png",
                           "https://preview.redd.it/rd8d9f59d3zh1.png?auto=webp&s=75101ea0f3f82e170c0b2c483a92efd9160720dc"]
    assert e["videos"] == [] and e["links"] == [] and e["comments"] == [] and e["stickied"] is False
    assert e["flair"] == "Reliable" and e["relay"] is True and e["body_truncated"] is False

    # 1xlhem9: gallery + x.com link with markdown escapes inside the bare URL
    gal = ri.record_to_entry(_rec(id="1xlhem9", sub="HonkaiStarRail", url="https://www.reddit.com/gallery/1xlhem9",
                                  permalink="/r/HonkaiStarRail/comments/1xlhem9/mydei_halloween_event_in_china/",
                                  media=["https://preview.redd.it/6ymlc2ap23zh1.jpeg?width=1706&s=b",
                                         "https://preview.redd.it/a5jn0lcp23zh1.jpeg?width=1290&s=d",
                                         "https://preview.redd.it/ks74vkdp23zh1.jpeg?width=2048&s=6"],
                                  body="https://x.com/minmyouk\\_xx/status/2113590961083429023?a=2RST8IJE8ot\\_RtI82vg&s=19"))
    assert len(gal["images"]) == 3 and gal["links"] == ["https://x.com/minmyouk_xx/status/2113590961083429023?a=2RST8IJE8ot_RtI82vg&s=19"]
    assert "minmyouk_xx" in gal["text"] and "\\_" not in gal["text"]

    # 1xli6di: v.redd.it video -> canonical CMAF_720 candidate (the muxed-audio proxy chain takes it
    # from there, as for every JSON/redlib entry); the external poster stays an image; body truncated
    vid = ri.record_to_entry(_rec(id="1xli6di", sub="Genshin_Impact", url="https://v.redd.it/awoc4x5w73zh1",
                                  permalink="/r/Genshin_Impact/comments/1xli6di/i_made_my_very_first_game/",
                                  media=["https://v.redd.it/awoc4x5w73zh1/CMAF_1080.mp4?source=fallback",
                                         "https://external-preview.redd.it/NHhv.png?format=pjpg&auto=webp&s=79d1"],
                                  body="Hi everyone! \\n\\n# How it works\\n\\n\\\\- Pick Pyro", bodyTruncated=True))
    assert vid["videos"] == ["https://v.redd.it/awoc4x5w73zh1/CMAF_720.mp4"]
    assert vid["images"] == ["https://external-preview.redd.it/NHhv.png?format=pjpg&auto=webp&s=79d1"]
    assert vid["links"] == [] and vid["body_truncated"] is True and vid["text"].startswith("Hi everyone!")

    # 1xlh3fw: text post, images pasted inline in the markdown body, media: []
    inl = ri.record_to_entry(_rec(id="1xlh3fw", sub="Genshin_Impact", media=[],
                                  url="https://www.reddit.com/r/Genshin_Impact/comments/1xlh3fw/i_wish/",
                                  permalink="/r/Genshin_Impact/comments/1xlh3fw/i_wish/",
                                  body="https://preview.redd.it/fg9aonrlz2zh1.png?width=1805&s=3f05\n\n"
                                       "https://preview.redd.it/d6jmman303zh1.png?width=1809&s=7efc\n\nhot take"))
    assert inl["images"] == ["https://preview.redd.it/fg9aonrlz2zh1.png?width=1805&s=3f05",
                             "https://preview.redd.it/d6jmman303zh1.png?width=1809&s=7efc"] and inl["links"] == []

    # 1xlkfrm: YouTube link post -> the destination is a link, the poster an image
    yt = ri.record_to_entry(_rec(id="1xlkfrm", sub="Genshin_Impact", url="https://youtu.be/6Lwi4vskUR0?is=N5kYk",
                                 permalink="/r/Genshin_Impact/comments/1xlkfrm/here_is/",
                                 media=["https://external-preview.redd.it/KTTh.jpeg?auto=webp&s=bde7"], body="fun fight"))
    assert yt["links"] == ["https://youtu.be/6Lwi4vskUR0?is=N5kYk"] and len(yt["images"]) == 1

    # 1xljyzj: .gif (i.redd.it destination, preview copy in media[])
    gif = ri.record_to_entry(_rec(id="1xljyzj", sub="ZZZ_Official", url="https://i.redd.it/gf8qf6o1o3zh1.gif",
                                  permalink="/r/ZZZ_Official/comments/1xljyzj/ohhh/",
                                  media=["https://preview.redd.it/gf8qf6o1o3zh1.gif?s=f1d9"], body=""))
    assert gif["images"] == ["https://i.redd.it/gf8qf6o1o3zh1.gif", "https://preview.redd.it/gf8qf6o1o3zh1.gif?s=f1d9"]
    assert gif["links"] == []

    # official-post rule unchanged: flair / distinguished / verified author
    hsr = load_games()["starrail"]
    off = ri.record_to_entry(_rec(id="1xljt2l", sub="HonkaiStarRail", author="Lumpy-Manager8580", flair="Official Media"))
    assert _is_official_subreddit_post(hsr, off)
    assert not _is_official_subreddit_post(hsr, ri.record_to_entry(_rec(id="1xlia06", sub="HonkaiStarRail",
                                                                        author="chunga-bunga69", flair="Meme / Fluff")))
    assert _is_official_subreddit_post(hsr, ri.record_to_entry(_rec(id="1xlzzzz", sub="HonkaiStarRail",
                                                                    author="HonkaiStarRail", flair="")))


def test_reader_statuses_paging_and_shared_channel():
    recs = [_rec(id=f"1xl{i:04d}", created=int(NOW) - 40 - i * 60) for i in range(3)]
    # healthy, with other bots chatting in the same channel: age comes from the newest RECORD
    f = _Fetcher(_msgs(recs, [40, 100, 160], noise=150))
    r = _run(ri.IntakeReader(f, _settings()).fetch(NOW))
    assert r["status"] == "ok" and r["newest_age"] == 40 and r["seen"] == 3 and len(r["records"]) == 3
    assert f.calls[0][0] == f"{ri.INTAKE_API}/channels/1296308518470549606/messages"
    assert f.calls[0][2]["Authorization"] == "Bot tok" and "before" not in f.calls[0][1]
    assert "tok" not in f.calls[0][0] and "tok" not in json.dumps(f.calls[0][1])
    # newest-first paging with `before`, bounded, stops at the lookback cutoff
    many = [_rec(id=f"1xm{i:04d}", created=int(NOW) - 40 - i) for i in range(230)]
    f = _Fetcher(_msgs(many, [40 + i for i in range(230)]))
    r = _run(ri.IntakeReader(f, _settings()).fetch(NOW))
    assert r["status"] == "ok" and len(r["records"]) == 230 and len(f.calls) == 3
    assert int(f.calls[2][1]["before"]) < int(f.calls[1][1]["before"])
    old = _msgs([_rec(id="1xoldd1")], [21600 + 600])
    f = _Fetcher(_msgs(recs, [40, 100, 160]) + old)
    r = _run(ri.IntakeReader(f, _settings()).fetch(NOW))
    assert len(r["records"]) == 3 and len(f.calls) == 1
    # relay silent while other bots chat -> stale; empty content (intent off) -> empty with the hint
    assert _run(ri.IntakeReader(_Fetcher(_msgs(recs, [5000, 5100, 5200], noise=20)), _settings()).fetch(NOW))["status"] == "stale"
    blank = _run(ri.IntakeReader(_Fetcher([{"id": m["id"], "content": ""} for m in _msgs(recs, [40, 100, 160])]),
                                 _settings()).fetch(NOW))
    assert blank["status"] == "empty" and "Message Content Intent" in blank["detail"]
    assert _run(ri.IntakeReader(_Fetcher([]), _settings()).fetch(NOW))["status"] == "empty"
    # HTTP errors map to the owner-readable hints; unconfigured / off never touch the network
    assert _run(ri.IntakeReader(_Fetcher(fail="HTTP 403"), _settings()).fetch(NOW))["status"] == "http_403"
    assert "Read Message History" in _run(ri.IntakeReader(_Fetcher(fail="HTTP 403"), _settings()).fetch(NOW))["detail"]
    f = _Fetcher()
    assert _run(ri.IntakeReader(f, load_settings({})).fetch(NOW))["status"] == "unconfigured" and not f.calls
    f = _Fetcher()
    assert _run(ri.IntakeReader(f, _settings(INTAKE_READER="off")).fetch(NOW))["status"] == "off" and not f.calls
    # a removed record never becomes a listing entry
    gone = _run(ri.IntakeReader(_Fetcher(_msgs([_rec(id="1xgone1", removed="moderator")], [40])), _settings()).fetch(NOW))
    assert gone["records"] == [] and gone["status"] == "ok"


def test_client_serves_relay_first_and_falls_back_per_target():
    recs = [
        _rec(id="1xliq84"),
        _rec(id="1xljt2l", sub="HonkaiStarRail", author="Lumpy-Manager8580", flair="Official Media",
             permalink="/r/HonkaiStarRail/comments/1xljt2l/aha/", created=int(NOW) - 300),
        _rec(id="1xlfast", sub="Genshin_Impact", author="genshinimpact", flair="Official Media",
             permalink="/r/Genshin_Impact/comments/1xlfast/v/", created=int(NOW) - 200,
             title="Version 7.3 Special Program Preview", body="Premieres on 2026-11-20 at 08:00 (UTC-4)!"),
    ]
    f = _Fetcher(_msgs(recs, [60, 300, 200], noise=10))
    s = _settings()
    rc = RedditClient(f, s)  # type: ignore[arg-type]
    rc.intake.now = NOW
    sub = _run(rc.subreddit("HonkaiStarRail"))
    assert sub and [e["id"] for e in sub] == ["1xljt2l"] and "r/honkaistarrail" in rc.reachable
    user = _run(rc.user("genshinimpact"))
    assert user and user[0]["id"] == "1xlfast" and "u/genshinimpact" in rc.reachable
    # one channel read for the whole run, zero redlib / reddit.com requests for served targets
    assert len([c for c in f.calls if "discord.com" in c[0]]) == 1
    assert all("discord.com" in c[0] for c in f.calls)
    # a target without records in the window falls back to the old chain (which the stub fails)
    assert _run(rc.user("AnantaOfficial")) is None
    assert any("discord.com" not in c[0] for c in f.calls)
    assert rc.intake.summary_line().startswith("📡 reddit intake: relay ok — 3 record(s)")
    assert "r/HonkaiStarRail" in rc.intake.summary_line() and "u/genshinimpact" in rc.intake.summary_line()

    # items(): games with a publisher account read `u/<account>`; the relay serves it from the
    # records authored by that account (its subreddit is polled too, so dormant accounts simply
    # have no record and fall back to the old chain exactly as before)
    f5 = _Fetcher(_msgs(recs + [_rec(id="1xlhsr1", sub="HonkaiStarRail", author="HonkaiStarRail",
                                     flair="Official Media", permalink="/r/HonkaiStarRail/comments/1xlhsr1/v/",
                                     created=int(NOW) - 120, title="Version 3.9 Special Program Preview")],
                        [60, 300, 200, 120]))
    rc5 = RedditClient(f5, _settings())  # type: ignore[arg-type]
    rc5.intake.now = NOW
    items = _run(rc5.items(load_games()["starrail"], lambda text: True, int(NOW) - 3600))
    assert [it.id for it in items] == ["1xlhsr1"] and items[0].official is True and items[0].source == "reddit"
    assert items[0].title == "Version 3.9 Special Program Preview" and items[0].author == "HonkaiStarRail"
    assert all("discord.com" in c[0] for c in f5.calls)   # zero redlib requests
    # WuWa has no publisher account: the subreddit listing + flair rule, served by the relay
    f6 = _Fetcher(_msgs([_rec(id="1xlwuwa", sub="WutheringWaves", author="WutheringWavesMod", flair="Official News",
                              permalink="/r/WutheringWaves/comments/1xlwuwa/v/", created=int(NOW) - 90,
                              title="Version 3.2 Special Broadcast")], [90]))
    rc6 = RedditClient(f6, _settings())  # type: ignore[arg-type]
    rc6.intake.now = NOW
    wuwa = _run(rc6.items(load_games()["wuwa"], lambda text: True, int(NOW) - 3600))
    assert [it.id for it in wuwa] == ["1xlwuwa"] and wuwa[0].official is True

    # shadow: reads, logs, but the old chain decides
    f2 = _Fetcher(_msgs(recs, [60, 300, 200]))
    rc2 = RedditClient(f2, _settings(INTAKE_READER="shadow"))  # type: ignore[arg-type]
    rc2.intake.now = NOW
    assert _run(rc2.subreddit("HonkaiStarRail")) is None
    assert "shadow" in rc2.intake.summary_line()
    # stale relay -> old chain, summary says why
    f3 = _Fetcher(_msgs(recs, [5000, 5300, 5200]))
    rc3 = RedditClient(f3, _settings())  # type: ignore[arg-type]
    rc3.intake.now = NOW
    assert _run(rc3.subreddit("HonkaiStarRail")) is None
    assert rc3.intake.summary_line().startswith("📡 reddit intake: relay stale")
    # unconfigured: identical to before the round — no Discord call, old chain, one summary line
    f4 = _Fetcher()
    rc4 = RedditClient(f4, load_settings({}))  # type: ignore[arg-type]
    assert _run(rc4.subreddit("HonkaiStarRail")) is None
    assert not any("discord.com" in c[0] for c in f4.calls)
    assert "unconfigured" in rc4.intake.summary_line()


def _update_msg(age: int, posts: list[dict]) -> dict:
    return {"id": str(ri.snowflake_from_epoch(NOW - age)),
            "content": "```json\n" + json.dumps({"v": 2, "kind": "update", "at": int(NOW - age), "posts": posts}) + "\n```"}


def _item(pid: str, **over) -> dict:
    base = {"id": pid, "sub": "HonkaiStarRail_leaks", "age": 640, "stage": 1, "removed": None, "deleted": False,
            "edited": False, "spoiler": False, "nsfw": False, "locked": False, "score": 3, "comments": 1}
    base.update(over)
    return base


_CDN = "https://cdn.discordapp.com/attachments/1/{}/record.json?ex=1&is=2&hm=3"


def _att(msg: dict, pid: str) -> dict:
    msg["attachments"] = [{"id": "9", "filename": "record.json", "url": _CDN.format(pid),
                           "content_type": "application/json"}]
    return msg


def test_relay_007_updates_attachments_and_edits():
    """Relay 0.0.7: v2 re-check messages, `record.json` completion, re-sent records after edits."""
    # -- update parser ------------------------------------------------------------------------
    upd = ri.parse_intake_update(_update_msg(30, [_item("1xl0000"), {"id": "bad id"}, _item("1xl0001", removed="moderator")])["content"])
    assert upd and [p["id"] for p in upd["posts"]] == ["1xl0000", "1xl0001"] and upd["posts"][1]["removed"] == "moderator"
    assert ri.parse_intake_update(_fence(_rec())) is None
    assert ri.parse_intake_record(_update_msg(30, [])["content"]) is None
    assert ri.parse_intake_update("```json\n{\"v\": 2, \"kind\": \"other\"}\n```") is None

    # -- re-checks: freshness, latest look wins, removed/deleted posts leave the listing ---------
    recs = [_rec(id=f"1xl000{i}", permalink=f"/r/HonkaiStarRail_leaks/comments/1xl000{i}/t/") for i in range(3)]
    msgs = ([_update_msg(20, [_item("1xl0000", stage=2, removed="moderator")]),
             _update_msg(200, [_item("1xl0000"), _item("1xl0001", deleted=True, removed="deleted")])]
            + _msgs(recs, [900, 960, 1020]))
    res = _run(ri.IntakeReader(_Fetcher(msgs), _settings()).fetch(NOW))
    assert res["status"] == "ok" and res["newest_age"] == 20, res
    assert res["update_msgs"] == 2 and res["updates"] == 2 and res["seen"] == 3
    assert [e["id"] for e in res["records"]] == ["1xl0002"], "removed (latest look) and deleted posts are not listed"
    # an initially removed record that the re-check later shows live IS listed (approved from the queue)
    approved = [_update_msg(20, [_item("1xl0000", stage=1)])] + _msgs([_rec(id="1xl0000", removed="automod_filtered",
                                                                           permalink="/r/HonkaiStarRail_leaks/comments/1xl0000/t/")], [900])
    assert [e["id"] for e in _run(ri.IntakeReader(_Fetcher(approved), _settings()).fetch(NOW))["records"]] == ["1xl0000"]
    # a channel holding only re-check messages is a live relay with nothing new (not stale)
    only_upd = _run(ri.IntakeReader(_Fetcher([_update_msg(20, [_item("1xl0000")])]), _settings()).fetch(NOW))
    assert only_upd["status"] == "empty" and only_upd["newest_age"] == 20 and only_upd["seen"] == 0
    assert only_upd["detail"].startswith("relay alive"), only_upd["detail"]

    # -- record.json completion -----------------------------------------------------------------
    cut = _rec(id="1xlcut1", permalink="/r/HonkaiStarRail_leaks/comments/1xlcut1/c/", url="https://www.reddit.com/gallery/1xlcut1",
               body="first part…", bodyTruncated=True, media=["https://preview.redd.it/a1.png?width=2048&s=1"],
               mediaTruncated=True, full=True)
    full = dict(cut, body="first part, then the whole code list " * 40, bodyTruncated=False,
                media=[f"https://preview.redd.it/a{i}.png?width=2048&s={i}" for i in range(1, 9)], mediaTruncated=False, parent=None)
    xp = _rec(id="1xlxpst", permalink="/r/HonkaiStarRail_leaks/comments/1xlxpst/x/",
              url="https://www.reddit.com/r/HonkaiStarRail/comments/1xlpar0/parent/", body="", media=[], xpost="t3_1xlpar0", full=True)
    parent = _rec(id="1xlpar0", sub="HonkaiStarRail", permalink="/r/HonkaiStarRail/comments/1xlpar0/parent/",
                  media=["https://i.redd.it/parent1.png", "https://i.redd.it/parent2.png"], body="parent body")
    plain = _rec(id="1xlplain", permalink="/r/HonkaiStarRail_leaks/comments/1xlplain/p/", full=True)
    cut2 = dict(cut, id="1xlcut2", permalink="/r/HonkaiStarRail_leaks/comments/1xlcut2/c/")
    cut3 = dict(cut, id="1xlcut3", permalink="/r/HonkaiStarRail_leaks/comments/1xlcut3/c/")
    msgs = _msgs([cut, xp, plain, cut2, cut3], [100, 200, 300, 400, 500])
    for m, r in zip(msgs, [cut, xp, plain, cut2, cut3]):
        _att(m, r["id"])
    f = _Fetcher(msgs, files={_CDN.format("1xlcut1"): full, _CDN.format("1xlxpst"): dict(xp, parent=parent, xpostPermalink=parent["permalink"]),
                              _CDN.format("1xlcut2"): dict(full, id="1xlother"), _CDN.format("1xlplain"): full})
    res = _run(ri.IntakeReader(f, _settings()).fetch(NOW))
    by = {e["id"]: e for e in res["records"]}
    assert res["completed"] == 2, res
    assert by["1xlcut1"]["body_truncated"] is False and len(by["1xlcut1"]["text"]) > 1000 and len(by["1xlcut1"]["images"]) == 8
    assert by["1xlxpst"]["crosspost_url"] == "https://www.reddit.com/r/HonkaiStarRail/comments/1xlpar0/parent/"
    assert by["1xlxpst"]["images"] == parent["media"], "a crosspost shows its parent's media"
    assert by["1xlcut2"]["body_truncated"] is True and by["1xlcut2"]["text"] == "first part…", "wrong-post attachment ignored"
    assert by["1xlcut3"]["body_truncated"] is True, "missing attachment -> inline record kept"
    assert sorted(f.cdn_calls) == sorted(_CDN.format(x) for x in ("1xlcut1", "1xlxpst", "1xlcut2", "1xlcut3")), "complete records cost no CDN request"
    many = _msgs([dict(cut, id=f"1xlcu{i:02d}", permalink=f"/r/HonkaiStarRail_leaks/comments/1xlcu{i:02d}/c/") for i in range(15)],
                 [10 + i for i in range(15)])
    for i, m in enumerate(many):
        _att(m, f"1xlcu{i:02d}")
    f2 = _Fetcher(many)
    _run(ri.IntakeReader(f2, _settings()).fetch(NOW))
    assert len(f2.cdn_calls) == ri.INTAKE_ATTACHMENT_FETCHES == 10

    # -- edits: the re-sent record (newer message, revision 2) supersedes ----------------------------
    e1 = _msgs([dict(plain, body="v1 body")], [600])[0]
    e2 = _msgs([dict(plain, body="v2 body (edited)", edited=True, revision=2)], [60])[0]
    ed = _run(ri.IntakeReader(_Fetcher([e2, e1]), _settings()).fetch(NOW))["records"]
    assert len(ed) == 1 and ed[0]["text"] == "v2 body (edited)" and ed[0]["edited"] is True

    # -- summary line mentions re-checks without ids --------------------------------------------------
    r = ri.IntakeReader(_Fetcher(msgs[:1] + [_update_msg(20, [_item("1xlcut1")])]), _settings())
    _run(r.fetch(NOW))
    line = r.summary_line()
    assert "re-checked" in line and "1xl" not in line, line


def test_relay_008_complete_records_need_no_resolve():
    """Relay 0.0.8 / round 104: a complete record needs no post-page resolve (redlib / reddit.com) —
    `items()` and `program_post()` alike. (The relay's record.json carries no comments; the
    owner dropped the publisher-comment follow-up.)"""
    inline = _rec(id="1xlofcl", sub="HonkaiStarRail", author="HonkaiStarRail", flair="Official Media",
                  permalink="/r/HonkaiStarRail/comments/1xlofcl/o/", created=int(NOW) - 100,
                  title="Version 3.9 Maintenance Notice", body="Servers go down at 06:00 (UTC+8).",
                  resent="approved", revision=2)
    plain = _rec(id="1xlplain", sub="HonkaiStarRail", author="HonkaiStarRail", flair="Official Media",
                 permalink="/r/HonkaiStarRail/comments/1xlplain/p/", created=int(NOW) - 200,
                 title="Version 3.9 Special Program Preview", body="")
    msgs = _msgs([inline, plain], [100, 200])
    _att(msgs[0], "1xlofcl")
    f = _Fetcher(msgs, files={_CDN.format("1xlofcl"): dict(inline, parent=None)})
    rc = RedditClient(f, _settings())  # type: ignore[arg-type]
    rc.intake.now = NOW
    res = _run(rc.intake.fetch(NOW))
    by = {e["id"]: e for e in res["records"]}
    assert res["completed"] == 0 and f.cdn_calls == [], "a re-sent complete record costs no CDN request"
    assert by["1xlofcl"]["relay_complete"] is True and by["1xlplain"]["relay_complete"] is True
    assert by["1xlofcl"]["comments"] == [] and by["1xlofcl"]["edited"] is False
    assert ri.record_to_entry(_rec(id="1xlcutx", bodyTruncated=True))["relay_complete"] is False
    assert ri.record_to_entry(_rec(id="1xlxpnp", xpost="t3_abc"))["relay_complete"] is False

    # items(): the empty-text complete record (title-only post) is NOT resolved through the old
    # chain — zero redlib / reddit.com requests for relay-served targets
    items = _run(rc.items(load_games()["starrail"], lambda text: True, int(NOW) - 3600))
    assert sorted(it.id for it in items) == ["1xlofcl", "1xlplain"], [it.id for it in items]
    assert all("discord.com" in c[0] for c in f.calls), [c[0] for c in f.calls if "discord.com" not in c[0]]
    # a cut record the CDN could not complete still takes the old-chain resolve (budgeted)
    f2 = _Fetcher(_msgs([dict(plain, body="", bodyTruncated=True)], [200]))
    rc2 = RedditClient(f2, _settings())  # type: ignore[arg-type]
    rc2.intake.now = NOW
    _run(rc2.items(load_games()["starrail"], lambda text: True, int(NOW) - 3600))
    assert any("discord.com" not in c[0] for c in f2.calls), "fallback resolve kept for cut records"

    # program_post(): a complete relay record with the announcement in its body needs no resolve
    prog = _rec(id="1xlprog", sub="HonkaiStarRail", author="HonkaiStarRail", flair="Official Media",
                permalink="/r/HonkaiStarRail/comments/1xlprog/v/", created=int(NOW) - 150,
                title="Version 3.9 Special Program Preview",
                body="The Version 3.9 Special Program premieres on 2026-11-20 at 08:00 (UTC-4)!")
    f3 = _Fetcher(_msgs([prog], [150]))
    rc3 = RedditClient(f3, _settings())  # type: ignore[arg-type]
    rc3.intake.now = NOW
    hit = _run(rc3.program_post(load_games()["starrail"], "3.9"))
    assert hit is not None and hit.id == "1xlprog", hit
    assert all("discord.com" in c[0] for c in f3.calls), [c[0] for c in f3.calls if "discord.com" not in c[0]]
    src = inspect.getsource(RedditClient.program_post) + inspect.getsource(RedditClient.items)
    assert src.count('e.get("relay_complete")') == 2 and "resolvable = not e.get" in src
    assert not hasattr(ri, "parse_op_comments") and "opComments" not in inspect.getsource(ri)


def test_round107_post_approved_from_the_mod_queue_hours_later_is_a_plain_new_item():
    """News-Express round 107 cross-check (live 1xlrj90, 2026-11-02): a post created 08:11Z sat in
    the mod queue until ~12:35Z; the relay listed it at 12:36Z (`removed: null`, score 1). In
    News-Express an archive hold from the old chain suppressed it. Game-Express keeps no hold
    memory for Reddit (a removed post is simply not an item), so the same record must be a plain
    new item — listing time is the message, `published_ts` stays the Reddit creation time, and
    nothing older than the `lookback_hours` window is consulted through the old chain."""
    created = int(NOW) - 5 * 3600 - 300                       # 5 h 05 min before the relay listed it
    late = _rec(id="1xlrj90", sub="HonkaiStarRail", author="HonkaiStarRail", flair="Official Media",
                permalink="/r/HonkaiStarRail/comments/1xlrj90/full_m0r1_sunbringer/", created=created,
                title="Version 3.9 Maintenance Notice", body="Servers go down at 06:00 (UTC+8).",
                score=1, comments=0, spoiler=True)
    f = _Fetcher(_msgs([late], [240]))                        # relay message 4 min old
    rc = RedditClient(f, _settings())  # type: ignore[arg-type]
    rc.intake.now = NOW
    res = _run(rc.intake.fetch(NOW))
    assert [e["id"] for e in res["records"]] == ["1xlrj90"], "a fresh relay message lists an old post"
    assert res["records"][0]["ts"] == created and res["records"][0]["relay_complete"] is True
    items = _run(rc.items(load_games()["starrail"], lambda text: True, int(NOW) - 72 * 3600))
    assert [it.id for it in items] == ["1xlrj90"] and items[0].published_ts == created
    assert all("discord.com" in c[0] for c in f.calls), [c[0] for c in f.calls if "discord.com" not in c[0]]
    # outside the lookback window the item is dropped by `since_ts` alone (no memory, no probe)
    assert _run(rc.items(load_games()["starrail"], lambda text: True, int(NOW) - 3600)) == []
    # a record the relay itself reports removed is not an item; an approval re-check lists it again
    rc_gone = RedditClient(_Fetcher(_msgs([dict(late, removed="moderator")], [240])), _settings())  # type: ignore[arg-type]
    rc_gone.intake.now = NOW
    assert _run(rc_gone.intake.fetch(NOW))["records"] == [] and _run(rc_gone.intake.fetch(NOW))["seen"] == 1
    from gamexpress.sources import reddit as rmod
    src = inspect.getsource(rmod)
    assert "removal_notice" not in src and "pending_reddit" not in src and not hasattr(rc, "pending"), \
        "Game-Express must stay memory-free here: a hold store would need the round-107 restore rule"


def test_wiring_is_in_place():
    from gamexpress import runner
    from gamexpress.sources import reddit as rmod
    src = inspect.getsource(rmod.RedditClient._gather_listing)
    assert src.index("await self.intake.listing(kind, target)") < src.index("feed_q = format_reddit_feed_query")
    assert "reddit.intake.summary_line()" in inspect.getsource(runner.run_once)
    assert isinstance(Fetcher.get_json, object) and time.time() > 0


def main() -> int:
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print("REDDIT INTAKE TEST:", "ALL PASS" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
