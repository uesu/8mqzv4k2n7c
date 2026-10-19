"""Render Components V2 payloads as a Discord-like HTML page (dark theme).

  python -m gamexpress preview            -> previews/index.html (+ one JSON per card)

It is an APPROXIMATION of Discord's renderer, good enough to judge layout, wording,
buttons and timestamps before posting: containers with the accent bar, sections with a
thumbnail, galleries, separators, link buttons (with your custom emojis), role mentions,
`-#` subtext, headings, lists, inline code and Discord timestamps — which, like Discord,
are formatted in the VIEWER's timezone by a few lines of JavaScript.
"""

from __future__ import annotations

import html
import json
import re

LINK_ICON = ('<svg class="ext" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" '
             'd="M15 2a1 1 0 0 0 0 2h3.59l-8.3 8.3a1 1 0 1 0 1.42 1.4L20 5.42V9a1 1 0 1 0 2 0V3a1 1 0 0 0-1-1h-6Z"/>'
             '<path fill="currentColor" d="M5 5a1 1 0 0 0-1 1v13a1 1 0 0 0 1 1h13a1 1 0 0 0 1-1v-6a1 1 0 1 1 2 0v6a3 3 0 0 1-3 '
             '3H5a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3h6a1 1 0 1 1 0 2H5Z"/></svg>')


def _emoji_img(eid: str, name: str, animated: bool, cls: str = "emoji") -> str:
    ext = "gif" if animated else "webp"
    return (f'<img class="{cls}" src="https://cdn.discordapp.com/emojis/{eid}.{ext}?size=48&quality=lossless" '
            f'alt=":{html.escape(name)}:" title=":{html.escape(name)}:">')


def _emoji(e: dict | None) -> str:
    if not e:
        return ""
    if e.get("id"):
        return _emoji_img(str(e["id"]), e.get("name") or "emoji", bool(e.get("animated")))
    return f'<span class="uemoji">{html.escape(e.get("name") or "")}</span>'


# ------------------------------------------------------------------ inline markdown
def inline(text: str) -> str:
    """Discord inline markdown -> HTML (escaped). Placeholders keep code/links intact."""
    keep: list[str] = []

    def stash(fragment: str) -> str:
        keep.append(fragment)
        return f"\x00{len(keep) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
                  lambda m: stash(f'<a href="{html.escape(m.group(2))}" target="_blank" rel="noopener">'
                                  f'{inline(m.group(1))}</a>'), text)
    text = re.sub(r"<t:(-?\d+)(?::([tTdDfFR]))?>",
                  lambda m: stash(f'<span class="ts" data-ts="{m.group(1)}" data-f="{m.group(2) or "f"}">'
                                  f'&lt;t:{m.group(1)}&gt;</span>'), text)
    text = re.sub(r"<@&(\d+)>", lambda m: stash(f'<span class="mention" title="role {m.group(1)}">@ping-role</span>'),
                  text)
    text = re.sub(r"@(everyone|here)\b", lambda m: stash(f'<span class="mention">@{m.group(1)}</span>'), text)
    text = re.sub(r"<(a?):(\w{2,32}):(\d{15,25})>",
                  lambda m: stash(_emoji_img(m.group(3), m.group(2), m.group(1) == "a")), text)
    text = re.sub(r"(?<![\"'=])(https?://[^\s<>()]+)",
                  lambda m: stash(f'<a href="{html.escape(m.group(1))}" target="_blank" rel="noopener">'
                                  f'{html.escape(m.group(1))}</a>'), text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<em>\1</em>", text)
    for _ in range(3):   # nested placeholders (links containing code etc.)
        text = re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)
    return text


def markdown(content: str) -> str:
    """Block-level: # / ## / ### headings, -# subtext, - lists, > quotes, paragraphs."""
    out: list[str] = []
    in_list = False
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<div class=\"p\">" + "<br>".join(para) + "</div>")
            para.clear()

    for line in (content or "").split("\n"):
        m_h = re.match(r"^(#{1,3})\s+(.*)$", line)
        m_sub = re.match(r"^-#\s+(.*)$", line)
        m_li = re.match(r"^\s*[-*]\s+(.*)$", line) if not m_sub else None
        m_q = re.match(r"^>\s?(.*)$", line)
        if m_li:
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(m_li.group(1))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if m_h:
            flush_para()
            level = len(m_h.group(1))
            out.append(f"<h{level}>{inline(m_h.group(2))}</h{level}>")
        elif m_sub:
            flush_para()
            out.append(f'<div class="sub">{inline(m_sub.group(1))}</div>')
        elif m_q:
            flush_para()
            out.append(f"<blockquote>{inline(m_q.group(1))}</blockquote>")
        elif line.strip() == "":
            flush_para()
            out.append('<div class="gap"></div>')
        else:
            para.append(inline(line))
    flush_para()
    if in_list:
        out.append("</ul>")
    return "".join(out)


# ------------------------------------------------------------------ components
def _button(b: dict) -> str:
    label = html.escape(b.get("label") or "")
    emoji = _emoji(b.get("emoji"))
    disabled = " disabled" if b.get("disabled") else ""
    if b.get("style") == 5 and b.get("url"):
        return (f'<a class="btn{disabled}" href="{html.escape(b["url"])}" target="_blank" rel="noopener">'
                f'{emoji}<span>{label}</span>{LINK_ICON}</a>')
    return f'<span class="btn{disabled}">{emoji}<span>{label}</span></span>'


def component(c: dict) -> str:
    t = c.get("type")
    if t == 10:
        return f'<div class="td">{markdown(c.get("content") or "")}</div>'
    if t == 14:
        cls = "sep" + (" line" if c.get("divider", True) else "") + (" big" if c.get("spacing") == 2 else "")
        return f'<div class="{cls}"></div>'
    if t == 12:
        items = c.get("items") or []
        imgs = "".join(f'<img src="{html.escape((i.get("media") or {}).get("url") or "")}" alt="image" loading="lazy">'
                       for i in items)
        return f'<div class="gallery n{min(len(items), 4)}">{imgs}</div>'
    if t == 11:
        return f'<img class="thumb" src="{html.escape((c.get("media") or {}).get("url") or "")}" alt="thumbnail">'
    if t == 9:
        texts = "".join(component(x) for x in c.get("components") or [])
        acc = c.get("accessory") or {}
        side = component(acc) if acc.get("type") == 11 else (_button(acc) if acc.get("type") == 2 else "")
        return f'<div class="section"><div class="stexts">{texts}</div><div class="acc">{side}</div></div>'
    if t == 1:
        return '<div class="row">' + "".join(_button(b) for b in c.get("components") or []) + "</div>"
    if t == 2:
        return _button(c)
    if t == 17:
        color = c.get("accent_color")
        accent = f"#{int(color):06x}" if color is not None else "transparent"
        inner = "".join(component(x) for x in c.get("components") or [])
        return f'<div class="container" style="--accent:{accent}">{inner}</div>'
    return f'<div class="unknown">[component type {t}]</div>'


def render_payload(payload: dict) -> str:
    return "".join(component(c) for c in payload.get("components") or [])


CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#313338;color:#dbdee1;font:16px/1.375 "gg sans","Noto Sans","Helvetica Neue",Helvetica,Arial,sans-serif}
header.top{padding:18px 24px;border-bottom:1px solid #26272b;background:#2b2d31}
header.top h1{margin:0 0 4px;font-size:20px;color:#f2f3f5}
header.top p{margin:0;color:#b5bac1;font-size:14px}
nav{padding:10px 24px;font-size:14px;color:#b5bac1}
nav a{margin-right:14px}
h2.group{margin:26px 24px 6px;font-size:13px;letter-spacing:.02em;text-transform:uppercase;color:#949ba4}
.msg{display:flex;gap:16px;padding:10px 24px 12px 16px;max-width:980px}
.msg:hover{background:#2e3035}
.avatar{width:40px;height:40px;border-radius:50%;flex:none;background:linear-gradient(135deg,#e3b341,#c55a8a);display:flex;
  align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:15px}
.body{min-width:0;flex:1}
.head{margin-bottom:4px}
.head .name{color:#f2f3f5;font-weight:600}
.app{background:#5865f2;color:#fff;font-size:10.5px;border-radius:4px;padding:1px 5px;margin-left:5px;font-weight:600;vertical-align:2px}
.time{color:#949ba4;font-size:12px;margin-left:6px}
.label{font-size:12px;color:#949ba4;margin:0 0 6px}
.td{margin:0}
.td+.td,.td+.row,.row+.td,.section+.td,.td+.section,.gallery+.row,.row+.row,.gallery+.td,.td+.gallery,.section+.row{margin-top:8px}
.container{background:#2b2d31;border:1px solid #3a3c42;border-left:4px solid var(--accent);border-radius:8px;padding:14px 16px;
  max-width:640px;margin-top:4px}
.container>.td:first-child{margin-top:0}
h1,h2,h3{color:#f2f3f5;margin:4px 0 2px;line-height:1.25}
h1{font-size:24px}h2{font-size:20px}h3{font-size:16px}
.p{white-space:normal}
.sub{font-size:13px;color:#949ba4;line-height:1.3}
.gap{height:.6em}
ul{margin:2px 0;padding-left:22px}
blockquote{margin:2px 0;padding-left:10px;border-left:4px solid #4e5058}
code{font-family:Consolas,"Andale Mono WT","Andale Mono","Liberation Mono",Menlo,monospace;font-size:85%;background:#1e1f22;
  border:1px solid #1a1b1e;border-radius:4px;padding:.1em .3em}
a{color:#00a8fc;text-decoration:none}a:hover{text-decoration:underline}
.mention{background:rgba(88,101,242,.3);color:#c9cdfb;border-radius:3px;padding:0 2px;font-weight:500}
.ts{background:rgba(255,255,255,.07);border-radius:3px;padding:0 2px}
img.emoji{width:1.375em;height:1.375em;vertical-align:-.3em;object-fit:contain}
.uemoji{font-size:1.1em}
.sep{height:8px}.sep.big{height:16px}
.sep.line{height:17px;position:relative}.sep.line.big{height:33px}
.sep.line:after{content:"";position:absolute;left:0;right:0;top:50%;border-top:1px solid #3f4147}
.section{display:flex;gap:12px;align-items:flex-start}
.stexts{flex:1;min-width:0}
.thumb{width:85px;height:85px;border-radius:8px;object-fit:cover;background:#1e1f22}
.gallery{display:grid;gap:4px;margin-top:8px}
.gallery.n1{grid-template-columns:1fr}.gallery.n2,.gallery.n4{grid-template-columns:1fr 1fr}.gallery.n3{grid-template-columns:1fr 1fr 1fr}
.gallery img{width:100%;max-height:360px;object-fit:cover;border-radius:8px;background:#1e1f22;min-height:120px}
.row{display:flex;flex-wrap:wrap;gap:8px}
.btn{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 14px;border-radius:8px;background:#4e5058;color:#fff;
  font-size:14px;font-weight:500;white-space:nowrap}
.btn:hover{background:#6d6f78;text-decoration:none}
.btn img.emoji{width:20px;height:20px;vertical-align:middle}
.btn .ext{opacity:.9;margin-left:2px}
.btn.disabled{opacity:.5;pointer-events:none}
details{margin:6px 0 0;font-size:12px;color:#949ba4}
details pre{white-space:pre-wrap;word-break:break-all;background:#1e1f22;padding:8px;border-radius:6px;max-height:300px;overflow:auto}
footer{padding:24px;color:#949ba4;font-size:12px}
"""

JS = r"""
(function(){
  const O={t:{hour:'numeric',minute:'2-digit'},T:{hour:'numeric',minute:'2-digit',second:'2-digit'},
    d:{year:'numeric',month:'2-digit',day:'2-digit'},D:{year:'numeric',month:'long',day:'numeric'},
    f:{year:'numeric',month:'long',day:'numeric',hour:'numeric',minute:'2-digit'},
    F:{weekday:'long',year:'numeric',month:'long',day:'numeric',hour:'numeric',minute:'2-digit'}};
  const rtf=new Intl.RelativeTimeFormat('en',{numeric:'auto'});
  function rel(ts){const s=ts-Date.now()/1000,a=Math.abs(s);
    const u=a<60?['second',1]:a<3600?['minute',60]:a<86400?['hour',3600]:a<2592000?['day',86400]:a<31536000?['month',2592000]:['year',31536000];
    return rtf.format(Math.round(s/u[1]),u[0]);}
  document.querySelectorAll('.ts').forEach(function(el){
    const ts=+el.dataset.ts,f=el.dataset.f||'f';
    el.textContent=f==='R'?rel(ts):new Intl.DateTimeFormat('en-US',O[f]||O.f).format(new Date(ts*1000));
    el.title=new Intl.DateTimeFormat('en-US',O.F).format(new Date(ts*1000));
  });
  const now=new Date();
  document.querySelectorAll('.time').forEach(function(el){
    el.textContent='Today at '+new Intl.DateTimeFormat('en-US',O.t).format(now);});
  document.getElementById('tz').textContent=Intl.DateTimeFormat().resolvedOptions().timeZone||'your timezone';
})();
"""


def render_page(cards: list[tuple[str, str, dict]], title: str = "Game-Express — card preview") -> str:
    """cards: [(group, label, payload)] -> full HTML document."""
    groups: dict[str, list[tuple[str, dict]]] = {}
    for group, label, payload in cards:
        groups.setdefault(group, []).append((label, payload))
    nav = " ".join(f'<a href="#g-{html.escape(g)}">{html.escape(g)}</a>' for g in groups)
    body = []
    for group, items in groups.items():
        body.append(f'<h2 class="group" id="g-{html.escape(group)}">{html.escape(group)}</h2>')
        for label, payload in items:
            pj = html.escape(json.dumps(payload, ensure_ascii=False, indent=2))
            body.append(
                f'<div class="msg" id="{html.escape(label)}"><div class="avatar">GE</div><div class="body">'
                f'<div class="head"><span class="name">Game-Express</span><span class="app">APP</span>'
                f'<span class="time">Today</span></div>'
                f'<div class="label">{html.escape(label)}</div>'
                f'{render_payload(payload)}'
                f'<details><summary>payload JSON (paste into discohook.app)</summary><pre>{pj}</pre></details>'
                f'</div></div>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f'<style>{CSS}</style></head><body>'
            f'<header class="top"><h1>{html.escape(title)}</h1><p>Discord-like approximation of the Components V2 '
            f'cards. Timestamps show in <b id="tz">your timezone</b>, exactly like Discord does for every reader. '
            f'Images and custom emojis load from their public CDNs.</p></header><nav>{nav}</nav>'
            f'{"".join(body)}<footer>Generated by <code>python -m gamexpress preview</code>.</footer>'
            f'<script>{JS}</script></body></html>')
