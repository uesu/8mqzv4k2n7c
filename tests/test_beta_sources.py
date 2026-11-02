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
