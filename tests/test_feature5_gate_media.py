"""Feature 5 rules: a Gachabase card needs a complete kit, and media follows the source order.

- A Gachabase-built card posts only when its kit is complete (name, stat block, and text for each
  required group). An incomplete kit is never posted: the item waits.
- Gachabase's kit parser stops at the page's navigation, so UI labels never count as kit text.
- WuWa: Encore's own image URLs fill the card after nanoka's art (only absolute image URLs that
  Encore publishes; no guessed paths). Every candidate is verified before use.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from test_smoke import GAMES, _beta_ctx  # noqa: E402

from gamexpress import beta as beta_mod  # noqa: E402
from gamexpress.gachabase_kit import kit_built, kit_complete  # noqa: E402


def _kit(name: str) -> dict:
    return kit_built((FIXTURES / name).read_text(encoding="utf-8"))

FIXTURES = Path(__file__).parent / "fixtures" / "gachabase"


def test_complete_kits_pass_and_incomplete_kits_fail():
    assert kit_complete(_kit("mitya_en.html"), "gi")
    assert kit_complete(_kit("jufufu_en.html"), "zzz")
    # Aha's page shows no Constellation text: incomplete, so it is not posted.
    assert not kit_complete(_kit("aha_en.html"), "hsr")
    assert not kit_complete(None, "gi")
    assert not kit_complete(_kit("mitya_en.html"), "ww")      # WuWa is never a Gachabase kit


def test_kit_text_stops_at_navigation_so_ui_labels_are_not_kit_text():
    aha = _kit("aha_en.html")
    kit_text = " ".join(line for s in aha["sections"] for line in s.lines)
    assert "Description Display" not in kit_text
    assert "Show Ruby Text" not in kit_text
    assert all(s.category != "Constellations" or not s.lines for s in aha["sections"])


def _page(body: str) -> str:
    return f"<html><head><title>Ju Fufu | Gachabase</title></head><body>{body}</body></html>"


def test_incomplete_gachabase_kit_is_not_posted_and_the_item_waits():
    # Talents heading with a title and no body text, and no stat block: incomplete.
    page = _page("<h2>Talents</h2><p>Basic Attack</p>")
    manifest = {"zzz": {"latest": "3.4.2", "live": "3.3.1", "available": ["3.3.1", "3.4.2"],
                        "new": {"character": [1391], "weapon": [], "relic": [], "bangboo": []}}}

    class Fetcher:
        async def get_json(self, url, **_kw):
            return manifest if url.endswith("/manifest.json") else None

        async def get_text(self, url, **_kw):
            return page if "/agents/1391/" in url else ""

        async def verify_media(self, _url):
            return False

    with tempfile.TemporaryDirectory() as tmp:
        ctx = _beta_ctx(Path(tmp) / "state.json", Fetcher(), games=["zzz"])
        patch = asyncio.run(beta_mod.build_patch(ctx, GAMES["zzz"], manifest, beta_mod._budget(ctx)))
        assert patch is not None and not patch.characters
        assert any("incomplete" in w and "not posted" in w for w in ctx.warnings), ctx.warnings
        assert not any("built from Gachabase" in w for w in ctx.warnings)


def test_encore_character_images_come_from_its_named_image_fields_in_order():
    """Encore's character detail names its absolute image URLs (checked against the live API)."""
    payload = {
        "Id": 1512, "Name": {"Title": "x", "Content": "Lily"},
        "RolePortrait": "https://api.encore.moe/resource/Data/Game/Aki/UI/P.webp",
        "RoleStand": "https://api.encore.moe/resource/Data/Game/Aki/UI/S.webp",
        "RoleHeadIcon": "https://api.encore.moe/resource/Data/Game/Aki/UI/H.webp",
        "Model3D": [{"Model": "https://s3-storage-api.encore.moe/x.uemodel"}],
        "Icon": "/Game/Aki/UI/UIResources/Common/Image/IconRup/T_IconRup_1402_UI.T_IconRup_1402_UI",
    }
    urls = beta_mod._encore_image_candidates(payload, "character")
    assert urls == ["https://api.encore.moe/resource/Data/Game/Aki/UI/P.webp",
                    "https://api.encore.moe/resource/Data/Game/Aki/UI/S.webp",
                    "https://api.encore.moe/resource/Data/Game/Aki/UI/H.webp"]
    assert beta_mod._data_image_candidates("ww", "character", "1512", {"_encore": payload}) == urls
    # No Encore image fields: no candidates (the card posts without an image).
    assert beta_mod._data_image_candidates("ww", "character", "1512", {"_encore": {"Id": 1512}}) == []


def test_encore_weapon_icon_paths_become_the_host_webp_only_for_game_paths():
    payload = {"ItemId": 21030076, "Icon": "/Game/Aki/UI/UIResources/Common/Image/IconWeapon/"
                                            "T_IconWeapon21030056_UI.T_IconWeapon21030056_UI",
               "IconBig": ""}
    assert beta_mod._encore_image_candidates(payload, "weapon") == [
        "https://api.encore.moe/resource/Data/Game/Aki/UI/UIResources/Common/Image/IconWeapon/"
        "T_IconWeapon21030056_UI.webp"]
    assert beta_mod._encore_asset_url("https://example.org/x.webp") == ""
    assert beta_mod._encore_asset_url("") == ""




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
