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
