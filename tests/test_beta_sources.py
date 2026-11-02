"""Source selection for beta items: nanoka first, then Gachabase, then Encore (WuWa only)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamexpress.beta_sources import (  # noqa: E402
    SOURCE_ENCORE,
    SOURCE_GACHABASE,
    SOURCE_NANOKA,
    choose_source,
)


def test_nanoka_wins_when_it_has_data_even_if_gachabase_is_silent():
    choice = choose_source(nanoka={"v": 1}, gachabase=None, game="zzz")
    assert choice.source == SOURCE_NANOKA and choice.data == {"v": 1} and choice.note == ""


def test_gachabase_wins_when_nanoka_is_silent():
    choice = choose_source(nanoka=None, gachabase={"v": 2}, game="zzz")
    assert choice.source == SOURCE_GACHABASE and choice.data == {"v": 2}


def test_encore_is_only_used_for_wuwa_and_only_when_both_others_are_silent():
    assert choose_source(encore={"v": 3}, game="hsr").source == ""
    assert choose_source(encore={"v": 3}, game="ww", encore_applies=True).source == SOURCE_ENCORE
    # nanoka has data for the latest update: Encore's (still uploading) data never replaces it.
    late = choose_source(nanoka={"v": 1}, encore={"v": 9}, game="ww", encore_applies=True)
    assert late.source == SOURCE_NANOKA and late.data == {"v": 1}


def test_no_source_means_wait():
    choice = choose_source(nanoka=None, gachabase=None, encore=None, game="gi")
    assert choice.source == "" and choice.data is None


def test_matching_sources_post_without_a_note_and_mismatch_is_reported():
    same = choose_source(nanoka={"a": 1}, gachabase={"a": 1}, game="gi")
    assert same.source == SOURCE_NANOKA and same.note == ""
    differ = choose_source(nanoka={"a": 1}, gachabase={"a": 2}, game="gi")
    assert differ.source == SOURCE_NANOKA and "differ" in differ.note


def test_custom_comparison_is_used():
    loose = choose_source(nanoka={"a": 1.0}, gachabase={"a": 1}, game="gi",
                          same=lambda a, b: a == b)
    assert loose.note == "" and loose.source == SOURCE_NANOKA


def test_kit_agrees_only_when_every_gachabase_value_is_in_the_nanoka_kit():
    from gamexpress.beta_sources import kit_agrees

    nanoka = {"sections": [{"lines": ["1st-Hit DMG Multiplier **85.4%**", "Stagger 1,200"]}],
              "stats_lines": ["ATK 300"]}
    same = {"tables": [{"rows": [{"value": "85.4%"}, {"value": "1200"}]}]}
    off = {"tables": [{"rows": [{"value": "91.4%"}]}]}
    assert kit_agrees(nanoka, same)
    assert not kit_agrees(nanoka, off)
    assert not kit_agrees(nanoka, {"tables": []})     # nothing to compare is not agreement


def test_jufufu_output_file_matches_the_parser():
    """The committed Ju Fufu parser output is the parser's own result on the saved pages."""
    import json

    from gamexpress.gachabase_kit import parse_agent_page

    saved = json.loads((ROOT / "gamexpress" / "data" / "gachabase" / "zzz_jufufu.json")
                       .read_text(encoding="utf-8"))
    pages = ROOT / "tests" / "fixtures" / "gachabase"
    assert saved["locales"]["en"] == parse_agent_page((pages / "jufufu_en.html").read_text(encoding="utf-8"))
    assert saved["locales"]["chs"] == parse_agent_page((pages / "jufufu_chs.html").read_text(encoding="utf-8"))
    assert saved["locales"]["en"]["name"] == "Ju Fufu"
    assert saved["locales"]["chs"]["name"] == "橘福福"


def test_portrait_is_the_signed_asset_with_the_agents_own_name():
    from gamexpress.gachabase_kit import portrait_url

    page = ('<img alt="Ju Fufu" src="https://i.gachabase.net/SIG/q:50/f:webp/plain/zzz/assets/'
            'b2417de1e996acd49f8809edec5ace3d.png">'
            '<img alt="Yunkui Summit" src="https://i.gachabase.net/X/plain/zzz/assets/0155.png">'
            '<img alt="Ju Fufu" src="./local_files/b2417.webp">')
    assert portrait_url(page, "Ju Fufu").endswith("b2417de1e996acd49f8809edec5ace3d.png")
    assert portrait_url(page, "Nobody") == ""          # no image carries that name
    assert portrait_url('<img alt="Ju Fufu" src="https://cdn.gachabase.net/zzz/a.png">',
                        "Ju Fufu") == ""                # unsigned CDN path is not taken
    assert portrait_url("", "Ju Fufu") == ""


def test_encore_matching_nanoka_posts_and_mismatch_is_reported_with_nanoka():
    from gamexpress.beta_sources import kit_numbers_equal

    same_kit = {"sections": [{"lines": ["DMG 85.4%"]}]}
    other_kit = {"sections": [{"lines": ["DMG 91.4%"]}]}
    match = choose_source(nanoka=same_kit, encore={"sections": [{"lines": ["DMG 85.4%"]}]},
                          game="WuWa", encore_applies=True, same_encore=kit_numbers_equal)
    assert match.source == SOURCE_NANOKA and match.note == ""
    off = choose_source(nanoka=same_kit, encore=other_kit, game="WuWa", encore_applies=True,
                        same_encore=kit_numbers_equal)
    assert off.source == SOURCE_NANOKA and "Encore" in off.note
    # Encore is never consulted outside WuWa.
    zzz = choose_source(nanoka=same_kit, encore=other_kit, game="ZZZ", encore_applies=False,
                        same_encore=kit_numbers_equal)
    assert zzz.note == ""


def test_gachabase_unreachable_403_leaves_nanoka_to_win():
    """A 403 from Gachabase reads as no data, so nanoka wins with no note."""
    choice = choose_source(nanoka={"sections": [{"lines": ["x 1%"]}]}, gachabase=None,
                           game="ZZZ", same=lambda a, b: False)
    assert choice.source == SOURCE_NANOKA and choice.note == ""


def test_gachabase_outage_is_reported_once_and_not_retried_in_the_run():
    import asyncio
    import types

    from gamexpress import beta as beta_mod

    class Down:
        def __init__(self):
            self.calls = 0

        async def get_text(self, url, **_kw):
            self.calls += 1
            return None                      # 403 / empty answers read as None

    class Up:
        async def get_text(self, url, **_kw):
            return ('<title>Ju Fufu (Agents) | Zenless Zone Zero | Gachabase</title>'
                    '<table><tr><td>1st-Hit DMG Multiplier 1391001</td><td>85.4%</td></tr></table>')

    ctx = types.SimpleNamespace(fetcher=Down(), warnings=[])
    for item in ("1391", "1392", "1393"):
        assert asyncio.run(beta_mod._gachabase_kit(ctx, "zzz", item)) == {}
    assert ctx.fetcher.calls == 1                       # later characters skip the request
    assert len([w for w in ctx.warnings if "Gachabase gave no answer" in w]) == 1
    # Other games and WuWa never ask Gachabase at all.
    ctx2 = types.SimpleNamespace(fetcher=Down(), warnings=[])
    assert asyncio.run(beta_mod._gachabase_kit(ctx2, "ww", "1512")) == {}
    assert ctx2.fetcher.calls == 0
    # A readable page yields the tables and the portrait.
    ctx3 = types.SimpleNamespace(fetcher=Up(), warnings=[])
    kit = asyncio.run(beta_mod._gachabase_kit(ctx3, "zzz", "1391"))
    assert kit["name"] == "Ju Fufu" and kit["portrait"] == ""   # no skill heading or signed portrait here


if __name__ == "__main__":
    TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)


def test_wuwa_encore_is_second_and_gachabase_is_third_when_nanoka_is_silent():
    from gamexpress.beta_sources import SOURCE_ENCORE, SOURCE_GACHABASE, choose_source

    both = choose_source(nanoka=None, gachabase={"v": 2}, encore={"v": 3}, game="ww", encore_applies=True)
    assert both.source == SOURCE_ENCORE
    gb_only = choose_source(nanoka=None, gachabase={"v": 2}, encore=None, game="zzz")
    assert gb_only.source == SOURCE_GACHABASE


def test_test_named_heading_posts_with_a_placeholder_warning_and_real_headings_do_not():
    from gamexpress.beta import _test_placeholder_warning

    flagged = _test_placeholder_warning(["**[Robin Test Engine] *(R1)*:**", "    * **[NEW]** text"])
    assert flagged[0].startswith("-# ⚠️ This item's name") and "**[Robin Test Engine]" in flagged[1]
    plain = ["**[Concerto]** *(R1)*:", "    * **[NEW]** Real effect text."]
    assert _test_placeholder_warning(plain) == plain
