"""The user's four reference cards (HSR 4.6, WW 3.7, GI 7.1, ZZZ 3.2) as structured data.

Used by `python -m gamexpress test-card` / `preview` and by the golden tests — every
value below is copied from the reference cards; images are the public originals
(the reference cards used expiring Discord-CDN attachment links).
"""

from __future__ import annotations

SCHEDULE_SAMPLES: dict[str, dict] = {
    "starrail": {
        "version": "4.6", "program_name": "Special Program", "program_ts": 1789903800,
        "title_url": "https://x.com/honkaistarrail/status/2099440781115211916",
        "images": ["https://i.ytimg.com/vi/drFgtruoPe8/maxresdefault.jpg"],
        "preinstall_ts": 1790229600, "maint_start_ts": 1790546400, "maint_end_ts": 1790564400,
        "banners": {"reruns": [], "phase1": ["Pearl"], "phase1_4": [], "phase2": [], "phase2_4": []},
        "source_links": [["HoYoLAB", "https://www.hoyolab.com/article/46814308"]],
    },
    "wuwa": {
        "version": "3.7", "program_name": "Special Broadcast", "program_ts": 1789815600,
        "title_url": "https://x.com/Wuthering_Waves/status/2098607530780021002",
        "images": ["https://pbs.twimg.com/media/HR70hTAaoAA8Dzz.jpg?name=orig"],
        "preinstall_ts": 1790560800, "maint_start_ts": 1790712000, "maint_end_ts": 1790737200,
        "banners": {"reruns": ["Chisa", "Iuno", "Lynae", "Lucilla"],
                    "four_star": ["Buling", "Taoqi", "Youhu", "Lumi", "Danjin", "Yangyang"],
                    "phase1": ["Hsin"], "phase1_4": [], "phase2": ["Suoming"], "phase2_4": []},
        "source_links": [["X Post", "https://x.com/Wuthering_Waves/status/2098607530780021002"]],
    },
    "genshin": {
        "version": "7.1", "program_name": "Special Program", "program_ts": 1789214400,
        "title_url": "https://www.hoyolab.com/article/46604275",
        "images": ["https://upload-os-bbs.hoyolab.com/upload/2026/09/07/"
                   "3b807be584794bdac0cddc9ddf05bcec_5145680818623480549.jpg"],
        "preinstall_ts": 1789959600, "maint_start_ts": 1790114400, "maint_end_ts": 1790132400,
        "banners": {"reruns": ["Skirk", "Escoffier"], "phase1": ["Vesna"], "phase1_4": [],
                    "phase2": ["Vodyanitsa"], "phase2_4": []},
        "source_links": [["HoYoLAB", "https://www.hoyolab.com/article/46604275"]],
    },
    "zzz": {
        "version": "3.2", "program_name": "Special Program", "program_ts": 1787916600,
        "title_url": "https://www.hoyolab.com/article/46427336",
        "images": ["https://pbs.twimg.com/media/G8IYauwWUAIWYyc.jpg?name=orig"],
        "preinstall_ts": 1788753600, "maint_start_ts": 1788904800, "maint_end_ts": 1788922800,
        "banners": {"reruns": [], "phase1": ["Claret Flint"], "phase1_4": [],
                    "phase2": ["Roxy Ifrita Pryce"], "phase2_4": []},
        "source_links": [["HoYoLAB", "https://www.hoyolab.com/article/46427336"]],
    },
}

# live hoyo-codes.seria.moe responses captured 2026-09-25
CODE_SAMPLES: dict[str, list[dict]] = {
    # real codes seen on 2026-09-25 (GI / HSR / ZZZ / WW) — one sample card per game so the
    # monitor's test bench can check EVERY per-game codes channel (DISCORD_WEBHOOK_CODES_<GAME>)
    "genshin": [
        {"code": "VESNAONPATROL", "rewards": "Primogem*40;Mora*20000;Hero's Wit*3", "sources": ["seria", "codehub"]},
        {"code": "EPIC2026", "rewards": "40 primogems, five hero's wit, and 20k mora", "sources": ["seria", "codehub"]},
    ],
    "starrail": [
        {"code": "STARRAILGIFT", "rewards": ["Stellar Jade ×100", "Traveler's Guide ×4", "Bottled Soda ×5",
                                             "Credit ×50,000"], "sources": ["ennead", "fandom"]},
        {"code": "CREATIONNYMPH", "rewards": ["Stellar Jade ×60", "Fuel ×1", "Heroic Variable ×1"],
         "sources": ["ennead", "fandom"]},
    ],
    "zzz": [
        {"code": "ZZZINK32", "rewards": "Polychrome*20;Denny*2,222", "sources": ["seria"]},
        {"code": "ZZZVOID32", "rewards": "Polychrome*20;Denny*2,222", "sources": ["seria"]},
        {"code": "ZZZGRIND32", "rewards": "Polychrome*20;Denny*2,222", "sources": ["seria"]},
    ],
    "wuwa": [
        {"code": "WAKINGMOON", "rewards": "100 Astrite + 20 Premium Tuner + 5 Advanced Sealed Tube",
         "sources": ["fandom", "codehub"]},
    ],
    # pre-release games ("released": false): obviously fake sample codes, because there is
    # nothing real to fetch yet — test cards are labelled 🧪 TEST anyway
    "hna": [
        {"code": "HNASAMPLE01", "rewards": ["Sample reward ×1"], "sources": ["x"]},
    ],
    "ananta": [
        {"code": "ANANTASAMPLE1", "rewards": ["Sample reward ×1"], "sources": ["x"]},
    ],
}
