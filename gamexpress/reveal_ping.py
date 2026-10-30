"""Opt-in classifier and publication-minute deduplication for character reveals.

Heuristic recognition cannot guarantee future publisher formats or known roster coverage.
Video-bearing articles are excluded; the feature performs no network calls itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_TRACKED_CHARACTERS = 200
# A fuller version of the same name arriving within this window is the same reveal, not a new
# character (ZZZ publishes `Agent Record | Evelyn` and the 12:15 dossier minutes apart).
VARIANT_WINDOW_S = 12 * 3600
# Corroborated same-character aliases: `<fuller canonical key> -> <shorter canonical key>`.
# Only names confirmed to belong to ONE character belong here. A shared word is not evidence:
# `March 7th` is not `March`, and `Saber Alter` is not `Saber`, so those never collapse.
SAME_CHARACTER_ALIASES: dict[str, str] = {
    "evelyn chevalier": "evelyn",
}

# Direct video watch / short / file URLs (deliberately NOT matching channel homepages like
# `youtube.com/@GenshinImpact` that sit in standard publisher social footers).
_VIDEO_URL_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\b|shorts/|live/|embed/|v/)|youtu\.be/[A-Za-z0-9_-]{6,}|"
    r"bilibili\.com/video/|b23\.tv/|twitch\.tv/videos/|"
    r"\.(?:mp4|webm|mov|m4v)(?:[?#]|$))",
    re.I,
)

# Titles that are NEVER first-time static character announcements (trailers, combat/kit previews,
# skins/outfits, reruns, banners, patch/maintenance/events, comic episodes, non-character items).
_TITLE_EXCLUDE_RE = re.compile(
    r"\b(?:"
    # Video / audio / music / lore media series
    r"trailers?|teasers?|character\s+demo|collected\s+miscellany|myriad\s+celestia|"
    r"keeping\s+up\s+with|animated\s+short|animation|cutscenes?|behind\s+the\s+(?:scenes|agent)|"
    r"ost\b|soundtrack|album|concert|radio|podcast|voice[-\s]?overs?|voice\s+lines?|"
    r"\bep\b|\bpv\b|\bmv\b|"
    # Post-drip kit / skill / combat / mechanics previews
    r"skill\s+showcase|combat\s+(?:showcase|intel|intro)|resonator\s+showcase|"
    r"character\s+(?:preview|detail|anecdotes?)|agent\s+mechanics|mechanics\s+intro|"
    r"forte\s+intro|build\s+guide|talent\s+preview|"
    # Reruns & gacha banner notices (`Resonator Review` is Kuro's rerun series!)
    r"resonator\s+review|event\s+wish(?:es)?|event\s+warp|signal\s+search|"
    r"exclusive\s+(?:channel|rescreening)|limited[-\s]time\s+channels?|convenes?|"
    r"chronicled\s+wish|phase\s+(?:[i1v]+|\d+)|"
    # Skins / outfits / cosmetics (have portrait art + quotes, never new characters!)
    r"outfits?|costumes?|attires?|skins?|wardrobe|uniform\s+showcase|cosmetics?|"
    # System / patch / maintenance / events / community / merch / modes
    r"update\s+(?:details|notice|maintenance|summary|preview)|maintenance|patch\s+notes?|"
    r"special\s+(?:program|broadcast)|livestream|pre-?(?:download|install(?:ation)?|load)|"
    r"known\s+issues?|bug\s+fix(?:es)?|compensation|developers?'?\s+discussion|dev\s+talk|"
    r"web\s+event|prize\s+event|giveaway|winners?|contest|survey|questionnaire|"
    r"wallpapers?|calendars?|merch(?:andise)?|figures?|redemption\s+codes?|top-?up|battle\s+pass|"
    r"genius\s+invokation|\btcg\b|miliastra|simulated\s+universe|divergent\s+universe|"
    r"hollow\s+zero|illusive\s+realm|spiral\s+abyss|imaginarium\s+theater|deadly\s+assault|"
    r"shiyu\s+defense|apocalyptic\s+shadow|pure\s+fiction|tower\s+of\s+adversity|"
    r"closed\s+beta|beta\s+(?:test|recruitment)|recruitment|"
    # Non-character entities & serial episodes (`echo showcase`/`new echoes`, not poetic "Echoes of...")
    r"weapons?|light\s+cones?|w-engines?|bangboos?|artifacts?|relics?|"
    r"echo\s+(?:showcase|gallery|preview|system|intro|tuning|set|hunting)|new\s+echo(?:es)?|tacet\s+discords?|"
    r"planar\s+ornaments?|drive\s+discs?|enem(?:y|ies)|boss(?:es)?|monsters?|"
    r"new\s+(?:area|region|map|story|chapter|events?)|event\s+(?:details|notice|preview|overview)|"
    r"episodes?\s*\d+|ep\.\s*\d+|vol(?:ume)?\.?\s*\d+|part\s+(?:\d+|[ivx]+)|issue\s*#?\d+|"
    r"birthday"
    r")\b",
    re.I,
)

# Event / patch boilerplate inside body text that disqualifies dynamic Layer-2 matches.
_BODY_BOILERPLATE_RE = re.compile(
    r"\b(?:event\s+duration|event\s+time|how\s+to\s+participate|participation\s+requirements?|"
    r"eligibility|event\s+rewards?|gameplay\s+overview|update\s+time|maintenance\s+compensation|"
    r"pre-?download\s+period|storage\s+space|ban\s+list)\b",
    re.I,
)

# Non-character words that must never appear as the `<Candidate>` name in `<Series> | <Candidate>`.
_BAD_CANDIDATE_WORD_RE = re.compile(
    r"\b(?:episode|ep|part|vol|volume|chapter|act|phase|version|ver|update|notice|details|"
    r"preview|review|showcase|broadcast|program|livestream|radio|issue|pack|bundle|pass|shop|"
    r"guide|tutorial|faq|summary|recap|highlights?|strategy|wallpaper|calendar|commission|"
    r"report|notes?|patch|fix|server|winners?|list|announcement|event|events|system|mode|"
    r"retreat|prologue|interlude|epilogue|finale|ost|album|teaser|trailer|demo|pv|mv|"
    r"outfit|costume|skin|weapon|bangboo|relic|artifact|echo|convene|warp|wish)\b",
    re.I,
)

# Non-reveal `<Series>` prefixes before `|` that must not trigger Layer 2.
_BAD_SERIES_PREFIX_RE = re.compile(
    r"\b(?:laughter\s+outsmarts|keeping\s+up|insider\s+channel|developer|radio|episode\s*\d+|"
    r"vol\.?\s*\d+|issue\s*\d+|behind\s+the|voice|combat|skill|mechanic|event|update|patch|"
    r"outfit|costume|weapon|light\s+cone|w-engine|bangboo|resonator\s+review|anecdote)\b",
    re.I,
)

# --- Layer 1: Multi-era publisher templates ---
_DIAMOND_KV_RE = re.compile(r"(?m)^\s*[◆◇❖]\s*([^:\n：]{2,28})\s*[:：]\s*([^\n]{1,80})\s*$")
_DIAMOND_BULLET_RE = re.compile(r"(?m)^\s*[◆◇❖]\s*([^\n]{2,90})\s*$")

_GI_TITLE_INTRO_RE = re.compile(
    r"^(?:\[?\s*character\s+tales\s*\]?|new\s+character\s+introduction|character\s+introduction)"
    r"\s*(?:—|–|-|:|：|\||丨)\s*(?:[\"“「『][^\"”」』]{1,50}[\"”」』]\s*)?(.+?)\s*$",
    re.I,
)
_GI_TITLE_DOT_RE = re.compile(
    r"^(?:[\"“「『][^\"”」』]{2,60}[\"”」』]\s*[·•—–\-:|]\s*)?"
    r"([A-Z][\w'’.\-]*(?:[ •·]+[A-Z][\w'’.\-]*){0,3})"
    r"(?:\s*[·•—–\-:|]\s*[^|\n]{3,60})?$"
)

_HSR_SERIES_RE = re.compile(
    r"^[\"“「『]?(?:extraterrestrial\s+satellite\s+communication|amphoreus'?\s+saga\s+of\s+heroes|"
    r"aha\s+instant)(?:[\"”」』]\s*(?:[|丨:：—–\-]\s*)?|\s*[|丨:：—–\-]\s*)(.+?)\s*$",
    re.I,
)
_ZZZ_SERIES_RE = re.compile(
    r"^(?:agent\s+record|agent\s+dossier|new\s+agent(?:\s+(?:reveal|introduction))?)"
    r"\s*[|丨:：—–\-]\s*(.+?)\s*$",
    re.I,
)
_WUWA_STARS_RE = re.compile(
    r"^post-lament\s+anthropocene\s*[:：]\s*(?!resonator\s+review\b)[^|丨\n]{2,48}[|丨]\s*(.+?)\s*$",
    re.I,
)
_WUWA_REVEAL_RE = re.compile(
    r"^resonator\s+reveal\s*[|丨:：—–\-]\s*(.+?)\s*$",
    re.I,
)
_WUWA_PROFILE_RE = re.compile(
    r"^(?:profile\s+reveal\s*[|丨:：]|(?:resonator\s+)?profile\s*[:：|丨])\s*(.+?)\s*$",
    re.I,
)

# --- Layer 2: Unseen future `<Series> | <Character>` or `<Role> Reveal/Dossier | <Character>` ---
_GENERIC_PIPE_RE = re.compile(r"^\s*([^|丨\n]{4,60}?)\s*[|丨]\s*([^|丨\n]{2,44}?)\s*$")
_GENERIC_ROLE_REVEAL_RE = re.compile(
    r"^\s*(?:new\s+)?(?:character|agent|resonator|anima|esper|operator|valkyrie)\s+"
    r"(?:reveal|introduction|dossier|record|archive|file|profile)\s*[:：|丨—–\-]\s*(.+?)\s*$",
    re.I,
)

_OPENING_QUOTE_RE = re.compile(r"[\"“「『][^\"”」』]{12,420}[\"”」』]")
_DOSSIER_SIGNAL_RE = re.compile(
    r"(?:[◆▌]|^\s*✦\s*[A-Z]|"
    r"\b(?:voice(?:\s+actor)?|cv|va|affiliation|faction|allegiance|constellation|"
    r"vision|gnosis|moon\s+wheel|attribute|element|path|forte|archive\s*>>|loading\s+data)\s*[:：>])",
    re.I | re.M,
)

# A whole line wrapped in one emphasis marker (`**◆Name: Mitya**`, `_◆ Title: …_`, `~~…~~`).
# `textutil.news_blocks` renders a bold HoYoLAB run as `**…**`, so production bodies really do
# carry these markers; the matchers below must read the key, never the marker.
_LINE_EMPHASIS_RE = re.compile(r"(?m)^([ \t]*)(\*\*\*|\*\*|~~|__|\*|_)(?=\S)(.+?)(?<=\S)\2[ \t]*$")

_TRAILING_PAREN_RE = re.compile(r"\s*[\(（][^\)）]{1,40}[\)）]\s*$")
_QUOTE_WRAP_RE = re.compile(r"^[\s\"“”'‘’「」『』《》〈〉•·✦◆▌\-—–:：|丨]+|[\s\"“”'‘’「」『』《》〈〉•·✦◆▌\-—–:：|丨]+$")


@dataclass(frozen=True)
class RevealMatch:
    character: str
    canonical_key: str
    rule: str


def clean_character_name(raw: str) -> str:
    """Extract and validate a clean playable-character proper name, or return `""`."""
    if not raw:
        return ""
    s = _TRAILING_PAREN_RE.sub("", str(raw).strip())
    # Profile titles write `Astral Precision — Suoming` or `"Epithet" Name`: keep the character side.
    if re.search(r"[—–]|\s--\s", s):
        parts = [p.strip() for p in re.split(r"[—–]|\s--\s", s) if p.strip()]
        if parts:
            s = parts[-1]
    elif " - " in s:
        parts = [p.strip() for p in s.split(" - ") if p.strip()]
        if len(parts) == 2 and len(parts[1]) <= 28:
            s = parts[1]
    # Strip leading quoted epithet (`"Astute Luminary" Nahida` -> `Nahida`).
    s = re.sub(r"^[\"“「『][^\"”」』]{1,48}[\"”」』]\s*", "", s)
    s = _QUOTE_WRAP_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not (2 <= len(s) <= 36):
        return ""
    if _BAD_CANDIDATE_WORD_RE.search(s):
        return ""
    if re.search(r"[!?#@/\\=\[\]{}<>~_]|https?:|\d+\.\d+", s):
        return ""
    # Variant forms name the character plus a form suffix (`Rover: Aero`, `Yangyang: Xuanling`,
    # `Rover: Electro`). Both sides must be proper names; the canonical key keeps only the base
    # character so one character can never ping twice for its forms.
    if ":" in s or "：" in s:
        parts = [p.strip() for p in re.split(r"[:：]", s)]
        if len(parts) != 2 or not all(_is_proper_name(p) for p in parts):
            return ""
        return f"{parts[0]}: {parts[1]}"
    return s if _is_proper_name(s) else ""


def _is_proper_name(s: str) -> bool:
    """True for a playable-character proper name (`Astra Yao`, `Dan Heng • Imbibitor Lunae`,
    `The Herta`, `March 7th`, `Soldier 11`, `Kaedehara Kazuha`, `von Lycaon`, `de la ...`)."""
    if not (2 <= len(s) <= 36):
        return False
    tokens = [tok for tok in re.split(r"[\s•·&]+", s) if tok]
    if not (1 <= len(tokens) <= 5):
        return False
    connectors = {"of", "the", "von", "van", "de", "del", "da", "la", "le", "du", "no"}
    proper_tokens = 0
    for idx, tok in enumerate(tokens):
        if tok.lower() in connectors and idx > 0:
            continue
        if re.match(r"^(?:[A-ZÀ-ÖØ-Þ][\w'’.\-]*|\d+(?:st|nd|rd|th)?)$", tok):
            proper_tokens += 1
            continue
        return False
    return proper_tokens >= 1


def canonical_character_key(name: str) -> str:
    """Canonical per-game character key so multi-part reveals (`Stars Intertwined | Suoming` and
    `Profile Reveal | Astral Precision — Suoming`) collapse to the exact same key `"suoming"`."""
    cleaned = clean_character_name(name) or str(name or "").strip()
    cleaned = re.sub(r"[•·]+", " ", cleaned)
    cleaned = re.sub(r"['’`]", "'", cleaned)
    # A form suffix (`Rover: Aero`, `Yangyang: Xuanling`) is the same character, not a new one.
    cleaned = re.sub(r"\s*[:：]\s*.*$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return cleaned


def _article_body_and_counts(item) -> tuple[str, int, bool]:
    """Return `(body_text, image_count, has_video)` across both `article_blocks` and fallback fields."""
    blocks = getattr(item, "article_blocks", None) or []
    has_video = False
    image_urls: set[str] = set()
    text_parts: list[str] = []

    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        kind = blk.get("kind")
        url = str(blk.get("url") or "")
        if kind == "video" or (kind in ("link", "image") and _VIDEO_URL_RE.search(url)):
            # A media block is video whatever slot it arrived in: HoYoLAB publishes `vod` as
            # `video`, but a direct .mp4/.webm in an image slot is the same evidence, and
            # `Hard Rule 0` must never depend on which slot a publisher chose.
            has_video = True
        elif kind == "image" and url:
            image_urls.add(url)
        elif kind == "text":
            t = str(blk.get("text") or "")
            if "[native video unavailable" in t.lower():
                has_video = True
            if t.strip():
                text_parts.append(t.strip())

    for img in getattr(item, "images", None) or []:
        if isinstance(img, str) and img:
            image_urls.add(img)

    for lnk in getattr(item, "links", None) or []:
        if isinstance(lnk, str) and _VIDEO_URL_RE.search(lnk):
            has_video = True

    body = "\n\n".join(text_parts) if text_parts else str(getattr(item, "text", "") or "").strip()
    if "[native video unavailable" in body.lower() or _VIDEO_URL_RE.search(body):
        has_video = True
    return body, len(image_urls), has_video


def unmark_lines(text: str) -> str:
    """Drop whole-line markdown emphasis (`**◆Name: Mitya**` -> `◆Name: Mitya`).

    The production renderer bolds HoYoLAB dossier lines, and every Genshin, HSR, ZZZ and WuWa
    reveal since 2020 has shipped with those keys bold. Classification reads the key, never the
    marker; inline emphasis inside a sentence is left alone.
    """
    if not text:
        return text
    for _ in range(3):  # `***bold italic***` and other nested wrappers
        stripped = _LINE_EMPHASIS_RE.sub(r"\1\3", text)
        if stripped == text:
            break
        text = stripped
    return text


def _match_genshin_diamond_dossier(title: str, body: str) -> RevealMatch | None:
    """Match Genshin V2.0–V7.x+ `◆` character reveal blocks (including future power labels)."""
    kvs = [(k.strip(), v.strip()) for k, v in _DIAMOND_KV_RE.findall(body)]
    if len(kvs) >= 3:
        key_map = {k.lower(): v for k, v in kvs}
        raw_name = key_map.get("name") or key_map.get("character name") or key_map.get("character")
        has_title = "title" in key_map
        if raw_name and has_title:
            cname = clean_character_name(raw_name)
            if cname:
                return RevealMatch(cname, canonical_character_key(cname), "diamond_dossier")

    # Early V2.x / V3.x HoYoLAB format: 3–4 `◆` lines (`◆ Epithet`, `◆ Allegiance`, `◆ Vision: X`,
    # `◆ Constellation: Y`) where the character's name sits in the article title.
    bullets = _DIAMOND_BULLET_RE.findall(body)
    if len(bullets) >= 3 and re.search(
        r"\b(?:vision|gnosis|moon\s+wheel|constellation|astrolabe|delusion|authority|ancient\s+name)\b",
        "\n".join(bullets),
        re.I,
    ):
        cand_raw = ""
        if "·" in title or "•" in title:
            for seg in re.split(r"[·•]", title):
                cand = clean_character_name(seg)
                if cand:
                    cand_raw = cand
                    break
        if not cand_raw:
            m = _GI_TITLE_DOT_RE.match(title.strip())
            if m:
                cand_raw = clean_character_name(m.group(1))
        if cand_raw:
            return RevealMatch(cand_raw, canonical_character_key(cand_raw), "diamond_early_dossier")
    return None


def classify_character_reveal(game_key: str, item) -> RevealMatch | None:
    """Return `RevealMatch` iff `item` is a static new-character reveal/announcement post."""
    title = re.sub(r"\s+", " ", str(getattr(item, "title", "") or "")).strip()
    if not title:
        return None
    # A shared official feed is not proof that the post introduces a playable character.
    # Miliastra cosmetic dossiers share Genshin's gid and can have identical diamond fields.
    if not getattr(item, "official", False):
        return None
    if "miliastra" in str(getattr(item, "author", "") or "").casefold():
        return None

    body, image_count, has_video = _article_body_and_counts(item)
    # Hard Rule 0: NEVER include any post with native/embedded/linked video or video-series title.
    if has_video:
        return None
    if _TITLE_EXCLUDE_RE.search(title):
        return None
    # Keys are matched on the marker-free view of the body, because the renderer bolds them.
    body = unmark_lines(body)

    gk = (game_key or "").lower()

    # --- Layer 1: Multi-era publisher reveal templates ---
    diamond = _match_genshin_diamond_dossier(title, body)
    if diamond is not None:
        return diamond

    m_gi = _GI_TITLE_INTRO_RE.match(title)
    if m_gi:
        cname = clean_character_name(m_gi.group(1))
        if cname:
            return RevealMatch(cname, canonical_character_key(cname), "character_introduction")

    m_hsr = _HSR_SERIES_RE.match(title)
    if m_hsr:
        cname = clean_character_name(re.sub(r"^#(?=[A-Z])", "", m_hsr.group(1)))
        if cname:
            return RevealMatch(cname, canonical_character_key(cname), "hsr_satellite_series")

    m_zzz = _ZZZ_SERIES_RE.match(title)
    if m_zzz:
        raw_name = m_zzz.group(1).strip()
        # In this named series a complete quoted value is a name, not an epithet.
        if re.fullmatch(r'[\"“「『][^\"”」』]{1,48}[\"”」』]', raw_name):
            raw_name = raw_name[1:-1]
        cname = clean_character_name(raw_name)
        if cname:
            return RevealMatch(cname, canonical_character_key(cname), "zzz_agent_record")

    # Early ZZZ pre-1.0 / 1.0 quote-titled posts carrying `[Agent Record]` in the body.
    if gk == "zzz" and re.search(r"\[\s*agent\s+record\s*\]", body, re.I):
        for seg in reversed(re.split(r"[|丨—–\-:]", title)):
            cname = clean_character_name(seg)
            if cname:
                return RevealMatch(cname, canonical_character_key(cname), "zzz_body_agent_record")

    m_ww_stars = _WUWA_STARS_RE.match(title)
    if m_ww_stars:
        cname = clean_character_name(m_ww_stars.group(1))
        if cname:
            return RevealMatch(cname, canonical_character_key(cname), "wuwa_anthropocene")

    m_ww_rev = _WUWA_REVEAL_RE.match(title)
    if m_ww_rev:
        cname = clean_character_name(m_ww_rev.group(1))
        if cname:
            return RevealMatch(cname, canonical_character_key(cname), "wuwa_resonator_reveal")

    m_ww_prof = _WUWA_PROFILE_RE.match(title)
    if m_ww_prof and gk == "wuwa":
        cname = clean_character_name(m_ww_prof.group(1))
        if cname:
            return RevealMatch(cname, canonical_character_key(cname), "wuwa_profile_reveal")

    m_role = _GENERIC_ROLE_REVEAL_RE.match(title)
    if m_role:
        cname = clean_character_name(m_role.group(1))
        if cname:
            return RevealMatch(cname, canonical_character_key(cname), "role_reveal_title")

    # --- Layer 2: Dynamic unseen-future `<Series> | <Character>` structural detector ---
    # Covers future arc/planet series renames (e.g. HSR 5.0+, ZZZ 4.0+, WuWa 4.0+, HNA, Ananta)
    # without matching event notices, patch summaries, or gag comics (`Episode 6 | Retreat`).
    # A reveal can carry a long body (a lore-heavy introduction). Length alone must not hide it,
    # but a long body needs the strongest evidence: an opening quote AND a dossier key, so a
    # sprawling event notice cannot become a "character".
    long_body = len(body) > 3600
    if 1 <= image_count <= 5 and len(body) >= 40 and not _BODY_BOILERPLATE_RE.search(body):
        m_pipe = _GENERIC_PIPE_RE.match(title)
        if m_pipe:
            series_prefix, raw_cand = m_pipe.group(1).strip(), m_pipe.group(2).strip()
            if not _BAD_SERIES_PREFIX_RE.search(series_prefix):
                cname = clean_character_name(raw_cand)
                if cname:
                    has_quote = bool(_OPENING_QUOTE_RE.search(body[:650]))
                    has_dossier = bool(_DOSSIER_SIGNAL_RE.search(body))
                    primary_tok = next(
                        (t for t in re.split(r"[\s•·&]+", cname) if len(t) >= 3 and t.lower() != "the"),
                        cname,
                    )
                    mentions_name = bool(re.search(rf"\b{re.escape(primary_tok)}\b", body))
                    signals = int(has_quote) + int(has_dossier) + int(mentions_name)
                    if (signals >= 2 and (has_quote or has_dossier)
                            and (not long_body or (has_quote and has_dossier))):
                        return RevealMatch(cname, canonical_character_key(cname), "dynamic_future_reveal")

    return None


def ensure_reveal_baseline(record: dict, now: int, *, known_rerun_names: list[str] | None = None) -> None:
    """Initialize reveal tracking on `record` without mutating historical posted entries.

    Seeds optional `known_rerun_names` (e.g. already-known schedule rerun characters for this game)
    on first activation so future lore posts about old roster characters stay suppressed while brand
    new characters arriving after activation can ping cleanly.
    """
    chars = record.get("reveal_chars")
    if not isinstance(chars, dict):
        chars = {}
        record["reveal_chars"] = chars
    if not record.get("reveal_seeded_at"):
        record["reveal_seeded_at"] = int(now)
        for nm in known_rerun_names or []:
            ck = canonical_character_key(nm)
            if ck and ck not in chars:
                chars[ck] = int(now)
    prune_reveal_chars(record)


def prune_reveal_chars(record: dict, limit: int = MAX_TRACKED_CHARACTERS) -> None:
    chars = record.get("reveal_chars")
    if not isinstance(chars, dict) or len(chars) <= limit:
        return
    ordered = sorted(chars.items(), key=lambda kv: (int(kv[1]) if isinstance(kv[1], int) else 0, str(kv[0])))
    for k, _ in ordered[:-limit]:
        chars.pop(k, None)


def _same_character_variant(seen: str, candidate: str) -> bool:
    """`Evelyn` and `Evelyn Chevalier` are one Agent published twice minutes apart.

    Only corroborated aliases in `SAME_CHARACTER_ALIASES` collapse. A shared word is not a
    variant: `Herta` is not `The Herta`, `March` is not `March 7th`, and `Saber` is not
    `Saber Alter`, so those never suppress a genuinely different character's reveal.
    """
    if not seen or not candidate or seen == candidate:
        return False
    return (SAME_CHARACTER_ALIASES.get(candidate) == seen
            or SAME_CHARACTER_ALIASES.get(seen) == candidate)


def evaluate_reveal_ping_decision(
    record: dict, match: RevealMatch, *, published_ts: int,
) -> tuple[bool, str]:
    """Deduplicate characters and announcements in the same source minute."""
    chars = record.get("reveal_chars") if isinstance(record.get("reveal_chars"), dict) else {}
    if match.canonical_key in chars:
        return False, "already_pinged_character"
    if published_ts > 0:
        for seen_key, seen_ts in chars.items():
            if (isinstance(seen_ts, int) and abs(published_ts - seen_ts) <= VARIANT_WINDOW_S
                    and _same_character_variant(str(seen_key), match.canonical_key)):
                # The same reveal published under a fuller name minutes later (ZZZ 12:15 dossiers).
                return False, "same_character_variant"
    times = record.get("reveal_times") if isinstance(record.get("reveal_times"), dict) else {}
    if published_ts > 0 and any(
        str(key).isdigit() and int(key) // 60 == published_ts // 60 for key in times
    ):
        return False, "same_publication_time"
    return True, "new_character_reveal"


def commit_reveal_delivery(
    record: dict,
    match: RevealMatch,
    *,
    published_ts: int,
    now: int,
    pinged: bool,
) -> None:
    """Persist character reveal state AFTER message 0 has been accepted by Discord."""
    chars = record.setdefault("reveal_chars", {})
    if not isinstance(chars, dict):
        chars = {}
        record["reveal_chars"] = chars
    chars.setdefault(match.canonical_key, int(published_ts or now))
    if pinged:
        times = record.setdefault("reveal_times", {})
        if isinstance(times, dict) and published_ts > 0:
            times[str(published_ts - published_ts % 60)] = int(now)
            if len(times) > 200:
                for key in sorted(times, key=lambda k: (times[k] if isinstance(times[k], int) else 0, k))[:-200]:
                    times.pop(key, None)
    prune_reveal_chars(record)
