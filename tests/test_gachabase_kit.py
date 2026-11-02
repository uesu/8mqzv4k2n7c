"""Gachabase agent pages (Ju Fufu, EN and CHS) parse into the same skill tables."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamexpress.gachabase_kit import parse_agent_page  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "gachabase"


def _page(name: str) -> dict:
    return parse_agent_page((FIXTURES / name).read_text(encoding="utf-8"))


def test_en_page_name_and_flaming_claw_table():
    data = _page("jufufu_en.html")
    assert data["name"] == "Ju Fufu"
    first = data["tables"][0]
    assert first["title"] == "Basic Attack: Tiger Seven Forms - Flaming Claw"
    assert first["rows"][0] == {"label": "1st-Hit DMG Multiplier", "id": "1391001", "value": "85.4%"}
    assert first["rows"][1]["value"] == "91.4%"


def test_hidden_raw_value_tables_are_not_returned():
    for name in ("jufufu_en.html", "jufufu_chs.html"):
        for table in _page(name)["tables"]:
            assert all(not row["label"].isdigit() for row in table["rows"]), table["title"]


def test_chs_page_has_the_same_skill_structure():
    en = _page("jufufu_en.html")
    chs = _page("jufufu_chs.html")
    assert chs["name"] == "橘福福"
    assert len(chs["tables"]) == len(en["tables"])
    assert [len(t["rows"]) for t in chs["tables"]] == [len(t["rows"]) for t in en["tables"]]
    assert chs["tables"][0]["rows"][0]["value"] == "85.4%"
    assert chs["tables"][0]["rows"][0]["label"] == "一段伤害倍率"


def test_every_table_has_a_skill_title_and_numeric_values():
    for name in ("jufufu_en.html", "jufufu_chs.html"):
        tables = _page(name)["tables"]
        assert tables and all(table["title"] for table in tables)
        assert all(row["value"] for table in tables for row in table["rows"])


def test_empty_or_unrelated_html_parses_to_nothing():
    assert parse_agent_page("<html><title>X (Agents) | Y</title></html>") == {
        "name": "X", "tables": []}


def test_genshin_and_star_rail_pages_read_their_name_and_kit_text():
    from gamexpress.gachabase_kit import kit_built

    for name, fixture, expected in (
        ("Mitya", "mitya_en.html", "Normal Attack"),
        ("Aeon ★ Aha", "aha_en.html", "Basic ATK"),
    ):
        page = (FIXTURES / fixture).read_text(encoding="utf-8")
        assert parse_agent_page(page)["name"] == name
        built = kit_built(page)
        assert built and built["sections"], fixture
        titles = [s.title for s in built["sections"]]
        assert expected in titles, (fixture, titles[:6])
        assert all(s.tag == "[NEW]" for s in built["sections"])
        assert built["stats_lines"]


def test_a_page_without_kit_text_builds_nothing():
    from gamexpress.gachabase_kit import kit_built

    assert kit_built("<html><title>X (Agents) | Y</title></html>") is None


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
