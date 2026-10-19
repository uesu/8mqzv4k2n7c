"""A version page that names a SLOT instead of a character must never reach a card.

Regression: ZZZ 3.3 posted "Agent" as the phase-1, phase-2 and re-run line-up on 2026-10-06.

Run:   python tests/test_gachawiki_placeholder.py     (plain runner, what CI uses)
   or: python -m pytest -q tests/test_gachawiki_placeholder.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamexpress.sources.gachawiki import build_lineup, publishable_name  # noqa: E402

PLACEHOLDER_CHANNELS = """\
{{Upcoming}}
{{Version
|version     = 3.3
|date        = October 21, 2026
}}
==Confirmed Details==
===Playable Agents===
* {{Intro/Agent/Full|Phoenix Reffaella}}
* {{Intro/Agent/Full|Severian Lowell}}

===Signal Searches===
====Exclusive Channels====
* Phase 1:
** [[Exclusive Channel]] (Agent)

* Phase 2:
** [[Exclusive Channel]] (Agent)
"""

REAL_CHANNELS = """\
{{Version
|version     = 3.2
|date        = September 9, 2026
}}
==Confirmed Details==
===Playable Agents===
* {{Intro/Agent/Full|Claret Flint}}
* {{Intro/Agent/Full|Roxy Ifrita Pryce}}

===Signal Searches===
====Exclusive Channels====
* Phase 1:
** [[Bloodmoon Rising/2026-09-09|Bloodmoon Rising]] (Claret)
** [[Axiom of Captivation/2026-09-09|Axiom of Captivation]] ([[Nangong Yu]])

* Phase 2:
** [[Cindernight Respite/2026-09-30|Cindernight Respite]] (Roxy)
** [[Cold Rain Wanes in the Night/2026-09-30|Cold Rain Wanes in the Night]] ([[Promeia]])
"""


def test_slot_words_are_not_names():
    for word in ("Agent", "agents", "Character", "Resonator", "TBA", "Unknown Agent", "???"):
        assert not publishable_name(word), word
    for name in ("Phoenix Reffaella", "Claret", "Soldier 11", "Jane Doe", "Nangong Yu"):
        assert publishable_name(name), name


def test_placeholder_channels_fall_back_to_the_debut_roster():
    """No 'Agent' anywhere, and the names the page DOES state still reach the card."""
    out = build_lineup("zzz", PLACEHOLDER_CHANNELS, {}, four_star_count=2)
    assert out == {"confirmed": ["Phoenix Reffaella", "Severian Lowell"]}
    assert "Agent" not in str(out)


def test_real_channels_are_unaffected():
    out = build_lineup("zzz", REAL_CHANNELS, {}, four_star_count=2)
    assert out["phase1"] == ["Claret", "Nangong Yu"]
    assert out["phase2"] == ["Roxy", "Promeia"]
    assert out["reruns"] == ["Nangong Yu", "Promeia"]


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
