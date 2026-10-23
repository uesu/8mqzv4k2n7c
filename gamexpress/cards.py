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


def names(values: list[str] | None, disp: dict[str, str] | None = None) -> str:
    vals = [v for v in (values or []) if v and v.strip()]
    if disp and vals:
        vals = [disp.get(v, v) for v in vals]     # the wiki's full display name, when it has one
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
    lines = [heading, ""]
    # The early tier: the wiki knows WHO debuts in this version but no phase split exists yet
    # (the livestream has not aired). The line disappears by itself the moment real phase data
    # lands, because `confirmed` is not part of the banner key set.
    if b.get("confirmed") and not b.get("phase1"):
        lines.append(f"※ Confirmed: {names(b.get('confirmed'), disp)}")
    lines.append(f"※ Re-runs: {names(b.get('reruns'), disp)}")
    # The 4★ summary is the pre-notice signal (the wiki knows the full roster before any phase
    # notice exists, and often names phase 2's 4★s early); once BOTH per-phase lists are in,
    # the summary is just their union — noise — and hides on the same silent edit that filled
    # the second phase. Symmetric with the `※ Confirmed:` rule above.
    if game.card.four_star_summary and not (b.get("phase1_4") and b.get("phase2_4")):
        lines.append(f"※ 4 Star Characters: {names(b.get('four_star'), disp)}")
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
