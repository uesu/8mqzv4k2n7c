"""Game-Express — automated game version-schedule announcements + redemption-code poster.

Posts rich Discord Components V2 cards (buttons nested inside the card) for
HoYoverse games (Genshin Impact, Honkai: Star Rail, Zenless Zone Zero, Honkai:
Nexus Anima), Wuthering Waves, and NetEase's ANANTA.
"""

import os


def build_id() -> str:
    """Which code is running, for the run summary and `validate`.

    There is deliberately no hand-maintained version number. It used to be
    `__version__` here, and bumping it meant four files had to move in lockstep
    (this one, the changelog, the changelog index, the README summary) — miss
    one and the repo contradicted itself, which is exactly the kind of stale
    fact that keeps getting cleaned up. Actions already knows the commit, so
    the build identifies itself and can never drift out of date.

    Outside Actions there is no commit to report, hence "local".
    """
    return (os.getenv("GITHUB_SHA") or "")[:7] or "local"
