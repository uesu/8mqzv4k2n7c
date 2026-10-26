"""Discord Components V2 card builders — buttons are NESTED INSIDE the card container.

Layout (schedule card), converted 1:1 from the reference embed cards:

  [Container accent=14922399]        <- the whole post; NOTHING sits above it
     ## [Honkai: Star Rail Version 4.6 Special Program](url) <:ananta1:…>  <- title + url + scroll
     <t:…:F> or <t:…:R>  +  ※ maintenance-extended note
     ───────────
     **Version 4.6 Banners (STC)**  re-runs / phases / 4★ (TBA when unknown)
     ───────────
     **Maintenance Details (STC)**  ✦ Pre-Install / Start / End (+ compensation)
     [Media Gallery]  announcement image                                   <- embed image
     ───────────
     [Action Row]  Youtube · Twitch · Source (+ EXTRA_BUTTONS)             <- buttons INSIDE
     -# STC — Subject to Change • TBA — To be Announced <@&ROLE>          <- mention lives HERE

Hard limits enforced by validate_payload(): 40 components total (nested
included), 4000 characters across all Text Displays, 5 buttons per row,
10 media items per gallery, no `content`/`embeds` with IS_COMPONENTS_V2.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

from .config import Game, Ping, Settings
from .textutil import strip_invisible, truncate
from .timeparse import discord_ts

log = logging.getLogger("gamexpress.cards")

IS_COMPONENTS_V2 = 1 << 15
MAX_COMPONENTS = 40
MAX_TEXT_TOTAL = 4000
TBA = "TBA"
LEGEND = "STC — Subject to Change • TBA — To be Announced"


# --------------------------------------------------------------------------- primitives
def text(content: str) -> dict:
    # Last chokepoint before Discord. Scraped text is already cleaned at the source, but a
    # field can also arrive through an override or a sample, and an invisible character (bidi
    # override, zero-width space) on a card is never intentional - it only ever makes a card
    # display something other than what it says.
    return {"type": 10, "content": strip_invisible(content)}


def sep(divider: bool = True, spacing: int = 1) -> dict:
    return {"type": 14, "divider": divider, "spacing": spacing}


def gallery(urls: list[str]) -> dict:
    safe = [u for u in (safe_url(x) for x in urls) if u]
    return {"type": 12, "items": [{"media": {"url": u}} for u in safe[:10]]}


def thumbnail(url: str) -> dict:
    return {"type": 11, "media": {"url": safe_url(url) or ""}}


def section(texts: list[str], accessory: dict) -> dict:
    return {"type": 9, "components": [text(t) for t in texts[:3]], "accessory": accessory}


def link_button(label: str, url: str, emoji: dict | None = None) -> dict | None:
    """None when the URL cannot be trusted: the caller drops that one button and the rest of the
    card still posts. Keeping a broken button would fail validate_payload() and lose the whole
    announcement over a single bad field."""
    safe = safe_url(url)
    if not safe or len(safe) > BUTTON_URL_MAX:
        log.warning("dropping button %r - unusable url %r", label, (url or "")[:120])
        return None
    b: dict[str, Any] = {"type": 2, "style": 5, "label": truncate(label, 80), "url": safe}
    if emoji:
        b["emoji"] = emoji
    return b


def buttons_of(*candidates: dict | None) -> list[dict]:
    """Keep the buttons that survived link_button()."""
    return [b for b in candidates if b]


def emoji_str(e: dict | None) -> str:
    """An emoji as Discord TEXT syntax — buttons take the dict, text components take this:
    '<:name:id>' (or '<a:name:id>' animated), the unicode character itself, or '' when off."""
    if not e:
        return ""
    if e.get("id"):
        return f"<a:{e['name']}:{e['id']}>" if e.get("animated") else f"<:{e['name']}:{e['id']}>"
    return e.get("name") or ""


def action_row(buttons: list[dict]) -> dict:
    return {"type": 1, "components": [b for b in buttons if b][:5]}


def container(children: list[dict], color: int | None = None) -> dict:
    return {
        "type": 17,
        "accent_color": None if color is None else (int(color) & 0xFFFFFF),
        "components": children,
    }


def _md_link_text(s: str) -> str:
    return s.replace("[", "(").replace("]", ")")


SAFE_URL_MAX = 1024
BUTTON_URL_MAX = 512


def safe_url(url: str | None) -> str | None:
    """A URL that is safe to put on a card, or None if it cannot be trusted.

    Most URLs on a card are scraped from somebody else's server - community nitter and Redlib
    mirrors, rich-embed proxies, wikis, code APIs - so any of them can turn hostile the day a
    mirror changes hands. Two things go wrong if the string is used as-is:

      * a non-http scheme ("javascript:", "data:") in a link button makes Discord reject the
        whole message, so one poisoned field silently kills a real announcement;
      * a ")" inside a markdown link closes it early: [title](https://ok/x) [FREE CODES](evil)
        renders as an extra clickable link nobody here wrote - a phishing line inside a card
        readers trust because it came from this bot.

    So: http(s) only, no whitespace or control characters, bounded length, and parentheses are
    percent-encoded (servers decode them back; markdown stops seeing them).
    """
    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")) or len(u) > SAFE_URL_MAX:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in u):
        return None
    return u.replace("(", "%28").replace(")", "%29")


def _is_x_url(url: str) -> bool:
    """An x.com / twitter.com link — the announcement tweet itself, which the card already links
    to in its title, so it never needs its own button."""
    return bool(re.match(r"https?://(?:www\.|mobile\.)?(?:x|twitter|fxtwitter|vxtwitter|fixupx)\.com/",
                         url or "", re.I))


_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def _clean_banner_name(s: str) -> str:
    s = _WIKILINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), str(s or ""))
    s = s.replace("[[", "").replace("]]", "").replace("&amp;", "&")
    return re.sub(r"\s{2,}", " ", s).strip()


def names(values: list[str] | None, disp: dict[str, str] | None = None) -> str:
    vals = [_clean_banner_name(v) for v in (values or []) if v and _clean_banner_name(v)]
    if disp and vals:
        clean_disp = {_clean_banner_name(k): _clean_banner_name(v) for k, v in disp.items()}
        vals = [clean_disp.get(v, disp.get(v, v)) for v in vals]     # the wiki's full display name, when it has one
        vals = [_clean_banner_name(v) for v in vals if _clean_banner_name(v)]
    return ", ".join(vals) if vals else TBA


# --------------------------------------------------------------------------- validation
def count_components(components: list[dict]) -> int:
    total = 0
    for c in components or []:
        total += 1
        total += count_components(c.get("components", []))
        if isinstance(c.get("accessory"), dict):
            total += 1
    return total


def text_length(components: list[dict]) -> int:
    total = 0
    for c in components or []:
        if c.get("type") == 10:
            total += len(c.get("content", ""))
        total += text_length(c.get("components", []))
    return total


def validate_payload(payload: dict) -> list[str]:
    """Return a list of problems (empty = safe to send)."""
    problems: list[str] = []
    comps = payload.get("components") or []
    if not payload.get("flags", 0) & IS_COMPONENTS_V2:
        problems.append("missing IS_COMPONENTS_V2 flag")
    if payload.get("content") or payload.get("embeds"):
        problems.append("content/embeds are not allowed with Components V2")
    n = count_components(comps)
    if n > MAX_COMPONENTS:
        problems.append(f"{n} components > {MAX_COMPONENTS}")
    t = text_length(comps)
    if t > MAX_TEXT_TOTAL:
        problems.append(f"{t} text chars > {MAX_TEXT_TOTAL}")

    def walk(cs: list[dict], parent: int | None) -> None:
        for c in cs:
            ty = c.get("type")
            if ty == 2 and parent not in (1, 9):
                problems.append("button outside an action row")
            if ty == 1 and not 1 <= len(c.get("components", [])) <= 5:
                problems.append("action row needs 1-5 buttons")
            if ty == 2 and c.get("style") == 5 and (
                not str(c.get("url", "")).startswith(("http://", "https://"))
                or len(str(c.get("url", ""))) > BUTTON_URL_MAX
            ):
                problems.append(f"link button without a valid url: {c.get('label')}")
            if ty == 10 and not c.get("content"):
                problems.append("empty text display")
            if ty == 12 and not 1 <= len(c.get("items", [])) <= 10:
                problems.append("media gallery needs 1-10 items")
            if isinstance(c.get("accessory"), dict):
                walk([c["accessory"]], ty)
            walk(c.get("components", []), ty)
    walk(comps, None)
    return problems


def _payload(card: dict, ping: Ping) -> dict:
    """One card, nothing above it. The mention lives INSIDE the container (on the legend line
    of a schedule card, on the '… new code(s) • detected …' line of a codes card) so the post
    reads as a single block instead of a bare @role floating above a card."""
    return {"flags": IS_COMPONENTS_V2, "components": [card], "allowed_mentions": ping.allowed_mentions}


def _with_ping(line: str, ping: Ping) -> str:
    """Append the mention to a small-text line. Empty Ping -> line is returned untouched, so a
    no-ping card never renders a stray space or a dead blue pill."""
    return f"{line} {ping.text}" if ping else line


def _std_buttons(game: Game, settings: Settings, extra: list[dict] | None = None) -> list[dict]:
    buttons: list[dict] = []
    if game.youtube:
        buttons += buttons_of(link_button("Youtube", game.youtube, settings.emoji.get("youtube")))
    if game.twitch:
        buttons += buttons_of(link_button("Twitch", game.twitch, settings.emoji.get("twitch")))
    buttons.extend(b for b in (extra or []) if b)
    for b in settings.extra_buttons:
        buttons += buttons_of(link_button(b["label"], b["url"], _emoji_of(b.get("emoji"))))
    return buttons[:5]


ESTIMATE_LABELS = {
    "program_ts": "program time",
    "preinstall_ts": "pre-install",
    "maint_start_ts": "maintenance start",
    "maint_end_ts": "maintenance end",
}


def _emoji_of(value) -> dict | None:
    from .config import parse_emoji
    if isinstance(value, dict):
        return value
    return parse_emoji(value) if value else None


# --------------------------------------------------------------------------- schedule card
def program_title(game: Game, d: dict) -> str:
    program = d.get("program_name") or game.program_label
    return game.card.title.format(game=game.name, version=d.get("version") or TBA, program=program,
                                  version_name=d.get("version_name") or "").strip()


def banners_block(game: Game, d: dict) -> str:
    b = d.get("banners") or {}
    # The wiki's display names: the official notice writes 'Phoenix', the wiki 'Phoenix
    # Reffaella', and the card shows the full form. Render-time only — the stored names,
    # their provenance and their locks are exactly as each source wrote them (schedule's
    # _display_map built this dict), so a wiki correction simply re-renders the card.
    disp = d.get("banners_display") or {}
    v = d.get("version") or TBA
    heading = f"Version {v} Banners (STC)"
    heading = f"**[{heading}]({game.card.banners_url})**" if game.card.banners_url else f"**{heading}**"
    lines = [heading]
    notes: list[str] = []
    # The early tier: the wiki knows WHO debuts in this version but no phase split exists yet
    # (the livestream has not aired). The line disappears by itself the moment real phase data
    # lands, because `confirmed` is not part of the banner key set.
    if b.get("confirmed") and not b.get("phase1"):
        notes.append(f"※ Confirmed: {names(b.get('confirmed'), disp)}")
    # The 4★ summary is the pre-notice signal (the wiki knows the full roster before any phase
    # notice exists, and often names phase 2's 4★s early); once BOTH per-phase lists are in,
    # the summary is just their union — noise — and hides on the same silent edit that filled
    # the second phase. Symmetric with the `※ Confirmed:` rule above.
    if game.card.four_star_summary and not (b.get("phase1_4") and b.get("phase2_4")):
        notes.append(f"※ 4 Star Characters: {names(b.get('four_star'), disp)}")
    if notes:
        lines += ["", *notes]
    four = game.card.four_star_label
    lines += ["", f"✦ First Half/Phase: {names(b.get('phase1'), disp)}",
              f"- {four}: {names(b.get('phase1_4'), disp)}", "",
              f"✦ Second Half/Phase: {names(b.get('phase2'), disp)}",
              f"- {four}: {names(b.get('phase2_4'), disp)}"]
    return "\n".join(lines)


def maintenance_block(game: Game, d: dict) -> str:
    lines = [f"**{game.card.maintenance_heading}**", ""]
    pre, start, end = d.get("preinstall_ts"), d.get("maint_start_ts"), d.get("maint_end_ts")
    if game.card.maintenance_style == "range":
        lines.append(f"✦ Pre-Install: {discord_ts(pre, 'F')}")
        if start and end:
            lines.append(f"✦ Maintenance: {discord_ts(start, 'f')} to {discord_ts(end, 't')}")
        elif start:
            lines.append(f"✦ Maintenance: {discord_ts(start, 'f')} to {TBA}")
        else:
            lines.append(f"✦ Maintenance: {TBA}")
    else:
        lines += [f"✦ Pre-Install: {discord_ts(pre, 'F')}",
                  f"✦ Start: {discord_ts(start, 'F')}",
                  f"✦ End: {discord_ts(end, 'F')}"]
    if d.get("compensation"):
        lines.append(f"✦ Compensation: {d['compensation']}")
    return "\n".join(lines)


def schedule_payload(game: Game, d: dict, settings: Settings, ping: Ping,
                     updated_ts: int | None = None) -> dict:
    title = _md_link_text(program_title(game, d))
    url = safe_url(d.get("title_url") or d.get("source_url"))
    # The scroll on the headline, every game: the title emoji (EMOJI_TITLE), bare when "none".
    tail = f" {emoji_str(settings.emoji.get('title'))}".rstrip()
    head = f"## [{title}]({url}){tail}" if url else f"## {title}{tail}"
    pts = d.get("program_ts")
    if pts:
        ts_line = f"{discord_ts(pts, 'F')} or {discord_ts(pts, 'R')}"
    elif d.get("maint_start_ts") or d.get("preinstall_ts"):
        ts_line = ""      # the program aired before the update notice — never show a misleading "TBA"
    else:
        ts_line = f"{d.get('program_name') or game.program_label}: {TBA}"
    top = f"{head}\n{ts_line}" if ts_line else head
    maint = maintenance_block(game, d)
    banners = banners_block(game, d)

    children: list[dict] = []
    if game.card.maintenance_first:          # WW order: timestamps → maintenance + note → banners
        children.append(text(top))
        children += [sep(), text(f"{maint}\n\n{game.card.note}" if game.card.note else maint)]
        if game.card.show_banners:
            children += [sep(), text(banners)]
    else:                                     # GI / HSR / ZZZ order
        first = top + (f"\n\n{game.card.note}" if game.card.note else "")
        children.append(text(first))
        if game.card.show_banners:
            children += [sep(), text(banners)]
        children += [sep(), text(maint)]
    images = [u for u in (d.get("images") or ([d["image"]] if d.get("image") else [])) if u][:4]
    if images:
        children.append(gallery(images))
    children.append(sep())

    extra: list[dict] = []
    src = d.get("source_url")
    # No "x" button. The announcement tweet is already the card's title link whenever the version
    # has no single YouTube video (GI/ZZZ), and when it does (HSR/WW) the title points there — so
    # a third button labelled "x" only ever duplicated something already on the card.
    if src and src != url and not _is_x_url(src):
        extra += buttons_of(link_button(d.get("source_label") or "Source", src,
                                        settings.emoji.get("source")))
    elif d.get("youtube_video") and d.get("youtube_video") != url:
        extra += buttons_of(link_button("Watch", d["youtube_video"], settings.emoji.get("youtube")))
    buttons = _std_buttons(game, settings, extra)
    if buttons:
        children.append(action_row(buttons))

    foot: list[str] = []
    if settings.show_legend:
        foot.append(LEGEND)
    if d.get("estimated"):
        what = ", ".join(ESTIMATE_LABELS.get(k, k) for k in d["estimated"])
        est_src = ", ".join((d.get("estimate_sources") or ["countdown sites"])[:2])
        foot.append(f"🕒 {what} estimated from {est_src} — the official notice replaces it automatically")
    if updated_ts:
        foot.append(f"Updated {discord_ts(updated_ts, 'R')}")
    # The mention rides on the FIRST footer line (normally the legend). If every footer is
    # switched off it gets a line of its own — the ping must never be silently dropped.
    if ping:
        if foot:
            foot[0] = _with_ping(foot[0], ping)
        else:
            foot.append(ping.text)
    for part in foot:                      # one small-text line each, not one run-on line
        children.append(text("-# " + part))
    return _payload(container(children, game.color), ping)


# --------------------------------------------------------------------------- codes card
CODES_PER_CARD = 10


def pretty_rewards(raw: str | list | None) -> str:
    """'Primogem*60;Mora*20,000' -> 'Primogem ×60 • Mora ×20,000'"""
    if not raw:
        return ""
    if isinstance(raw, list):
        return " • ".join(str(x).strip() for x in raw if str(x).strip())
    parts = [p.strip() for p in str(raw).replace("\n", ";").split(";") if p.strip()]
    out = []
    for p in parts:
        if "*" in p:
            name, _, qty = p.rpartition("*")
            out.append(f"{name.strip()} ×{qty.strip()}")
        else:
            out.append(p)
    return " • ".join(out)


def codes_card(game: Game, chunk: list[dict], settings: Settings, ping: Ping, detected_ts: int,
               part: tuple[int, int] = (1, 1)) -> dict:
    """ONE code message. A code dict may carry "expired": True — it is then shown struck
    through, without a Redeem button (used when a posted card is edited after the code dies)."""
    n = len(chunk)
    dead = sum(1 for c in chunk if c.get("expired"))
    plural = "s" if n != 1 else ""
    head = f"## 🎁 {game.name} Redemption Code{plural}"
    if dead and dead == n:
        sub = f"-# {'all ' if n > 1 else ''}{n} code{plural} in this post expired • posted {discord_ts(detected_ts, 'R')}"
    elif dead:
        sub = f"-# {n} codes • {dead} expired • detected {discord_ts(detected_ts, 'R')}"
    else:
        sub = f"-# {n} new code{plural} • detected {discord_ts(detected_ts, 'R')}"
    if part[1] > 1:
        sub += f" • part {part[0]}/{part[1]}"
    # Only the first part of a split drop mentions anyone: one notification per batch of codes.
    if part[0] == 1:
        sub = _with_ping(sub, ping)
    children: list[dict] = []
    if game.icon:
        children.append(section([head, sub], thumbnail(game.icon)))
    else:
        children.append(text(f"{head}\n{sub}"))
    children.append(sep())
    lines = []
    for c in chunk:
        rw = pretty_rewards(c.get("rewards"))
        code_md = f"~~`{c['code']}`~~ · expired" if c.get("expired") else f"`{c['code']}`"
        lines.append(f"✦ {code_md}" + (f"\n-# {truncate(rw, 180)}" if rw else ""))
    children.append(text("\n".join(lines) if lines else "No codes."))
    children.append(sep())
    live = [c for c in chunk if not c.get("expired")]
    if game.redeem_url and live:
        btns = buttons_of(*(link_button(c["code"], game.redeem_url.format(code=quote(c["code"])),
                                        settings.emoji.get("redeem")) for c in live))
        for i in range(0, len(btns), 5):
            children.append(action_row(btns[i:i + 5]))
    hint = game.codes.get("redeem_hint")
    if hint and live:
        children.append(text(f"※ {hint}"))
    children.append(sep())
    # Own row, under the codes and separated from the per-code Redeem links: the community
    # invite (+ EXTRA_BUTTONS). No Redeem Page / Youtube / Twitch here — those belong to the
    # livestream (special program / special broadcast) card, not to a codes card.
    buttons = buttons_of(*(link_button(b["label"], b["url"], _emoji_of(b.get("emoji")))
                           for b in (settings.community_buttons + settings.extra_buttons)))[:5]
    if buttons:
        children.append(action_row(buttons))
    srcs = sorted({s for c in chunk for s in c.get("sources", [])})
    foot = "-# Source: " + (", ".join(SOURCE_LABELS.get(s, s) for s in srcs) if srcs else "—")
    foot += " • Codes expire — redeem soon." if live else " • Expired codes are kept for reference."
    children.append(text(foot))
    return _payload(container(children, game.color), ping if part[0] == 1 else Ping())


def codes_payloads(game: Game, codes: list[dict], settings: Settings, ping: Ping,
                   now_ts: int) -> list[dict]:
    """One card per ≤10 codes. Each code gets a prefilled 'Redeem' button when the game
    has web redemption (GI / HSR / ZZZ); otherwise the in-game hint is shown. Only the
    first card of a batch pings."""
    chunks = [codes[i:i + CODES_PER_CARD] for i in range(0, len(codes), CODES_PER_CARD)] or [[]]
    return [codes_card(game, chunk, settings, ping, now_ts, (idx + 1, len(chunks)))
            for idx, chunk in enumerate(chunks)]


SOURCE_LABELS = {
    "hoyolab": "HoYoLAB (official)",
    "x": "Official X",
    "kuro": "Kuro official news",
    "reddit_official": "Official Reddit",
    "reddit": "Reddit",
    "seria": "hoyo-codes.seria.moe (verified)",
    "humbao": "hoyoverse-codes (verified)",
    "ogc": "Open Gacha Codes",
    "ennead": "api.ennead.cc",
    "fandom": "Fandom wiki",
    "codehub": "PromoGacha",
    "wuthering.gg": "wuthering.gg",
    "manual": "manual",
}

TEST_NOTE = "🧪 TEST CARD — sample / test data, not a real announcement"


def mark_test(payload: dict, note: str = TEST_NOTE) -> dict:
    """Label a card as a test: a '🧪 TEST CARD …' line inserted at the very top of the card.
    Used by the monitor's test bench so nobody mistakes a test post for real news or real codes.
    Idempotent — marking an already-marked card does not stack a second banner."""
    import copy
    p = copy.deepcopy(payload)
    for c in p.get("components") or []:
        if c.get("type") == 17:
            kids = c.setdefault("components", [])
            if not (kids and str(kids[0].get("content", "")).startswith("-# 🧪")):
                kids.insert(0, text(f"-# {note}"))
            break
    return p


# Full-content news: each part independently satisfies existing Components V2 limits.
NEWS_DONATE = "https://ko-fi.com/jieunlatte"
NEWS_COMMUNITY = "https://discord.gg/HyrVP9wRXu"
NEWS_EXPAND_MAX_PAGES = 12
NEWS_EXPAND_MAX_CHARS = 120000
NEWS_EXPAND_MAX_MEDIA = 120

MAINT_HEADING_EMOJIS = "<a:starwardlove:1483082509439733893><a:starwardspark3:1483083423290490891>"
MAINT_GEAR_EMOJI = "<a:gearfinal:1483076702551805983>"
CITLALI_FOOTER_TEXT = "<a:starwardsleepywink:1483078175436050432> **Citlali News**\n-# Delivering news and official updates"
CITLALI_FOOTER_EMOJI = {"id": "1483074533073223843", "name": "starwardyayy", "animated": True}


def maintenance_payload(
    game: Game,
    version: str,
    kind: str,
    *,
    start_ts: int,
    end_ts: int | None = None,
    ping: Ping | None = None,
    media_url: str | None = None,
    estimated: bool = False,
    estimate_source: str = "",
    observed: bool = False,
    silent: bool = False,
) -> dict:
    """Build a Pre-Load Started or Maintenance Started Components V2 payload.

    Follows the approved Components V2 template without any third-party social button:
      * Flags 32768 (`IS_COMPONENTS_V2`), type 17 container (`accent_color: None`)
      * Type 10 header with `[SHORT - VERSION]` + approved emojis + configured ping
      * Type 14 divider (`spacing: 1`)
      * Type 10 timestamp line (`:f` + `:R` for preload; `:f` + known `:t` for maintenance)
      * Optional type 12 media gallery (omitted cleanly when no valid version-matched media exists)
      * Type 9 Citlali News section footer with `starwardyayy` link button accessory
    """
    if kind not in ("preload", "maintenance"):
        raise ValueError(f"unsupported maintenance announcement kind: {kind!r}")
    ver = strip_invisible(version).strip()
    if not ver:
        raise ValueError("maintenance announcement requires a non-empty version")
    ping_obj = ping or Ping()
    label = "Pre-Load Started" if kind == "preload" else "Maintenance Started"
    headline = f"## [{game.short} - {ver}] {label} {MAINT_HEADING_EMOJIS}"
    if ping_obj:
        headline += f"\n{ping_obj.text}"
    if kind == "preload":
        timing = (
            f"※ **Date Time:** <t:{int(start_ts)}:f> "
            f"※ **Hour Time:** <t:{int(start_ts)}:R> {MAINT_GEAR_EMOJI}"
        )
    elif end_ts is not None and int(end_ts) > int(start_ts):
        timing = (
            f"※ **Date Time:** <t:{int(start_ts)}:f> "
            f"※ **End Time:** <t:{int(end_ts)}:t> {MAINT_GEAR_EMOJI}"
        )
    else:
        timing = f"※ **Date Time:** <t:{int(start_ts)}:f> {MAINT_GEAR_EMOJI}"
    if observed:
        timing += "\n-# Observed available in launcher — official start time not published"
    elif estimated:
        source_note = estimate_source.strip() or "version cadence"
        timing += f"\n-# Estimated ({source_note}) — subject to official notice"

    children: list[dict] = [
        text(headline),
        sep(),
        text(timing),
    ]
    if media_url:
        safe_media = safe_url(media_url)
        if safe_media:
            children.append(gallery([safe_media]))
    footer_btn = link_button("Citlali News", NEWS_COMMUNITY, CITLALI_FOOTER_EMOJI)
    if footer_btn:
        children.append(section([CITLALI_FOOTER_TEXT], footer_btn))
    return {
        "flags": IS_COMPONENTS_V2,
        "components": [container(children, None)],
        "allowed_mentions": {"parse": []} if silent else ping_obj.allowed_mentions,
    }


_UNESCAPED_OPEN_BRACKET_RE = re.compile(r"(?<!\\)\[")
_UNESCAPED_CLOSE_BRACKET_RE = re.compile(r"(?<!\\)\]")


def _brackets_balanced(piece: str) -> bool:
    if "\\" not in piece:
        return piece.count("[") == piece.count("]")
    return len(_UNESCAPED_OPEN_BRACKET_RE.findall(piece)) == len(
        _UNESCAPED_CLOSE_BRACKET_RE.findall(piece)
    )


def _balanced_markdown_piece(piece: str) -> bool:
    return (
        piece.count("**") % 2 == 0
        and piece.count("`") % 2 == 0
        and _brackets_balanced(piece)
    )


def _split_expanded_text(content: str, max_chars: int) -> list[str] | None:
    """Split a long normalized text block at safe paragraph/line/word boundaries.

    Guarantees `"".join(result) == content` whenever splitting succeeds. Returns `None`
    if an indivisible token exceeds `max_chars` or leaves unbalanced inline formatting.
    """
    if len(content) <= max_chars:
        return [content] if _balanced_markdown_piece(content) else None
    out: list[str] = []
    paragraphs = [p for p in re.findall(r".+?(?:\n\n|$)", content, re.S) if p]
    for para in paragraphs:
        if len(para) <= max_chars and _balanced_markdown_piece(para):
            out.append(para)
            continue
        lines = [ln for ln in re.findall(r".+?(?:\n|$)", para, re.S) if ln]
        for line in lines:
            if len(line) <= max_chars and _balanced_markdown_piece(line):
                out.append(line)
                continue
            tokens = [tok for tok in re.findall(r"\S+\s*", line) if tok]
            buf = ""
            for tok in tokens:
                if len(tok) > max_chars:
                    return None
                cand = buf + tok
                if len(cand) <= max_chars:
                    buf = cand
                    continue
                if not buf or not _balanced_markdown_piece(buf):
                    return None
                out.append(buf)
                buf = tok
            if buf:
                if not _balanced_markdown_piece(buf) or len(buf) > max_chars:
                    return None
                out.append(buf)
    return out if out and "".join(out) == content else None


def news_payloads(
    game: Game,
    item,
    *,
    timestamps: bool = False,
    expand_full: bool = False,
    max_pages: int | None = None,
    max_chars: int | None = None,
    max_media: int | None = None,
) -> list[dict]:
    """Full article in one message, or paged according to the active policy switch.

    When `expand_full` is False (default): attempts 1 message, then at most 2 pages up to
    7,000 normalized text chars, otherwise 1 halved-word excerpt with 1 lead image.
    When `expand_full` is True: lifts the 2-page / 7,000-char policy ceiling up to the
    explicit `NEWS_EXPAND_MAX_PAGES` / `NEWS_EXPAND_MAX_CHARS` / `NEWS_EXPAND_MAX_MEDIA`
    operational safety bounds, preserving inline text/media order and falling back honestly
    if any bound is exceeded.
    """
    title = strip_invisible(item.title).replace("[", "(").replace("]", ")")
    if len(title) > 500 or not safe_url(item.url):
        raise ValueError("article title/URL cannot be rendered safely")
    author = strip_invisible(item.author)
    if item.source == "hoyolab" and author in ("", "HoYoLAB official news"):
        author = game.name
    if len(author) > 200:
        raise ValueError("article attribution exceeds safe header size")
    byline = f"-# {item.source_label}" + (f" · {author}" if author else "")
    if item.published_ts and item.published_ts > 0:
        byline += f" · <t:{item.published_ts}:f>"
    header = text(f"## [{title}]({item.url})\n{byline}")
    buttons = action_row(buttons_of(
        link_button("Read Post", item.url, {"id": "1472388018689282261", "name": "starwardhmm", "animated": True}),
        link_button("Citlali News", NEWS_COMMUNITY, {"id": "1439878792653832253", "name": "starward11", "animated": True}),
        link_button("Donate", NEWS_DONATE, {"id": "1509026327548657914", "name": "starwardfans", "animated": True})))

    def polish(value):
        value = strip_invisible(value).strip()
        value = re.sub(r"(?m)^([ \t]*)(\*\*)?[•●][ \t]*",
                       lambda m: m[1] + "- " + (m[2] or ""), value)
        # Marker padding, not blanket paragraph insertion before every footnote.
        value = re.sub(r"([※✦])(?=[\w[(])", r"\1 ", value)
        value = re.sub(r"(?<=[\w)\]])✦", " ✦", value)
        value = re.sub(r"(?m)^(※[^\n]+)\n(?=※)", r"\1\n\n", value)
        value = re.sub(r"(?m)^([ \t]*- [^\n]+)\n(?:[ \t]*\n)+(?=[ \t]*- )", r"\1\n", value)
        # Adjacent enumerated entries stay compact; do not rewrite their wording.
        for marker in (r"\d+[.)]", r"[①-⑳㉑-㉟㊱-㊿]", r"[a-z][.)]", r"■"):
            value = re.sub(rf"(?m)^([ \t]*(?:{marker})[ \t]+[^\n]+)\n(?:[ \t]*\n)+"
                           rf"(?=[ \t]*(?:{marker})[ \t]+)", r"\1\n", value)
        value = re.sub(r"(?<!\n)\n(?=\*\*▌)", "\n\n", value)
        value = re.sub(r'(?m)^("[^\n]+" Signal Search Details|The Final Callback - Audition Stage Details)$',
                       r"**\1**", value)
        return value

    units, article_text = [], []
    pending = ""

    def flush():
        nonlocal pending
        value = polish(pending)
        pending = ""
        if value:
            if timestamps:
                from .news_timestamps import convert_news_timestamps

                value = convert_news_timestamps(
                    value, published_ts=int(item.published_ts or 0), enabled=True
                )
            article_text.append(value)
            units.append(text(value))

    for block in item.article_blocks:
        if block["kind"] == "text":
            pending += block.get("text", "")
            continue
        flush()
        url = safe_url(block.get("url"))
        if not url:
            raise ValueError("unsupported article media URL")
        if block["kind"] == "link" or (block["kind"] == "video" and not re.search(r"\.(?:mp4|webm)(?:[?#]|$)", url, re.I)):
            units.append(text(f"[Open original media]({url})"))
        else:
            # A separate inline gallery preserves each text/image relationship.
            units.append(gallery([url]))
    flush()
    if not units:
        raise ValueError("empty full article")
    cover = safe_url(item.images[0]) if item.images else None
    if item.images and not cover:
        raise ValueError("unsupported article cover URL")
    # Covers are listing artwork, not body content. Do not prepend a separate
    # poster when the full article already renders a playable native video.
    # Also avoid repeating a cover already present verbatim in body media.
    # Keep explicit body images/order, link-only video covers and excerpt leads.
    body_media = {entry["media"]["url"] for unit in units if unit["type"] == 12
                  for entry in unit["items"]}
    native_video = any(block["kind"] == "video" and re.search(
        r"\.(?:mp4|webm)(?:[?#]|$)", block.get("url", ""), re.I)
                       for block in item.article_blocks)
    full_cover = None if native_video or cover in body_media else cover
    chunks, current = [], []
    for unit in units:
        # Do not split one uninterrupted media run merely at the eight-unit
        # readability threshold. Galleries remain separate; no long-strip policy.
        media_run = current and current[-1]["type"] == 12 and unit["type"] == 12
        if len(current) >= 8 and not media_run:
            chunks.append(current)
            current = []
        current.append(unit)
    if current:
        chunks.append(current)
    containers = []
    for index, chunk in enumerate(chunks):
        kids = ([header] + ([gallery([full_cover])] if full_cover else [])) if index == 0 else []
        if len(chunks) > 1:
            kids += [text(f"-# Part {index + 1}/{len(chunks)}"), sep()]
        kids += chunk
        if index == len(chunks) - 1 and buttons["components"]:
            kids += [sep(), buttons]
        containers.append(container(kids, game.color))
    payload = {"flags": IS_COMPONENTS_V2, "allowed_mentions": {"parse": []}, "components": containers}
    if not validate_payload(payload):
        return [payload]

    def page_prefix(number):
        return (([header] + ([gallery([full_cover])] if full_cover else [])) if number == 1 else []) + [
            text(f"-# Page {number}"), sep()]

    def page_payload(kids, final=False):
        return {"flags": IS_COMPONENTS_V2, "allowed_mentions": {"parse": []},
                "components": [container(kids + ([sep(), buttons] if final and buttons["components"] else []), game.color)]}

    excerpt_notice = ("**Excerpt — this article exceeds Discord's single-message limits.** "
                      "Read Post opens the complete article and remaining media.")

    if expand_full:
        eff_max_pages = max(1, int(NEWS_EXPAND_MAX_PAGES if max_pages is None else max_pages))
        eff_max_chars = max(1, int(NEWS_EXPAND_MAX_CHARS if max_chars is None else max_chars))
        eff_max_media = max(1, int(NEWS_EXPAND_MAX_MEDIA if max_media is None else max_media))
        distinct_media = set(body_media) | ({full_cover} if full_cover else set())
        if text_length([header] + units) > eff_max_chars or len(distinct_media) > eff_max_media:
            excerpt_notice = (
                "**Excerpt — this article exceeds the expanded delivery safety limit.** "
                "Read Post opens the complete article and remaining media."
            )
        else:
            first_page_budget = MAX_TEXT_TOTAL - text_length(page_prefix(1)) - 16
            cont_page_budget = MAX_TEXT_TOTAL - 32
            page_units = []
            splittable = True
            for unit in units:
                if unit["type"] != 10:
                    page_units.append(unit)
                    continue
                budget = first_page_budget if not page_units else cont_page_budget
                pieces = _split_expanded_text(unit["content"], max(1, budget))
                if pieces is None:
                    splittable = False
                    break
                page_units.extend(text(piece) for piece in pieces)
            if not splittable:
                excerpt_notice = (
                    "**Excerpt — this article contains content that exceeds single-message limits.** "
                    "Read Post opens the complete article and remaining media."
                )
            else:
                pages, current = [], page_prefix(1)
                have_body = False
                for unit in page_units:
                    trial = list(current)
                    if unit["type"] == 10 and have_body and trial[-1]["type"] == 10:
                        trial[-1] = text(trial[-1]["content"] + unit["content"])
                    else:
                        trial.append(unit)
                    if not validate_payload(page_payload(trial, final=True)):
                        current, have_body = trial, True
                        continue
                    if not have_body or len(pages) + 1 >= eff_max_pages:
                        excerpt_notice = (
                            "**Excerpt — this article exceeds the expanded delivery safety limit.** "
                            "Read Post opens the complete article and remaining media."
                        )
                        break
                    pages.append(page_payload(current))
                    next_num = len(pages) + 1
                    current, have_body = page_prefix(next_num) + [unit], True
                    if validate_payload(page_payload(current, final=True)):
                        excerpt_notice = (
                            "**Excerpt — this article contains content that exceeds single-message limits.** "
                            "Read Post opens the complete article and remaining media."
                        )
                        break
                else:
                    if have_body:
                        pages.append(page_payload(current, final=True))
                        if (
                            1 <= len(pages) <= eff_max_pages
                            and all(not validate_payload(page) for page in pages)
                        ):
                            return pages
    # Moderate overflow may retain the full source in at most two messages when default mode is active.
    # The 7,000 ceiling measures normalized text + title/byline ONCE; page labels
    # are additional overhead checked independently against each 4,000 cap.
    elif text_length([header] + units) <= 7000:
        page_units = []
        for unit in units:
            if unit["type"] != 10 or len(unit.get("content", "")) <= MAX_TEXT_TOTAL - text_length([header]) - 64:
                page_units.append(unit)
                continue
            # Split only between existing paragraphs, never inside links/bold.
            # Preserve the exact separators in content when flattened again.
            pieces = re.findall(r".+?(?:\n\n|$)", unit["content"], re.S)
            if any(piece.count("**") % 2 or not _brackets_balanced(piece) for piece in pieces):
                page_units.append(unit)
            else:
                page_units.extend(text(piece) for piece in pieces)

        pages, current = [], page_prefix(1)
        have_body = False
        for unit in page_units:
            # Coalesce text fragments to avoid artificial paragraph components.
            trial = list(current)
            if unit["type"] == 10 and have_body and trial[-1]["type"] == 10:
                trial[-1] = text(trial[-1]["content"] + unit["content"])
            else:
                trial.append(unit)
            # Reserve the final controls even while packing the first page.
            if not validate_payload(page_payload(trial, final=True)):
                current, have_body = trial, True
                continue
            if pages or not have_body:
                break
            pages.append(page_payload(current))
            current, have_body = page_prefix(2) + [unit], True
            if validate_payload(page_payload(current, final=True)):
                break
        else:
            if pages and have_body:
                pages.append(page_payload(current, final=True))
                if len(pages) == 2 and all(not validate_payload(page) for page in pages):
                    return pages
        # Never publish a partial full article: fall through to the excerpt.

    # Oversized article: retain the opening text, ONE lead image, and source access.
    # This deliberately replaces the previous multi-message full-body policy.
    lead = cover or next((safe_url(b.get("url")) for b in item.article_blocks if b["kind"] == "image"), None)
    notice = text(excerpt_notice)
    kids = [header] + ([gallery([lead])] if lead else []) + [notice, sep()]
    body = "\n\n".join(article_text) or "Open the original article for its remaining media."
    remaining = MAX_TEXT_TOTAL - text_length(kids) - 4
    if remaining <= 0:
        raise ValueError("article metadata leaves no excerpt budget")
    if len(body) > remaining:
        cut = max(body.rfind("\n", 0, remaining), body.rfind(" ", 0, remaining))
        body = body[:cut if cut > remaining // 2 else remaining].rstrip()
    # Compact overflow only: halve the WORDS of the already budget-fitting excerpt,
    # not the full source (half a long article could still exceed Discord's limit).
    words = list(re.finditer(r"\S+", body))
    if len(words) > 1:
        body = body[:words[max(1, len(words) // 2) - 1].end()].rstrip()
    else:
        body = body[:max(1, len(body) // 2)]
    if body.count("**") % 2:
        body += "**"
    body += "…"
    kids += [text(body)] + ([sep(), buttons] if buttons["components"] else [])
    payload["components"] = [container(kids, game.color)]
    if validate_payload(payload):
        raise ValueError("article excerpt exceeds Discord limits")
    return [payload]


BETA_FOOTER_EMOJI = {"id": "1439878792653832253", "name": "starward11", "animated": True}
BETA_RESOURCE_LINKS = {
    "genshin": (
        "Resource: [gachabase](https://gi.gachabase.net/changelog/beta?lang=en)"
        " / [nanoka](https://gi.nanoka.cc/)"
    ),
    "starrail": (
        "Resource: [gachabase](https://hsr.gachabase.net/changelog/beta?lang=en)"
        " / [nanoka](https://hsr.nanoka.cc/)"
    ),
    "zzz": (
        "Resource: [gachabase](https://zzz.gachabase.net/changelog/beta?lang=en)"
        " / [nanoka](https://zzz.nanoka.cc/)"
    ),
    "wuwa": "Resource: [nanoka](https://ww.nanoka.cc/) / [encore](https://encore.moe/new?v=Beta)",
}

_HSR_STANDARD_PATHS = {
    "Destruction", "Hunt", "Erudition", "Harmony",
    "Nihility", "Preservation", "Abundance", "Remembrance", "Elation",
}
_BOLD_COMBAT_NUM_RE = re.compile(
    r"(?<![\w\-\*~\[(#@/\.])("
    r"[+-]?\d+(?:\.\d+)?%(?:/[+-]?\d+(?:\.\d+)?%)+"
    r"|\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?){2,}"
    r"|[+-]?\d+(?:\.\d+)?(?:%|s\b)"
    r")(?![\w\*~\])])"
)
_SKILL_ATTR_HEADER_RE = re.compile(r"^\*\*Skill Attributes\b", re.I)


def _bold_beta_numbers(line: str) -> str:
    """Bold standalone combat percentages/durations without touching links, strikethroughs, or IDs."""
    if not line or "Equipment-line-" in line:
        return line
    protected: list[str] = []

    def _stash(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"\x01{len(protected) - 1}\x01"

    work = re.sub(
        r"\*\*[^*\n]+\*\*|~~[^~\n]+~~|`[^`\n]+`|\[[^\]\n]+\]\([^)\n]+\)|<[^>\n]+>|https?://\S+",
        _stash,
        line,
    )
    work = _BOLD_COMBAT_NUM_RE.sub(r"**\1**", work)
    return re.sub(r"\x01(\d+)\x01", lambda m: protected[int(m.group(1))], work)


def _beta_cycle_label(beta_ver: str) -> str:
    parts = [p for p in str(beta_ver or "").split(".") if p]
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0]}.{parts[1]}"
    return beta_ver or "?"


def _short_rev_label(rev_label: str, beta_ver: str) -> str:
    label = str(rev_label or "").strip()
    if not label:
        return "Update"
    m = re.match(r"^v?\d+(?:\.\d+){1,3}\s+(Rev\.\s*.+)$", label, re.I)
    if m:
        return m.group(1).strip()
    if label.lower() == f"v{beta_ver}".lower():
        return f"v{beta_ver}"
    return label


def _format_beta_rarity(game_key: str, rarity: int | None, *, is_equipment: bool = False) -> str:
    if not rarity:
        return ""
    r = max(1, min(5, int(rarity)))
    if game_key == "zzz":
        return "S-Rank" if r >= 5 else ("A-Rank" if r == 4 else "B-Rank")
    if is_equipment:
        return f"{r}★"
    return "★" * r


def _format_beta_stats_block(game_key: str, stats_lines: list[str]) -> list[str]:
    """Render character stats in Prime Formatting pipe-separated bold labels without blockquotes."""
    if not stats_lines:
        return []
    base_parts: list[str] = []
    bonus_lines: list[str] = []
    other_lines: list[str] = []

    for raw in stats_lines:
        sln = strip_invisible(str(raw or "")).lstrip("> ").strip()
        if not sln:
            continue
        m_lv = re.match(r"^Lv\.\s*\d+\s*[—-]\s*(.+)$", sln)
        if m_lv:
            for seg in [x.strip() for x in m_lv.group(1).split("·") if x.strip()]:
                sm = re.match(
                    r"^(HP|ATK|DEF|SPD|Max Energy|Crit\.?\s*Rate|Crit\.?\s*DMG|"
                    r"Energy Regen|Energy Recharge|Healing Bonus|Tune Break Boost)\s+(.+)$",
                    seg,
                    re.I,
                )
                if sm:
                    raw_k = sm.group(1).strip()
                    val_s = sm.group(2).strip()
                    if raw_k.lower() == "tune break boost" and re.fullmatch(r"0(?:\.0+)?%?", val_s):
                        continue
                    up = raw_k.upper()
                    if up in ("HP", "ATK", "DEF", "SPD"):
                        base_parts.append(f"**Base {up}:** {val_s}")
                    elif re.match(r"^crit\.?\s*rate$", raw_k, re.I):
                        base_parts.append(f"**CRIT Rate:** {val_s}")
                    elif re.match(r"^crit\.?\s*dmg$", raw_k, re.I):
                        base_parts.append(f"**CRIT DMG:** {val_s}")
                    else:
                        base_parts.append(f"**{raw_k}:** {val_s}")
                else:
                    base_parts.append(seg)
            continue
        if sln.lower().startswith(("trace stat bonuses:", "stat bonuses:")):
            head, _, rest = sln.partition(":")
            bonus_lines.append(f"**{head.strip()}:** {rest.strip()}")
            continue
        m_kv = re.match(
            r"^(SPD|Max Energy|CRIT Rate|CRIT DMG|Impact|Anomaly Mastery|"
            r"Anomaly Proficiency|PEN Ratio|Laceration DMG|Elemental Mastery|"
            r"Energy Recharge|Healing Bonus|HP%|ATK%|DEF%)\s+(.+)$",
            sln,
        )
        if m_kv:
            key_lbl = "Base SPD" if (game_key == "starrail" and m_kv.group(1) == "SPD") else m_kv.group(1)
            base_parts.append(f"**{key_lbl}:** {m_kv.group(2).strip()}")
            continue
        other_lines.append(sln)

    out: list[str] = []
    if base_parts:
        out.append(" | ".join(base_parts))
    out.extend(bonus_lines)
    out.extend(other_lines)
    return out


def _category_heading(game_key: str, cat: str, *, is_new: bool) -> str:
    clean_cat = strip_invisible(cat).strip()
    if not clean_cat:
        return ""
    if clean_cat == "Eidolons":
        return "## Eidolons" if is_new else "## Eidolon Changes"
    mapped = {
        ("starrail", "Skills"): "Base Kit",
        ("starrail", "Traces"): "Bonus Abilities",
    }.get((game_key, clean_cat), clean_cat)
    if not is_new and not mapped.lower().endswith("changes") and mapped not in ("Profile Changes",):
        mapped = f"{mapped} Changes"
    return f"### {mapped}"


_SKILL_SLOT_PREFIX_RE = re.compile(
    r"^(Normal Attack|Charged Attack|Plunging Attack|Heavy Attack|Mid-air Attack|"
    r"Elemental Skill|Elemental Burst|Alternate Sprint|Basic ATK|Basic Attack|"
    r"Enhanced Basic ATK|Enhanced Skill|Skill|Ultimate|Talent|Technique|"
    r"Memosprite Skill|Memosprite Talent|Elation Skill|Special Attack|EX Special Attack|"
    r"Dodge|Dash Attack|Dodge Counter|Quick Assist|Defensive Assist|Evasive Assist|"
    r"Counter Assist|Assist Follow-Up|Chain Attack|Entry Skill|Core Passive|"
    r"Additional Ability|Resonance Skill|Resonance Liberation|Forte Circuit|"
    r"Intro Skill|Outro Skill|Tune Break|Inherent Skill|Joint Attack|Summon|"
    r"Passive Skill|Bonus Ability(?:\s+\d+)?|Passive(?:\s+\d+)?):\s*(.+)$",
    re.I,
)
_IMPERATIVE_SUBHEAD_STOP_RE = re.compile(
    r"^(?:Press|Hold|Tap|Cast|Casting|Consume|Consumes|Attack|Attacks|Deals|Deal|"
    r"Switch|Gain|Gains|Increases|Increase|Restores|Restore|Summons|Summon|"
    r"Unleashes|Triggers|Trigger|Enters|Enter|While|When|After|During|Every|"
    r"Each|Character|All|For|Upon)\b",
    re.I,
)
_STAT_ROW_KEYWORD_RE = re.compile(
    r"\b(?:DMG|Cost|CD|Cooldown|Duration|Healing|Shield|Interval|Hits?|Multiplier|"
    r"Regen|Energy|Perfume|Chime|Limit|Stacks?|Consume|Consumption|STA|Daze|Anomaly)\b",
    re.I,
)


def _beta_masked_title(name: str, url: str | None) -> str:
    clean_name = strip_invisible(str(name or "Unknown")).strip() or "Unknown"
    safe = safe_url(url)
    if not safe:
        return clean_name
    return f"[{_md_link_text(clean_name)}]({safe})"


def _is_skill_subheading(ln: str, next_ln: str | None, tag: str) -> bool:
    """True when `ln` is an internal mode/sub-skill header (e.g. WuWa 'Slumber', GI 'Charged Attack')."""
    if tag not in ("[NEW]", "[CHANGED]") or not next_ln:
        return False
    if not (2 <= len(ln) <= 48) or len(ln.split()) > 6:
        return False
    if not ln[0].isupper() or not (ln[-1].isalnum() or ln[-1] in ("'", ")")):
        return False
    if re.search(r"[\d%\.\!\?:;,\[\]\*~→]", ln):
        return False
    if _IMPERATIVE_SUBHEAD_STOP_RE.match(ln):
        return False
    if next_ln.startswith(("**(", "-#", "⚠️", "**[")):
        return False
    return len(next_ln) > len(ln) or next_ln.endswith((".", "!", ")"))


def _format_skill_bullet_head(title: str, meta: str) -> str:
    clean_title = strip_invisible(title).strip()
    clean_meta = strip_invisible(meta).strip()
    meta_part = f" *({clean_meta})*" if clean_meta else ""

    m_dash = re.match(r"^(E\d+|C\d+|Sequence Node \d+|Stat Bonus \d+)\s*[—-]\s*(.+)$", clean_title)
    if m_dash:
        slot, rest = m_dash.group(1).strip(), m_dash.group(2).strip()
        return f"* **{slot}** **[{rest}]:**{meta_part}"

    m_colon = _SKILL_SLOT_PREFIX_RE.match(clean_title)
    if m_colon:
        role_lbl, skill_name = m_colon.group(1).strip(), m_colon.group(2).strip()
        if skill_name.lower().startswith(f"{role_lbl.lower()}:"):
            skill_name = skill_name[len(role_lbl) + 1:].strip()
        return f"* **{role_lbl}** **[{skill_name}]:**{meta_part}"

    clean_no_colon = clean_title.rstrip(":").strip()
    if clean_no_colon in ("Core Passive", "Additional Ability", "Profile & Base Stats"):
        return f"* **{clean_no_colon}:**{meta_part}"
    if clean_no_colon.startswith("Mindscape Cinema "):
        return f"* **{clean_no_colon}:**{meta_part}"
    return f"* **[{clean_no_colon}]:**{meta_part}"


def _format_beta_skill_entry(
    game_key: str,
    entry: Any,
    include_cat_header: bool,
    *,
    is_new: bool = True,
) -> str:
    parts: list[str] = []
    cat = strip_invisible(str(getattr(entry, "category", "") or "")).strip()
    if include_cat_header and cat:
        heading = _category_heading(game_key, cat, is_new=is_new)
        if heading:
            parts.append(heading)

    title = strip_invisible(str(getattr(entry, "title", "") or "")).strip()
    tag = strip_invisible(str(getattr(entry, "tag", "") or "[NEW]")).strip()
    meta = strip_invisible(str(getattr(entry, "meta", "") or "")).strip()
    parts.append(_format_skill_bullet_head(title, meta))

    params = [p for p in (getattr(entry, "params", ()) or ()) if isinstance(p, dict)]
    param_names_lower = {
        strip_invisible(str(p.get("name") or "")).strip().lower()
        for p in params if strip_invisible(str(p.get("name") or "")).strip()
    }

    cleaned_lines = [
        ln
        for raw_ln in (getattr(entry, "lines", ()) or ())
        if (ln := strip_invisible(str(raw_ln or "")).lstrip("> ").strip())
        and not _SKILL_ATTR_HEADER_RE.match(ln)
    ]

    rendered_lines: list[str] = []
    inline_stat_pairs: list[str] = []
    in_subhead = False

    def _flush_inline_stats() -> None:
        if not inline_stat_pairs:
            return
        for idx in range(0, len(inline_stat_pairs), 3):
            parts.append("    * " + " | ".join(inline_stat_pairs[idx:idx + 3]))
        inline_stat_pairs.clear()

    for line_idx, ln in enumerate(cleaned_lines):
        rendered_lines.append(ln)
        if ln.startswith("**("):
            _flush_inline_stats()
            parts.append(f"        * {_bold_beta_numbers(ln)}")
            continue
        if ln.startswith("-#"):
            _flush_inline_stats()
            parts.append(f"        * {ln[2:].strip()}")
            continue
        if ln.startswith("⚠️"):
            _flush_inline_stats()
            parts.append(f"        * {ln}")
            continue
        if ln.startswith("**["):
            _flush_inline_stats()
            in_subhead = False
            parts.append(f"    * {_bold_beta_numbers(ln)}")
            continue

        m_stat = re.match(r"^([^:]{2,96}):\s*([+-]?\d[^;\n]{0,64})$", ln)
        if m_stat and (
            m_stat.group(1).strip().lower() in param_names_lower
            or _STAT_ROW_KEYWORD_RE.search(m_stat.group(1))
        ):
            in_subhead = False
            lbl, val = m_stat.group(1).strip(), m_stat.group(2).strip()
            val_fmt = val if "**" in val else f"**{val}**"
            inline_stat_pairs.append(f"{lbl}: {val_fmt}")
            continue

        _flush_inline_stats()
        next_ln = cleaned_lines[line_idx + 1] if line_idx + 1 < len(cleaned_lines) else None
        if _is_skill_subheading(ln, next_ln, tag):
            in_subhead = True
            parts.append(f"    * **{ln}:**")
            continue

        pfx = f"**{tag}** " if tag else ""
        indent = "        * " if in_subhead else "    * "
        parts.append(f"{indent}{pfx}{_bold_beta_numbers(ln)}")

    _flush_inline_stats()

    missing: list[str] = []
    lower_lines = "\n".join(rendered_lines).lower()
    for param in params:
        name = strip_invisible(str(param.get("name") or "")).strip()
        value = strip_invisible(str(param.get("text") or "")).strip()
        if not name or not value:
            continue
        if name.lower() not in lower_lines or value.lower() not in lower_lines:
            missing.append(f"{name}: **{value}**")
    if missing:
        label = strip_invisible(str(getattr(entry, "values_label", "") or "")).strip()
        for idx in range(0, len(missing), 3):
            chunk = " | ".join(missing[idx:idx + 3])
            if idx == 0 and label:
                parts.append(f"    * *({label})* {chunk}")
            else:
                parts.append(f"    * {chunk}")
    return "\n".join(parts)


def _beta_entry_sources(item: Any) -> str:
    """Revision-diff link when present; full-kit URLs are mask-linked on the item's own title."""
    diff = safe_url(getattr(item, "diff_url", ""))
    return f"[Revision diff]({diff})" if diff else ""


def _normalize_continuation_indent(block: str) -> str:
    """Promote leading 4-space sub-bullets to top-level `* ` so Discord never renders code blocks."""
    lines = block.split("\n")
    out: list[str] = []
    promoting = True
    for ln in lines:
        if promoting:
            if ln.startswith("    * ") or ln.startswith("        * "):
                out.append(ln[4:])
                continue
            if ln.strip():
                promoting = False
        out.append(ln)
    return "\n".join(out)


def _is_header_only_line(ln: str) -> bool:
    s = ln.strip()
    if s.startswith("#"):
        return True
    return bool(s.startswith("* **") and s.endswith(":**"))


def _split_one_oversized_line(line: str, room: int) -> tuple[str, str]:
    """Split a single line longer than `room` cleanly at a word boundary after any bullet prefix."""
    m_bul = re.match(r"^(\s*\*\s+)", line)
    prefix_len = len(m_bul.group(1)) if m_bul and len(m_bul.group(1)) < room // 3 else (
        len(line) - len(line.lstrip(" "))
    )
    cont_prefix = m_bul.group(1) if m_bul and len(m_bul.group(1)) < room // 3 else ""
    min_cut = max(prefix_len + 1, room // 3)
    cut = line.rfind(" ", min_cut, room + 1)
    if cut < min_cut:
        cut = room
    head_part = line[:cut].rstrip(" ")
    tail_raw = line[cut:].lstrip(" ")
    if not tail_raw:
        return head_part, ""
    return head_part, cont_prefix + tail_raw


def _split_beta_once(block: str, max_chars: int) -> tuple[str, str]:
    """Split off one clean container-fitting prefix and return `(head, tail_remainder)`.

    Never strands an orphan heading at the end of `head`, avoids leaving 1-2 lonely
    stat/bullet lines in `tail_remainder` when balanced lines can move together, and
    never slices inside leading bullet indentation spaces.
    """
    if max_chars < 1:
        raise ValueError("beta text capacity must be positive")
    if len(block) <= max_chars:
        return block, ""

    lines = block.split("\n")
    if len(lines) == 1 or len(lines[0]) > max_chars:
        head_line, rest_first = _split_one_oversized_line(lines[0], max_chars)
        rest_lines = ([rest_first] if rest_first else []) + lines[1:]
        return head_line, _normalize_continuation_indent("\n".join(rest_lines))

    acc_len = 0
    k = 0
    for idx, ln in enumerate(lines):
        cand_len = acc_len + (1 if idx > 0 else 0) + len(ln)
        if cand_len > max_chars:
            break
        acc_len = cand_len
        k = idx + 1

    if k == 0:
        head_line, rest_first = _split_one_oversized_line(lines[0], max_chars)
        rest_lines = ([rest_first] if rest_first else []) + lines[1:]
        return head_line, _normalize_continuation_indent("\n".join(rest_lines))

    rem_room = max_chars - acc_len - 1
    if k < len(lines) and len(lines[k]) > max_chars and rem_room >= 200:
        h_part, r_part = _split_one_oversized_line(lines[k], rem_room)
        head_lines = lines[:k] + [h_part]
        tail_lines = ([r_part] if r_part else []) + lines[k + 1:]
        return "\n".join(head_lines), _normalize_continuation_indent("\n".join(tail_lines))

    head_lines = lines[:k]
    tail_lines = lines[k:]

    while len(head_lines) > 1 and (
        not head_lines[-1].strip() or _is_header_only_line(head_lines[-1])
    ):
        moved = head_lines.pop()
        if moved.strip():
            tail_lines.insert(0, moved)

    while (
        tail_lines
        and (len(tail_lines) <= 2 or sum(len(x) for x in tail_lines) < 220)
        and len(head_lines) >= 5
    ):
        candidate_move = head_lines[-1]
        if not candidate_move.strip() or _is_header_only_line(candidate_move):
            break
        if len(head_lines) >= 2 and _is_header_only_line(head_lines[-2]):
            break
        tail_lines.insert(0, head_lines.pop())

    while len(head_lines) > 1 and (
        not head_lines[-1].strip() or _is_header_only_line(head_lines[-1])
    ):
        moved = head_lines.pop()
        if moved.strip():
            tail_lines.insert(0, moved)

    return "\n".join(head_lines), _normalize_continuation_indent("\n".join(tail_lines))


def _split_beta_block(block: str, max_chars: int) -> list[str]:
    """Split an oversized block into clean sequential container chunks."""
    if max_chars < 1:
        raise ValueError("beta text capacity must be positive")
    chunks: list[str] = []
    rem = block
    while rem:
        if len(rem) <= max_chars:
            chunks.append(rem)
            break
        head, rem = _split_beta_once(rem, max_chars)
        chunks.append(head)
    return chunks


_ZZZ_SUBSTAT_DISPLAY = {
    "percent hp": "HP%",
    "percent atk": "ATK%",
    "percent def": "DEF%",
}


def _format_equipment_stats_line(
    game_key: str,
    stats_line: str,
    sub_type: str,
    weapon_class: str,
) -> list[str]:
    out: list[str] = []
    if game_key == "starrail" and weapon_class:
        p_lbl = f"The {weapon_class}" if weapon_class in _HSR_STANDARD_PATHS else weapon_class
        out.append(f"**Path:** {p_lbl}")
    clean = strip_invisible(stats_line).lstrip("> ").strip()
    sub_disp = _ZZZ_SUBSTAT_DISPLAY.get(sub_type.strip().lower(), sub_type.strip())
    is_redundant_wclass = bool(
        sub_disp and weapon_class and sub_disp.lower() == weapon_class.strip().lower()
    )
    if clean:
        m_lv = re.match(r"^Lv\.\s*\d+\s*[—-]\s*(.+)$", clean)
        raw_body = m_lv.group(1) if m_lv else clean
        segs = [s.strip() for s in raw_body.split("·") if s.strip()]
        fmt_segs: list[str] = []
        for seg in segs:
            sm = re.match(
                r"^(Base ATK|Lv\.\s*1 Base ATK|HP|ATK|DEF|CRIT Rate|CRIT DMG|"
                r"Energy Regen|Energy Recharge|Elemental Mastery)\s+(.+)$",
                seg,
                re.I,
            )
            if sm:
                lbl = sm.group(1)
                if game_key == "starrail" and lbl.upper() in ("HP", "ATK", "DEF"):
                    lbl = f"Base {lbl.upper()}"
                fmt_segs.append(f"**{lbl}:** {sm.group(2).strip()}")
            else:
                fmt_segs.append(seg)
        if sub_disp and not is_redundant_wclass and sub_disp.lower().rstrip("%") not in clean.lower():
            fmt_segs.append(f"**Substat:** {sub_disp}")
        out.append(" | ".join(fmt_segs))
    elif sub_disp and not is_redundant_wclass:
        out.append(f"**Substat:** {sub_disp}")
    return out


def _equipment_type_badge(
    game_key: str,
    eq_cat: str,
    eq_rarity: int | None,
    weapon_class: str,
    sub_type: str,
) -> str:
    rar = _format_beta_rarity(game_key, eq_rarity, is_equipment=True)
    cat_low = eq_cat.lower()
    if "light cone" in cat_low:
        return f"{rar} Light Cone".strip()
    if "w-engine" in cat_low:
        base = f"{rar} W-Engine".strip()
        return f"{base} — {weapon_class}" if weapon_class else base
    if "weapon" in cat_low:
        cls = weapon_class or sub_type or "Weapon"
        return f"{rar} {cls}".strip()
    return rar


def beta_payloads_with_map(
    game: Game,
    patch: Any,
    settings: Settings | None = None,
    ping: Ping | None = None,
    *,
    silent: bool = False,
) -> tuple[list[dict], dict[str, int]]:
    """Build Discord Components V2 payloads for a beta patch or revision diff.

    Automatically splits long character kits and equipment lists across sequential
    containers/pages so every payload strictly satisfies Discord's 4,000-character and
    40-component limits. Returns `(payloads, char_msg_map)` where `char_msg_map` maps
    each character ID to the 0-based index of its primary container payload.
    """
    ping_obj = ping or Ping()
    beta_ver = strip_invisible(str(getattr(patch, "beta_version", "") or "")).strip() or "?"
    rev_label = strip_invisible(str(getattr(patch, "rev_label", "") or "")).strip() or "Update"
    res_line = BETA_RESOURCE_LINKS.get(game.key, BETA_RESOURCE_LINKS["genshin"])

    cycle_ver = _beta_cycle_label(beta_ver)
    rev_short = _short_rev_label(rev_label, beta_ver)
    header_lines = [
        f"## [{game.name} - {cycle_ver} Beta] Patch Notes: {rev_short}",
        res_line,
    ]
    if ping_obj and not silent and ping_obj.text:
        header_lines.append(f"- {ping_obj.text}")
    patch_header_str = "\n".join(header_lines)

    characters = list(getattr(patch, "characters", ()) or ())
    equipment = list(getattr(patch, "equipment", ()) or ())

    footer_reserve = 140
    raw_containers: list[list[dict]] = []
    char_msg_map: dict[str, int] = {}
    placed_patch_header = False

    for char in characters:
        char_id = str(getattr(char, "id", "") or getattr(char, "name", ""))
        char_name = strip_invisible(str(getattr(char, "name", "") or "Unknown")).strip()
        rarity = int(getattr(char, "rarity", 5) or 5)
        element = strip_invisible(str(getattr(char, "element", "") or "")).strip()
        role = strip_invisible(str(getattr(char, "role", "") or "")).strip()
        faction = strip_invisible(str(getattr(char, "faction", "") or "")).strip()
        attack_type = strip_invisible(str(getattr(char, "attack_type", "") or "")).strip()
        overview = strip_invisible(str(getattr(char, "overview", "") or "")).strip()
        subtext = strip_invisible(str(getattr(char, "subtext", "") or "")).strip()
        is_new_char = bool(getattr(char, "is_new", True))
        stats_lines = [
            strip_invisible(str(s)).strip()
            for s in (getattr(char, "stats_lines", ()) or ())
            if str(s).strip()
        ]

        char_title = _beta_masked_title(char_name, getattr(char, "page_url", None))
        intro_parts = [f"## {char_title}"]
        rarity_str = _format_beta_rarity(game.key, rarity)
        meta_bits: list[str] = []
        if game.key == "zzz":
            if element:
                meta_bits.append(f"**Attribute:** {element}")
            if role:
                meta_bits.append(f"**Specialty:** {role}")
            if rarity_str:
                meta_bits.append(f"**Rarity:** {rarity_str}")
            if attack_type:
                meta_bits.append(f"**Attack Type:** {attack_type}")
        elif game.key == "starrail":
            if element:
                meta_bits.append(f"**Element:** {element}")
            if role:
                path_lbl = f"The {role}" if role in _HSR_STANDARD_PATHS else role
                meta_bits.append(f"**Path:** {path_lbl}")
            if rarity_str:
                meta_bits.append(f"**Rarity:** {rarity_str}")
        elif game.key == "wuwa":
            if element:
                meta_bits.append(f"**Attribute:** {element}")
            if role:
                meta_bits.append(f"**Weapon:** {role}")
            if rarity_str:
                meta_bits.append(f"**Rarity:** {rarity_str}")
        else:
            if element:
                meta_bits.append(f"**Element:** {element}")
            if role:
                meta_bits.append(f"**Weapon:** {role}")
            if rarity_str:
                meta_bits.append(f"**Rarity:** {rarity_str}")
        if meta_bits:
            intro_parts.append(" | ".join(meta_bits))
        if faction:
            lbl = "Faction" if game.key == "zzz" else "Affiliation"
            intro_parts.append(f"**{lbl}:** {faction}")

        intro_parts.extend(_format_beta_stats_block(game.key, stats_lines))
        char_diff = safe_url(getattr(char, "diff_url", ""))
        clean_sub = subtext[2:].strip() if subtext.startswith("-#") else subtext
        if clean_sub:
            if char_diff and "Revision diff" in clean_sub:
                clean_sub = clean_sub.replace(
                    "Revision diff", f"[Revision diff]({char_diff})", 1
                )
                char_diff = None
            intro_parts.append(f"-# {clean_sub}")
        if char_diff:
            intro_parts.append(f"-# [Revision diff]({char_diff})")
        if overview:
            intro_parts.append("")
            intro_parts.append(overview)
        intro_str = "\n".join(intro_parts)

        eidolon_imgs = [
            u for u in (safe_url(x) for x in (getattr(char, "eidolon_images", None) or ())) if u
        ][:10]

        stream: list[tuple[str, Any]] = [("text", intro_str)]
        last_cat = None
        placed_eidolon_gallery = False
        for entry in getattr(char, "sections", ()) or ():
            cat = strip_invisible(str(getattr(entry, "category", "") or "")).strip()
            include_cat = bool(cat and cat != last_cat)
            if cat:
                last_cat = cat
            if include_cat and cat == "Eidolons" and eidolon_imgs and not placed_eidolon_gallery:
                placed_eidolon_gallery = True
                stream.append((
                    "section_gallery",
                    (_category_heading(game.key, cat, is_new=is_new_char), eidolon_imgs),
                ))
                stream.append((
                    "text",
                    _format_beta_skill_entry(game.key, entry, False, is_new=is_new_char),
                ))
            else:
                stream.append((
                    "text",
                    _format_beta_skill_entry(game.key, entry, include_cat, is_new=is_new_char),
                ))

        total_bonuses = strip_invisible(str(getattr(char, "total_stat_bonuses", "") or "")).strip()
        if total_bonuses:
            stream.append(("text", f"**Total Stat Bonuses:** {total_bonuses}"))

        mech_defs = list(getattr(char, "mechanic_defs", ()) or ())
        if mech_defs:
            m_lines = ["### Mechanic Definitions"]
            for m_name, m_desc in mech_defs:
                c_name = strip_invisible(str(m_name)).strip()
                c_desc = strip_invisible(str(m_desc)).strip()
                m_lines.append(f"* **({c_name})** {c_desc}")
            stream.append(("text", "\n".join(m_lines)))

        fresh_max_body = MAX_TEXT_TOTAL - footer_reserve - 24
        first_container_for_char = True
        idx_stream = 0
        carry_text: list[str] = []

        while first_container_for_char or idx_stream < len(stream) or carry_text:
            kids: list[dict] = []
            used_chars = footer_reserve
            if not placed_patch_header:
                kids.append(text(patch_header_str))
                used_chars += len(patch_header_str)
                placed_patch_header = True

            if first_container_for_char:
                char_msg_map[char_id] = len(raw_containers)
                img_url = safe_url(getattr(char, "image_url", None))
                if img_url:
                    kids.append(gallery([img_url]))
                first_container_for_char = False

            cur_body = ""
            while carry_text or idx_stream < len(stream):
                max_body = MAX_TEXT_TOTAL - used_chars - 24
                if carry_text:
                    nxt = carry_text[0]
                    cand = f"{cur_body}\n\n{nxt}" if cur_body else nxt
                    if len(cand) <= max_body:
                        cur_body = cand
                        carry_text.pop(0)
                        continue
                    if not cur_body:
                        head, rem = _split_beta_once(nxt, max_body)
                        cur_body = head
                        carry_text = ([rem] if rem else []) + carry_text[1:]
                    break

                kind, payload_val = stream[idx_stream]
                if kind == "section_gallery":
                    heading_str, g_urls = payload_val
                    next_txt = stream[idx_stream + 1][1] if (
                        idx_stream + 1 < len(stream) and stream[idx_stream + 1][0] == "text"
                    ) else ""
                    body_len = len(cur_body) + 2 if cur_body else 0
                    first_entry_need = (
                        len(next_txt)
                        if len(heading_str) + len(next_txt) <= fresh_max_body
                        else min(len(next_txt), 600)
                    )
                    needed_chars = len(heading_str) + first_entry_need + body_len
                    add_comps = (1 if cur_body else 0) + 2
                    if (cur_body or len(kids) > 1) and (
                        needed_chars > max_body
                        or count_components(kids) + add_comps > MAX_COMPONENTS - 4
                    ):
                        break
                    if cur_body:
                        kids.append(text(cur_body))
                        used_chars += len(cur_body)
                        cur_body = ""
                    kids.append(text(heading_str))
                    used_chars += len(heading_str)
                    kids.append(gallery(g_urls))
                    idx_stream += 1
                    continue

                nxt = str(payload_val)
                cand = f"{cur_body}\n\n{nxt}" if cur_body else nxt
                if len(cand) <= max_body:
                    cur_body = cand
                    idx_stream += 1
                elif not cur_body:
                    # Single block exceeds this container even when empty: split cleanly once.
                    head, rem = _split_beta_once(nxt, max_body)
                    cur_body = head
                    carry_text = [rem] if rem else []
                    idx_stream += 1
                    break
                elif len(nxt) <= fresh_max_body:
                    # Smart container fit: keep the entire skill/section block intact on the
                    # next container instead of stranding trailing stat or prose lines.
                    break
                elif max_body - len(cur_body) - 2 >= 900:
                    room = max_body - len(cur_body) - 2
                    head, rem = _split_beta_once(nxt, room)
                    cur_body = f"{cur_body}\n\n{head}"
                    carry_text = [rem] if rem else []
                    idx_stream += 1
                    break
                else:
                    break

            if cur_body:
                kids.append(text(cur_body))
            if kids:
                raw_containers.append(kids)

    if equipment:
        kids = []
        used_chars = footer_reserve
        if not placed_patch_header:
            kids.append(text(patch_header_str))
            used_chars += len(patch_header_str)
            placed_patch_header = True

        def _eq_display_cat(item: Any) -> str:
            c_lbl = strip_invisible(str(getattr(item, "category_label", "") or "Equipment")).strip()
            s_kind = strip_invisible(str(getattr(item, "set_kind", "") or "")).strip()
            if c_lbl == "Relics & Ornaments" and "planar ornament" in s_kind.lower():
                return "Planar Ornaments"
            return c_lbl

        cat_tags: dict[str, list[str]] = {}
        for eq in equipment:
            d_lbl = _eq_display_cat(eq)
            t_lbl = strip_invisible(str(getattr(eq, "tag", "") or "[NEW]")).strip()
            cat_tags.setdefault(d_lbl, []).append(t_lbl)

        last_eq_cat = None
        for eq in equipment:
            eq_set_kind = strip_invisible(str(getattr(eq, "set_kind", "") or "")).strip()
            display_cat = _eq_display_cat(eq)
            eq_name = strip_invisible(str(getattr(eq, "name", "") or "Unknown")).strip()
            eq_rarity = getattr(eq, "rarity", None)
            eq_sub = strip_invisible(str(getattr(eq, "sub_type", "") or "")).strip()
            eq_wclass = strip_invisible(str(getattr(eq, "weapon_class", "") or "")).strip()
            eq_parts_line = strip_invisible(str(getattr(eq, "set_parts", "") or "")).strip()
            eq_tag = strip_invisible(str(getattr(eq, "tag", "") or "[NEW]")).strip()
            eq_stats = strip_invisible(str(getattr(eq, "stats_line", "") or "")).strip()
            eq_eff_title = strip_invisible(str(getattr(eq, "effect_title", "") or "")).strip()
            eq_lines = [
                strip_invisible(str(x)).lstrip("> ").strip()
                for x in (getattr(eq, "lines", ()) or ())
                if str(x).strip()
            ]
            eq_glossary = list(getattr(eq, "glossary", ()) or ())
            raw_urls = list(getattr(eq, "image_urls", None) or ())
            eq_imgs = [u for u in (safe_url(x) for x in raw_urls) if u]
            if not eq_imgs:
                single_eq_img = safe_url(getattr(eq, "image_url", None))
                if single_eq_img:
                    eq_imgs = [single_eq_img]
            eq_imgs = eq_imgs[:10]

            include_cat = display_cat != last_eq_cat
            last_eq_cat = display_cat

            cat_header_str = ""
            if include_cat and display_cat:
                all_tags = cat_tags.get(display_cat, [])
                is_diff_cat = (
                    bool(all_tags)
                    and all(t != "[NEW]" for t in all_tags)
                    and not display_cat.lower().endswith("changes")
                )
                cat_header_str = f"## {display_cat} Changes" if is_diff_cat else f"## {display_cat}"

            badge = _equipment_type_badge(game.key, display_cat, eq_rarity, eq_wclass, eq_sub)
            eq_title = _beta_masked_title(eq_name, getattr(eq, "page_url", None))
            title_line = f"### {eq_title} ({badge})" if badge else f"### {eq_title}"

            eq_parts: list[str] = [title_line]
            if eq_set_kind:
                eq_parts.append(f"*{eq_set_kind}*")
            if eq_parts_line:
                eq_parts.append(f"*Parts: {eq_parts_line}*")
            eq_parts.extend(_format_equipment_stats_line(game.key, eq_stats, eq_sub, eq_wclass))
            if eq_eff_title:
                eq_parts.append("")
                ref_note = " *(S1 values)*" if game.key == "starrail" and eq_tag == "[NEW]" else ""
                eq_parts.append(f"**[{eq_eff_title}]{ref_note}:**")
            elif eq_lines:
                eq_parts.append("")

            has_eq_bullet = False
            for eln in eq_lines:
                if eln.startswith("-#"):
                    note_txt = eln[2:].strip()
                    if has_eq_bullet:
                        eq_parts.append(f"    * {note_txt}")
                    else:
                        eq_parts.append(f"-# {note_txt}")
                elif eln.startswith("**["):
                    has_eq_bullet = True
                    eq_parts.append(f"* {_bold_beta_numbers(eln)}")
                else:
                    has_eq_bullet = True
                    m_pc = re.match(r"^(\d+-Piece(?:\s+Set)?):\s*(.+)$", eln, re.I)
                    if m_pc and eq_tag == "[NEW]":
                        eq_parts.append(f"*{m_pc.group(1)}:*")
                        eq_parts.append(f"* **{eq_tag}** {_bold_beta_numbers(m_pc.group(2).strip())}")
                    else:
                        pfx = f"**{eq_tag}** " if eq_tag else ""
                        eq_parts.append(f"* {pfx}{_bold_beta_numbers(eln)}")
            for g_name, g_desc in eq_glossary:
                c_gn = strip_invisible(str(g_name)).strip()
                c_gd = _bold_beta_numbers(strip_invisible(str(g_desc)).strip())
                eq_parts.append(f"    * **({c_gn})** {c_gd}")

            eq_sources = _beta_entry_sources(eq)
            if eq_sources:
                eq_parts.append(f"-# {eq_sources}")
            eq_text_str = "\n".join(eq_parts)

            hdr_reserve = len(cat_header_str) + 4 if cat_header_str else 0
            max_eq_chars = MAX_TEXT_TOTAL - footer_reserve - 24 - hdr_reserve
            first_room = MAX_TEXT_TOTAL - used_chars - 24 - hdr_reserve
            if (
                len(kids) == 1
                and kids[0].get("content") == patch_header_str
                and len(eq_text_str) > max_eq_chars
                and first_room >= 600
            ):
                head_eq, rem_eq = _split_beta_once(eq_text_str, first_room)
                pieces = [head_eq] + (_split_beta_block(rem_eq, max_eq_chars) if rem_eq else [])
            else:
                pieces = _split_beta_block(eq_text_str, max_eq_chars)
            for piece_idx, piece in enumerate(pieces):
                piece_images = eq_imgs if piece_idx == 0 else []
                hdr = cat_header_str if piece_idx == 0 and cat_header_str else ""
                extra_len = len(piece) + (len(hdr) + 2 if hdr else 0)
                add_comps = (1 if hdr else 0) + (1 if piece_images else 0) + 1
                if kids and (
                    used_chars + extra_len + 4 > MAX_TEXT_TOTAL - 16
                    or count_components(kids) + add_comps > MAX_COMPONENTS - 4
                ):
                    raw_containers.append(kids)
                    kids = []
                    used_chars = footer_reserve
                if hdr:
                    if (
                        kids
                        and kids[-1].get("type") == 10
                        and kids[-1].get("content") != patch_header_str
                    ):
                        kids[-1] = text(kids[-1]["content"] + "\n\n" + hdr)
                        used_chars += 2 + len(hdr)
                    else:
                        kids.append(text(hdr))
                        used_chars += len(hdr)
                if piece_images:
                    kids.append(gallery(piece_images))
                if (
                    not piece_images
                    and kids
                    and kids[-1].get("type") == 10
                    and kids[-1].get("content") != patch_header_str
                ):
                    kids[-1] = text(kids[-1]["content"] + "\n\n" + piece)
                    used_chars += 2 + len(piece)
                else:
                    kids.append(text(piece))
                    used_chars += len(piece)

        if kids:
            raw_containers.append(kids)

    if not raw_containers:
        empty_note = "\n\nNo new character or equipment kit changes in this revision."
        raw_containers.append([text(patch_header_str + empty_note)])

    btn_label = "Citlali News"
    btn_url = NEWS_COMMUNITY
    btn_emoji: dict | None = BETA_FOOTER_EMOJI
    if settings and settings.community_buttons:
        b0 = settings.community_buttons[0]
        btn_label = b0.get("label") or btn_label
        btn_url = b0.get("url") or btn_url
        btn_emoji = _emoji_of(b0.get("emoji")) or btn_emoji
    footer_btn = link_button(btn_label, btn_url, btn_emoji)

    total_pages = len(raw_containers)
    payloads: list[dict] = []
    for idx, kids in enumerate(raw_containers, start=1):
        foot_str = f"-# {game.name} {beta_ver} Beta • Page {idx}/{total_pages}"
        container_kids = list(kids)
        if footer_btn:
            container_kids.append(section([foot_str], footer_btn))
        else:
            container_kids.append(text(foot_str))
        p = {
            "flags": IS_COMPONENTS_V2,
            "components": [container(container_kids, None)],
            "allowed_mentions": (
                ping_obj.allowed_mentions if (idx == 1 and not silent) else {"parse": []}
            ),
        }
        problems = validate_payload(p)
        if problems:
            raise ValueError(
                f"beta payload page {idx}/{total_pages} failed validation: {'; '.join(problems)}"
            )
        payloads.append(p)

    return payloads, char_msg_map


def beta_payloads(
    game: Game,
    patch: Any,
    settings: Settings | None = None,
    ping: Ping | None = None,
    *,
    silent: bool = False,
) -> list[dict]:
    return beta_payloads_with_map(game, patch, settings, ping, silent=silent)[0]
