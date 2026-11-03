"""Reddit use cases for Game-Express, end to end and offline (2026-11-03).

Covers every place Game-Express reads Reddit, in the new order (Devvit relay first, the old
redlib / reddit.com / Arctic chain as the automatic fallback), plus the fully-offline case:

  * schedule-card backup source            -> items()
  * Special Program detection              -> program_post()
  * community-code corroboration           -> community_codes()
  * single-post resolution                 -> resolve_post() (only for cut / empty records)

Run:  python tests/test_reddit_usecases.py     (CI runs it next to test_reddit_intake.py)
No network, no secrets. Helpers are shared with tests/test_reddit_intake.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_reddit_intake import (  # noqa: E402
    NOW,
    _Fetcher,
    _msgs,
    _rec,
    _run,
    _settings,
)

from gamexpress.config import load_games, load_settings  # noqa: E402
from gamexpress.http import SourceHealth  # noqa: E402
from gamexpress.sources.reddit import RedditClient  # noqa: E402

GAMES = load_games()


class _FleetDead(_Fetcher):
    """The relay answers normally (Discord + CDN); every old-chain request fails (RSS gone, redlib dark)."""

    async def get_json(self, url, *, source, headers=None, params=None, retries=2, timeout=None):
        if "discord.com" in url or source == "intake-cdn":
            return await super().get_json(url, source=source, headers=headers, params=params,
                                          retries=retries, timeout=timeout)
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        self.health[source] = SourceHealth(fail=1, last_error="HTTP 503")
        return None

    async def get_text(self, url, **kw):
        self.calls.append((url, {}, {}))
        return None


def _fleet_calls(f: _Fetcher) -> list[str]:
    """Every request that went to the old chain (anything that is not the Discord channel)."""
    return [c[0] for c in f.calls if "discord.com" not in c[0]]


def _client(f: _Fetcher, **env) -> RedditClient:
    rc = RedditClient(f, _settings(**env))  # type: ignore[arg-type]
    rc.intake.now = NOW
    return rc


def _window(hours: float = 1) -> int:
    return int(NOW - hours * 3600)


# ---------------------------------------------------------------- 1. relay healthy, all four uses

def test_uc1_relay_serves_all_four_uses_with_zero_fleet_requests():
    """Healthy relay with records for the subs and accounts: every use is served from it."""
    recs = [
        # schedule backup: an official account post (genshinimpact) about the next Special Program
        _rec(id="1xuc001", sub="Genshin_Impact", author="genshinimpact", flair="Official Media",
             permalink="/r/Genshin_Impact/comments/1xuc001/v73/", created=int(NOW) - 600,
             title="Version 7.3 Special Program Preview",
             body="Premieres on 2026-11-20 at 08:00 (UTC-4)! Redeem code: GENSHINGIFT2026 for rewards."),
        # community codes: a sub post carrying a redeem code (no official author needed)
        _rec(id="1xuc002", sub="Genshin_Impact", author="someone", flair="Leak",
             permalink="/r/Genshin_Impact/comments/1xuc002/codes/", created=int(NOW) - 900,
             title="New codes", body="Code: ZZXXYYWW9988 (EU server)"),
    ]
    f = _Fetcher(_msgs(recs, [600, 900], noise=5))
    rc = _client(f)
    items = _run(rc.items(GAMES["genshin"], lambda text: True, _window()))
    assert [it.id for it in items] == ["1xuc001"], items
    assert items[0].official is True and items[0].author == "genshinimpact"
    codes = _run(rc.community_codes("Genshin_Impact"))
    assert codes is not None and {c.code for c in codes} >= {"ZZXXYYWW9988"}, codes
    pp = _run(rc.program_post(GAMES["genshin"], "7.3"))
    assert pp is not None and pp.id == "1xuc001", pp
    assert _fleet_calls(f) == [], f"relay served everything, the old chain must not run: {_fleet_calls(f)}"
    assert "relay ok" in rc.intake.summary_line()


# ---------------------------------------------------------------- 2. fully offline (the 13 Nov case)

def test_uc2_fully_offline_relay_and_old_chain_dead_never_crashes_and_posts_nothing_from_reddit():
    """Relay unreachable AND every old-chain request fails (RSS gone, redlib dark, Arctic down)."""
    f = _Fetcher(fail="HTTP 503")
    rc = _client(f)
    assert _run(rc.items(GAMES["genshin"], lambda text: True, _window())) == []
    assert _run(rc.program_post(GAMES["genshin"], "7.3")) is None
    assert _run(rc.community_codes("Genshin_Impact")) is None
    assert _run(rc.subreddit("Genshin_Impact")) is None and _run(rc.user("genshinimpact")) is None
    assert _run(rc.resolve_post("Genshin_Impact", "1xuc001")) is None
    # the fallback really ran (it is not silently skipped), and the step summary says why
    assert _fleet_calls(f), "the old chain must be attempted when the relay is down"
    assert rc.intake.summary_line() == "📡 reddit intake: relay http_503 (HTTP 503) — redlib/reddit.com chain used"


def test_uc2b_offline_leaves_official_sources_unaffected_by_reddit():
    """items() of a Reddit-only game returns [] (no Reddit backup), it does not raise."""
    f = _Fetcher(fail="HTTP 503")
    rc = _client(f)
    assert _run(rc.items(GAMES["wuwa"], lambda text: True, _window())) == []


# ---------------------------------------------------------------- 3. every non-ok state falls back

def test_uc3_each_non_ok_state_falls_back_with_one_reason_line():
    cases = [
        ("unconfigured", None, None),
        ("http_401", _Fetcher(fail="HTTP 401"), "bot token rejected"),
        ("http_403", _Fetcher(fail="HTTP 403"), "View Channel"),
        ("http_404", _Fetcher(fail="HTTP 404"), "channel id unknown"),
        ("http_429", _Fetcher(fail="HTTP 429"), "rate limited"),
        ("no_messages", _Fetcher([]), "no messages"),
        ("no_relay_records", _Fetcher(_msgs([], [], noise=12)), "none is a relay record"),
        ("stale", _Fetcher(_msgs([_rec(id="1xucst1")], [5000])), "newest relay record is"),
    ]
    for label, fetcher, reason in cases:
        if fetcher is None:
            rc = RedditClient(_Fetcher(), load_settings({}))  # type: ignore[arg-type]
            assert _run(rc.subreddit("Genshin_Impact")) is None, label
            assert "unconfigured" in rc.intake.summary_line(), label
            continue
        rc = _client(fetcher)
        assert _run(rc.subreddit("Genshin_Impact")) is None, label
        line = rc.intake.summary_line()
        assert line.startswith("📡 reddit intake:"), (label, line)
        if reason:
            assert reason in line, (label, line)
        assert _fleet_calls(fetcher), f"{label}: old chain must run"


def test_uc3b_fresh_bot_chatter_never_vouches_for_a_silent_relay():
    """A fresh non-relay bot message must not make a stale relay look healthy."""
    recs = [_rec(id="1xucold")]
    msgs = _msgs(recs, [5000], noise=3)  # noise is fresh (NOW-3..), the relay record is 5000 s old
    rc = _client(_Fetcher(msgs))
    assert _run(rc.subreddit("Genshin_Impact")) is None
    assert rc.intake.summary_line().startswith("📡 reddit intake: relay stale")


def test_uc3c_mode_off_never_reads_the_channel():
    f = _Fetcher(_msgs([_rec(id="1xucoff")], [60]))
    rc = _client(f, INTAKE_READER="off")
    assert _run(rc.subreddit("Genshin_Impact")) is None
    assert not any("discord.com" in c[0] for c in f.calls)


# ---------------------------------------------------------------- 4. removal and single-post resolve

def test_uc4_removed_and_deleted_posts_never_reach_the_schedule_or_codes():
    recs = [
        _rec(id="1xucrm1", sub="Genshin_Impact", author="genshinimpact", flair="Official Media",
             permalink="/r/Genshin_Impact/comments/1xucrm1/x/", created=int(NOW) - 200,
             title="Version 7.3 Special Program Preview", body="Premieres soon", removed="removed_by_moderator"),
    ]
    rc = _client(_Fetcher(_msgs(recs, [200])))
    assert _run(rc.user("genshinimpact")) is None  # only a removed record: relay alive, no entry
    assert "relay" in rc.intake.summary_line()


def test_uc5_cut_record_with_dead_old_chain_posts_the_relay_text_and_does_not_crash():
    """A body cut by the relay that record.json could not complete: the post still goes through the
    normal pipeline with the text it has (resolve_post fails, so nothing is invented)."""
    cut = _rec(id="1xuccut", sub="Genshin_Impact", author="genshinimpact", flair="Official Media",
               permalink="/r/Genshin_Impact/comments/1xuccut/x/", created=int(NOW) - 300,
               title="Version 7.3 Special Program Preview", body="Premieres on 2026-11-20 at 08:00 (UTC-4)! Redeem",
               bodyTruncated=True)
    f = _Fetcher(_msgs([cut], [300]))
    rc = _client(f)
    items = _run(rc.items(GAMES["genshin"], lambda text: True, _window()))
    assert [it.id for it in items] == ["1xuccut"]
    # the body is exactly what the relay sent: the cut text is kept, nothing is invented
    assert items[0].text == "Premieres on 2026-11-20 at 08:00 (UTC-4)! Redeem"
    # a record that already has text is not re-resolved by items(); the old chain stays idle here
    assert _fleet_calls(f) == []


def test_uc5b_empty_cut_record_with_dead_old_chain_posts_title_only_and_tries_the_fallback():
    """Title-only relay record (body cut to nothing, no record.json): items() tries the old chain for
    the body; it is dead, so the post goes through with the title alone and no invented text."""
    cut = _rec(id="1xucemp", sub="Genshin_Impact", author="genshinimpact", flair="Official Media",
               permalink="/r/Genshin_Impact/comments/1xucemp/x/", created=int(NOW) - 300,
               title="Version 7.3 Special Program Preview", body="", bodyTruncated=True)
    f = _FleetDead(_msgs([cut], [300]))
    rc = _client(f)
    items = _run(rc.items(GAMES["genshin"], lambda text: True, _window()))
    assert [it.id for it in items] == ["1xucemp"] and items[0].text == ""
    assert items[0].title == "Version 7.3 Special Program Preview"
    assert _fleet_calls(f), "the body lookup must have been attempted on the old chain"


def test_uc6_complete_record_never_calls_resolve_even_when_old_chain_is_alive():
    """relay_complete records are the whole post: no redlib / proxy / Arctic lookup at all."""
    full = _rec(id="1xucful", sub="Genshin_Impact", author="genshinimpact", flair="Official Media",
                permalink="/r/Genshin_Impact/comments/1xucful/x/", created=int(NOW) - 300,
                title="Version 7.3 Special Program Preview", body="Premieres on 2026-11-20 at 08:00 (UTC-4)!")
    f = _Fetcher(_msgs([full], [300]))
    rc = _client(f)
    items = _run(rc.items(GAMES["genshin"], lambda text: True, _window()))
    assert [it.id for it in items] == ["1xucful"]
    assert _fleet_calls(f) == []


# ---------------------------------------------------------------- 5. per-target fallback and paging

def test_uc7_per_target_fallback_serves_what_the_relay_has_and_only_that():
    recs = [_rec(id="1xucsub", sub="Genshin_Impact", author="someone", flair="Leak",
                 permalink="/r/Genshin_Impact/comments/1xucsub/x/", created=int(NOW) - 100)]
    f = _Fetcher(_msgs(recs, [100]))
    rc = _client(f)
    assert _run(rc.subreddit("Genshin_Impact"))  # served by the relay
    before = len(_fleet_calls(f))
    assert _run(rc.subreddit("ZZZ_Official_NoRecords")) is None  # no record -> old chain
    assert len(_fleet_calls(f)) > before


def test_uc8_records_older_than_the_lookback_window_are_ignored():
    """Lookback is 6 h: a 7 h old record is outside the window, so the target falls back."""
    old = [_rec(id="1xucold7", sub="Genshin_Impact", created=int(NOW) - 7 * 3600)]
    # one fresh non-relay message keeps the channel "alive"; the only relay record is 7 h old
    rc = _client(_Fetcher(_msgs(old, [7 * 3600], noise=1)))
    assert _run(rc.subreddit("Genshin_Impact")) is None


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
    print("REDDIT USE CASES TEST:", "ALL PASS" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
