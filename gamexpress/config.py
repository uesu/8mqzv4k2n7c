"""Settings (environment) + per-game config (config/games.json).

Secrets (GitHub → Settings → Secrets):   webhook URLs, NITTER_RSS_TOKEN, REDDIT_FEED_TOKEN
Variables (GitHub → Settings → Variables): everything else — ping roles, emojis,
feature switches, instance role. Nothing in a variable is sensitive, so they are
visible/editable in one click (the "easy, intuitive" switches).
"""

from __future__ import annotations

import html
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DEFAULT_STATE_PATH = ROOT / "state" / "state.json"

FEATURES = ("schedule", "codes", "news", "maintenance", "beta")
DEDICATED_FEATURES = ("news", "maintenance", "beta")
DEFAULT_OPTIONAL_GAMES = ("genshin", "starrail", "zzz", "wuwa", "hna")
DEFAULT_BETA_GAMES = ("genshin", "starrail", "zzz", "wuwa")
ALL_MAINTENANCE_GAMES = ("genshin", "starrail", "zzz", "wuwa", "hna", "ananta")
DEFAULT_COLOR = 14922399  # the gold accent used by every reference card
MAINTENANCE_NOTE = ("※ In the event that the maintenance is extended, the dev team will issue a "
                    "follow-up notice and adjust the compensation accordingly.")

# Emojis from the reference cards (animated, from the user's server). "title" is the scroll on
# the card headline, every game; EMOJI_TITLE="none" leaves the headline bare.
DEFAULT_EMOJI = {
    "title": "ananta1:1558340472525693018",
    "youtube": "a:kurobbseventcalendar:1508619925940473966",
    "twitch": "a:hsrbbsevanescia:1508647148902813776",
    "source": "a:starwardcat:1564242587987087420",
    "redeem": "🎁",
}

# Shown on every CODES card, in its own row under the codes (separated from the per-code
# Redeem links): a link to the user's own community server. Override with the variable
# COMMUNITY_BUTTONS (JSON list, max 3) — set it to "none" to switch the row off.
DEFAULT_COMMUNITY_BUTTONS = [
    {"label": "Citlali News", "url": "https://discord.gg/HyrVP9wRXu",
     "emoji": "a:starward11:1439878792653832253"},
]

# nitter.cf leads: it is the mirror this monitor is built against and the one with the longest
# unbroken run of live production evidence (6/6 handles, every run). The chain stops at the
# first TWO instances that answer with real entries. What follows is the fleet's dated audit
# trail, oldest first -- each rotation is a measurement, and the list below is its verdict.
#
# SLOTS 1-4 ARE THE HOT PATH. twitter.py probes NITTER_BATCH (= 4) instances IN PARALLEL on
# every run, for every handle, whether or not the ones ahead of them answered -- so a mirror
# that cannot answer costs a request on every single run from slot 4 just as surely as slot 1.
#
# This list was ordered by published uptime from status.d420.de on 2026-09-27, which put
# nitter.meowing.monster (95%) and nitter.netbub.com (93%) into slots 3 and 4. Live run
# 36305123457 measured what GitHub's runners actually get from them:
#   * nitter.meowing.monster -- HTTP 200 but 0 entries (bot check / stale), all 4 handles
#   * nitter.netbub.com      -- HTTP 403, all 4 handles
# 8 wasted requests per run -- the same failure x.n0g.xyz was demoted for, twice over. A
# published uptime figure is not a measurement of what THIS client gets, so the hot path is
# now ordered by what the runners observed, and the two are demoted rather than deleted:
# they answer for other clients, and they may work from a VPS.
#
# NOTHING IN THIS FLEET IS DELETED -- MIRRORS ARE DEMOTED, NOT REMOVED. x.n0g.xyz answered
# HTTP 404 then 429 on every handle in three consecutive live runs (36296323488, 36298632006,
# 36299586254), so it sits dead last rather than being cut. That placement is free:
# twitter.XClient.timeline() breaks out of its batch loop the moment TWO instances have
# answered, so anything behind that point costs zero requests in a healthy run -- and is a
# real backup again the run the fleet ahead of it degrades. Deleting a mirror throws away a
# host that may simply be blocking GitHub's runners while answering fine from a VPS.
#
# 2026-10-03 AUDIT. Every host below was fetched by hand on /Wuthering_Waves/rss and the
# BODY was read, not just the status code -- status.d420.de probes the homepage, which is why
# it still scores two walled mirrors as "RSS ✅". Three of the four hot-path slots were dead
# weight: xitter.cf is the SAME BACKEND as nitter.cf (every link in its feed points back at
# nitter.cf -- a second domain, not a second mirror), and nitter.jaydenha.uk answers /rss with
# a "click anywhere to enter" splash page. Two mirrors verified to serve clean RSS take their
# places; both are new to the hot path and the next live run is what confirms them from
# GitHub's runners, so nitter.cf -- the only host with production evidence -- still leads.
#
# 2026-10-03 04:25 RUN -- BOTH OF THOSE PROMOTIONS FAILED, AND THAT IS THE POINT OF THE RULE:
# a hand probe from a VPS does not predict what an Actions runner gets.
#   * nitter.kareem.one -- HTTP 403 on all 6 handles, despite a clean feed off-runner and the
#     best score on status.d420.de. Demoted to the runner-blocked tier.
#   * tw.eir-nya.gay    -- not blocked, just SLOW: still silent when nitter.cf + miningtcup had
#     already answered, so NITTER_GRACE killed it on 4/6 handles, and it returned 0 entries on
#     the other 2. A mirror that cannot win the race is dead weight in a parallel slot, so it
#     moves to the head of the fallbacks instead.
# Slots 2 and 3 now carry the next two candidates, and the same rule applies to them: they are
# TRIALS until a live run says otherwise. nitter.cf + nitter.miningtcup.me answered 6/6 handles
# each, so the quorum never depends on a trial slot -- an experiment here costs wasted requests
# at worst, never a missed announcement. Nothing is deleted, as always.
#
# Note the real ceiling this exposes: nitter.cf and xitter.cf are ONE machine, so the only
# independent redundancy today is the token-gated mirror. Finding a second backend that answers
# Actions IPs is what these two trial slots are for.
#
# 2026-10-13 RUN #2312 -- ROTATION #3, SAME RULE: shitter.thepixora.com (slot 2's TRIAL since
# 2026-10-03) got its runner verdict -- HTTP 403 on all 6 handles, a clean body off-runner, the
# exact nitter.kareem.one signature. It joins the runner-blocked tier (NITTER_CANNOT_ANSWER in
# the test suite). The slot it vacated does NOT go to an untested candidate -- there isn't one
# -- it goes to tw.eir-nya.gay, promoted back from the fallbacks despite still losing the
# NITTER_GRACE race (that verdict stands unchanged): a mirror that is merely slow can still win
# the race on a day nitter.cf or miningtcup answers late, while a mirror GitHub's runners get
# 403'd from contributes nothing, ever. Lesser evil over guaranteed-dead weight. Demoted, not
# deleted: thepixora moves back up the day it answers the runners again.
#
# 2026-10-13 ROTATION #4 (run #2323, the first run after #3): the eir-nya promotion answered
# the runners with HTTP 200 and 0 entries on all six handles -- a body that parses to nothing.
# That is its second measured failure (it lost the NITTER_GRACE race on 2026-10-03), so slot 4
# goes back to a mirror with runner evidence: xitter.cf answered throughout its hot-path tenure
# before the 2026-10-03 audit demoted it for being nitter.cf's second domain -- a redundancy
# demotion, never a failure. Slot 4's job is to answer, not to add independence (the fleet's
# independent answerers are meowing.monster and the token-gated miningtcup.me either way).
# eir-nya drops to the fallbacks behind thepixora; nothing is deleted, and it moves back up
# the day it delivers a real feed from the runners.
#
# 2026-10-14 ROTATION #5 (v3, post-test): meowing.monster went dark ~8 h (05:15-13:35 UTC,
# ClientConnectorError on every handle, runs #2603-#2616), the first spec demoted it, its
# pre-check stopped that PR when the host revived (~13:35: d420 healthy at 13:45, 5/6
# handles delivered from the runners in run #2621), and the operator then ordered a
# full-coverage test before deciding. Result: xcopy.uk answered 8/8 monitored handles
# across the day (all six game feeds + the two News-Express accounts, bodies read, top
# tweet ids matching the nitter.cf controls -- current, not stale), while meowing's
# revival is real but structurally slow for GitHub's IPv4-only runners (d420 lists the
# host IPv6; the Game-Express bot measured NITTER_GRACE kills even after recovery, 5/6
# at best). So slot 2 passes to xcopy.uk as a TRIAL -- the hot path's every-run probe is
# the only mechanism that converts body evidence into a runner verdict, and the quorum
# never depends on slot 2 (slots 1/3/4 are three measured answerers, NITTER_WORKING=2).
# meowing, a proven answerer through one bad day, takes the HEAD of the fallbacks: zero
# cost while the hot path is healthy, first backup, back up the day it outdelivers the
# trial. No ledger moves -- a dark day plus revival is a position change, not a verdict.
# The audit that chose xcopy: nitter.pp.ua (98% on d420) and nitter.poast.org refused
# the hand probe on /rss outright -- no readable body, no trial -- and the same-day
# fill-in sweep closed the pp.ua question for good: its /rss serves bodies generated
# by shitter.thepixora.com, an alias of a host this fleet already audits, never an
# independent candidate (do not re-probe); shi.meowing.de fronts a browser challenge;
# xnologin.com redirects to a "RSS feed is disabled" page; nitter.privacyredirect.com
# errors; nitter.catsarch.com shut down. Slots 1/3/4 untouched.
# If the first run after merge shows the runners 403 xcopy: rollback is one line (meowing
# back to slot 2), and THAT verdict is what the ledgers record.
#
# 2026-10-15 ROTATION #6 (the trial's verdict + the operator's re-rank): the first
# four post-merge runs delivered the verdict rotation #5 was built for -- xcopy.uk
# returned HTTP 403 to the runners on all 6 handles, every run, across four Azure
# regions (westus, westus3, eastus2, centralus; runs #2662-#2665, 24/24 fetches),
# the exact nitter.kareem.one signature: clean body off-runner, flat refusal to
# Actions IPs. The operator then set this round's ranking rule -- no member that
# blocks GitHub Actions / Azure datacenter IPs sits above one that doesn't -- and
# directed the head: nitter.cf, xitter.cf, meowing.monster, miningtcup.me; the first
# time all four hot slots carry positive runner evidence (meowing's IPv6 slowness is
# a grace cost on a handle, never a quorum cost: slots 1/2/4 answer, the engine
# needs two). The union-coverage test then filled the bench: freedit.eu is the only
# host answering 8/8 union handles with nitter.cf parity and zero refusals anywhere
# (fallback head, TRIAL); gravitywell.xyz answers 5/8 with deterministic not-found
# gaps (GenshinImpact new this round, plus Wuthering_Waves and HonkaiNA) whose
# complement is kareem.one at the bottom, so between them every union handle is
# covered somewhere below the hot path (TRIAL). eir-nya passes the no-block rule
# but keeps the fleet's only measured runner non-deliveries (grace losses,
# 200-with-0-entries on all six, and 429s on every GE-handle probe this round,
# retry included), so it sits behind the two trials; thepixora's runner 403 flapped
# fresh (blocked 10-13 three days after 25 ok on 10-10), so it sits behind everyone
# who has never refused. xcopy records its verdict beside kareem, demoted not
# deleted. Fleet 19 -> 21; nothing deleted, as always.
#
# 2026-10-22 ROTATION #7 (teapawt TLD expansion + mie.foundation): all six new
# candidates answered 8/8 union handles live with 100% top-tweet id parity against
# nitter.cf/xitter.cf and zero refusals -- all five newly listed 4cuck/teapawt sibling
# domains on status.d420.net (commit 145ca9eb, 2026-10-22 07:55 UTC): teapawt.org
# (426 ms avg, 100%), xitter.gq (450 ms, 100%), nitter.gq (455 ms, 100%), teapawt.net
# (462 ms, 100%), and teapaw.org (488 ms, 100%), plus codeberg.org/shitter/shitter's
# working-list host nitter.mie.foundation (rev a75ff9fa14). Every RSS link on
# teapawt.org, teapaw.org, teapawt.net, xitter.gq and nitter.gq points back at
# xitter.cf -- they front the same runner-proven teapawt origin behind independent
# .org/.net/.gq TLDs alongside .cf, with explicit operator scraping permission on
# status.d420.net and working /<account>/status/<id>/rss feeds. Keeping all four
# runner-proven hosts in Batch 1 (nitter.cf, xitter.cf, meowing, miningtcup) avoids
# extra parallel hits on the teapawt origin during normal runs while avoiding
# un-runner-tested 403 risk; Batch 2 (slots 5-8) opens with teapawt.org -> teapaw.org
# -> teapawt.net -> nitter.mie.foundation (TRIAL), and Batch 3 (slots 9-12) opens with
# xitter.gq -> nitter.gq -> nitter.freedit.eu -> nitter.gravitywell.xyz, preserving
# multi-backend diversity in every 4-host wave while spanning .cf/.org/.net/.gq.
# Fleet 21 -> 27 (26 live); nothing deleted.
DEFAULT_REDDIT = [
    # Tier 1 — Active healthy dual-path (`200 OK` across `/r/<sub>/new`, `/user/<acct>`, AND
    # `/r/<sub>/comments/<id>/`) + token-gated private Redlib (`NITTER_RSS_TOKEN`).
    # `https://www.reddit.com` leaps to slot 0 automatically on `/r/<sub>/new.rss?feed=...` when
    # `REDDIT_FEED_TOKEN` is configured; without a token, unauthenticated `reddit.com` /
    # `old.reddit.com` 403 on Azure Actions runner IPs and sit in Tier 3 below (`0` 403 waste).
    # Fleet 22 -> 24 (`2026-10-24`): added `redlib.reallyaweso.me` (Tier 1 full dual-path `200 OK`
    # on `/new`, `/user/`, and `/comments/` with full multi-image gallery + comments) and
    # `redlib.ducks.party` (Tier 2 `v0.40.0` `/new` listing with inline gallery carousels), and
    # demoted `redlib.vanillax.me` to Tier 3 (`The backend server is having issues` error body);
    # Fleet 24 -> 27 (`2026-10-27`): added `r.naa.gg` (`Nachtalb/redlib` GraphQL engine),
    # `redlib.thepixora.com` (`PR #603` GraphQL comments patch), and `redlib.r4fo.com`
    # (`redlib-instances#121` / `redlib#604` verified working instance); excluded `redlib.cow.rip`
    # (`redlib-instances#120` Cloudflare + custom verification wall); nothing deleted.
    "https://redlib.mint.lgbt",           # Redlib 0.36.0 (`0baefabf` Shreddit build), full listing + `/user/` + `/comments/`
    "https://reddit.idevicehacked.com",   # Redlib 0.40.0 (`848f3013`), full listing + inline gallery + `/user/` + `/comments/`
    "https://redlib.reallyaweso.me",      # Redlib (`redlib.blueberry.reallyaweso.me`), full `/new` + `/user/` + `/comments/` 200 OK
    "https://r.naa.gg",                   # Nachtalb/redlib GraphQL primary instance, full `/new` + `/user/` + `/comments/` 200 OK
    "https://redlib.thepixora.com",       # Redlib w/ PR #603 GraphQL comments patch, full `/new` + `/user/` + `/comments/` 200 OK
    "https://redlib.r4fo.com",            # Verified working Redlib instance (`redlib-instances#121` / `redlib#604`)
    "https://redlib.catsarch.com",        # runner-proven fast `/new` & `/user/` answerer
    "https://snoo.habedieeh.re",          # Redlib v3.0.0 (#592), listing + `/user/` + 1080p CMAF
    "https://redlib.miningtcup.me",       # token-gated private Redlib (`NITTER_RSS_TOKEN`)
    "https://redlib.tiekoetter.com",      # Redlib (#592), `/new` + `/user/`
    "https://redlib-1.privadency.com",    # direct Privadency node #1 (bypasses splash wall)
    "https://redlib-2.privadency.com",    # direct Privadency node #2 (bypasses splash wall)
    # Tier 2 — Verified `/new` (+ `/user/`) listing-only mirrors (`REDDIT_LISTING_ONLY`; skipped on `/comments/`)
    "https://redlib.ducks.party",         # Redlib 0.40.0 (`848f3013`), `/r/<sub>/new` 200 OK with inline gallery carousels
    "https://reddit.jeremyh.xyz",         # Redlib 0.31.0 (`redlib-instances#118`), US direct `/new` + `/user/` 200 OK
    "https://redlib.jeikobu.net",         # Redlib 0.36.0 (`fca8b45c`), `/new` + `/user/` 200 OK
    "https://red.artemislena.eu",         # `/new` + `/user/` 200 OK; pre-#592 on `/comments/`
    "https://rl.awy.one",                 # `/new` + `/user/` 200 OK; pre-#592 on `/comments/`
    "https://redlib.freedit.eu",          # `/new` + `/user/` 200 OK; pre-#592 on `/comments/`
    "https://redlib.utilibre.org",        # `/new` + `/user/` 200 OK; pre-#592 on `/comments/`
    # Tier 3 — Token-promoted official origins & demoted/degraded legacy tail (`0` hits while Tier 1 answers)
    "https://www.reddit.com",             # promoted to head when `REDDIT_FEED_TOKEN` is set; 403 unauth on Actions IPs
    "https://old.reddit.com",             # official RSS fallback origin (403 unauth on Actions IPs)
    "https://redlib.vanillax.me",         # demoted (`2026-10-24`): HTTP 200 `The backend server is having issues` error body
    "https://redlib.privacyredirect.com", # demoted (`2026-10-23`): upstream 403 error page on `/comments/` & `/user/`
    "https://eddrit.com",                 # legacy RSS frontend (`/r/<sub>/.rss`, rate-limited)
    "https://redlib.privadency.com",      # legacy splash redirect in front of `redlib-1` / `redlib-2`
    "https://safereddit.com",             # legacy mirror (upstream 429 rate-limited)
    "https://redlib.nadeko.net",          # legacy mirror (`__goaway_challenge` bot wall)
]

DEFAULT_REDDIT_PROXIES = [
    "https://www.vxreddit.com",           # primary: oEmbed JSON + muxed video+audio `redditvideo.mp4`
    "https://embedez.com",                # secondary: `providers/search` + `proxy.embedez.com` 1080p muxed fallback
    "https://embeddit.deltandy.me",       # tertiary: v1 Mastodon JSON + v2 `discord:component-embed` (`Discordbot/2.0`)
]

DEFAULT_REDDIT_ARCHIVE = "https://arctic-shift.photon-reddit.com"

DEFAULT_NITTER = [
    # ── hot path: probed on EVERY run (NITTER_BATCH = 4) — measured answerers only ──
    "https://nitter.cf",                # proven from the runners: 6/6 handles, every run
    "https://xitter.cf",                # rotation #6 (2026-10-15): back to slot 2 -- it
                                        # ANSWERED from the runners across its whole
                                        # hot-path tenure (run #2323) and was only ever
                                        # demoted for being nitter.cf's second domain,
                                        # never for failing a probe. The rotation-5 trial
                                        # it replaces got its verdict (see the refused
                                        # tier below); the slot goes back to a measured
                                        # answerer.
    "https://nitter.meowing.monster",   # rotation #6 (2026-10-15): OPERATOR-DIRECTED
                                        # slot 3, up from the fallback head. A PROVEN
                                        # answerer (6/6 in run #2323, 5/6 in run #2621)
                                        # whose body re-verified this round
                                        # (GenshinImpact + ZZZ_EN identical top ids to
                                        # the nitter.cf controls; one transient HTTP 500
                                        # on GenshinImpact that cleared on retry). Known
                                        # cost, kept on purpose: d420 lists the host IPv6
                                        # and the runners are IPv4-only, so it may lose
                                        # a NITTER_GRACE race on a handle -- grace, never
                                        # the quorum (slots 1/2/4 answer; the engine
                                        # needs two).
    "https://nitter.miningtcup.me",     # token-gated — proven from the runners: 6/6 handles
    # ── fallbacks (batch 2+): zero requests while batch 1 gives two working feeds ──
    "https://teapawt.org",              # rotation #7 (2026-10-22): NEW, head of batch 2.
                                        # Official 4cuck/teapawt .org sibling domain of
                                        # nitter.cf/xitter.cf (493 ms avg on d420, #1
                                        # fastest, 100% uptime, explicit scraping
                                        # permission, 8/8 union + /status/<id>/rss).
                                        # Same runner-proven teapawt origin on an
                                        # independent .org TLD: zero load while .cf
                                        # answers, instant failover if .cf DNS degrades.
    "https://teapaw.org",               # rotation #7 (2026-10-22): NEW. Official
                                        # 4cuck/teapawt .org sibling (548 ms avg on d420,
                                        # 100% uptime, 8/8 union + /status/<id>/rss).
    "https://teapawt.net",              # rotation #7 (2026-10-22): NEW. Official
                                        # 4cuck/teapawt .net sibling (578 ms avg on d420,
                                        # 100% uptime, 8/8 union + /status/<id>/rss).
    "https://nitter.mie.foundation",    # rotation #7 (2026-10-22): NEW, independent
                                        # shitter/shitter working-list host (rev
                                        # a75ff9fa14). 8/8 union handles verified with
                                        # 100% top-id parity, zero refusals, native t.co
                                        # expansion; rewrites YouTube links to
                                        # piped.video (handled by youtube_id). TRIAL.
    "https://xitter.gq",                # rotation #7 (2026-10-22): NEW, head of batch 3.
                                        # Official 4cuck/teapawt .gq sibling on
                                        # status.d420.net (450 ms avg, 100% uptime,
                                        # explicit scraping permission, 8/8 union +
                                        # /status/<id>/rss). Seated alongside independent
                                        # shitter/nitter hosts in batch 3 so neither
                                        # batch 2 nor batch 3 is single-origin.
    "https://nitter.gq",                # rotation #7 (2026-10-22): NEW. Official
                                        # 4cuck/teapawt .gq sibling on status.d420.net
                                        # (455 ms avg, 100% uptime, explicit scraping
                                        # permission, 8/8 union + /status/<id>/rss).
    "https://nitter.freedit.eu",        # rotation #6 (2026-10-15): NEW, head of the
                                        # fallbacks. The only host in existence with
                                        # full union coverage: 5/5 News-Express accounts
                                        # (2026-10-14) + 3/3 Game-Express-exclusive
                                        # handles this round (GenshinImpact,
                                        # honkaistarrail, ZZZ_EN -- identical top ids to
                                        # the nitter.cf controls, entries current through
                                        # Oct 14 10:00 UTC) = 8/8, and zero refusals on
                                        # record anywhere, from any vantage. Not on
                                        # d420, zero runner evidence: TRIAL -- the
                                        # fallback head costs zero requests while the
                                        # hot path is healthy, and the first degraded
                                        # day is its runner test.
    "https://nitter.gravitywell.xyz",   # rotation #6 (2026-10-15): NEW. 5/8 union:
                                        # honkaistarrail, ZZZ_EN, TYPEII_EN,
                                        # PomPom_HonkaiSR and Ananta_EN serve current
                                        # with identical top ids; `User not found` on
                                        # GenshinImpact (first seen this round) plus the
                                        # known Wuthering_Waves and HonkaiNA gaps --
                                        # deterministic account-resolution failures,
                                        # kareem style, not downtime. Its complement is
                                        # kareem.one in the refused tier (covers
                                        # Wuthering_Waves, HonkaiNA, Ananta_EN), so
                                        # between them every union handle is covered
                                        # below the hot path. Zero runner evidence:
                                        # TRIAL; moves per evidence.
    "https://tw.eir-nya.gay",           # rotation #6 (2026-10-15): passes the no-block
                                        # rule (it answers the runners HTTP 200) but
                                        # holds the fleet's only measured runner
                                        # non-deliveries: NITTER_GRACE race losses
                                        # (2026-10-03) then HTTP 200 with 0 entries on
                                        # all 6 handles (rotation #4); this round it
                                        # 429'd every GE-handle probe, retry included --
                                        # reachable, throttled, never a hard refusal.
                                        # Seated behind the two zero-history TRIALs;
                                        # moves up the day a run log shows a real
                                        # delivery.
    "https://shitter.thepixora.com",    # FLAPPING runner 403: blocked on all 6 handles
                                        # 2026-10-13 (run #2312) three days after
                                        # 25 ok / 0 fail on 2026-10-10 (runs
                                        # #1378/#1386); clean body off-runner. Seated
                                        # below every member that has never refused the
                                        # runners; moves back up the day it answers
                                        # again.
    "https://nitter.jaydenha.uk",       # demoted: /rss serves a "click anywhere to enter" splash
    "https://nitter.click",             # browser check (__gandalf) in front of /rss
    "https://nitter.tiekoetter.com",    # last-resort: Anubis proof-of-work wall
    "https://nitter.xitter.cc",         # Cloudflare 502 Bad gateway
    "https://nitter.perennialte.ch", "https://nitter.privacydev.net", "https://nitter.net",
    # ── last: known to refuse GitHub's runners; a request only when everything above failed ──
    "https://nitter.kareem.one",        # HTTP 403 on all 6 handles (run 2026-10-03 04:25) even
                                        # though it serves a clean feed off-runner
    "https://nitter.netbub.com",        # HTTP 403 to GH runners (36305123457); off-runner it
                                        # answers only behind a __goaway_challenge redirect
    "https://xcopy.uk",                 # rotation #5 TRIAL VERDICT (2026-10-15): HTTP 403
                                        # on all 6 handles in each of the first four
                                        # post-merge runs (#2662-#2665, 2026-10-14
                                        # 17:19-17:30 UTC) across four Azure regions
                                        # (westus, westus3, eastus2, centralus) -- 24/24
                                        # fetches refused, the most thoroughly measured
                                        # hard refusal in this fleet's history, the
                                        # kareem.one signature: a clean body off-runner
                                        # (8/8 union handles, nitter.cf parity,
                                        # 2026-10-14) and a flat refusal to Actions /
                                        # Azure IPs. The trial worked exactly as
                                        # designed: the body bought the seat, the
                                        # every-run probe cashed it in, and the quorum
                                        # never depended on slot 2. Demoted, not
                                        # deleted; moves back up the day it answers the
                                        # runners.
    "https://xcancel.com",              # suspended 2026-09-14
    "https://x.yuuki.sh",               # HTTP 403 to GH runners (2026-09-25)
    "https://x.n0g.xyz",                # dead last, kept as a backup: 404 then 429 in runs
                                        # 36296323488 / 36298632006 / 36299586254
]


# --------------------------------------------------------------------------- env helpers
def _env(env: Mapping[str, str], name: str, default: str = "") -> str:
    v = env.get(name)
    return default if v is None else str(v).strip()


def _bool(env, name, default=False) -> bool:
    v = _env(env, name, "")
    if v == "":
        return default
    return v.lower() in ("1", "true", "yes", "on", "y")


def _int(env, name, default: int) -> int:
    try:
        return int(_env(env, name, str(default)) or default)
    except ValueError:
        return default


def slug(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", key).upper()


def parse_emoji(value: str | None) -> dict | None:
    """'a:name:123' | 'name:123' | '<a:name:123>' | '<:name:123>' | unicode | '' / 'none'."""
    if value is None:
        return None
    v = html.unescape(value).strip()
    if not v or v.lower() in ("none", "off", "0", "false"):
        return None
    # The animated marker needs its own colon ('a:name:id'). A plain 'name:id' whose name
    # happens to start with 'a' is a STATIC emoji: 'ananta1:1558…' is <:ananta1:…>, never an
    # animated <:nanta1:…>.
    m = re.fullmatch(r"<?(a):([A-Za-z0-9_~]{1,32}):(\d{15,25})>?", v)
    if m:
        return {"id": m.group(3), "name": m.group(2), "animated": True}
    m = re.fullmatch(r"<?:?([A-Za-z0-9_~]{1,32}):(\d{15,25})>?", v)
    if m:
        return {"id": m.group(2), "name": m.group(1)}
    return {"name": v[:8]}  # unicode emoji


@dataclass
class Ping:
    """Resolved mention for one card. Empty = no ping (the 'optional ping' toggle)."""
    roles: list[str] = field(default_factory=list)
    everyone: bool = False
    here: bool = False

    @property
    def text(self) -> str:
        parts = [f"<@&{r}>" for r in self.roles]
        if self.everyone:
            parts.insert(0, "@everyone")
        if self.here:
            parts.insert(0, "@here")
        return " ".join(parts)

    @property
    def allowed_mentions(self) -> dict:
        # Never lets anything ping except exactly what was configured.
        am: dict[str, Any] = {"parse": []}
        if self.everyone or self.here:
            am["parse"] = ["everyone"]
        if self.roles:
            am["roles"] = self.roles[:100]
        return am

    def __bool__(self) -> bool:
        return bool(self.roles or self.everyone or self.here)


def parse_ping(value: str) -> Ping | None:
    """None = inherit (unset); Ping() = explicit no-ping ('none'/'off')."""
    v = html.unescape(value or "").strip()
    if v == "":
        return None
    if v.lower() in ("none", "off", "no", "false", "0", "-"):
        return Ping()
    p = Ping()
    for tok in re.split(r"[,\s]+", v):
        t = tok.strip().lstrip("<@&").rstrip(">")
        if not t:
            continue
        if t.lower() in ("everyone", "@everyone"):
            p.everyone = True
        elif t.lower() in ("here", "@here"):
            p.here = True
        elif t.isdigit() and 15 <= len(t) <= 25:
            p.roles.append(t)
    return p


# --------------------------------------------------------------------------- settings
@dataclass
class Settings:
    env: Mapping[str, str]
    features: set[str]
    games_filter: set[str]
    dry_run: bool
    bootstrap_post: bool
    edit_on_update: bool
    post_on_maintenance: bool
    lookback_hours: int
    codes_min_sources: int
    x_enabled: bool
    nitter_instances: list[str]
    nitter_token: str
    instance_name: str
    instance_role: str          # primary | standby
    peer_state_url: str
    failover_after_min: int
    heartbeat_min: int
    state_path: Path
    extra_buttons: list[dict]
    community_buttons: list[dict]
    emoji: dict[str, dict | None]
    show_legend: bool
    repost: str
    http_timeout: int
    no_ping: bool = False              # NO_PING=1 -> never ping (test runs)
    force_webhook: str = ""            # FORCE_WEBHOOK -> every card goes to ONE channel (test channel)
    test_mode: bool = False            # TEST_MODE=1 -> post current items, mark cards as TEST (never commit)
    enable_games: set[str] = field(default_factory=set)   # ENABLE_GAMES=hna,ananta -> switch on prepared games
    aliases: dict[str, list[str]] = field(default_factory=dict)   # game key -> extra env-name slugs (GI, HSR…)
    codes_mark_expired: bool = True    # edit posted code cards when every source lists the code as expired
    countdown_estimates: bool = True   # COUNTDOWN_ESTIMATES=0 -> never fill times from countdown sites
    program_media: bool = True         # PROGRAM_MEDIA=0 -> never look an announcement up on the news page
    banner_feed: bool = True           # BANNER_FEED=0 -> never fill banner lineups from hub.json
    banner_search: bool = True         # BANNER_SEARCH=0 -> never look up banner notices by title
    banner_calendar: bool = True       # BANNER_CALENDAR=0 -> never read the official calendar (api.ennead.cc) for banner names
    gacha_wiki: bool = True            # GACHA_WIKI=0 -> never fill banner lineups from the game wikis
    reddit_enabled: bool = True
    reddit_instances: list[str] = field(default_factory=lambda: list(DEFAULT_REDDIT))
    reddit_proxies: list[str] = field(default_factory=lambda: list(DEFAULT_REDDIT_PROXIES))
    reddit_token: str = ""

    news_enabled: bool = False
    news_games: set[str] = field(default_factory=set)
    news_group_long_strips: bool = False
    news_timestamps: bool = False
    news_expand_full: bool = False
    news_expand_max_pages: int = 12
    news_expand_max_chars: int = 120000
    news_expand_max_media: int = 120
    maintenance_enabled: bool = False
    maintenance_games: set[str] = field(default_factory=set)
    beta_enabled: bool = False
    beta_games: set[str] = field(default_factory=lambda: set(DEFAULT_BETA_GAMES))

    # -- routing ---------------------------------------------------------------
    def _game_slugs(self, game_key: str) -> list[str]:
        out = [slug(game_key)]
        for a in self.aliases.get(game_key, []):
            if slug(a) not in out:
                out.append(slug(a))
        return out

    def webhook_source(self, feature: str, game_key: str) -> tuple[str, str | None]:
        """(secret name, url). Order: FORCE_WEBHOOK (test channel) > DISCORD_WEBHOOK_<FEATURE>_<GAME>
        (the game key, or its short name: CODES_GENSHIN / CODES_GI, CODES_STARRAIL / CODES_HSR …)
        > DISCORD_WEBHOOK_<FEATURE> > DISCORD_WEBHOOK_URL."""
        if self.force_webhook and feature not in DEDICATED_FEATURES:
            return "FORCE_WEBHOOK", self.force_webhook
        f = slug(feature)
        if feature == "beta":
            names = [
                *(f"DISCORD_WEBHOOK_BETA_{g}" for g in self._game_slugs(game_key)),
                "DISCORD_WEBHOOK_BETA",
                *(f"DISCORD_WEBHOOK_NEWS_{g}" for g in self._game_slugs(game_key)),
            ]
        else:
            names = [f"DISCORD_WEBHOOK_{f}_{g}" for g in self._game_slugs(game_key)]
            # Opt-in per-game features must never inherit the schedule/code catch-all or shared channel.
            if feature not in DEDICATED_FEATURES:
                names += [f"DISCORD_WEBHOOK_{f}", "DISCORD_WEBHOOK_URL"]
        for name in names:
            v = _env(self.env, name)
            if v:
                return name, v
        return "", None

    def webhook(self, feature: str, game_key: str) -> str | None:
        return self.webhook_source(feature, game_key)[1]

    def mirror_webhook_source(self, feature: str, game_key: str) -> tuple[str, str | None]:
        """(secret name, url) for the OPTIONAL second copy of a card — the game's own channel.

        Order: DISCORD_WEBHOOK_<FEATURE>_MIRROR_<GAME> > DISCORD_WEBHOOK_<FEATURE>_MIRROR.

        Deliberately NOT routed through webhook_source(). That chain ends at
        DISCORD_WEBHOOK_URL, and an unset mirror has to mean "do not fan out" — never "fall
        back to the catch-all". Otherwise the day DISCORD_WEBHOOK_URL is filled in, every
        card would silently start arriving twice in that one channel.

        FORCE_WEBHOOK (the test channel) switches the fan-out off entirely: it already
        redirects the primary card, so a second copy must not leak into a real game channel.
        """
        if self.force_webhook or feature in DEDICATED_FEATURES:
            return "", None
        f = slug(feature)
        names = [f"DISCORD_WEBHOOK_{f}_MIRROR_{g}" for g in self._game_slugs(game_key)]
        names.append(f"DISCORD_WEBHOOK_{f}_MIRROR")
        for name in names:
            v = _env(self.env, name)
            if v:
                return name, v
        return "", None

    def expected_webhook_names(self, feature: str, game_key: str) -> str:
        f = slug(feature)
        if feature == "beta":
            s = slug(game_key)
            return f"DISCORD_WEBHOOK_BETA_{s} (or DISCORD_WEBHOOK_BETA / DISCORD_WEBHOOK_NEWS_{s})"
        if feature in DEDICATED_FEATURES:
            return f"DISCORD_WEBHOOK_{f}_{slug(game_key)}"
        return f"DISCORD_WEBHOOK_{f}_{slug(game_key)} (or DISCORD_WEBHOOK_{f})"

    def ping(self, feature: str, game_key: str) -> Ping:
        """PING_<FEATURE>_<GAME>  >  PING_<FEATURE>  (>  PING_ROLE_ID for schedule/codes).
        'none' = explicit off. NO_PING=1 (test runs) silences everything."""
        if self.no_ping:
            return Ping()
        f = slug(feature)
        names = [f"PING_{f}_{g}" for g in self._game_slugs(game_key)] + [f"PING_{f}"]
        if feature not in ("maintenance", "beta"):
            names.append("PING_ROLE_ID")
        for name in names:
            p = parse_ping(_env(self.env, name))
            if p is not None:
                return p
        return Ping()


def _off_to_empty(value: str) -> str:
    """'none' / 'off' / '-' switch a URL setting off explicitly (an empty value would be
    re-filled from the GE_VARS_JSON catch-all)."""
    return "" if value.strip().lower() in ("none", "off", "-", "no", "false", "0") else value


def _merge_json_blobs(env: dict) -> None:
    """GitHub Actions passes ALL repo secrets/variables as JSON (GE_SECRETS_JSON / GE_VARS_JSON)
    so any per-game override you add (e.g. DISCORD_WEBHOOK_CODES_WUWA) works without
    editing the workflow. Explicit non-empty env values always win."""
    for blob in ("GE_SECRETS_JSON", "GE_VARS_JSON"):
        raw = env.pop(blob, "")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for k, v in (data or {}).items():
            if isinstance(k, str) and k.isupper() and not str(env.get(k, "")).strip():
                env[k] = str(v)


def _buttons_env(env: Mapping[str, str], name: str, limit: int = 3) -> list[dict]:
    """EXTRA_BUTTONS / COMMUNITY_BUTTONS = JSON list of {label,url[,emoji]}; bad JSON -> []."""
    raw = _env(env, name)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [b for b in parsed if isinstance(b, dict) and b.get("url") and b.get("label")][:limit]


def _game_set_env(
    env: Mapping[str, str],
    name: str,
    aliases: Mapping[str, list[str]],
    default: tuple[str, ...] = DEFAULT_OPTIONAL_GAMES,
    all_games: tuple[str, ...] = DEFAULT_OPTIONAL_GAMES,
) -> set[str]:
    raw = _env(env, name)
    if not raw:
        return set(default)
    reverse = {a.lower(): key for key, vals in aliases.items() for a in vals}
    tokens = [g.strip().lower() for g in raw.split(",") if g.strip()]
    if not tokens:
        return set(default)
    if len(tokens) == 1 and tokens[0] in ("none", "off", "0", "-"):
        return set()
    out: set[str] = set()
    for tok in tokens:
        if tok == "all":
            out.update(all_games)
        else:
            out.add(reverse.get(tok, tok))
    return out


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    _merge_json_blobs(env)
    feats_raw = _env(env, "ENABLED_FEATURES", "schedule,codes").lower()
    only = _env(env, "ONLY", "").lower()
    standby = feats_raw in ("none", "off", "standby", "0")
    if standby:
        features: set[str] = set()
    else:
        features = {f.strip() for f in feats_raw.split(",") if f.strip() in FEATURES}
    # Explicit opt-in even if a pre-existing installation asks for all features.
    features.discard("news")
    features.discard("maintenance")
    features.discard("beta")
    if not standby and _bool(env, "NEWS_ENABLED"):
        features.add("news")
    if not standby and _bool(env, "MAINTENANCE_ENABLED"):
        features.add("maintenance")
    if not standby and _bool(env, "BETA_ENABLED"):
        features.add("beta")
    if only:
        only_set = {f.strip() for f in only.split(",") if f.strip() in FEATURES}
        if only_set:
            features &= only_set
    news_enabled = "news" in features
    maintenance_enabled = "maintenance" in features
    beta_enabled = "beta" in features
    aliases = _game_aliases()
    games_filter = {g.strip().lower() for g in (_env(env, "GAMES") + "," + _env(env, "GAME")).split(",")
                    if g.strip()}
    nitter = [u.strip().rstrip("/") for u in _env(env, "NITTER_INSTANCES").split(",") if u.strip()]
    reddit = [u.strip().rstrip("/") for u in _env(env, "REDDIT_INSTANCES").split(",") if u.strip()]
    reddit_proxies = [u.strip().rstrip("/") for u in _env(env, "REDDIT_PROXIES").split(",") if u.strip()]
    extra = _buttons_env(env, "EXTRA_BUTTONS", 3)
    community = _buttons_env(env, "COMMUNITY_BUTTONS") or list(DEFAULT_COMMUNITY_BUTTONS)
    if _env(env, "COMMUNITY_BUTTONS", "").strip().lower() in ("none", "off", "-", "0", "false"):
        community = []
    emoji = {k: parse_emoji(_env(env, f"EMOJI_{k.upper()}", v) or v) for k, v in DEFAULT_EMOJI.items()}
    state_path = Path(_env(env, "STATE_PATH")) if _env(env, "STATE_PATH") else DEFAULT_STATE_PATH
    return Settings(
        env=env,
        features=features,
        games_filter=games_filter,
        dry_run=_bool(env, "DRY_RUN"),
        bootstrap_post=_bool(env, "BOOTSTRAP_POST"),
        edit_on_update=_bool(env, "EDIT_ON_UPDATE", True),
        post_on_maintenance=_bool(env, "POST_ON_MAINTENANCE_NOTICE", True),
        lookback_hours=_int(env, "LOOKBACK_HOURS", 72),
        codes_min_sources=max(1, _int(env, "CODES_MIN_SOURCES", 2)),
        x_enabled=_bool(env, "X_ENABLED", True),
        nitter_instances=nitter or list(DEFAULT_NITTER),
        nitter_token=_env(env, "NITTER_RSS_TOKEN"),
        instance_name=_env(env, "INSTANCE_NAME", "alpha") or "alpha",
        instance_role=(_env(env, "INSTANCE_ROLE", "primary") or "primary").lower(),
        peer_state_url=_off_to_empty(_env(env, "PEER_STATE_URL")),
        failover_after_min=_int(env, "FAILOVER_AFTER_MINUTES", 90),
        heartbeat_min=max(5, _int(env, "HEARTBEAT_MINUTES", 1440)),
        state_path=state_path,
        extra_buttons=extra,
        community_buttons=community,
        emoji=emoji,
        countdown_estimates=_bool(env, "COUNTDOWN_ESTIMATES", True),
        program_media=_bool(env, "PROGRAM_MEDIA", True),
        banner_feed=_bool(env, "BANNER_FEED", True),
        banner_search=_bool(env, "BANNER_SEARCH", True),
        banner_calendar=_bool(env, "BANNER_CALENDAR", True),
        gacha_wiki=_bool(env, "GACHA_WIKI", True),
        reddit_enabled=_bool(env, "REDDIT_ENABLED", True),
        reddit_instances=reddit or list(DEFAULT_REDDIT),
        reddit_proxies=reddit_proxies or list(DEFAULT_REDDIT_PROXIES),
        reddit_token=_env(env, "REDDIT_FEED_TOKEN"),
        show_legend=_bool(env, "SHOW_LEGEND", True),
        repost=_env(env, "REPOST").replace(" ", "").lower(),
        http_timeout=_int(env, "HTTP_TIMEOUT", 20),
        no_ping=_bool(env, "NO_PING"),
        force_webhook=_env(env, "FORCE_WEBHOOK"),
        test_mode=_bool(env, "TEST_MODE"),
        enable_games={g.strip().lower() for g in _env(env, "ENABLE_GAMES").split(",") if g.strip()},
        aliases=aliases,
        codes_mark_expired=_bool(env, "CODES_MARK_EXPIRED", True),
        news_enabled=news_enabled,
        news_games=_game_set_env(env, "NEWS_GAMES", aliases),
        news_group_long_strips=_bool(env, "NEWS_GROUP_LONG_STRIPS", False),
        news_timestamps=_bool(env, "NEWS_TIMESTAMPS", False),
        news_expand_full=_bool(env, "NEWS_EXPAND_FULL", False),
        news_expand_max_pages=max(1, _int(env, "NEWS_EXPAND_MAX_PAGES", 12)),
        news_expand_max_chars=max(1, _int(env, "NEWS_EXPAND_MAX_CHARS", 120000)),
        news_expand_max_media=max(1, _int(env, "NEWS_EXPAND_MAX_MEDIA", 120)),
        maintenance_enabled=maintenance_enabled,
        maintenance_games=_game_set_env(
            env, "MAINTENANCE_GAMES", aliases, default=(), all_games=ALL_MAINTENANCE_GAMES
        ),
        beta_enabled=beta_enabled,
        beta_games=_game_set_env(
            env, "BETA_GAMES", aliases, default=DEFAULT_BETA_GAMES, all_games=DEFAULT_BETA_GAMES
        ) - {"hna", "ananta"},
    )


def _game_aliases() -> dict[str, list[str]]:
    """key -> [SHORT] from config/games.json, so DISCORD_WEBHOOK_CODES_HSR works like
    DISCORD_WEBHOOK_CODES_STARRAIL (and PING_CODES_GI like PING_CODES_GENSHIN)."""
    try:
        raw = json.loads((CONFIG_DIR / "games.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, list[str]] = {}
    for key, g in (raw.get("games") or {}).items():
        if key.startswith("_") or not isinstance(g, dict):
            continue
        names = [g.get("short", "")] + list(g.get("aliases") or [])
        out[key] = [n for n in names if n and slug(n) != slug(key)]
    return out


# --------------------------------------------------------------------------- games
@dataclass
class CardStyle:
    title: str = "{game} Version {version} {program}"
    maintenance_heading: str = "Maintenance Details"
    maintenance_style: str = "start_end"      # start_end | range (WW)
    maintenance_first: bool = False           # WW: maintenance block before banners
    four_star_summary: bool = False           # WW: '※ 4 Star Characters:' summary line
    four_star_label: str = "4 Star Characters"
    """Heading of the per-phase 4★ line.

    ZZZ overrides it with '4 Star Characters (Default)': its A-Rank rate-ups are
    player-customisable ("Custom Search", HoYoLAB post 46015688), so the list the wiki
    documents is the DEFAULT line-up, not a guarantee."""
    banners_url: str = ""                     # GI: banner heading links to lunaris.moe/banners
    show_banners: bool = True                 # ANANTA is not a character gacha -> False
    note: str = MAINTENANCE_NOTE


WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@lru_cache(maxsize=16)
def _fixed_tz(offset_h: float) -> timezone:
    """One shared tzinfo per offset — these are rebuilt on every card render otherwise."""
    return timezone(timedelta(hours=offset_h))


def _weekday(value, default: int | None = None) -> int | None:
    """Accept 'Wed' / 'wednesday' / 2. Anything else -> default (never raises)."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if 0 <= value <= 6 else default
    if isinstance(value, str):
        return WEEKDAYS.get(value.strip()[:3].lower(), default)
    return default


def _hhmm(value, default: tuple[int, int] | None = None) -> tuple[int, int] | None:
    """Accept 'HH:MM' -> (hour, minute). Anything malformed -> default (never raises)."""
    if not isinstance(value, str) or ":" not in value:
        return default
    hh, _, mm = value.strip().partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        return default
    return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else default


@dataclass(frozen=True)
class Cadence:
    """Per-game release rhythm, used ONLY to speculate when no live source has spoken yet.

    Every field is a cold-start fallback measured from that game's published history. Once this
    installation has seen two or more real maintenance notices, the observed median gap and the
    observed modal weekday replace `days` and `maint_weekday` (see schedule.predict_cycle). A
    speculated timestamp is always written at PRIORITY["pattern"], the lowest tier, so any real
    source replaces it on the next run.
    """
    days: float = 42.0                      # median gap between maintenance starts
    maint_weekday: int = 2                  # 0=Mon .. 6=Sun; the day maintenance usually starts
    maint_time: tuple[int, int] = (6, 0)    # local wall clock of the maintenance start
    maint_hours: float = 5.0                # typical downtime
    program_weekday: int = 4                # special program / livestream day
    program_time: tuple[int, int] = (20, 0)
    program_lead_days: int = 12             # days before maintenance, before snapping to the weekday
    tz_offset_h: float = 8.0                # publisher clock; UTC+8 for HoYoverse and Kuro
    confidence: str = "low"                 # high | medium | low — how often the backtest was exact
    anchor_version: str = ""                # last known real cycle, used when state has no history
    anchor_ts: int = 0

    @property
    def tz(self) -> timezone:
        return _fixed_tz(self.tz_offset_h)


def _cadence(raw) -> Cadence | None:
    """Build a Cadence from games.json. Returns None when the game declares no rhythm."""
    if not isinstance(raw, dict) or not raw:
        return None
    base = Cadence()
    try:
        days = float(raw.get("days", base.days))
        hours = float(raw.get("maint_hours", base.maint_hours))
        lead = int(raw.get("program_lead_days", base.program_lead_days))
        tz_h = float(raw.get("tz_offset_h", base.tz_offset_h))
    except (TypeError, ValueError):
        return None
    if not 1 <= days <= 365 or not 0 < hours <= 72 or not 0 <= lead <= 90 or not -14 <= tz_h <= 14:
        return None
    anchor = raw.get("anchor_ts") or 0
    try:
        anchor = int(anchor)
    except (TypeError, ValueError):
        anchor = 0
    return Cadence(
        days=days,
        maint_weekday=_weekday(raw.get("maint_weekday"), base.maint_weekday),
        maint_time=_hhmm(raw.get("maint_time"), base.maint_time),
        maint_hours=hours,
        program_weekday=_weekday(raw.get("program_weekday"), base.program_weekday),
        program_time=_hhmm(raw.get("program_time"), base.program_time),
        program_lead_days=lead,
        tz_offset_h=tz_h,
        confidence=str(raw.get("confidence") or base.confidence).lower(),
        anchor_version=str(raw.get("anchor_version") or ""),
        anchor_ts=anchor if anchor > 0 else 0,
    )


@dataclass
class Game:
    key: str
    name: str
    short: str
    enabled: bool
    color: int
    # False = announced but not out yet. Separate from `enabled`: a pre-release game is still
    # monitored for announcements, it just has no real codes to fetch, so the test bench checks
    # its card with sample data (`test-card --unlaunched`, see __main__.sample_payloads).
    released: bool = True
    icon: str = ""
    hoyolab_gid: int | None = None
    c3kay_feed: str = ""
    launcher: dict = field(default_factory=dict)
    x_accounts: list[str] = field(default_factory=list)
    subreddits: list[str] = field(default_factory=list)
    reddit_official: list[str] = field(default_factory=list)
    kuro_news: bool = False
    program_label: str = "Special Program"
    program_patterns: list[str] = field(default_factory=lambda: ["special program"])
    banner_patterns: list[str] = field(default_factory=list)
    banner_titles: list[str] = field(default_factory=list)   # title templates, {v} = version, {p} = phase I/II
    official_uid: int | None = None      # the game's official HoYoLAB account (reposts are not announcements)
    youtube: str = ""
    twitch: str = ""
    news_url: str = ""                 # official news page: archives every announcement + its key art
    program_feeds: list[str] = field(default_factory=list)   # RSS/Atom mirrors of that news list
    card: CardStyle = field(default_factory=CardStyle)
    codes: dict = field(default_factory=dict)
    note: str = ""
    four_star_count: int | None = None   # rate-up 4★ per banner phase (GI/HSR/WW 3, ZZZ 2); other counts -> TBA
    auto_enable_on: str = ""             # YYYY-MM-DD: a prepared game switches itself on at launch
    cadence: Cadence | None = None       # release rhythm; None = never speculate for this game
    beta: dict = field(default_factory=dict)   # feature 5: nanoka namespace + beta changelog URL

    @property
    def redeem_url(self) -> str:
        return self.codes.get("redeem_url", "")


def load_games(path: Path | None = None) -> dict[str, Game]:
    path = path or (CONFIG_DIR / "games.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    defaults = raw.get("defaults", {})
    games: dict[str, Game] = {}
    for key, g in (raw.get("games") or {}).items():
        if key.startswith("_"):
            continue
        card_raw = {**defaults.get("card", {}), **g.get("card", {})}
        card = CardStyle(**{k: v for k, v in card_raw.items() if k in CardStyle.__dataclass_fields__})
        hl = g.get("hoyolab") or {}
        rd = g.get("reddit") or {}
        games[key] = Game(
            key=key,
            name=g["name"],
            short=g.get("short", key.upper()),
            enabled=bool(g.get("enabled", True)),
            released=bool(g.get("released", True)),
            color=int(g.get("color", defaults.get("color", DEFAULT_COLOR))),
            icon=g.get("icon", ""),
            hoyolab_gid=hl.get("gid"),
            c3kay_feed=hl.get("c3kay", ""),
            launcher=g.get("launcher") or {},
            x_accounts=list(g.get("x_accounts") or []),
            subreddits=list(rd.get("subreddits") or []),
            reddit_official=list(rd.get("official") or []),
            kuro_news=bool(g.get("kuro_news", False)),
            program_label=g.get("program_label", "Special Program"),
            program_patterns=list(g.get("program_patterns") or ["special program"]),
            banner_patterns=list(g.get("banner_patterns") or []),
            banner_titles=list(g.get("banner_titles") or []),
            official_uid=(int(hl["official_uid"]) if hl.get("official_uid") else None),
            youtube=g.get("youtube", ""),
            twitch=g.get("twitch", ""),
            news_url=g.get("news_url", ""),
            program_feeds=list(g.get("program_feeds") or []),
            card=card,
            codes=dict(g.get("codes") or {}),
            note=g.get("note", ""),
            four_star_count=(int(g["four_star_count"]) if g.get("four_star_count") else None),
            auto_enable_on=str(g.get("auto_enable_on") or ""),
            cadence=_cadence(g.get("cadence")),
            beta=(g.get("beta") if isinstance(g.get("beta"), dict) else {}),
        )
    return games


def game_is_on(g: Game, settings: Settings, now: float | None = None) -> bool:
    """enabled in games.json, or listed in ENABLE_GAMES, or its auto_enable_on date has arrived."""
    if g.enabled or g.key in settings.enable_games:
        return True
    if g.auto_enable_on:
        import time as _t
        today = _t.strftime("%Y-%m-%d", _t.gmtime(now if now is not None else _t.time()))
        return today >= g.auto_enable_on
    return False


def active_games(games: dict[str, Game], settings: Settings, now: float | None = None) -> list[Game]:
    out = []
    for g in games.values():
        if settings.games_filter and g.key not in settings.games_filter:
            continue
        if not game_is_on(g, settings, now) and g.key not in settings.games_filter:
            continue
        out.append(g)
    return out


def load_overrides(path: Path | None = None) -> dict:
    path = path or (CONFIG_DIR / "overrides.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"config/overrides.json is not valid JSON: {e}") from e
    return {k: v for k, v in data.items() if not k.startswith("_")}
