"""Version-schedule announcements: detect → extract → merge → post once → keep the card accurate.

Posting only happens on a keyword/pattern match (special program / special broadcast /
livestream announcement, or an official update-maintenance notice) — never daily.

Accuracy rules:
  * official posts (HoYoLAB / official X / Kuro) and config/overrides.json always win;
    countdown and learned-cadence fallbacks are visibly labelled estimated.
  * unknown values render as TBA; banners always carry (STC).
  * one card per game+version; later official info (maintenance notice, pre-install,
    banner notice, overrides edits) EDITS the same message silently (no re-ping).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from urllib.parse import urlparse

from .cards import ESTIMATE_LABELS, mark_test, schedule_payload
from .config import Game, Ping
from .discord import webhook_fingerprint
from .media import rank, youtube_thumb
from .models import Item
from .sources import calendarfeed, gachawiki
from .sources.bannerfeed import banner_feed_for
from .sources.codes import extract_codes_from_text
from .state import stable_hash
from .textutil import (
    bump_minor,
    find_version,
    find_version_name,
    sentences,
    twitch_url,
    version_key,
    youtube_video_url,
)
from .timeparse import find_datetimes, find_duration_hours, find_time_ranges, parse_iso

log = logging.getLogger("gamexpress.schedule")

MAINT_RE = re.compile(
    r"update\s+(?:and\s+)?maintenance|maintenance\s+(?:notice|preview|details|announcement|time)|"
    r"update\s+details|version\s+update\s+(?:notice|details)|pre-?install(?:ation)?|pre-?download|pre-?load",
    re.I)
PROGRAM_EXCLUDE = re.compile(
    r"\b(recap|replay|re-?watch|highlights?|vod|thank(?:s| you) for (?:watching|tuning)|has ended|"
    r"codes? (?:are|is) (?:now )?(?:live|available|here))\b", re.I)
NOT_PROGRAM_TITLE = re.compile(
    r"\b(?:(?:prize|sharing|fan[- ]?art|creation|community|login|check-?in|web|in-?game|"
    r"time-?limited|lucky|anniversary|redeem(?:ption)?)\s+events?|event\s+prizes?|participate|"
    r"winners?|giveaway|redemption\s+cod(?:e|es)|update\s+(?:details|and\s+maintenance)|"
    r"maintenance\s+notice|version\s+update|hotfix|bug\s+fix(?:es)?|compensation\s+"
    r"(?:notice|details)|merchandise|recap|replay|vod|survey|questionnaire|thank\s+you|"
    r"collaboration|insider\s+channel|combat\s+intro|character\s+demo)\b", re.I)
DEFAULT_BANNER_RE = (r"event\s+wish|event\s+warp|signal\s+search|exclusive\s+channel|featured\s+resonator|"
                     r"resonator\s+convene|character\s+event|limited[-\s]time\s+(?:character|agent)")

# --------------------------------------------------------------------- X-first announcement test
# X is the PRIMARY schedule source, so this test has to be tight. One positive requirement does
# most of the work: an announcement says WHEN it airs. That is what separates a real announcement
# from the posts that merely mention one —
#   "Insider Channel: Special Program | Signs of Imprisonment: Part Three"  -> no air time (lore)
#   "[Prize Event] ... the Version 3.2 Special Program will air on August 28" -> prize-event title
#   "Follow @Wuthering_Waves and repost. 10 winners will be chosen..."        -> giveaway INSIDE the
#        real 3.7 announcement, so the giveaway words must not be allowed to veto it
PROGRAM_AIR = re.compile(
    r"\b(?:will\s+(?:premiere|begin|air|release|start|go\s+live|be\s+broadcast|be\s+available)|"
    r"is\s+scheduled\s+to\s+air|scheduled\s+for|airing\s+on|premieres?\s+on|"
    r"livestream\s+starts?|tune\s+in\s+(?:on|at)|starts?\s+(?:on|at)\b)", re.I)
AIR_DATE = re.compile(
    r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2}|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}", re.I)
# Only the HEADLINE is screened for "this is some other kind of post". Every real announcement
# quotes its own giveaways and redemption codes in the body, so screening the whole text would
# reject all four of them (GI drops codes, WW runs a gift-card draw, ZZZ drops a code).
HEADLINE_LEN = 160


def is_program_announcement(game: Game, text: str, title: str = "") -> bool:
    """Strict positive test for 'this post IS the version's Special Program announcement'.

    Three things must all hold: the game's own phrase for its livestream, a stated air time, and
    a headline that is not some other kind of post. Verified against the four real 2026-09
    announcements (GI 7.1, HSR 4.6, ZZZ 3.2, WW 3.7) and against the three posts that used to
    steal the card's link."""
    body = text or ""
    if not _any(program_patterns(game), f"{title}\n{body}"):
        return False
    if title and NOT_PROGRAM_TITLE.search(title):
        return False
    if NOT_PROGRAM_TITLE.search(body[:HEADLINE_LEN]):
        return False
    if PROGRAM_EXCLUDE.search(body):
        return False
    return bool(PROGRAM_AIR.search(body)) and bool(AIR_DATE.search(body))


PRE_WORDS = re.compile(r"pre-?install|pre-?download|pre-?installation|pre-?load", re.I)
MAINT_WORDS = re.compile(r"maintenance|update\s+time|update\s+schedule|^begins?\s+at|^starts?\s+at|downtime", re.I)
MAINT_EXCLUDE = re.compile(
    r"compensation|claim|eligib|reached|reach\s|purchase|top-?up|expire|event\s+(?:period|duration)|"
    r"will\s+end|ends?\s+at|until|closed|deadline|reset", re.I)
COMP_RE = re.compile(r"(?:Maintenance\s+)?Compensation\s*[:：]\s*([^\n(（]{3,80})", re.I)
FIVE_RE = re.compile(r"(?:5[-\s]?star|5\s?★|★\s?5|S[-\s]?Rank)\s+(?:limited[-\s]?(?:time\s+)?)?(?:event[-\s]exclusive\s+)?"
                     r"(?:character|resonator|agent)s?", re.I)
FOUR_RE = re.compile(r"(?:4[-\s]?star|4\s?★|★\s?4|A[-\s]?Rank)\s+(?:character|resonator|agent)s?", re.I)
QUOTED = re.compile(r"[\"“「『]([^\"”」』\n]{2,40})[\"”」』]")
PHASE1 = re.compile(r"phase\s*(?:I|1)\b(?!I)|first\s+(?:half|phase)|1st\s+(?:half|phase)", re.I)
PHASE2 = re.compile(r"phase\s*(?:II|2)\b|second\s+(?:half|phase)|2nd\s+(?:half|phase)", re.I)

# HoYoverse notices name CHARACTERS bare and put WEAPONS in quotes:
#     the limited 5-star character Pearl (Elation: Ice) and the limited 5-star Light Cone
#     "Colors for Tomorrow (Elation)" will be boosted ...
#     the 4-star characters Qingque (Erudition: Quantum), Xueyi (Destruction: Quantum), and
#     Misha (Destruction: Ice), as well as the 4-star Light Cones "Post-Op Conversation ..."
# Until 2026-09-27 extract_banner read ONLY the quoted names, so HSR 4.6 shipped with phase1 =
# ["Celestial Invitation", "An Ocean in a Pearl", "The Demoiselle in Charge"] -- three banner
# TITLES -- while the real 5-stars (Pearl, Evanescia) and every 4-star were dropped, and
# phase1_4 held three 4-star LIGHT CONES. Source: HoYoLAB post 46851682, "Version 4.6 Event
# Warp: Phase I". The plural matters: `light\s+cone\b` never matched "Light Cones", so the
# guard that was supposed to cut the weapon clause off did not fire at all.
TIER_STOP = re.compile(
    r"\b(?:light\s+cones?|weapons?|w-engines?|as\s+well\s+as|will\s+(?:be|return|receive)|"
    r"and\s+the\s+limited|drop[-\s]rates?|receives?|significantly|(?:have|has)\s+(?:significantly\s+)?boosted)\b",
    re.I)                       # \"Sanhua receive boosted\" (WuWa), \"Nicole have significantly boosted\" (ZZZ)
NAME_SPLIT = re.compile(r",\s*(?:and\s+)?|\s+and\s+|\s*;\s*")
# A star-tier phrase is often followed by an article before the name ("5-star character \"Pearl\"",
# "…and the 4-star characters …"), and truncating at the NEXT tier leaves a dangling "the". Those
# pass plausible_name(), so they have to be dropped here rather than trusted as a name.
NAME_FILLER = {"the", "a", "an", "and", "as", "of", "for", "will", "is", "are", "its", "both"}
QUOTED_FIRST = re.compile(r"\s*[\"“「『]")
OPEN_QUOTE = re.compile(r"[\"“「『]")
# A wish notice TITLES the banner and then names the character:
#     the event-exclusive 5-star character "Tasteful Excellence" Escoffier (Cryo)
#     the 4-star characters "Ode and Oblation" Dahlia (Hydro), "Golden Vow" Candace (Hydro), ...
# The quoted span is the BANNER's name; the character is the bare word straight after it. Because
# the quote comes first, QUOTED_FIRST used to claim the whole clause and the real name was thrown
# away -- Genshin 7.1 shipped phase2 = ["Tasteful Excellence"] and phase2_4 = ["Ode and Oblation",
# "Golden Vow", "Coordinates of Clear Frost"] instead of Escoffier / Dahlia, Candace, Mika
# (HoYoLAB 47010361, seen live 2026-10-08). Those strings pass plausible_name(), so no amount of
# name screening can catch them; only the SHAPE of the sentence distinguishes them.
# A closing quote followed immediately by a capitalised bare word is always this shape -- when the
# quotes really do hold the character, what follows is punctuation, a conjunction, or prose.
EPITHET_NAME = re.compile(
    r"[\"“「『][^\"”」』\n]{2,40}[\"”」』]\s+([A-Z][\w'’.\-]*(?:[ •·&]+[A-Z0-9][\w'’.\-]*){0,3})")
# A name list follows the star-tier phrase IMMEDIATELY. When prose comes first, the quoted span
# that follows belongs to a DIFFERENT clause -- and in a HoYoverse notice that clause names a
# BANNER, not a character:
#     ※ obtainable 5-star characters include the featured 5-star characters and the
#       custom-selected characters from "Celestial Invitation."
#     ※ the limited 5-star character Pearl (Elation: Ice) can only be obtained from the
#       "An Ocean in a Pearl" Character Event Warp
# Both sentences made extract_banner return banner TITLES as 5-stars on the real post 46851682
# (verified 2026-09-27 against getPostFull: five = Pearl, Evanescia, Celestial Invitation,
# An Ocean in a Pearl). Because the hub knows those strings as banner titles, the cross-check in
# apply_banner_feed then replaced the whole phase on EVERY run -- so phase1 was carried by the
# community feed at priority 5 instead of by the official notice at 50, and in the feed's order.
# The quoted fallback is therefore only trusted when nothing but a name can sit between the
# star-tier phrase and the opening quote.
PROSE_BETWEEN = re.compile(
    r"\b(?:can|could|will|would|shall|may|is|are|was|were|be|been|being|include[sd]?|including|"
    r"obtain(?:s|ed|able)?|only|from|during|that|which|who|such|boosted|returns?|available|"
    r"features?|featured|following|below|above)\b", re.I)
# The gacha-mode phrase names the CHANNEL (the banner), never the character:
#     S-Rank Agent Phoenix Signal Search "Into the Flames of Life"
# Phoenix is the agent; "Into the Flames of Life" is the channel. ZZZ posts its channels on X in
# exactly this shape (V3.3, 2026-10-09: tweets 2108524400761061839 / 2108524541022818638), and the
# first 3.3 run read the quoted CHANNEL TITLE as the 5-star agent — the card showed "Into the
# Flames of Life, Graceful Gale" instead of Phoenix, Velina. Two consequences, both game-wide:
# a bare name ends where the mode phrase begins, and a quote that follows a mode phrase is a
# channel title, never a character. The vocabulary is the games' own (config/games.json
# `banner_patterns`): event wish(es), event warp / character event, signal search / exclusive
# channel(s) / limited-time channels, featured resonator / resonator / weapon convene.
CHANNEL_MODE = re.compile(
    r"\b(?:signal\s+search|exclusive\s+channels?|exclusive\s+rescreening|limited[-\s]time\s+channels?|"
    r"event\s+wishes?|event\s+warp|character\s+event|featured\s+resonators?|resonator\s+convenes?|"
    r"weapon\s+convenes?|convenes?)\b", re.I)


def _cut_channel_mode(run: str) -> str:
    """Cut `run` where the gacha-mode phrase begins — but only OUTSIDE quotes. A quoted span that
    FOLLOWS the mode phrase is the CHANNEL's name ('Agent "Phoenix" Signal Search "Into the
    Flames of Life"': the first quote holds the agent, the second the channel), while a quoted
    span that merely CONTAINS a mode word ("Event Wish" fed as a name) is a name, not prose,
    and must survive the cut."""
    spans = [(m.start(), m.end()) for m in QUOTED.finditer(run)]
    for m in CHANNEL_MODE.finditer(run):
        if not any(m.start() < e and s < m.end() for s, e in spans):
            return run[: m.start()]
    return run


# Every word of a real character name is capitalised or starts with a digit ("Ben Bigger",
# "Dan Heng • Imbibitor Lunae", "March 7th", "Topaz & Numby"). A bare run is raw prose, so a
# trailing clause TIER_STOP does not know ("... character Pearl debuts soon.") would otherwise
# pass plausible_name() and be posted as a 5-star -- a wrong name where the quoted-only reader
# used to post nothing. Quoted names are exempt: the quotes are the author saying where the
# name ends, so this rule is applied ONLY to bare runs.
NAME_CAPPED = re.compile(r"[A-Z0-9]")


def bare_names(tail: str) -> list[str]:
    """Character names written WITHOUT quotes after a star-tier phrase, in order."""
    run = TIER_STOP.split(tail, maxsplit=1)[0]
    # A bare name ends where the channel begins: 'S-Rank Agent Phoenix Signal Search "Into the
    # Flames of Life"' names the agent Phoenix and the channel "Into the Flames of Life" — reading
    # past the mode phrase would glue the quoted channel title onto the name and reject it whole
    # (ZZZ 3.3, 2026-10-09). The same cut at the first opening quote keeps a quoted span out of a
    # bare name ("Agent Phoenix "Into the Flames of Life" will ...").
    run = _cut_channel_mode(run)
    q = OPEN_QUOTE.search(run)
    if q:
        run = run[: q.start()]
    # ZZZ writes two default agents as "Anton (Electric - Attack) & Nicole (Ether - Support)". A
    # ')' before the '&' means two annotated names, so it is split. "Topaz & Numby" has no ')'
    # and stays one name.
    run = re.sub(r"\)\s*&\s*", "), ", run)
    out: list[str] = []
    for piece in NAME_SPLIT.split(run):
        n = _clean_name(piece)
        if not n or n.lower() in NAME_FILLER or n in out:
            continue
        if not all(NAME_CAPPED.match(w) for w in re.split(r"[\s•·&:]+", n) if w):
            continue                                # prose tail, not a name
        out.append(n)
    return out

# HoYoverse notices label their numbers with bracketed section headers and put the VALUE ON THE
# NEXT LINE, so a sentence scanner sees "2026-09-09 06:00 (UTC+8) : We estimate this will take five
# hours." with no "maintenance" anywhere in it and throws the time away. Read label+value as a pair.
# Reference: ZZZ 3.2 (HoYoLAB 46604333) -- "[Version Update Time]" / "[Pre-Download Period]".
LABELLED = re.compile(
    r"[\[【〓]?\s*(pre-?download\s+period|pre-?install(?:ation)?\s+(?:period|time)|"
    r"version\s+update\s+time|update\s+maintenance\s+(?:time|information)|maintenance\s+time)"
    r"\s*[\]】〓]?\s*[:：]?\s*([^\n]{0,140})", re.I)
# "[Pre-Installation Details]" is followed by storage sizes, not a clock -- never read those.
LABEL_NO_TIME = re.compile(r"storage|space|GB|file\s+size|download\s+size", re.I)

# precedence of sources for a field (higher wins). Derived values are below external countdown
# estimates; the banner feed is the lowest (5). Any official source or override replaces both.
PRIORITY = {"override": 100, "hoyolab": 50, "kuro": 50, "news": 45, "x": 40, "launcher": 20,
            "countdown": 10, "pattern": 9, "gachawiki": 6, "bannerfeed": 5}
# Which program post may HOLD the card's link and key art (the lock). X first: its post carries the
# announcement's own URL and its photo at full size. HoYoLAB and the official sites hold the card
# when X cannot be read (nitter and the tweet-data services all down). A card locked to one of
# them is upgraded once X shows the same announcement -- see _upgrade_announcement().
LOCK_RANK = {"x": 3, "hoyolab": 2, "kuro": 2, "news": 1}
ESTIMATED_KEYS = ("program_ts", "preinstall_ts", "maint_start_ts", "maint_end_ts")
MAINT_HOURS_ESTIMATE = 5          # typical HoYoverse / Kuro maintenance window
WIKI_RECHECK_H = 6                  # a complete banner block with unconfirmed names is re-read this often
WIKI_INCOMPLETE_H = 3               # a banner block that still has TBA is re-read this often (not every run)
CARD_FREEZE_D = 45                 # after this many days past maintenance a card is never edited
PROGRAM_FRESH_H = 36              # how long after the air time a program still counts as news

# Cold-start pre-install leads, measured from maintenance START. Once this installation has seen
# a real pre-install + maintenance pair for a game, observed_lead_h() supplies that game's median
# instead. A derived value is never recorded as an observation, so the fallback cannot teach itself.
# Measured over 10-18 published versions per game, not over the single latest one: a lead taken
# from one version happens to pick up that version's slip. Modal value, with the count that agreed.
PREINSTALL_LEAD_H = {
    "genshin": 43,                 # Mon 11:00 -> Wed 06:00 (UTC+8); 10 of 10 versions identical
    "starrail": 40,                # Mon 14:00 -> Wed 06:00 (UTC+8);  8 of 11 (4.6 alone was 88 h)
    "zzz": 42,                     # Mon 12:00 -> Wed 06:00 (UTC+8);  9 of 10
    "wuwa": 42,                    # Tue 10:00 -> Thu 04:00 (UTC+8); 17 of 18
}
MAX_LEAD_H = 14 * 24               # reject corrupt/outlier observations (e.g. a mis-parsed 700 h)

# Speculation guard rails. A predicted cycle is only believable near the present: anything already
# past, or further out than one-and-a-bit cycles, is dropped rather than shown to readers.
SPECULATE_MIN_HISTORY = 2          # real maintenance dates needed before the shipped cadence is replaced
SPECULATE_MAX_AHEAD_D = 120        # same horizon apply_estimates() allows for countdown sites
CADENCE_MIN_D, CADENCE_MAX_D = 14, 120   # plausible gap between two versions of a live service
# State is long-lived and hand-editable, so a stored timestamp can be anything. Values outside a
# plausible calendar window are ignored rather than fed to datetime.fromtimestamp(), which raises
# ValueError past year 9999 -- and an exception here would abort the whole schedule run.
TS_MIN, TS_MAX = 1_000_000_000, 4_102_444_800        # 2001-09-09 .. 2100-01-01


@dataclass
class Extract:
    item: Item
    kind: str                         # program | maintenance | banner
    version: str | None
    version_inferred: bool = False
    fields: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- classification
def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns if p)


def program_patterns(game: Game) -> list[str]:
    return [re.escape(p) if not any(ch in p for ch in "\\[]()?*+|") else p for p in game.program_patterns]


def wants(game: Game):
    """Combined pre-filter used before fetching full posts (schedule + codes share it)."""
    pats = program_patterns(game) + [MAINT_RE.pattern, DEFAULT_BANNER_RE] + game.banner_patterns + \
        [r"redemption\s+codes?|redeem\s+codes?|\bcodes?\s*[:：]"]
    rx = re.compile("|".join(f"(?:{p})" for p in pats), re.I)
    return lambda text: bool(rx.search(text or ""))


def classify(game: Game, item: Item) -> str | None:
    lead = f"{item.title}\n{item.text[:400]}"
    if MAINT_RE.search(item.title or "") or (item.source == "x" and MAINT_RE.search(lead)):
        return "maintenance"
    if _any(program_patterns(game), lead) and not PROGRAM_EXCLUDE.search(lead) \
            and not NOT_PROGRAM_TITLE.search(item.title or "") \
            and not extract_codes_from_text(lead):      # a codes post is not an announcement
        return "program"
    banner_rx = "|".join([DEFAULT_BANNER_RE] + game.banner_patterns)
    if re.search(banner_rx, item.title or item.text[:200], re.I):
        return "banner"
    if MAINT_RE.search(lead):
        return "maintenance"
    return None


# --------------------------------------------------------------------------- extraction
def extract_program(game: Game, item: Item) -> dict:
    text = item.full
    ref = item.published_ts or None
    prog_rx = re.compile("|".join(program_patterns(game) + [r"premiere", r"\bair\b", r"livestream", r"broadcast",
                                                           r"will (?:begin|release|start)"]), re.I)
    ts = None
    for s in sentences(text):
        if prog_rx.search(s):
            found = find_datetimes(s, ref)
            if found:
                ts = found[0].ts
                break
    head = item.title if item.title else item.text[:240]
    if ts is None and _any(program_patterns(game), head):
        found = find_datetimes(text, ref)            # the post IS the announcement
        ts = found[0].ts if found else None
    if ts and item.published_ts and not (item.published_ts - 86400 <= ts <= item.published_ts + 45 * 86400):
        ts = None  # a date far from the post date is not this program's air time
    f: dict = {}
    if ts:
        f["program_ts"] = ts
    m = re.search(r"special\s+broadcast|special\s+program|livestream|showcase", text, re.I)
    if m:
        f["program_name"] = " ".join(w.capitalize() for w in m.group(0).split())
    yt = youtube_video_url(item.links, text)
    if yt:
        f["youtube_video"] = yt
    tw = twitch_url(item.links, text)
    if tw:
        f["twitch_live"] = tw
    if item.images:
        f["images"] = item.images[:4]
    return f


def extract_labelled(text: str, ref: int | None) -> dict:
    """Times that sit UNDER a bracketed section header instead of inside their own sentence.

    ZZZ 3.2 (HoYoLAB 46604333) is the case that motivated this: its maintenance start and its
    whole pre-download window are label/value pairs, so the sentence scanner saw neither and the
    card showed TBA for both even though the official notice stated them plainly.
    """
    f: dict = {}
    for m in LABELLED.finditer(text):
        label, value = m.group(1).lower(), m.group(2)
        if LABEL_NO_TIME.search(value):
            continue
        if label.startswith("pre-"):
            rngs = find_time_ranges(value, ref)
            if rngs:
                f.setdefault("preinstall_ts", rngs[0][0])
                continue
            found = find_datetimes(value, ref)
            if found:
                f.setdefault("preinstall_ts", found[0].ts)
        else:
            found = find_datetimes(value, ref)
            if found:
                f.setdefault("maint_start_ts", found[0].ts)
    return f


def extract_maintenance(item: Item) -> dict:
    text = item.full
    ref = item.published_ts or None
    f: dict = extract_labelled(text, ref)   # labelled values win; the loop only setdefaults
    for s in sentences(text):
        rngs = find_time_ranges(s, ref)
        if rngs and (MAINT_WORDS.search(s) or "maintenance" in s.lower()) and not MAINT_EXCLUDE.search(s):
            f.setdefault("maint_start_ts", rngs[0][0])
            f.setdefault("maint_end_ts", rngs[0][1])
            continue
        found = find_datetimes(s, ref)
        if not found:
            continue
        if PRE_WORDS.search(s) and not re.search(r"\b(?:end|ends|until|close|closes)\b", s, re.I):
            f.setdefault("preinstall_ts", found[0].ts)
            continue
        if MAINT_WORDS.search(s) and not MAINT_EXCLUDE.search(s):
            f.setdefault("maint_start_ts", found[0].ts)
            if len(found) > 1 and found[1].ts > found[0].ts and \
                    re.search(r"[-–~]|\bto\b|\buntil\b", s[found[0].end:found[1].start]):
                f.setdefault("maint_end_ts", found[1].ts)
    if "maint_start_ts" in f and "maint_end_ts" not in f:
        hours = find_duration_hours(text)
        if hours:
            f["maint_end_ts"] = int(f["maint_start_ts"] + hours * 3600)
    # HoYoverse posts the maintenance PREVIEW at the moment pre-install opens, so when the body
    # says "pre-installation ... is now available" the post's own timestamp IS the pre-install
    # time. Verified on GI 7.1 (HoYoLAB 46771203): created_at 1789960208, exactly 608 s after the
    # 11:00 UTC+8 open that game8 lists.
    # NOTE the phrase must be searched in title AND body. `item.title or text[:200]` short-circuits
    # to the TITLE ALONE whenever a title exists, and titles like "Version 7.1 Update Maintenance
    # Preview" never contain "pre-install" -- that one `or` was the whole gap.
    if "preinstall_ts" not in f and item.published_ts and \
            PRE_WORDS.search(f"{item.title or ''}\n{text[:400]}") and \
            re.search(r"now\s+(?:available|open)|has\s+(?:begun|started)|is\s+now\s+live", text, re.I):
        f["preinstall_ts"] = int(item.published_ts)      # "Pre-installation is now available"
    m = COMP_RE.search(text)
    if m:
        comp = m.group(1).strip(" .;,")
        if re.search(r"\d", comp):
            f["compensation"] = comp
    if item.images:
        f["notice_images"] = item.images[:1]
    return f


def _clean_name(raw: str) -> str:
    name = re.sub(r"\s*[\(（][^)）]*[\)）]\s*", "", raw).strip(" .,;:!")
    return name if 1 < len(name) <= 40 and not re.search(r"https?://|\d{3,}", name) else ""


# words that never appear in a character name — a "name" containing one is an extraction slip
NOT_A_NAME = {
    "character", "characters", "resonator", "resonators", "agent", "agents", "banner", "banners",
    "wish", "wishes", "warp", "warps", "convene", "convenes", "signal", "search", "event", "events",
    "weapon", "weapons", "cone", "cones", "w-engine", "w-engines", "engine", "version", "phase", "half",
    "tba", "tbd", "unknown", "rerun", "re-run", "reruns", "boosted", "rate", "rates", "drop", "limited",
    "star", "stars", "rank", "s-rank", "a-rank", "exclusive", "featured", "promotional", "reward", "rewards",
    "update", "maintenance", "livestream", "program", "broadcast", "http", "https", "www",
}
_NAME_CHARS = re.compile(r"[^\W\d_][\w'’.\-&•·: ]*")


def plausible_name(name: str) -> bool:
    """True for something that can be a character name: starts with a letter, ≤ 32 chars,
    1-6 words, letters/digits/space/'-.&•·: only, no 3+ digit runs, no generic words
    ('Event', 'Wish', 'Character' …). 'March 7th', 'Soldier 11', 'Topaz & Numby',
    'Dan Heng • Imbibitor Lunae' pass; 'Event Wish', '2026', 'Character Event' don't."""
    if not name or len(name) > 32 or re.search(r"\d{3,}", name) or not _NAME_CHARS.fullmatch(name):
        return False
    words = [w for w in re.split(r"[\s•·&:]+", name) if w]
    if not 1 <= len(words) <= 6:
        return False
    return not any(w.lower().strip(".'’-") in NOT_A_NAME for w in words)


def extract_banner(item: Item) -> dict:
    """Conservative: only quoted names that directly follow '5-star character' /
    'S-Rank Agent' / '5-star Resonator' (and 4★ equivalents). Weapons never match.
    A 4★ list that contains anything implausible (or a name that is also a 5★) is flagged
    `banner_four_unsure` -> the card shows TBA instead of a possibly wrong list."""
    text = item.full
    five: list[str] = []
    four: list[str] = []
    four_unsure = False
    for s in sentences(text):
        for rx, bucket in ((FIVE_RE, five), (FOUR_RE, four)):
            m = rx.search(s)
            if not m:
                continue
            tail = s[m.end(): m.end() + 260]
            other = (FOUR_RE if rx is FIVE_RE else FIVE_RE).search(tail)
            if other:                                   # stop at the next star tier
                tail = tail[: other.start()]
            # Which style is this notice? HoYoverse writes characters bare and quotes the
            # weapons; other official posts quote the characters too. The first character after
            # the star-tier phrase decides, so a quoted name is never read as a bare one.
            run = TIER_STOP.split(tail, maxsplit=1)[0]
            # A quote that follows the gacha-mode phrase is the CHANNEL's name, not a
            # character's (see CHANNEL_MODE) — the run ends where the mode phrase begins, so a
            # quoted agent followed by its channel reads as the agent alone.
            run = _cut_channel_mode(run)
            quoted = [_clean_name(q) for q in QUOTED.findall(run)]
            # `"Epithet" Name` beats every other reading: the bare name after a closing quote is
            # the character, and the quote is the banner it is featured on. Strictly additive --
            # when no quote is followed by a usable name this list is empty and the original
            # bare/quoted decision below runs exactly as before.
            titled = [n for n in (_clean_name(p) for p in EPITHET_NAME.findall(run))
                      if plausible_name(n) and n.lower() not in NAME_FILLER]
            if titled:
                names = titled
            elif QUOTED_FIRST.match(tail):
                names = quoted
            else:
                names = [n for n in bare_names(tail) if plausible_name(n)]
                if not names and quoted:
                    # Fallback for notices that quote their characters. Only when a NAME could
                    # sit between the tier phrase and the quote -- prose there means the quote is
                    # a banner title from another clause (see PROSE_BETWEEN), and so does a
                    # gacha-mode phrase: 'Agent Phoenix Signal Search "Into the Flames of Life"'
                    # quotes the CHANNEL, the agent is the bare word before it (see CHANNEL_MODE).
                    q = OPEN_QUOTE.search(tail)
                    between = tail[: q.start()] if q else ""
                    names = [] if (PROSE_BETWEEN.search(between) or CHANNEL_MODE.search(between)) else quoted
            for n in names:
                if not n:
                    continue
                if not plausible_name(n):
                    if bucket is four:
                        four_unsure = True
                    continue
                if n not in bucket:
                    bucket.append(n)
    if any(n in five for n in four):
        four_unsure = True
        four = [n for n in four if n not in five]
    if not five and not four and not four_unsure:
        return {}
    phase = 1 if PHASE1.search(item.full[:600]) else 2 if PHASE2.search(item.full[:600]) else None
    return {"banner_five": five, "banner_four": four, "banner_four_unsure": four_unsure, "banner_phase": phase}


def extract(game: Game, item: Item) -> Extract | None:
    kind = classify(game, item)
    if not kind:
        return None
    version = find_version(f"{item.title}\n{item.text}")
    if kind == "program":
        fields = extract_program(game, item)
        if "program_ts" not in fields:
            return None                       # no air time -> not an announcement we can post
        vn = find_version_name(item.full, version)
        if vn:
            fields["version_name"] = vn
    elif kind == "maintenance":
        fields = extract_maintenance(item)
        if not fields.get("maint_start_ts") and not fields.get("preinstall_ts"):
            return None
    else:
        fields = extract_banner(item)
        if not fields:
            return None
    return Extract(item=item, kind=kind, version=version, fields=fields)


# --------------------------------------------------------------------------- merging
def infer_versions(extracts: list[Extract], live: str | None, records: dict) -> None:
    """Fill missing versions (e.g. Genshin tweets say 'the new version'): match another
    program post within 3 h, else a tracked record within 3 h, else live+0.1."""
    known = [e for e in extracts if e.version and e.kind == "program"]
    for e in extracts:
        if e.version:
            continue
        pts = e.fields.get("program_ts")
        if e.kind == "program" and pts:
            match = next((k for k in known if abs((k.fields.get("program_ts") or 0) - pts) <= 3 * 3600), None)
            if match:
                e.version = match.version
                continue
            rec = next((v for v, r in records.items()
                        if abs(((r.get("data") or {}).get("program_ts") or 0) - pts) <= 3 * 3600), None)
            if rec:
                e.version = rec
                continue
            if live:
                e.version, e.version_inferred = bump_minor(live), True


def _to_ts(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return parse_iso(str(value))


def _four_star_problem(game: Game, four: list[str], existing: list[str] | None,
                       sticky: str | None) -> str | None:
    """Why a 4★ list must NOT be shown (-> TBA), or None when it is trustworthy.

    An incomplete list is not by itself a reason to print TBA; extract_banner already removes
    implausible names. Wrong counts, overlap, disagreement and overrides still apply."""
    if sticky == "official sources disagree":
        return sticky
    if game.four_star_count and len(four) != game.four_star_count:
        return f"{len(four)} name(s) found, {game.four_star_count} expected"
    if existing and sorted(existing) != sorted(four):
        return "official sources disagree"
    return None


def _unmark_estimated(data: dict, key: str) -> None:
    if key in (data.get("estimated") or []):
        data["estimated"] = [k for k in data["estimated"] if k != key]
        if not data["estimated"]:
            data.pop("estimated", None)
            data.pop("estimate_sources", None)


def apply_estimates(data: dict, prov: dict, estimates: dict | None, now: int) -> list[str]:
    """Fill program / maintenance times a countdown site predicts, but ONLY fields no official
    source has given yet (PRIORITY['countdown'] is the lowest). Returns the keys it estimated."""
    if not estimates:
        return []
    added: list[str] = []
    for key in ESTIMATED_KEYS:
        ts = estimates.get(key)
        if not ts or data.get(key):
            continue
        ts = int(ts)
        if not now - 12 * 3600 <= ts <= now + 120 * 86400:      # nonsense / ancient -> ignore
            continue
        data[key] = ts
        prov[key] = [PRIORITY["countdown"], now]
        added.append(key)
    if "maint_start_ts" in added and not data.get("maint_end_ts"):
        data["maint_end_ts"] = int(data["maint_start_ts"]) + MAINT_HOURS_ESTIMATE * 3600
        prov["maint_end_ts"] = [PRIORITY["countdown"], now]
        added.append("maint_end_ts")
    if added:
        known = data.get("estimated") or []
        data["estimated"] = sorted(set(known) | set(added),
                                   key=lambda k: ESTIMATED_KEYS.index(k) if k in ESTIMATED_KEYS else 99)
        src = list(data.get("estimate_sources") or [])
        for label in estimates.get("labels") or []:
            if label not in src:
                src.append(label)
        data["estimate_sources"] = src[:3]
    return added


def observed_lead_h(records: dict) -> float | None:
    """Return the median real pre-install lead learned from this game's version records.

    State is long-lived and human-editable, so malformed values, non-finite numbers and implausible
    outliers are ignored. ``statistics.median`` deliberately averages the two middle values for an
    even-sized history; unlike a mean, one bad-but-still-plausible edge value cannot drag every
    later version towards it.
    """
    values: list[float] = []
    for record in (records or {}).values():
        value = record.get("preinstall_offset_h")
        try:
            hours = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(hours) and 0 < hours <= MAX_LEAD_H:
            values.append(hours)
    return float(median(values)) if values else None


def derive_preinstall(game: Game, data: dict, prov: dict, now: int,
                      lead_h: float | None = None) -> int | None:
    """Fill a missing pre-install time from maintenance start and mark it as estimated.

    ``lead_h`` is the median learned from real notices for this game. With no history, the shipped
    per-game value is only a cold-start fallback. A real/external pre-install value always wins;
    only a value previously derived by this function may be recalculated as the learned median or
    maintenance date changes.
    """
    start = data.get("maint_start_ts")
    if not start:
        return None
    current = data.get("preinstall_ts")
    own_estimate = ("preinstall_ts" in (data.get("estimated") or [])
                    and prov.get("preinstall_ts", [0])[0] == PRIORITY["pattern"])
    if current and not own_estimate:
        return None

    try:
        hours = float(lead_h) if lead_h is not None else float(PREINSTALL_LEAD_H[game.key])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(hours) or not 0 < hours <= MAX_LEAD_H:
        return None

    derived = int(round(int(start) - hours * 3600))
    if derived <= 0:
        return None
    if own_estimate and current == derived:
        return None         # an unchanged prediction rewrites nothing: no new clock, no state churn
    data["preinstall_ts"] = derived
    prov["preinstall_ts"] = [PRIORITY["pattern"], now]
    estimated = set(data.get("estimated") or [])
    estimated.add("preinstall_ts")
    data["estimated"] = sorted(estimated,
                               key=lambda k: ESTIMATED_KEYS.index(k) if k in ESTIMATED_KEYS else 99)
    sources = list(data.get("estimate_sources") or [])
    if "version cadence" not in sources:
        sources.append("version cadence")
    data["estimate_sources"] = sources[:3]
    return derived


# --------------------------------------------------------------- native cycle speculation
def _snap_weekday(when: datetime, weekday: int) -> datetime:
    """Move `when` to the nearest given weekday, keeping its time of day.

    Nearest, not next: a prediction that lands on Tuesday when the game always patches on
    Wednesday is one day early, and shifting forward one day is right far more often than
    shifting forward six. Ties beyond three days resolve backwards.
    """
    shift = (weekday - when.weekday()) % 7
    if shift > 3:
        shift -= 7
    return when + timedelta(days=shift)


def observed_starts(records: dict) -> list[tuple[str, int]]:
    """Real (never speculated) maintenance starts for a game as (version, ts), oldest first.

    Only timestamps an external source actually published are returned. A value this module
    derived is listed in ``data["estimated"]``, so the cadence model can never be trained on its
    own output — the same rule ``observed_lead_h`` follows for pre-install leads.
    """
    out: list[tuple[str, int]] = []
    for version, record in (records or {}).items():
        if not isinstance(record, dict):
            continue
        data = record.get("data")
        if not isinstance(data, dict):
            continue
        start = data.get("maint_start_ts")
        if not start or "maint_start_ts" in (data.get("estimated") or []):
            continue
        try:
            value = int(start)
        except (TypeError, ValueError, OverflowError):
            continue
        if TS_MIN <= value <= TS_MAX:
            out.append((str(version), value))
    return sorted(out, key=lambda pair: pair[1])


def observed_cadence_days(starts: list[tuple[str, int]]) -> float | None:
    """Median gap, in days, between consecutive real maintenance starts.

    The full history is used on purpose. A short trailing window tracks a holiday-shortened patch
    and then mispredicts every later version; across four games the full-history median backtested
    strictly better (ZZZ: 54% exact vs 9% for a 3-version window).
    """
    times = [ts for _v, ts in starts]
    gaps = [(b - a) / 86400 for a, b in zip(times, times[1:])
            if CADENCE_MIN_D <= (b - a) / 86400 <= CADENCE_MAX_D]
    return round(float(median(gaps)), 3) if gaps else None


def _modal_weekday(starts: list[tuple[str, int]], tz, fallback: int) -> int:
    days = [datetime.fromtimestamp(ts, tz).weekday() for _v, ts in starts]
    return max(set(days), key=days.count) if days else fallback


def next_version(latest: str) -> tuple[str, str]:
    """Guess the next version label: (most likely, alternative-if-the-major-rolls-over).

    These series roll to a new major after .7 *or* .8 with no announced rule — Genshin went
    4.8 -> 5.0 but also 6.7 -> 7.0, and Wuthering Waves jumped 1.4 -> 2.0. Past .6 the
    alternative is reported rather than hidden. The predicted dates do not depend on which label
    turns out to be right, because the cadence is measured in days, not in version numbers.
    """
    if not latest or version_key(latest) == (0, 0):
        return "", ""
    major, minor = version_key(latest)
    return (bump_minor(latest), f"{major + 1}.0") if minor >= 7 else (bump_minor(latest), "")


def predict_cycle(game: Game, records: dict, now: int,
                  lead_h: float | None = None) -> dict | None:
    """Speculate the next version's program / maintenance / pre-install times.

    Everything is anchored on the last *real* maintenance start plus the cadence, because the
    maintenance date is the one timestamp every publisher states precisely. Times of day are
    reapplied as local wall clocks rather than added as durations: across 74 versions the hour of
    the day never drifted, only the date did.

    Returns None when the game declares no cadence, when there is nothing to anchor on, or when
    the result falls outside the believable horizon.
    """
    cadence = getattr(game, "cadence", None)
    if cadence is None:
        return None
    tz = cadence.tz
    starts = observed_starts(records)
    learned = observed_cadence_days(starts) if len(starts) >= SPECULATE_MIN_HISTORY else None
    days = learned if learned is not None else float(cadence.days)
    if not math.isfinite(days) or not CADENCE_MIN_D <= days <= CADENCE_MAX_D:
        return None

    anchor_version, anchor_ts = starts[-1] if starts else (cadence.anchor_version,
                                                           int(cadence.anchor_ts or 0))
    if not TS_MIN <= anchor_ts <= TS_MAX:        # also covers a hand-edited games.json anchor
        return None
    weekday = _modal_weekday(starts, tz, cadence.maint_weekday) if len(starts) >= SPECULATE_MIN_HISTORY \
        else cadence.maint_weekday

    start = datetime.fromtimestamp(anchor_ts, tz) + timedelta(days=days)
    start = _snap_weekday(start, weekday).replace(hour=cadence.maint_time[0],
                                                  minute=cadence.maint_time[1], second=0, microsecond=0)
    # A stale state file can leave the anchor several cycles behind; roll forward to the first
    # cycle that has not already happened instead of advertising a date in the past.
    guard = 0
    while start.timestamp() <= now and guard < 24:
        start = _snap_weekday(start + timedelta(days=days), weekday).replace(
            hour=cadence.maint_time[0], minute=cadence.maint_time[1], second=0, microsecond=0)
        guard += 1
    maint_start = int(start.timestamp())
    if not now < maint_start <= now + SPECULATE_MAX_AHEAD_D * 86400:
        return None

    program = _snap_weekday(start - timedelta(days=cadence.program_lead_days), cadence.program_weekday)
    program = program.replace(hour=cadence.program_time[0], minute=cadence.program_time[1],
                              second=0, microsecond=0)
    try:
        hours = float(lead_h) if lead_h is not None else float(PREINSTALL_LEAD_H[game.key])
    except (KeyError, TypeError, ValueError):
        hours = None
    if hours is not None and not (math.isfinite(hours) and 0 < hours <= MAX_LEAD_H):
        hours = None

    guess, alt = next_version(anchor_version)
    return {
        "version": guess,
        "version_alt": alt,
        "anchor_version": anchor_version,
        "program_ts": int(program.timestamp()),
        "preinstall_ts": int(maint_start - hours * 3600) if hours is not None else None,
        "maint_start_ts": maint_start,
        "maint_end_ts": int(maint_start + cadence.maint_hours * 3600),
        "cadence_days": round(days, 3),
        "confidence": cadence.confidence,
        "observed": len(starts),
        "anchor_ts": anchor_ts,
        "learned": learned is not None,
    }


def derive_cycle(game: Game, version: str, data: dict, prov: dict, now: int, records: dict,
                 lead_h: float | None = None) -> list[str]:
    """Write speculated timestamps into a version that no live source has dated yet.

    Only empty fields, or fields this function itself filled on an earlier run, are touched: the
    guard is identical to derive_preinstall's, so an official notice always wins and is never
    overwritten on a later run. Returns the keys written.
    """
    if not version or getattr(game, "cadence", None) is None:
        return []
    keys = ("program_ts", "maint_start_ts", "maint_end_ts")
    estimated = set(data.get("estimated") or [])
    writable = []
    for key in keys:
        own = key in estimated and prov.get(key, [0])[0] == PRIORITY["pattern"]
        if not data.get(key) or own:
            writable.append(key)
    if "maint_start_ts" not in writable:      # a real maintenance date exists: nothing to speculate
        return []

    guess = predict_cycle(game, records, now, lead_h=lead_h)
    if not guess:
        return []
    # Only speculate FORWARD. The prediction is "one cadence after the newest real maintenance",
    # so it belongs to a version newer than that anchor. Without this check a card for the anchor
    # version itself — announced by a livestream post, with its maintenance not yet published —
    # would be stamped with its successor's dates. The anchor is read from the full history on
    # purpose: hiding this version from the model would silently promote its predecessor.
    anchor = guess.get("anchor_version") or ""
    if not anchor or version_key(version) <= version_key(anchor):
        return []

    written: list[str] = []
    for key in writable:
        value = guess.get(key)
        # Rewriting an unchanged value would re-stamp prov with the current time on every run and
        # churn the committed state file for no reason. Touch only what actually moved.
        if not value or data.get(key) == int(value):
            continue
        data[key] = int(value)
        prov[key] = [PRIORITY["pattern"], now]
        written.append(key)
    newly_estimated = {k for k in writable if data.get(k)} - estimated
    if not written and not newly_estimated:
        return []
    data["estimated"] = sorted(estimated | newly_estimated | set(written),
                               key=lambda k: ESTIMATED_KEYS.index(k) if k in ESTIMATED_KEYS else 99)
    sources = list(data.get("estimate_sources") or [])
    if "version cadence" not in sources:
        sources.append("version cadence")
    data["estimate_sources"] = sources[:3]
    return written


def program_settled(data: dict, now: int, fresh_h: float = PROGRAM_FRESH_H) -> bool:
    """True once this version's special program has aired and stopped being news.

    The announcement already happened, so on a live run the version is historical: nothing about
    it can surprise anyone, and opening a card for it -- or re-creating one -- is noise. Test runs
    keep rendering it, because proving the fetch still resolves the real tweet is their purpose.

    In-place edits of an already-posted card are deliberately NOT covered. A settled program does
    not mean a finished version: HSR 4.6's program aired 2026-09-20 while its maintenance started
    2026-09-28, and its banners kept arriving from HoYoLAB in between. Those corrections still
    have to reach the card that is already sitting in the channel.

    The maintenance date settles a version too, and it has to: a record created from an *Update
    Details* notice never gets a program_ts at all, and only regains one if the cached
    announcement in config/program_announcements.json can be replayed — needs_program_lookup()
    switches the archived-announcement lookup off 12 h past maintenance. Reading only program_ts
    therefore left every such version permanently "unsettled" -- Genshin 7.1 (first seen
    2026-09-25, two days AFTER its 09-23 maintenance, with no program_ts in state) had a card
    published for it on 2026-10-08, fifteen days after the version shipped. The 12 h horizon is the same one
    needs_program_lookup() uses to call the card history, so the two agree by construction.
    """
    pts = data.get("program_ts")
    if pts and TS_MIN <= int(pts) <= TS_MAX and int(pts) < now - fresh_h * 3600:
        return True
    start = data.get("maint_start_ts")
    return bool(start and TS_MIN <= int(start) <= TS_MAX and now > int(start) + 12 * 3600)


def needs_estimate(state, game_key: str, now: int) -> bool:
    """True when this game still misses a program / maintenance time -> worth asking a
    countdown site (otherwise the run doesn't spend a single request on it)."""
    for rec in (state.schedule_records(game_key) or {}).values():
        data = rec.get("data") or {}
        start = data.get("maint_start_ts")
        if start and now > int(start) + 12 * 3600:
            continue                                   # version already live — nothing to predict
        if not data.get("program_ts") or not start:
            return True
    return False


def data_release_ts(data: dict) -> int | None:
    """The version's launch / maintenance-end timestamp, used to phase-split banner lineups."""
    return data.get("maint_end_ts") or data.get("maint_start_ts")


# --- banner confirmation and lock (2026-10-09) ---------------------------------------------------
# A banner name is CONFIRMED once two different groups name the same value: the official notice
# ("official"), the community hub ("feed") or the game wiki ("wiki"). An override confirms alone.
# A confirmed name is LOCKED: no later run changes it, so the card settles. The wiki-derived fields
# (reruns, the 4★ summary) lock only once the wiki names them AND the phase lists they come from are
# already locked. A banner the wiki names differently from the notice stays open, as before.
# Maintenance is not part of this: official maintenance times already replace estimates natively.
DERIVED_BANNER_INPUTS = {"reruns": ("phase1", "phase2"), "four_star": ("phase1_4", "phase2_4")}


def _names(value) -> tuple[str, ...]:
    """A comparable form of a list of names: case and surrounding spaces ignored."""
    return tuple(str(n).strip().lower() for n in (value or []))


def banner_settled(data: dict) -> set[str]:
    """The banner keys that are confirmed and locked on this card."""
    return set(data.get("banners_settled") or [])


def _derived_ok(key: str, value, banners: dict) -> bool:
    """A wiki-derived field must agree with the phase lists it comes from. The 4★ summary is the
    list both phases share; re-runs are names featured in a phase, so they are a subset of them."""
    if key == "four_star":
        return all(_names(banners.get(k)) == _names(value) for k in DERIVED_BANNER_INPUTS[key])
    phase_names = set(_names(banners.get("phase1"))) | set(_names(banners.get("phase2")))
    return bool(phase_names) and set(_names(value)) <= phase_names


def _official_owned(prov: dict, key: str) -> bool:
    """True when an official notice (HoYoverse, Kuro, X, news) wrote banner `key`. Such a name is
    final: the feed and the wiki can never change it, and a later official notice that names someone
    else is logged as a conflict instead of applied. Only a human override outranks it."""
    return PRIORITY["gachawiki"] < prov.get(f"b_{key}", [0])[0] < PRIORITY["override"]


def _sync_official_locks(data: dict, prov: dict) -> None:
    """An official name that is on the card is locked. Records written before the official lock existed
    hold official names that were never marked, so without this they would stay open (and be read by the
    wiki) for ever. Idempotent: it only ever adds keys."""
    banners = data.get("banners") or {}
    keys = [k for k in BANNER_KEYS if banners.get(k) and _official_owned(prov, k)]
    if keys:
        data["banners_settled"] = sorted(banner_settled(data) | set(keys))


def _official_may_write(prov: dict, key: str) -> bool:
    """An official notice may write an empty name, or one the feed or the wiki wrote (an official
    notice outranks both, so it corrects them). It never writes over an official name or an override."""
    return prov.get(f"b_{key}", [0])[0] <= PRIORITY["gachawiki"]


def _confirm(data: dict, prov: dict, key: str, group: str, current, value, banners: dict) -> None:
    """`group` names `value` for banner `key`, and the card currently holds `current` for it.

    A matching name is one more witness. An official notice locks its name on its own; feed and wiki
    names lock once they agree (two groups). The two derived 4★-summary keys keep their own rule.
    Nothing happens when the names differ: the caller decides what to write in that case."""
    if not value or _names(current) != _names(value):
        return
    groups = set(prov.get(f"b_{key}_by") or []) | {group}
    prov[f"b_{key}_by"] = sorted(groups)
    settled = banner_settled(data)
    if key in settled:
        return
    inputs = DERIVED_BANNER_INPUTS.get(key)
    if inputs is not None:
        locked = (group == "wiki" and _derived_ok(key, value, banners)
                  and all(k in settled for k in inputs))
    elif group == "official":
        locked = True                   # a clean official notice is the confirmation on its own
    else:
        locked = len(groups) >= 2       # feed and wiki must agree before they lock
    if locked:
        data["banners_settled"] = sorted(settled | {key})


def _fill_phase(data: dict, prov: dict, banners: dict, key: str, group: str, value, other, now: int) -> bool:
    """One fill source (the hub `feed` or the `calendar`) for one 5★ phase. Returns True when it wrote.

    - Same names as the card: one more witness (_confirm). Two groups agreeing lock the phase.
    - Empty phase: the source fills it (PRIORITY['bannerfeed'], the lowest writer), still unlocked.
    - The source changed its mind about a phase it filled itself: it follows, still unlocked.
      When the OTHER fill source also backs the card's current names, the phase is shared and
      this source leaves it alone, so the two never overwrite each other run after run.
    - Anything else (an official or wiki name, or a disagreement) is left as it is.
    `other` is the other source's current value for the phase, or None."""
    if not value:
        return False
    have = banners.get(key)
    if have and _names(have) == _names(value):
        _confirm(data, prov, key, group, have, value, banners)
        return False
    owner = prov.get(f"b_{key}", [0])[0]
    if not have:
        if owner <= PRIORITY["bannerfeed"]:
            banners[key] = value
            prov[f"b_{key}"] = [PRIORITY["bannerfeed"], now]
            prov[f"b_{key}_by"] = [group]
            return True
        return False
    if owner != PRIORITY["bannerfeed"]:
        return False
    by = prov.get(f"b_{key}_by")
    if not by:                                   # written before the per-group record: it was the hub
        by = ["feed"]
    fill = {g for g in by if g in ("feed", "calendar")}
    if group not in fill:
        return False
    other_backs_card = bool(other) and _names(other) == _names(have)
    if fill == {group} or not other_backs_card:
        banners[key] = value
        prov[f"b_{key}"] = [PRIORITY["bannerfeed"], now]
        prov[f"b_{key}_by"] = [group]
        return True
    return False


def apply_banner_feed(data: dict, prov: dict, feed: dict | None, now: int, calendar: dict | None = None) -> None:
    """Fill 5★ banner phases from the community hub (`feed`, hub.json) and the official calendar
    (`calendar`, api.ennead.cc). Both are fill sources at the lowest writer priority, so they only
    fill an EMPTY phase, or follow their own later change (see _fill_phase).

    Exception, and it is a cross-check: the hub is the only source that separates a banner's NAME from
    the character featured on it. If a phase holds something the hub knows as a banner title, the
    notice was mis-read -- the hub's featured list replaces it even though its priority is the lowest.
    That is what caught HSR 4.6 phase1 carrying "An Ocean in a Pearl" (a banner) instead of Pearl.

    Two fill sources that name the same phase confirm it (_confirm, two groups = lock). A locked phase
    is never changed here, except by the title correction above."""
    if not feed and not calendar:
        return
    feed = feed or {}
    calendar = calendar or {}
    banners = dict(data.get("banners") or {})
    titles = {str(t).lower() for t in feed.get("titles") or []}
    changed = False
    for key in ("phase1", "phase2", "phase1_4", "phase2_4"):
        have = banners.get(key)
        hub, cal = feed.get(key), calendar.get(key)
        if key in ("phase1", "phase2") and have and titles and hub and any(str(n).lower() in titles for n in have):
            # A banner title is never a character, whoever wrote it, so this is a mis-read to correct --
            # even a locked one. The hub's value is unconfirmed, so the phase is unlocked again.
            log.info("banner %s held a banner TITLE, not a character — replaced from the feed", key)
            banners[key] = hub
            prov[f"b_{key}"] = [PRIORITY["bannerfeed"], now]
            prov[f"b_{key}_by"] = ["feed"]
            if key in banner_settled(data):
                data["banners_settled"] = sorted(banner_settled(data) - {key})
            changed = True
            continue
        if key in banner_settled(data):
            continue
        changed |= _fill_phase(data, prov, banners, key, "feed", hub, cal, now)
        changed |= _fill_phase(data, prov, banners, key, "calendar", cal, hub, now)
    if changed:
        data["banners"] = banners


BANNER_KEYS = ("phase1", "phase1_4", "phase2", "phase2_4", "reruns")
"""Everything a complete banner block needs. `confirmed` is deliberately NOT in here: it is the
early-tier line, so a version that has real phase data is complete without it — which is also
what makes the line delete itself."""


def banner_block_complete(game: Game, data: dict) -> bool:
    """True when nothing in the banner block is TBA."""
    if not game.card.show_banners:
        return True
    banners = data.get("banners") or {}
    keys = list(BANNER_KEYS) + (["four_star"] if game.card.four_star_summary else [])
    return all(banners.get(k) for k in keys)


def wiki_recheck_due(game: Game, data: dict, now: int) -> bool:
    """Whether the wiki should be read for this version now.

    An incomplete banner block (still TBA) is read at most every WIKI_INCOMPLETE_H hours, and a
    complete block with an unconfirmed name at most every WIKI_RECHECK_H hours. The wiki only has to
    confirm a name once. A settled block is never read again. Before this throttle an incomplete block
    was read on every five-minute run, which is 288 requests a day for one version."""
    if banner_block_settled(game, data):
        return False
    interval = WIKI_RECHECK_H if banner_block_complete(game, data) else WIKI_INCOMPLETE_H
    return now - int(data.get("banners_checked_ts") or 0) >= interval * 3600


def banner_block_settled(game: Game, data: dict) -> bool:
    """True when the banner block is complete AND every name in it is confirmed and locked. Only
    then is the wiki never asked. A complete block that still has an unconfirmed name keeps being
    checked, until the card freezes, so the wiki can confirm it."""
    if not banner_block_complete(game, data):
        return False
    if not game.card.show_banners:
        return True
    keys = list(BANNER_KEYS) + (["four_star"] if game.card.four_star_summary else [])
    settled = banner_settled(data)
    return all(k in settled for k in keys)


def banner_phases_done(game: Game, data: dict) -> bool:
    """Whether the card's banner PHASES are final, so the title search and the calendar stop watching it.

    A phase is final when its 5★ list and its 4★ (A-rank) list are both locked: confirmed by two reputable
    sources, or by an official notice. Phase 2 must be final too, so while it has no names it is still
    watched. Re-runs and the 4★ summary are derived from the phase lists and come only from the wiki, so
    they do not hold this up; the wiki keeps its own schedule (wiki_recheck_due). A game with no banner
    block is done at once."""
    if not game.card.show_banners:
        return True
    banners = data.get("banners") or {}
    settled = banner_settled(data)
    for phase in ("phase1", "phase2"):
        if not banners.get(phase) or not banners.get(f"{phase}_4"):
            return False
        if phase not in settled or f"{phase}_4" not in settled:
            return False
    return True


def _display_map(banners: dict, candidates: list[str]) -> dict[str, str]:
    """{short: full} for card names this wiki read states longer. ZZZ's Version page writes
    'Phoenix' in the channel list and 'Phoenix Reffaella' in the debut roster, and the official
    notice says just 'Phoenix'; the card can show the full name while every comparison keeps
    the short one. The rule is gachawiki's own debut rule (a name plus a space-separated suffix
    is the same character): each card name maps to the longest candidate it is a prefix of.
    A wiki that states only single names (GI, HSR, WuWa) yields an empty map, so the whole
    feature is gated on the data showing longer forms and is a no-op everywhere else."""
    out: dict[str, str] = {}
    for key in BANNER_KEYS + ("four_star", "confirmed"):
        for n in banners.get(key) or []:
            if not n:
                continue
            best = n
            for c in candidates:
                if len(c) > len(best) and c.startswith(n + " "):
                    best = c
            if best != n:
                out[n] = best
    return out


def apply_gacha_wiki(game: Game, data: dict, prov: dict, wiki: dict | None, now: int) -> None:
    """Fill banner lists from the game's own wiki wherever the card still says TBA, and keep
    the entries this source owns in step with what the wiki currently says.

    PRIORITY['gachawiki'] is 6: above the community banner feed, below every official source
    and below config/overrides.json. A 4★ list the reader could not verify never arrives here —
    gachawiki.build_lineup drops it rather than publish a half-right line-up.

    A name the wiki gives identically to the card is one more confirmation (see _confirm). A
    confirmed name is never changed here.

    `wiki is None` means the wikis were not read (complete block, frozen card, failed request)
    and nothing is touched. A dict means the page WAS read, so an entry this source wrote and
    the wiki no longer supports is withdrawn rather than left on the card for ever.
    """
    if wiki is None:
        return                      # the wikis were not consulted this run -> touch nothing
    # Stamp every read the wiki actually answered. The throttle (wiki_recheck_due) needs it, and a
    # read happens at most once per WIKI_INCOMPLETE_H, so this writes the state file that rarely.
    data["banners_checked_ts"] = now
    banners = dict(data.get("banners") or {})
    changed = False
    for key in list(BANNER_KEYS) + ["four_star", "confirmed"]:
        if key in banner_settled(data):
            continue
        value = [n for n in (wiki.get(key) or []) if gachawiki.publishable_name(n)]
        owner = prov.get(f"b_{key}", [0])[0]
        have = banners.get(key)
        if have:
            if _names(have) == _names(value):
                _confirm(data, prov, key, "wiki", have, value, banners)
                continue
            # A value is only as good as the read it came from, so the wiki reader is allowed
            # to correct -- and to withdraw -- what the wiki reader itself wrote. Everything
            # official, and every human override, outranks this source and is never touched.
            # Without this a single bad read is permanent: ZZZ 3.3 sat on "Agent" because
            # "already filled" was treated as "already right".
            if owner > PRIORITY["gachawiki"]:
                continue                        # official notices and overrides outrank the wiki
            if value:
                banners[key] = value
                prov[f"b_{key}"] = [PRIORITY["gachawiki"], now]
                prov[f"b_{key}_by"] = ["wiki"]
                _confirm(data, prov, key, "wiki", value, value, banners)   # the read itself is a witness
            elif owner == PRIORITY["gachawiki"]:
                banners.pop(key, None)          # the wiki no longer says it -> back to TBA
                prov.pop(f"b_{key}", None)
                prov.pop(f"b_{key}_by", None)
            else:
                continue                        # the wiki is silent about a feed value: keep the feed's
            changed = True
            continue
        if not value or owner > PRIORITY["gachawiki"]:
            continue
        banners[key] = value
        prov[f"b_{key}"] = [PRIORITY["gachawiki"], now]
        prov[f"b_{key}_by"] = ["wiki"]
        _confirm(data, prov, key, "wiki", value, value, banners)           # the read itself is a witness
        changed = True
    # The early-tier line is a stand-in for phase data. Once the phases are known it is noise,
    # so it goes on the same silent edit that brings the real line-up in.
    if banners.get("confirmed") and banners.get("phase1"):
        banners.pop("confirmed")
        prov.pop("b_confirmed", None)
        prov.pop("b_confirmed_by", None)
        changed = True
    if changed:
        data["banners"] = banners
    # The wiki's display names, kept apart from the banner data itself: the stored names (and
    # every lock and comparison above) stay exactly as the source that wrote them left them,
    # and the card maps short -> full at render time (cards.banners_block). A wiki that stops
    # stating a full name withdraws it here, and the next silent edit takes the card back.
    disp = _display_map(banners, wiki.get("candidates") or [])
    if disp:
        data["banners_display"] = disp
    else:
        data.pop("banners_display", None)


def announcement_locked(data: dict | None) -> bool:
    """True once this card has its Special Program / Special Broadcast announcement.

    From that moment the card's title link, source button, key art and air time belong to THAT
    post and nothing else. Only the banners and the maintenance details keep updating (a countdown
    estimate is replaced by the official notice, as before).

    Why: the card used to rebuild its link and key art on every run from every program-looking
    post in the 72 h lookback window. ZZZ 3.3's announcement (x.com/ZZZ_EN/status/2106957553435312559,
    posted 2026-10-05) aged out of that window, and a giveaway post that repeated the air time
    (x.com/ZZZ_EN/status/2108422202974499088, "Share to Win Master Tape x10", 2026-10-09 05:00 UTC)
    became the only program post left -- so the card's link and key art silently switched to it.
    Nothing in the code remembered which post had opened the card.

    Records written before the flag existed count as locked too: they already carry program_seen
    or media_from, which only an announcement can set. Their link is kept as it is.
    """
    d = data or {}
    if "announcement_locked" in d:
        return bool(d["announcement_locked"])
    return bool(d.get("program_seen") or d.get("media_from"))


def apply_program_media(data: dict, prov: dict, media: dict | None, now: int) -> list[str]:
    """Give the card the ANNOUNCEMENT it should be showing: the program article's own link and
    its key art, instead of whatever the run happened to see.

    Why: HSR 4.6 linked to the *Update and Maintenance Notice* and showed its Pompom cover,
    because the Special Program preview (article 46691962) was 11 days older and had already
    fallen out of the lookback window. The official news pages archive every announcement, so
    the right link and the right picture are recoverable weeks later.

    program_ts is filled too when no official post has given one — the article IS official, it
    just arrived before the window. Returns the keys it changed."""
    if not media or not media.get("url"):
        return []
    changed: list[str] = []
    locked = announcement_locked(data)     # the link and key art are already someone's announcement
    ts = media.get("program_ts")
    # A recovered announcement is OFFICIAL, so an air time that has already passed is kept (the
    # card renders it as "5 days ago") — unlike a countdown estimate, only absurd values go.
    #
    # It must also DISPLACE a countdown estimate. `not data.get("program_ts")` alone made the
    # estimate sticky forever: HSR 4.6 filled program_ts from hsr-countdown on an early run, and
    # every later run that found the real announcement refused to overwrite it, so the card kept
    # showing 1790341813 instead of the tweet's own 1789903800. Anything this code estimated is
    # listed in data["estimated"] -- that is exactly the case an official source should replace.
    est = set(data.get("estimated") or [])
    if ts and now - 90 * 86400 <= int(ts) <= now + 120 * 86400 and \
            (not data.get("program_ts") or "program_ts" in est):
        data["program_ts"] = int(ts)
        prov["program_ts"] = [PRIORITY["news"], now]
        changed.append("program_ts")
        est.discard("program_ts")
        if "estimated" in data:
            data["estimated"] = [k for k in (data.get("estimated") or []) if k != "program_ts"]
        if not est:
            data.pop("estimated", None)
            data.pop("estimate_sources", None)
    if locked:
        # The announcement that opened this card is locked: a later lookup may still fill an
        # air time that was only a countdown estimate, but it never moves the link or the key art.
        return changed
    label = media.get("source") or "Official News"
    # The title links the ANNOUNCEMENT itself. `media["youtube"] or media["url"]` made the card
    # headline link the YouTube stream instead of the post that announced it -- seen live on
    # HSR 4.6 (2026-09-27), where the title pointed at youtube.com/watch?v=drFgtruoPe8 while the
    # real source was x.com/honkaistarrail/status/2099440781115211916. The stream stays reachable
    # through data["youtube_video"] and the Youtube button.
    data["title_url"] = media["url"]
    data["source_url"] = media["url"]
    data["source_label"] = label
    if media.get("youtube"):
        data["youtube_video"] = media["youtube"]
    links = [(lbl, u) for lbl, u in (data.get("source_links") or []) if u != media["url"]]
    links.insert(0, (label, media["url"]))
    data["source_links"] = links[:3]
    images = rank(media.get("images"))
    if images:
        data["images"] = images
        data["media_from"] = label           # the card says where the key art came from
    data["announcement_locked"] = True       # from here on the link and key art belong to this post
    return changed + ["title_url", "source_url"]


def version_extracts(ctx, game: Game, log_missing: bool = False) -> dict[str, list[Extract]]:
    """Group this run's official extracts by version. The monitor and program lookup share this
    grouping so they can never disagree about which versions are current."""
    live = (ctx.versions.get(game.key) or {}).get("live")
    records = ctx.state.schedule_records(game.key)
    extracts = [e for e in (extract(game, it) for it in ctx.items.get(game.key, [])) if e]
    infer_versions(extracts, live, records)
    by_version: dict[str, list[Extract]] = {}
    for e in extracts:
        if not e.version:
            if log_missing:
                log.info("[%s] %s item %s has no version yet — waiting for another source",
                         game.key, e.kind, e.item.url)
            continue
        if live and version_key(e.version) < version_key(live):
            continue
        by_version.setdefault(e.version, []).append(e)
    return by_version


def _recoverable_announcement(data: dict, game_key: str, now: int, maint_start) -> bool:
    """True when a live version's card is STILL missing its announcement and the tweet is cached.

    A record built from an *Update Details* notice never gets a program_ts, and nothing else can
    ever supply one. The card then drops its air-time line completely (cards.py prints nothing
    rather than a misleading TBA), keeps the maintenance notice's cover as key art and links the
    notice instead of the announcement — exactly the broken Genshin 7.1 card of 2026-10-08, whose
    `images` had been carried forward in `data` since 2026-09-25 with nothing able to replace it.
    The plain 12 h cutoff would leave every such version that way for ever.

    Recovery is allowed only when config/program_announcements.json already holds the tweet id.
    That makes the lookup one fxtwitter call which is going to succeed and set `media_from`, so it
    happens once and then stops — rather than an open-ended retry every ten minutes. A version
    with no cached id keeps the old behaviour and never retries, and a frozen card ends it either
    way. The cache is also what makes this work on a `mode=test` run, whose state starts empty.
    """
    if data.get("program_ts") or not game_key or not data.get("version"):
        return False
    if now > int(maint_start) + CARD_FREEZE_D * 86400:
        return False                       # frozen card: nothing the lookup finds can be shown
    from .sources.twitter import program_seed  # deferred: sources.twitter imports schedule
    return bool(program_seed(game_key, str(data["version"])).get("id"))


def announcement_gap(record: dict, game_key: str, now: int) -> str:
    """Why a live card can never get its announcement again, or "" when it can.

    A record opened by an *Update Details* notice has no program_ts, and the ordinary lookup
    stands down 12 h past maintenance; the cached-tweet recovery in needs_program_lookup() is
    what repairs those cards. When the cache holds no id for the version there is nothing left
    to try — and that state used to be completely silent, so a card kept the notice's cover and
    link for ever while looking exactly like a rendering bug. This returns the one thing the
    operator can act on, so the run summary can say it (once) instead.
    """
    data = (record or {}).get("data") or {}
    if data.get("program_ts") or data.get("program_seen") or data.get("media_from"):
        return ""                                  # the card has (or had) its announcement
    start = data.get("maint_start_ts")
    if not start or not TS_MIN <= int(start) <= TS_MAX:
        return ""
    if now <= int(start) + 12 * 3600:
        return ""                                  # the ordinary lookup window is still open
    if now > int(start) + CARD_FREEZE_D * 86400:
        return ""                                  # frozen: nothing a lookup found could be shown
    if not game_key or not data.get("version"):
        return ""
    if _recoverable_announcement(data, game_key, now, start):
        return ""                                  # the cached recovery will run
    return ("has no air time and no announcement link, and there is no cached tweet id to "
            "replay — add one for this version in config/program_announcements.json and the "
            "next run repairs the card in place")


def needs_program_lookup(extracts: list[Extract], record: dict, now: int,
                         game_key: str = "") -> bool:
    """Whether this version still needs its archived announcement looked up."""
    data = (record or {}).get("data") or {}
    start = data.get("maint_start_ts")
    if start and now > int(start) + 12 * 3600 \
            and not _recoverable_announcement(data, game_key, now, start):
        return False
    # Having found the announcement once is normally enough -- but NOT while the air time is still
    # only an estimate. Without this, a version whose program_ts came from a countdown site keeps
    # that guess forever: media_from is set, so no lookup runs, so apply_program_media gets no
    # media and can never displace it. HSR 4.6 was stuck on 1790341813 exactly this way.
    if (data.get("program_seen") or data.get("media_from")) and \
            "program_ts" not in (data.get("estimated") or []):
        return False
    return not any(e.kind == "program" for e in extracts)


def needs_media(state, game_key: str, now: int) -> bool:
    """True when a tracked version still shows somebody else's announcement -> worth one page
    fetch to find the program article. Never for a version that is already live, and never
    twice for the same version."""
    for rec in (state.schedule_records(game_key) or {}).values():
        data = rec.get("data") or {}
        start = data.get("maint_start_ts")
        if start and now > int(start) + 12 * 3600:
            continue                                   # long live — the card is history by now
        if data.get("program_seen") or data.get("media_from"):
            continue                                   # already the right announcement
        return True
    return False


def _link_label(url: str, fallback: str) -> str:
    """The button name for a pinned announcement link: the platform the link is on."""
    host = (urlparse(url).hostname or "").lower()
    if host == "x.com" or host.endswith((".x.com", "twitter.com")):
        return "X Post"
    if host == "hoyolab.com" or host.endswith(".hoyolab.com"):
        return "HoYoLAB"
    return fallback


def _pin_announcement(data: dict, e: Extract, others: list[tuple[str, str]]) -> None:
    """Point the card's link, source buttons and key art at ONE program post, and remember which
    post that was (source and time) so a later, better-ranked post can upgrade it once."""
    lf, item = e.fields, e.item
    data["title_url"] = item.url                   # the announcement, never the stream it mentions
    data["source_url"] = item.url
    data["source_label"] = item.source_label
    if lf.get("images"):
        data["images"] = rank(lf["images"])
    elif data.get("youtube_video"):
        data["images"] = [youtube_thumb(data["youtube_video"])]
    links = [(item.source_label, item.url)]
    for label, url in others:                      # the other program posts, as extra buttons
        if label not in [lbl for lbl, _ in links]:
            links.append((label, url))
    data["source_links"] = links[:3]
    data["announcement_source"] = item.source
    data["announcement_ts"] = item.published_ts


def _upgrade_announcement(data: dict, program_items: list) -> None:
    """A card locked to HoYoLAB (because X was down when the card was first seen) moves to X ONCE,
    and only when X's post is the same announcement: same air time, and published no later than the
    post the card is locked to. A giveaway repeating the air time is always published later, so it
    can never take the card -- the incident this lock exists to stop."""
    cur_rank = LOCK_RANK.get(data.get("announcement_source"), 0)
    cur_ts = data.get("announcement_ts")
    if "announcement_source" not in data or not cur_ts or not data.get("program_ts"):
        return                                     # legacy record, or no air time to match on
    better = [e for e in program_items
              if LOCK_RANK.get(e.item.source, 0) > cur_rank
              and e.item.published_ts <= cur_ts
              and e.fields.get("program_ts") == data.get("program_ts")]
    if better:
        best = sorted(better, key=lambda e: (-LOCK_RANK.get(e.item.source, 0), e.item.published_ts))[0]
        _pin_announcement(data, best, [(e.item.source_label, e.item.url) for e in program_items])
        log.info("announcement upgraded to %s: %s", best.item.source, best.item.url)


def merge(game: Game, version: str, extracts: list[Extract], record: dict, override: dict,
          launcher: dict, now: int, notes: list[str] | None = None,
          estimates: dict | None = None, media: dict | None = None,
          banner_feed: list[dict] | None = None, lead_h: float | None = None,
          records: dict | None = None, wiki: dict | None = None,
          calendar: list[dict] | None = None) -> dict:
    """Return the merged card data (record['data'] is the previous state).
    4★ rule: no data or ANY doubt (wrong count, odd name, sources disagree) -> TBA;
    config/overrides.json is always trusted."""
    prev = dict(record.get("data") or {})
    prov = dict(record.get("prov") or {})
    data = dict(prev)
    data["version"] = version
    # A card that is already locked (by the flag, or by the legacy markers program_seen / media_from)
    # keeps its lock written down on EVERY run, including runs with no program post in the window.
    if announcement_locked(prev):
        data["announcement_locked"] = True
    _sync_official_locks(data, prov)

    def put(key, value, source, ts):
        if value in (None, "", []):
            return
        pri = PRIORITY.get(source, 10)
        cur = prov.get(key)
        if cur is None or pri > cur[0] or (pri == cur[0] and ts >= cur[1]):
            data[key] = value
            prov[key] = [pri, ts]
            if pri > PRIORITY["countdown"]:
                _unmark_estimated(data, key)      # an official time replaces an estimate

    program_items = []
    for e in sorted(extracts, key=lambda x: x.item.published_ts):
        src, ts = e.item.source, e.item.published_ts
        f = e.fields
        if e.kind == "program":
            # Collected only. The announcement is decided ONCE, below, from a single post -- see
            # announcement_locked(). Merging every program post's fields here is what let a later
            # post overwrite the card's air time, link and key art.
            program_items.append(e)
        elif e.kind == "maintenance":
            for k in ("preinstall_ts", "maint_start_ts", "maint_end_ts", "compensation"):
                put(k, f.get(k), src, ts)
        elif e.kind == "banner":
            banners = dict(data.get("banners") or {})
            phase = f.get("banner_phase")
            if phase is None and data.get("maint_start_ts"):
                phase = 1 if ts < int(data["maint_start_ts"]) + 7 * 86400 else 2
            if phase in (1, 2):
                key5, key4 = f"phase{phase}", f"phase{phase}_4"
                five = f.get("banner_five")
                # Re-reading the SAME post after a parser fix heals its own name (see the 4★ note below).
                same5 = prov.get(f"b_{key5}") == [PRIORITY.get(src, 10), ts]
                if five and _official_owned(prov, key5) and not same5:
                    # Final. A different 5★ from a later official post is logged once, not applied.
                    if _names(banners.get(key5)) != _names(five):
                        conflict = ", ".join(five)
                        if prov.get(f"b_{key5}_conflict") != conflict and notes is not None:
                            notes.append(f"⚠️ {game.short} {version} phase {phase}: a later notice names "
                                         f"{conflict}, the locked official name is kept. Correct it via "
                                         "config/overrides.json if the notice is right.")
                        prov[f"b_{key5}_conflict"] = conflict
                elif five and (same5 or _official_may_write(prov, key5)):
                    if _names(banners.get(key5)) != _names(five):
                        prov[f"b_{key5}_by"] = ["official"]           # a new name from the notice
                    banners[key5] = five
                    prov[f"b_{key5}"] = [PRIORITY.get(src, 10), ts]
                    _confirm(data, prov, key5, "official", five, five, banners)   # the notice locks it
                four = list(f.get("banner_four") or [])
                if (four or f.get("banner_four_unsure")) and prov.get(f"b_{key4}", [0])[0] < PRIORITY["override"]:
                    flag = f"b_{key4}_tba"
                    # Re-reading the SAME post (same source, same timestamp) is not two official
                    # sources disagreeing -- it is this bot parsing one notice better than it did
                    # before. Comparing against the stored list there would latch the OLD, wrong
                    # list into a permanent TBA: HSR 4.6 phase1_4 held three light cones written
                    # by post 46851682, and the corrected reader re-reads that very post.
                    # Disagreement only means something between DIFFERENT posts.
                    same_post = prov.get(f"b_{key4}") == [PRIORITY.get(src, 10), ts]
                    owned = _official_owned(prov, key4)
                    # Only a list an OFFICIAL notice wrote can disagree with a new official notice.
                    # A list the feed and the wiki locked between them is corrected by the notice.
                    problem = _four_star_problem(
                        game, four,
                        None if (same_post or not owned) else banners.get(key4),
                        None if same_post else prov.get(flag))
                    if same_post:
                        prov.pop(flag, None)
                    held = (problem and key4 in banner_settled(data) and problem != "official sources disagree") \
                        or (owned and not four)           # an empty reading never wipes an official list
                    if held:
                        pass        # a doubtful reading never blanks or replaces a locked list
                    elif problem:
                        banners[key4] = []                                    # TBA
                        prov.pop(f"b_{key4}_by", None)
                        prov[f"b_{key4}"] = [PRIORITY.get(src, 10), ts]
                        if prov.get(flag) != problem and notes is not None:
                            notes.append(f"⚠️ {game.short} {version} phase {phase}: 4★ shown as TBA — {problem} "
                                         f"(found: {', '.join(four) or 'nothing usable'}). Confirm via "
                                         "config/overrides.json if you know the names.")
                        prov[flag] = problem
                    else:
                        if _names(banners.get(key4)) != _names(four):
                            prov[f"b_{key4}_by"] = ["official"]
                        banners[key4] = four
                        prov[f"b_{key4}"] = [PRIORITY.get(src, 10), ts]
                        _confirm(data, prov, key4, "official", four, four, banners)
            data["banners"] = banners

    # presentation: the announcement's link, key art, source and air time. Decided ONCE, from ONE
    # post, and then locked (see announcement_locked). Before the lock, every run rebuilt the link
    # and key art from whatever program posts were still inside the lookback window, so when the
    # real announcement aged out of that window a same-day giveaway post took the card over
    # (ZZZ 3.3, 2026-10-09). Earliest first: the announcement precedes any reminder about it.
    if program_items:
        locked = announcement_locked(data)   # read BEFORE program_seen is set below, or it is always True
        data["program_seen"] = True          # this card already shows the real announcement
        if not locked:
            # Only a post that GIVES an air time can open the lock. A teaser without one shows
            # for now (as it always did) but stays unlocked, so the real announcement can still
            # take the card -- and its air time, which the teaser never had.
            timed = [e for e in program_items if e.fields.get("program_ts")]
            # X first (LOCK_RANK), then the earliest post: the announcement precedes any reminder
            lock = sorted(timed or program_items,
                          key=lambda e: (-LOCK_RANK.get(e.item.source, 0), e.item.published_ts))[0]
            lf, lsrc, lts = lock.fields, lock.item.source, lock.item.published_ts
            for k in ("program_ts", "program_name", "version_name", "youtube_video"):
                put(k, lf.get(k), lsrc, lts)
            # the link and the key art come from the SAME post -- never one from each
            _pin_announcement(data, lock, [(e.item.source_label, e.item.url) for e in program_items])
            data["announcement_locked"] = bool(timed)
        elif data.get("announcement_locked"):
            _upgrade_announcement(data, program_items)
    else:
        # nobody in the lookback window announced the program: look the article up on the
        # official news page / HoYoLAB news list (link + full-size key art + air time).
        apply_program_media(data, prov, media, now)
        if not data.get("title_url"):
            notice = next((e for e in extracts if e.kind == "maintenance"), None)
            if notice:
                data.setdefault("title_url", notice.item.url)
                data.setdefault("source_url", notice.item.url)
                data.setdefault("source_label", notice.item.source_label)
                data.setdefault("source_links", [(notice.item.source_label, notice.item.url)])
                # NOTE: the notice's own cover is deliberately NOT used as the card image — a
                # maintenance notice cover (Pompom on HSR 4.6) is not the program's key art.

    # launcher: pre-download became available (official client signal)
    if launcher.get("pre") == version and not data.get("preinstall_ts"):
        data["preinstall_ts"] = int(record.get("pre_detected_ts") or now)
        record.setdefault("pre_detected_ts", data["preinstall_ts"])
        prov["preinstall_ts"] = [PRIORITY["launcher"], now]

    # countdown sites: an ESTIMATE for every field no official source has given yet
    apply_estimates(data, prov, estimates, now)

    # banner feed: fill empty banner phases from hub.json, and from the official calendar (names only)
    if banner_feed or calendar:
        rel_ts = data_release_ts(data)
        lineup = banner_feed_for(banner_feed, rel_ts) if (banner_feed and rel_ts) else {}
        cal = calendarfeed.lineup(calendar, rel_ts, game.four_star_count) if calendar else {}
        apply_banner_feed(data, prov, lineup, now, calendar=cal)

    # the game's own wiki: fill whatever is still TBA (phases, 4★, re-runs, the early tier)
    apply_gacha_wiki(game, data, prov, wiki, now)

    # human overrides win over everything
    for key in ("program_ts", "preinstall_ts", "maint_start_ts", "maint_end_ts"):
        if key in override:
            ts = _to_ts(override[key])
            if ts:
                data[key] = ts
                prov[key] = [PRIORITY["override"], now]
                _unmark_estimated(data, key)
    for key in ("program_name", "version_name", "compensation"):
        if override.get(key):
            data[key] = override[key]
    if override.get("title_url"):
        # A human-verified link pins the announcement itself: the title AND the source button move
        # together, and any stale copy of the same source is dropped (it would otherwise survive as
        # a second, wrong button next to the corrected one). The button is named for the host the
        # override points at, not for whatever the broken record carried: a HoYoLAB pin under an
        # "X Post" label is exactly the wrong card this override exists to repair.
        link = override["title_url"]
        label = _link_label(link, data.get("source_label") or "Source")
        replaced = {data.get("title_url"), data.get("source_url")} - {None, ""}   # the post being moved off
        rest = [(lbl, u) for lbl, u in (data.get("source_links") or [])
                if lbl != label and u != link and u not in replaced]
        data["title_url"] = link
        data["source_url"] = link
        data["source_label"] = label
        data["source_links"] = ([(label, link)] + rest)[:3]
    if override.get("image"):
        data["images"] = [override["image"]]
    if isinstance(override.get("banners"), dict):
        banners = dict(data.get("banners") or {})
        for k, v in override["banners"].items():
            if isinstance(v, list):
                banners[k] = v
                prov[f"b_{k}"] = [PRIORITY["override"], now]
                if v:                                  # a human-set name is confirmed and locked
                    data["banners_settled"] = sorted(banner_settled(data) | {k})
        data["banners"] = banners

    # Last, derive only what every external source and human override left empty. This can use a
    # real maintenance start or an explicitly labelled countdown estimate; either way the derived
    # pre-install value remains labelled estimated until an official notice replaces it.
    #
    # Order matters: derive_cycle may supply the maintenance start that derive_preinstall needs,
    # so a version nobody has dated yet still gets a complete, visibly-estimated set of times.
    derive_cycle(game, version, data, prov, now, records if records is not None else {}, lead_h)
    derive_preinstall(game, data, prov, now, lead_h)
    record["prov"] = prov
    return data


# --------------------------------------------------------------------------- pipeline
def repost_problem(repost: str, games: list, records_of) -> str | None:
    """Explain why a REPOST request can't be honoured (None = fine)."""
    if not repost:
        return None
    rg, _, rv = repost.strip().partition(":")
    keys = [g.key for g in games]
    if not rv:
        return f"⚠️ repost '{repost}': use game:version, e.g. starrail:4.6"
    if rg not in keys:
        return f"⚠️ repost '{repost}': unknown or inactive game '{rg}' (use one of: {', '.join(keys)})"
    tracked = sorted(records_of(rg), key=version_key)
    if rv not in tracked:
        have = ", ".join(tracked) if tracked else "none yet"
        return (f"⚠️ repost '{repost}': version {rv} isn't tracked — only versions the bot has already seen "
                f"in an official post can be reposted (tracked: {have})")
    return None


async def run(ctx) -> None:
    s = ctx.settings
    for game in ctx.games:
        live_info = ctx.versions.get(game.key, {})
        records = ctx.state.schedule_records(game.key)
        by_version = version_extracts(ctx, game, log_missing=True)
        overrides = ctx.overrides.get(game.key, {})
        for ver in overrides:
            if ver in records and ver not in by_version:
                by_version[ver] = []                       # override-only edits
        if s.repost:
            rg, _, rv = s.repost.partition(":")
            if rg == game.key and rv in records and rv not in by_version:
                by_version[rv] = []

        await gather_banner_search(ctx, game, records, by_version)
        await gather_calendar(ctx, game, records, by_version)
        bootstrapped = ctx.state.is_bootstrapped("schedule", game.key)
        est = (ctx.estimates.get(game.key) or {}) if s.countdown_estimates else {}
        media = (ctx.media.get(game.key) or {}) if s.program_media else {}
        feed_list = (ctx.banners.get(game.key) or []) if s.banner_feed else []
        for ver in sorted(by_version, key=version_key):
            version_est = est if (est.get("version") in (None, ver)) else {}
            await _handle_version(ctx, game, ver, by_version[ver], records, overrides.get(ver, {}),
                                  live_info, bootstrapped, version_est, media.get(ver), feed_list)
        if not bootstrapped and ctx.reachable.get(game.key, True):
            ctx.state.mark_bootstrapped("schedule", game.key)   # only after a source really answered
    problem = repost_problem(s.repost, ctx.games, ctx.state.schedule_records)   # after this run's records
    if problem:
        ctx.report.append(problem)


async def gather_wiki_lineup(ctx, game: Game, ver: str, record: dict) -> dict | None:
    """Ask the game's wiki for this version's line-up — only while a blank remains.

    The short-circuit is the whole traffic budget: a version whose banner block is complete AND
    confirmed costs zero requests, for ever, no matter how often the monitor runs. A complete block
    with an unconfirmed name is still checked until the card freezes, because the wiki may confirm it.
    """
    if not ctx.settings.gacha_wiki or game.key not in gachawiki.WIKIS:
        return None
    data = record.get("data")
    if data is None:
        data = record["data"] = {}          # a brand-new record: keep the read stamp (merge keeps it too)
    _sync_official_locks(data, record.get("prov") or {})
    if not wiki_recheck_due(game, data, ctx.now):
        return None
    start = data.get("maint_start_ts")
    if start and ctx.now > int(start) + CARD_FREEZE_D * 86400:
        return None            # the card is frozen (never edited again) -> asking is pure waste
    try:
        out = await gachawiki.fetch_lineup(ctx.fetcher, game.key, ver, game.four_star_count,
                                           game.card.four_star_summary)
    except Exception as e:                                   # noqa: BLE001 — one wiki, never fatal
        log.warning("%s wiki lookup failed for %s: %s", game.key, ver, e)
        out = None
    if out is None:
        data["banners_checked_ts"] = ctx.now                 # asked and failed: no retry until the interval passes
    return out


def banner_search_due(game: Game, data: dict, now: int) -> bool:
    """Whether the title search runs for this card now. Same budget as the wiki: a settled block is
    never searched, a frozen card is never searched, an incomplete block every WIKI_INCOMPLETE_H hours
    and a complete-but-unconfirmed block every WIKI_RECHECK_H hours. It has its own stamp
    (`banners_search_ts`), so the two sources do not reset each other's clock."""
    if not game.banner_titles or banner_phases_done(game, data):
        return False
    start = data.get("maint_start_ts")
    if start and now > int(start) + CARD_FREEZE_D * 86400:
        return False
    interval = WIKI_RECHECK_H if banner_block_complete(game, data) else WIKI_INCOMPLETE_H
    return now - int(data.get("banners_search_ts") or 0) >= interval * 3600


async def gather_banner_search(ctx, game: Game, records: dict, by_version: dict) -> None:
    """Find each live or upcoming card's banner notices BY TITLE, whatever their age, and feed the
    hits into the same merge as every other official post. This is what lets a Phase I notice that
    is older than the lookback window still lock its names (HSR 4.6 Phase I, 2026-09-27).

    Only cards that still hold a TBA or an unconfirmed name are asked, and only when due. A hit only
    adds a candidate: the names are still read from the body by extract_banner(), and the normal
    lock rules decide what they may overwrite."""
    if not ctx.settings.banner_search or not game.banner_titles or ctx.fetcher is None:
        return                                        # no lookup possible -> no request and no stamp
    from .sources import banner_search  # noqa: I001 — lazy: banner_search -> hoyolab -> this module
    live = (ctx.versions.get(game.key) or {}).get("live")
    due = []
    for ver, rec in records.items():
        if live and version_key(ver) < version_key(live):
            continue                                  # older cards are frozen history
        data = rec.get("data")
        if data is None:
            data = rec["data"] = {}                   # keep the stamp on a brand-new record
        _sync_official_locks(data, rec.get("prov") or {})
        if banner_search_due(game, data, ctx.now):
            due.append((ver, data))
    if not due:
        return
    found = await asyncio.gather(*(banner_search.find(ctx.fetcher, game, ver) for ver, _ in due),
                                 return_exceptions=True)
    for (ver, data), items in zip(due, found):
        data["banners_search_ts"] = ctx.now           # asked (answered or not): no retry until due again
        if isinstance(items, BaseException):
            log.warning("%s banner search failed for %s: %s", game.key, ver, items)
            continue
        have = {e.item.id for e in by_version.get(ver, [])}
        added = 0
        for item in items:
            if item.id in have:
                continue
            fields = extract_banner(item)
            if not fields:
                continue
            by_version.setdefault(ver, []).append(Extract(item=item, kind="banner", version=ver, fields=fields))
            have.add(item.id)
            added += 1
        log.info("[%s] %s banner search: %d notice(s) by title, %d new", game.key, ver, len(items), added)


async def gather_calendar(ctx, game: Game, records: dict, by_version: dict) -> None:
    """Read the official calendar (api.ennead.cc) for each live or upcoming card whose banner block
    still holds a TBA or an unconfirmed name, on the same budget as the title search (its own stamp,
    `calendar_ts`). It keeps the version's 5★ banners on the record, and the merge fills and confirms
    phases from them. One request per game per run, and none when nothing is due.

    A calendar name only adds a witness: it fills an empty phase, and it locks a name once another
    group (the hub, the wiki, an official notice) names the same character."""
    if not ctx.settings.banner_calendar or ctx.fetcher is None:
        return
    if game.key not in calendarfeed.SLUGS:
        return
    live = (ctx.versions.get(game.key) or {}).get("live")
    due = []
    for ver, rec in records.items():
        if live and version_key(ver) < version_key(live):
            continue                                  # older cards are frozen history
        data = rec.get("data") or {}
        if calendar_due(game, data, ctx.now):
            due.append((ver, rec))
    if not due:
        return
    banners = await calendarfeed.fetch_banners(ctx.fetcher, game.key)
    for ver, rec in due:
        data = rec.setdefault("data", {})
        data["calendar_ts"] = ctx.now                  # asked (answered or not): no retry until due again
        if banners is None:
            continue                                   # the request failed: keep what the card holds
        mine = [b for b in banners if b.get("version") == ver]
        rec["calendar"] = {"ts": ctx.now, "banners": mine}
        if mine and ver not in by_version:
            by_version[ver] = []                       # the merge must run for this version
        fours = sum(1 for b in mine if b.get("featured4"))
        log.info("[%s] %s calendar: %d banner(s) with a 5★ character, %d with a 4★ list",
                 game.key, ver, len(mine), fours)


def calendar_due(game: Game, data: dict, now: int) -> bool:
    """Same budget as the title search: a card whose phases are final never (banner_phases_done), a frozen
    card never, an incomplete block every WIKI_INCOMPLETE_H hours and an unconfirmed one every WIKI_RECHECK_H hours."""
    if banner_phases_done(game, data):
        return False
    start = data.get("maint_start_ts")
    if start and now > int(start) + CARD_FREEZE_D * 86400:
        return False
    interval = WIKI_RECHECK_H if banner_block_complete(game, data) else WIKI_INCOMPLETE_H
    return now - int(data.get("calendar_ts") or 0) >= interval * 3600


async def _sync_mirror(ctx, game: Game, ver: str, record: dict, data: dict, now: int,
                       settled: bool = False, repost: bool = False) -> None:
    """Deliver the same card to the game's own channel, in the same pass — but silently.

    This is a fan-out, not a Discord "follow": the copy is rebuilt from the same data and is
    edited together with the original for the whole life of the version. That is the point —
    a schedule card is a living document (TBA banners fill in, guessed maintenance times are
    replaced by the official notice), so a copy that were only posted once would sit in
    #zzz-news showing estimates forever, which is worse than having no copy at all.

    The copy is built with an empty `Ping()` on purpose, and that is the one way it differs
    from the original. The role has already been mentioned in the schedule channel; mentioning
    it again here would notify every member TWICE for one announcement. An empty Ping also
    removes the mention *text*, so the copy does not render a dead blue @role pill that
    notifies nobody — it just reads as a clean card.

    The copy is strictly a convenience, so every failure is reported and swallowed: a broken
    game-channel webhook must never stop the real card in the schedule channel from being
    posted or kept up to date, and must never fail the run. `mirror_hash` is tracked
    separately from `payload_hash` for the same reason — a copy that failed to update retries
    on the next run without dragging the primary card back through an edit it already made.
    (The two hashes are over different bodies anyway, since only one of them carries a ping.)
    """
    s = ctx.settings
    if record.get("mirror_retired") and not repost and not s.test_mode:
        return                                    # settled, and the copy is gone for good
    name, hook = s.mirror_webhook_source("schedule", game.key)
    if not hook:
        # Switched off, or the secret was removed. Drop the ids so that re-adding it later
        # posts a fresh copy instead of PATCHing a message in a channel we no longer know.
        for key in ("mirror_message_id", "mirror_webhook_fp", "mirror_hash"):
            record.pop(key, None)
        return
    if s.test_mode:
        # Never PATCH a live copy from a test run. Dropping the ids (in memory -- a test never
        # saves state) turns the branch below into a plain POST, so the bench still proves the
        # fan-out works and still leaves you a copy to delete, instead of silently rewriting
        # the production card in the game's channel and stamping a TEST banner on it.
        for key in ("mirror_message_id", "mirror_webhook_fp", "mirror_hash"):
            record.pop(key, None)
    primary = s.webhook("schedule", game.key)
    fp = webhook_fingerprint(hook)
    if primary and webhook_fingerprint(primary) == fp:
        return                                    # same channel — one card is enough

    def build(updated_ts: int | None = None) -> dict:
        payload = schedule_payload(game, data, s, Ping(), updated_ts=updated_ts)
        return mark_test(payload) if s.test_mode else payload

    body = build()                                # no "Updated …" footer -> stable to hash
    h = stable_hash(body["components"])
    mid = record.get("mirror_message_id")
    gone = None                                   # the copy existed, and Discord no longer has it
    if mid and record.get("mirror_webhook_fp") == fp:
        if record.get("mirror_hash") == h:
            return                                # copy already shows exactly this
        res = await ctx.webhook.edit(hook, mid, build(now))
        if res.ok:
            record["mirror_hash"] = h
            ctx.report.append(f"✏️ {game.short} {ver}: {name} copy updated")
            return
        if res.status != 404:
            ctx.report.append(f"⚠️ {game.short} {ver}: {name} copy not updated "
                              f"({res.status}) {res.error}")
            return
        gone = mid
        record.pop("mirror_message_id", None)     # 10008: gone for good -> one replacement
    # The copy follows the original, so CREATING one is held to the same rule the original's
    # 404 path follows: a version that is already out never gets a brand-new message. Callers
    # pass settled=True only from the maintenance path, where the original is an old card being
    # kept up to date; the post-success call site leaves it False because the original has just
    # been published and the copy must land beside it. Editing an existing copy returned above,
    # so living cards keep receiving their silent corrections either way.
    #
    # Without this the fan-out was the louder half of the 2026-10-08 regression: the primary card
    # at least had a settled check to skip, while this send() had none and dropped a fifteen-day-
    # old Genshin 7.1 card straight into #gi-news.
    if settled and not repost:
        record["mirror_retired"] = now
        # "copy not created" reads identically whether the copy was never made or was deleted
        # since, and those are different stories: the first is a fan-out that may never have
        # been wired, the second is somebody deleting a message. When this run is the one that
        # was told the copy is gone (10008), say which message vanished -- seen live on
        # 2026-10-09, where the 21:20 Genshin 7.1 card and its #gi-news copy were both removed
        # and the run could only report that no copy was being created, not that one had been.
        ctx.report.append(
            f"🗂 {game.short} {ver}: {name} copy not created — "
            + (f"copy {gone} was deleted (Discord 10008) and a settled version is not re-posted"
               if gone else "already out")
            + f" (repost with {game.key}:{ver} if you want it anyway)")
        return
    res = await ctx.webhook.send(hook, body)
    if res.ok:
        record.pop("mirror_retired", None)        # a copy exists again -> the retirement is over
        record.update({"mirror_message_id": res.message_id, "mirror_webhook_fp": fp,
                       "mirror_hash": h})
        ctx.report.append(f"🪞 {game.short} {ver}: also posted to {name} (no ping)")
    else:
        ctx.report.append(f"⚠️ {game.short} {ver}: {name} copy failed "
                          f"({res.status}) {res.error}")


# What a silent edit is allowed to be about, in the words the card uses. "Updated" on its own
# is the one report line that never says what it did, which is awkward for the one feature that
# is deliberately silent: a banner filling in, a guessed maintenance time being replaced by the
# official one and a corrected key art all read identically. Order is the order they matter in.
CARD_FIELDS = (("program_ts", "livestream time"), ("banners", "banners"),
               ("banners_display", "display names"),
               ("preinstall_ts", "pre-install"), ("maint_start_ts", "maintenance start"),
               ("maint_end_ts", "maintenance end"), ("compensation", "compensation"),
               ("images", "key art"), ("title_url", "link"), ("version_name", "version name"))


def banner_change_detail(before_banners: dict, after_banners: dict, prov: dict) -> str:
    """"banners" in an edit summary says nothing about WHAT moved. The sub-keys that differ,
    each with the writer groups that hold them (prov's b_<key>_by): run #1349's summary line
    would have read `✏️ ZZZ 3.3: schedule card updated — banners (reruns ← wiki)`. A key with
    no writer group was withdrawn (the wiki retracted it), and renders as the bare key."""
    parts = []
    for k in BANNER_KEYS + ("four_star", "confirmed"):
        if (before_banners or {}).get(k) == (after_banners or {}).get(k):
            continue
        writers = " + ".join((prov or {}).get(f"b_{k}_by") or [])
        parts.append(f"{k} ← {writers}" if writers else k)
    return ", ".join(parts)


def newly_locked_lines(before: dict, after: dict, prov: dict) -> list[str]:
    """One line per banner key that settled between two states, with the witnesses that
    settled it. Locks used to happen in total silence, so "why doesn't this name change any
    more?" was answerable only from the state file; the run summary now says it outright:
    `🔒 ZZZ 3.3: phase1_4 locked (calendar + wiki)`."""
    was, now = banner_settled(before), banner_settled(after)
    lines = []
    for k in BANNER_KEYS + ("four_star", "confirmed"):
        if k not in was and k in now:
            witnesses = " + ".join((prov or {}).get(f"b_{k}_by") or []) or "?"
            lines.append(f"{k} locked ({witnesses})")
    return lines


async def _handle_version(ctx, game: Game, ver: str, extracts: list[Extract], records: dict,
                          override: dict, live_info: dict, bootstrapped: bool,
                          estimates: dict | None = None, media: dict | None = None,
                          banner_feed: list[dict] | None = None) -> None:
    s, now = ctx.settings, ctx.now
    record = records.setdefault(ver, {"status": "new", "first_seen": now})
    notes: list[str] = []
    wiki = await gather_wiki_lineup(ctx, game, ver, record)
    data = merge(game, ver, extracts, record, override, live_info, now, notes, estimates, media,
                 banner_feed, lead_h=observed_lead_h(records), records=records, wiki=wiki,
                 calendar=(record.get("calendar") or {}).get("banners"))
    ctx.report.extend(notes)
    before = record.get("data") or {}      # merge() copies; record["data"] is reassigned below
    changed_fields = [lbl for key, lbl in CARD_FIELDS if before.get(key) != data.get(key)] if before else []
    if before:
        # "banners" alone answers nothing — say WHICH banner keys moved and who wrote them.
        # And a lock (a witness settling a name) changes no field at all, so it needs its own
        # line or it stays invisible: both are receipts for the silent in-place edit that
        # follows, and together they make a run summary enough to answer "what happened to
        # this card?" without opening the state file.
        detail = banner_change_detail(before.get("banners") or {}, data.get("banners") or {},
                                      record.get("prov") or {})
        changed_fields = [f"banners ({detail})" if lbl == "banners" and detail else lbl
                          for lbl in changed_fields]
        for line in newly_locked_lines(before, data, record.get("prov") or {}):
            ctx.report.append(f"🔒 {game.short} {ver}: {line}")
    # The three fields whose absence produced the wrong Genshin 7.1 card -- no air time, the
    # maintenance article as the title link, the Update Details picture as the key art -- all
    # arrive together, from the announcement. Finding them is the repair, and it used to happen
    # in total silence, so there was no way to tell a run that fixed a card from one that did
    # nothing. A card is never posted for it (the version may be settled); this is the receipt.
    pts_now, media_now = data.get("program_ts"), data.get("media_from")
    if pts_now and not before.get("program_ts"):
        # UTC+8 by hand: every one of these games runs on it, and adding a tzinfo
        # import for one summary line is not worth it.
        when = (datetime(1970, 1, 1) + timedelta(seconds=int(pts_now) + 8 * 3600)
                ).strftime("%a %d %b %Y %H:%M")
        ctx.report.append(f"🛰️ {game.short} {ver}: announcement found — programme airs {when} UTC+8"
                          + (f", key art and link from {media_now}" if media_now else ""))
    elif media_now and media_now != before.get("media_from"):
        ctx.report.append(f"🖼️ {game.short} {ver}: key art and title link now come from {media_now}")
    estimated = data.get("estimated") or []
    if estimated and estimated != record.get("estimated_reported"):
        record["estimated_reported"] = estimated
        what = ", ".join(ESTIMATE_LABELS.get(k, k) for k in estimated)
        ctx.report.append(f"🕒 {game.short} {ver}: {what} estimated from "
                          f"{', '.join(data.get('estimate_sources') or ['countdown sites'])} — "
                          "the official notice replaces it automatically")

    # Teach later versions only from a real pair. In particular, the pre-install timestamp this
    # code just derived is in `estimated`, so it can never confirm its own guess on the next run.
    if data.get("preinstall_ts") and data.get("maint_start_ts") \
            and not {"preinstall_ts", "maint_start_ts"}.intersection(estimated):
        offset_h = (int(data["maint_start_ts"]) - int(data["preinstall_ts"])) / 3600
        if 0 < offset_h <= MAX_LEAD_H:
            record["preinstall_offset_h"] = round(offset_h, 3)
    record["data"] = data
    kinds = {e.kind for e in extracts}
    status = record.get("status")
    webhook = s.webhook("schedule", game.key)
    ping = s.ping("schedule", game.key)
    repost = s.repost == f"{game.key}:{ver}"

    maint_start = data.get("maint_start_ts")
    frozen = bool(maint_start and now > int(maint_start) + CARD_FREEZE_D * 86400)

    settled = program_settled(data, now)
    # A test run RENDERS; it never repairs. Without `not s.test_mode` every version that has
    # already been posted -- which is every version anyone would want to check -- takes the edit
    # path below, so `mode=test` silently PATCHes the live card (unlabelled, because only the
    # repost arm marks it) and stamps "🧪 TEST" onto the production copy in the game's channel,
    # while the one thing the test bench promises -- a card you can look at and then delete --
    # is never posted. Falling through to the creation path gives exactly that: one NEW message,
    # marked TEST, built from this run's freshly fetched data, with the live card untouched.
    if status in ("posted", "live") and not repost and not s.test_mode:
        if record.get("card_retired") or not s.edit_on_update:
            return
        if frozen:
            # A frozen card (CARD_FREEZE_D past maintenance) is no longer edited — deliberate,
            # but invisible: the version simply stops appearing in the summary, so "why has my
            # card stopped updating?" has no answer anywhere. Say it once, keyed on the
            # maintenance date the freeze is measured from.
            if record.get("frozen_noted") != int(maint_start):
                record["frozen_noted"] = int(maint_start)
                ctx.report.append(f"🧊 {game.short} {ver}: card frozen — no more edits "
                                  f"{CARD_FREEZE_D} days past maintenance "
                                  f"(repost with {game.key}:{ver} to publish a new one)")
            return
        payload = schedule_payload(game, data, s, ping)
        h = stable_hash(payload["components"])
        # Before the no-change shortcut, never after it: a mirror secret added *after* this
        # version was posted has to be backfilled, and "the card itself did not change" is
        # exactly the state that version is in. Putting this below the return would mean the
        # copy never appears until the card happens to be edited for some other reason.
        await _sync_mirror(ctx, game, ver, record, data, now, settled=settled)
        if h == record.get("payload_hash"):
            return
        mid = record.get("message_id")
        if not webhook or not mid or record.get("webhook_fp") != webhook_fingerprint(webhook):
            record["payload_hash"] = h
            ctx.report.append(f"{game.short} {ver}: card data changed but the original message can't be edited here")
            return
        # The "Updated …" footer goes on the body an EDIT sends, but never into the hash above:
        # a timestamp that moves every run would look like a change and drive an edit loop.
        res = await ctx.webhook.edit(webhook, mid, schedule_payload(game, data, s, ping, updated_ts=now))
        if res.ok:
            record["payload_hash"] = h
            record["updated_at"] = now
            ctx.report.append(f"✏️ {game.short} {ver}: schedule card updated"
                              + (f" — {', '.join(changed_fields)}" if changed_fields else ""))
        elif res.status == 404 and settled:
            # The version is already out, so it is historical. Discord 10008 only says the id
            # no longer resolves -- the message may have been removed, or it may never have
            # existed on this webhook at all, which is exactly the Genshin 7.1 case: the card
            # people actually had was posted by a different bot before this one was deployed.
            # Either way, publishing is not a repair, it is a brand-new card for a programme
            # that aired a month ago, and it would happen again on every single run. Drop the
            # dead id so nothing retries. A test run never reaches this arm: it does not edit
            # live messages at all (see the test_mode check at the top of the edit path), it
            # renders a new marked card instead. REPOST=<game>:<version> is the single
            # deliberate override that re-creates a settled card. The data is still merged and
            # saved either way.
            record["card_retired"] = now
            record.pop("message_id", None)
            # Name the message: "its card is not re-created" is the whole answer, but not the
            # whole story -- an operator who did not delete it themselves needs to know WHICH
            # message failed to resolve. The record dropped the id above, so retain local `mid`.
            ctx.report.append(
                f"🗂 {game.short} {ver}: already out — its card is not re-created "
                f"(message {mid} no longer resolves — Discord 10008 — and a settled version is "
                f"never posted again; repost with {game.key}:{ver})")
        elif res.status == 404:
            # Discord 10008 means this id no longer resolves. Repeating the PATCH would fail
            # forever, so post the current card once and adopt its new id. This arm is reached
            # only when the version is NOT settled -- a programme still in the news, whose card
            # genuinely belongs in the channel. No other edit
            # failure is reposted: a malformed payload or transient outage must not create spam.
            fresh = schedule_payload(game, data, s, ping)
            if s.test_mode:
                fresh = mark_test(fresh)
            reposted = await ctx.webhook.send(webhook, fresh)
            if reposted.ok:
                record.update({"message_id": reposted.message_id, "posted_at": now,
                               "updated_at": now, "webhook_fp": webhook_fingerprint(webhook),
                               "payload_hash": stable_hash(fresh["components"])})
                ctx.report.append(f"♻️ {game.short} {ver}: schedule card re-created "
                                  f"after edit returned 404 {res.error}")
            else:
                # Keep the dead id and old payload hash: the next run retries the same recovery.
                ctx.errors.append(f"{game.short} {ver}: edit returned 404 {res.error}; repost failed "
                                  f"({reposted.status}) {reposted.error}")
        else:
            ctx.errors.append(f"{game.short} {ver}: edit failed ({res.status}) {res.error}")
        return

    # decide whether this version deserves a NEW post
    pts, fresh_program = data.get("program_ts"), False
    if "program" in kinds and pts:
        fresh_program = int(pts) > now - PROGRAM_FRESH_H * 3600
    # A schedule card announces a Special Program / Special Broadcast. A maintenance notice may
    # FILL one in — that is the whole point of the silent-edit design — but it must never be what
    # opens one, or the card is built from the wrong post and shows the wrong title link, the
    # wrong key art and no air time at all. Genshin 7.1 was created this way on 2026-09-25, two
    # days after its own maintenance, and never recovered. So the version must already be one
    # whose announcement this bot has actually seen.
    fresh_maint = bool(s.post_on_maintenance and "maintenance" in kinds and maint_start
                       and int(maint_start) > now - 6 * 3600
                       and (pts or data.get("program_seen")))
    # A schedule card ANNOUNCES. This bot was deployed after Genshin 7.1 and Wuthering Waves 3.7
    # had already aired their programmes, and a version whose programme is already in the past is
    # not an announcement — it is history, posted long ago by somebody else. Such a version is
    # still TRACKED and its record kept current, and a card this bot genuinely posted still gets
    # its corrections (the edit path returns long before here), but a NEW one must never be opened
    # for it. program_settled() was previously consulted only on the 404 arm, so the creation gate
    # below could not see it; that is how Genshin 7.1 — announced 2026-09-07, aired 09-12, shipped
    # 09-23 — got a brand-new card on 2026-10-08. It also closes the hole that the cached recovery
    # in needs_program_lookup() would otherwise open: supplying a long-past programme's program_ts
    # must never let a six-hour-wide maintenance notice promote it into a fresh post.
    if settled and not repost:
        fresh_program = fresh_maint = False
    if s.test_mode and (pts or maint_start):
        fresh_program = True                  # TEST MODE: show the latest real card, even if older
    if not (fresh_program or fresh_maint or repost):
        if status != "new" and not bootstrapped and "maintenance" in kinds and maint_start \
                and int(maint_start) > now - 6 * 3600 \
                and record.get("notice_only_at") != int(maint_start):
            # A notice arrived and deliberately did NOT open a card. Both reasons are correct
            # behaviour and both look identical from outside -- a run that posts nothing -- so
            # the one place they can be told apart is here. Keyed on the notice's own timestamp:
            # reported once, not every ten minutes for the six hours the notice stays fresh.
            record["notice_only_at"] = int(maint_start)
            if settled:
                ctx.report.append(f"🗂 {game.short} {ver}: notice merged into a version that is "
                                  f"already out — its card is kept current, no new card is opened")
            else:
                ctx.report.append(f"🗂 {game.short} {ver}: maintenance notice recorded — no Special "
                                  f"Program announcement has been seen for this version, so it opens "
                                  f"no card. The times appear on the card the moment one is found")
        if status == "new":
            record["status"] = "tracked"
            if not bootstrapped:
                ctx.report.append(f"🗂 {game.short} {ver}: already out / program long past — recorded, "
                                  f"nothing to announce (repost with {game.key}:{ver} if you want it anyway)")
        return
    if not bootstrapped and not (s.bootstrap_post or s.test_mode) and not repost:
        record["status"] = "seeded"
        ctx.report.append(f"🌱 {game.short} {ver}: seeded silently (first run — set BOOTSTRAP_POST=1 to post)")
        return
    if status == "seeded" and not repost and not s.test_mode:
        return                                    # a test run renders it; nothing is saved
    if not webhook:
        ctx.report.append(f"⚠️ {game.short} {ver}: no schedule webhook — add the secret "
                          f"{s.expected_webhook_names('schedule', game.key)}")
        return
    payload = schedule_payload(game, data, s, ping)
    if s.test_mode:
        payload = mark_test(payload)
    res = await ctx.webhook.send(webhook, payload)
    if res.ok:
        # A retirement is a statement about a message that no longer exists. This run has just
        # published a replacement, so it has to be lifted -- otherwise the guard at the top of
        # the edit path would see card_retired and return, and the brand-new card would never
        # receive another correction for the rest of its life.
        record.pop("card_retired", None)
        record.update({"status": "posted", "message_id": res.message_id, "posted_at": now,
                       "webhook_fp": webhook_fingerprint(webhook),
                       "payload_hash": stable_hash(payload["components"])})
        if s.test_mode:
            ctx.report.append(f"🧪 {game.short} {ver}: TEST card posted as a NEW message — delete it "
                              f"when you are done. The live card and its copy were not touched, and "
                              f"the state file was not written")
        else:
            ctx.report.append(f"📜 {game.short} {ver}: schedule card posted")
        # Same run, same data, so the copy lands together with the original — only the ping is
        # left off. Strictly after the real card succeeded: if the schedule channel rejected
        # the post there is nothing worth copying anywhere.
        await _sync_mirror(ctx, game, ver, record, data, now, repost=repost)
    else:
        ctx.errors.append(f"{game.short} {ver}: post failed ({res.status}) {res.error}")
