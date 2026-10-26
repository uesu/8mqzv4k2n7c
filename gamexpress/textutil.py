"""Text helpers: HTML / HoYoLAB structured content -> plain text, version and link extraction."""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser

VERSION_RE = re.compile(r"\b(?:Version|Ver\.?|V)\s?(\d{1,2}\.\d{1,2})\b", re.I)
_QUOTED_AFTER = r"\s*[\"“”「『]([^\"“”」』\n]{2,80})[\"“”」』]"
URL_RE = re.compile(r"https?://[^\s<>\"'）)\]]+", re.I)
YOUTUBE_ID_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^\s]*&)?v=|live/|shorts/|embed/)|"
    r"youtu\.be/|piped\.video/(?:watch\?(?:[^\s]*&)?v=)?)([A-Za-z0-9_-]{11})")


def _http_media_url(raw: str | None) -> str:
    url = html.unescape(str(raw or "")).strip()
    return url if url.lower().startswith(("http://", "https://")) else ""


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.images: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "img":
            src = _http_media_url(a.get("src"))
            if src:
                self.images.append(src)
        if tag in ("iframe", "video"):
            src = _http_media_url(a.get("src"))
            if src:
                self.links.append(src)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> tuple[str, list[str], list[str]]:
    """Return (text, links, images) from an HTML fragment."""
    p = _HTMLText()
    try:
        p.feed(raw or "")
        p.close()
    except Exception:  # malformed HTML: fall back to tag stripping
        return clean_text(re.sub(r"<[^>]+>", "\n", html.unescape(raw or ""))), [], []
    return clean_text("".join(p.parts)), dedupe(p.links), dedupe(p.images)


def structured_to_text(structured: str) -> tuple[str, list[str], list[str]]:
    """HoYoLAB 'structured_content' (Quill delta JSON) -> (text, links, images).

    getPostFull sometimes returns content == 'en-us' (a HoYoLAB quirk — the
    real body is only in structured_content)."""
    try:
        ops = json.loads(structured or "[]")
    except (json.JSONDecodeError, TypeError):
        return "", [], []
    parts: list[str] = []
    links: list[str] = []
    images: list[str] = []
    for op in ops if isinstance(ops, list) else []:
        ins = op.get("insert") if isinstance(op, dict) else None
        attrs = (op.get("attributes") or {}) if isinstance(op, dict) else {}
        if isinstance(ins, str):
            parts.append(ins)
            if attrs.get("link"):
                links.append(attrs["link"])
        elif isinstance(ins, dict):
            img = _http_media_url(ins.get("image"))
            if img:
                images.append(img)
            vid = _http_media_url(ins.get("video"))
            if vid:
                links.append(vid)
            lc = ins.get("link_card")
            if isinstance(lc, dict):
                lc_url = _http_media_url(lc.get("origin_url") or lc.get("landing_url") or lc.get("url"))
                lc_title = str(lc.get("title") or ins.get("backup_text") or "").strip()
                if lc_title:
                    parts.append(f"\n{lc_title}\n")
                if lc_url:
                    links.append(lc_url)
    return clean_text("".join(parts)), dedupe(links), dedupe(images)


# Characters that must never reach a Discord card. Scraped text passes through here, and these
# are invisible or layout-controlling: C0/C1 controls, zero-width joiners/spaces, the bidi
# overrides (U+202A-202E, U+2066-2069) that can display "NEWS" as "SWEN" or hide a URL's real
# host inside a line of text, and the BOM. Stripping them keeps a card honest about what it
# says. \n and \t are kept - they are real formatting.
_INVISIBLE = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f"
    "\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)


def strip_invisible(text: str) -> str:
    return _INVISIBLE.sub("", text or "")


def clean_text(text: str) -> str:
    text = html.unescape(text or "").replace("\r", "")
    text = strip_invisible(text).replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def dedupe(seq):
    out, seen = [], set()
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def find_versions(text: str) -> list[str]:
    return dedupe(m.group(1) for m in VERSION_RE.finditer(text or ""))


def find_version(text: str) -> str | None:
    v = find_versions(text)
    return v[0] if v else None


def find_version_name(text: str, version: str | None) -> str | None:
    """Version 4.5 "To Roll the Stars in Astropolis"  ->  To Roll the Stars in Astropolis"""
    if not text:
        return None
    if version:
        v = re.escape(version)
        for rx in (r"(?:Version|Ver\.?|V)\s?" + v + _QUOTED_AFTER,
                   r"[\"“「『]([^\"”」』\n]{2,80})[\"”」』]\s*(?:Version|Ver\.?|V)\s?" + v):
            m = re.search(rx, text, re.I)
            if m:
                name = m.group(1).strip().rstrip(".,!")
                if not re.search(r"special|program|broadcast", name, re.I):
                    return name
    return None


def version_key(v: str | None) -> tuple[int, int]:
    try:
        a, b = (v or "0.0").split(".")[:2]
        return int(a), int(b)
    except ValueError:
        return (0, 0)


def bump_minor(v: str) -> str:
    a, b = version_key(v)
    return f"{a}.{b + 1}"


def short_version(tag: str | None) -> str | None:
    """'7.1.0' -> '7.1'"""
    if not tag:
        return None
    m = re.match(r"(\d+)\.(\d+)", tag)
    return f"{int(m.group(1))}.{int(m.group(2))}" if m else None


def find_urls(text: str) -> list[str]:
    return dedupe(u.rstrip(".,!?;:") for u in URL_RE.findall(text or ""))


def youtube_id(url_or_text: str) -> str | None:
    m = YOUTUBE_ID_RE.search(url_or_text or "")
    return m.group(1) if m else None


def youtube_video_url(links: list[str], text: str = "") -> str | None:
    for candidate in list(links) + find_urls(text):
        vid = youtube_id(candidate)
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
    return None


def twitch_url(links: list[str], text: str = "") -> str | None:
    for candidate in list(links) + find_urls(text):
        if re.search(r"twitch\.tv/[A-Za-z0-9_]+", candidate):
            return candidate.split("?")[0].rstrip("/")
    return None


_SENT_SPLIT = re.compile(r"(?<!\bVer\.)(?<!\bNo\.)(?<!approx\.)(?<!\bvs\.)(?<=[.!?。！？])\s+(?=\S)")


def sentences(text: str) -> list[str]:
    """Split into sentence-ish chunks: lines, HoYoverse section markers (〓Title〓 ▌ ■ ●),
    then sentence punctuation (abbreviations like 'Ver. 4.6' stay intact)."""
    text = re.sub(r"〓([^〓\n]{1,60})〓", r"\n\1\n", text or "")
    text = re.sub(r"\s*[▌■●]\s*", "\n", text)
    out: list[str] = []
    for line in text.split("\n"):
        line = line.strip(" ※")
        if not line:
            continue
        for part in _SENT_SPLIT.split(line):
            part = part.strip()
            if part:
                out.append(part)
    return out


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


# Ordered article blocks are deliberately separate from schedule/code extraction.
# No image ranking/aspect-ratio dedup here: two different same-shaped pictures are not duplicates.
MARKDOWN_LINK_INSIDE = r"(?:\[[^\[\]\n]*\]|[^\[\]\n])+"
MARKDOWN_LINK_RE = re.compile(rf"\[({MARKDOWN_LINK_INSIDE})\]\((https?://[^)\s]+)\)")
_BARE_HTTP_URL_RE = re.compile(r"^https?://[^\s<>\[\]]+$", re.I)
_INLINE_HTTP_URL_RE = re.compile(r"https?://[^\s<>\[\]()]+", re.I)


def _escape_cms_stars(text: str) -> str:
    """Escape literal '*' in CMS/HTML prose without altering URLs or already-escaped '\\*'."""
    if "*" not in text:
        return text
    out: list[str] = []
    last = 0
    for m in _INLINE_HTTP_URL_RE.finditer(text):
        out.append(re.sub(r"(?<!\\)\*", r"\*", text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(re.sub(r"(?<!\\)\*", r"\*", text[last:]))
    return "".join(out)


def _coalesce_bold(text: str) -> str:
    """Merge adjacent **...** runs separated only by horizontal spaces/tabs."""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        line = re.sub(r"(?<!\\)\*\*([ \t]*)\*\*", r"\1", line)
        if len(re.findall(r"(?<!\\)\*\*", line)) > 2:
            lifted = re.sub(
                r"\[\*\*((?:\[[^\[\]\n]*\]|[^*\[\]\n])+)\*\*\]\((https?://[^)\s]+)\)",
                r"**[\1](\2)**",
                line,
            )
            if lifted != line:
                line = re.sub(r"(?<!\\)\*\*([ \t]*)\*\*", r"\1", lifted)
        line = re.sub(r"~~([ \t]*)~~", r"\1", line)
        out.append(line)
    return "".join(out)


def _wrap_inline_marker(text: str, marker: str) -> str:
    """Wrap each non-empty line in marker...marker ('_' or '~~'), skipping bare URLs."""
    if not text or not text.strip():
        return text
    esc_m = re.escape(marker)
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"^([ \t]*)(.*?)([ \t\r\n]*)$", line, re.S)
        lead, core, tail = m.groups()
        if not core.strip() or _BARE_HTTP_URL_RE.fullmatch(core.strip()):
            out.append(line)
            continue
        pieces: list[str] = []
        pos = 0
        for um in _INLINE_HTTP_URL_RE.finditer(core):
            if um.start() >= 2 and core[um.start() - 2:um.start()] == "](":
                continue
            seg = core[pos:um.start()]
            if seg.strip():
                sm = re.match(r"^([ \t]*)(.*?)([ \t]*)$", seg, re.S)
                pieces.append(f"{sm.group(1)}{marker}{sm.group(2)}{marker}{sm.group(3)}")
            else:
                pieces.append(seg)
            pieces.append(um.group(0))
            pos = um.end()
        rem = core[pos:]
        if rem.strip():
            sm = re.match(r"^([ \t]*)(.*?)([ \t]*)$", rem, re.S)
            pieces.append(f"{sm.group(1)}{marker}{sm.group(2)}{marker}{sm.group(3)}")
        else:
            pieces.append(rem)
        joined = re.sub(rf"{esc_m}([ \t]*){esc_m}", r"\1", "".join(pieces))
        out.append(f"{lead}{joined}{tail}")
    return "".join(out)


def _bold_text_run(text: str) -> str:
    """Wrap each non-empty line in **...**, keeping outer spaces/newlines and bare URLs clean."""
    if not text or not text.strip():
        return text
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"^([ \t]*)(.*?)([ \t\r\n]*)$", line, re.S)
        lead, core, tail = m.groups()
        core_clean = re.sub(r"(?<!\\)\*\*", "", core)
        if not core_clean.strip():
            out.append(line)
            continue
        if _BARE_HTTP_URL_RE.fullmatch(core_clean):
            out.append(f"{lead}{core_clean}{tail}")
            continue
        pieces: list[str] = []
        pos = 0
        for um in _INLINE_HTTP_URL_RE.finditer(core_clean):
            if um.start() >= 2 and core_clean[um.start() - 2:um.start()] == "](":
                continue
            seg = core_clean[pos:um.start()]
            if seg.strip():
                sm = re.match(r"^([ \t]*)(.*?)([ \t]*)$", seg, re.S)
                pieces.append(f"{sm.group(1)}**{sm.group(2)}**{sm.group(3)}")
            else:
                pieces.append(seg)
            pieces.append(um.group(0))
            pos = um.end()
        rem = core_clean[pos:]
        if rem.strip():
            sm = re.match(r"^([ \t]*)(.*?)([ \t]*)$", rem, re.S)
            pieces.append(f"{sm.group(1)}**{sm.group(2)}**{sm.group(3)}")
        else:
            pieces.append(rem)
        out.append(f"{lead}{_coalesce_bold(''.join(pieces))}{tail}")
    return "".join(out)


def news_link(label: str, url: str) -> str:
    """One Discord masked link, with its label and destination kept separate.

    Discord masked links do not unescape ``\\[`` / ``\\]`` inside ``[label](url)`` display
    titles (backslashes render literally), while balanced unescaped depth-1 ``[...]`` spans
    inside ``[label](url)`` render literal brackets cleanly across Discord and CommonMark.
    Excess/nested/unmatched brackets are emitted outside link spans as ``\\[`` / ``\\]``.
    """
    from urllib.parse import urlsplit

    label = html.unescape(label)
    url = html.unescape(str(url or "")).strip()
    try:
        parts = urlsplit(url)
        valid = (parts.scheme in ("http", "https") and bool(parts.hostname)
                 and not parts.username and not parts.password and not re.search(r"[\s<>\[\]]", url))
    except ValueError:
        valid = False
    if not valid:
        return label
    raw_url = url
    # Parentheses terminate a Markdown destination unless escaped/encoded.
    url = url.replace("(", "%28").replace(")", "%29")
    label_norm = _coalesce_bold(label)
    m_ws = re.match(r"^([ \t]*)(.*?)([ \t]*)$", label_norm, re.S)
    lead_ws, core_label, tail_ws = m_ws.groups()
    core_unbold = re.sub(r"(?:(?<!\\)\*\*|~~)", "", core_label).strip("_ \t")
    if core_unbold in (raw_url, url) or _BARE_HTTP_URL_RE.fullmatch(core_unbold):
        return f"{lead_ws}{url}{tail_ws}"  # bare URL avoids broken anti-phishing masked links
    if "\n" not in label:
        stripped = MARKDOWN_LINK_RE.sub("", label)
        if stripped != label and re.fullmatch(r"(?:\\\[|\\\]|\s)*", stripped):
            urls_present = [m.group(2) for m in MARKDOWN_LINK_RE.finditer(label)]
            if urls_present and all(u == url for u in urls_present):
                return label
        if re.fullmatch(rf"(?:\\\[|\\\])+ {re.escape(url)}", label):
            return label
    clean = label.replace(r"\[", "[").replace(r"\]", "]")
    if "[" not in clean and "]" not in clean:
        return f"[{clean}]({url})"

    keep_in_link = [True] * len(clean)
    inner_open = [False] * len(clean)
    inner_close = [False] * len(clean)
    stack: list[tuple[int, bool]] = []
    for idx, ch in enumerate(clean):
        if ch == "\n":
            for open_idx, _ in stack:
                keep_in_link[open_idx] = False
            stack.clear()
        elif ch == "[":
            stack.append((idx, False))
        elif ch == "]":
            if not stack:
                keep_in_link[idx] = False
            else:
                open_idx, has_child = stack.pop()
                if has_child:
                    keep_in_link[open_idx] = False
                    keep_in_link[idx] = False
                else:
                    inner_open[open_idx] = True
                    inner_close[idx] = True
                if stack:
                    parent_idx, _ = stack[-1]
                    stack[-1] = (parent_idx, True)
    for open_idx, _ in stack:
        keep_in_link[open_idx] = False

    out: list[str] = []
    seg: list[str] = []
    has_link = False
    saw_pair_without_nonspace = False
    for idx, ch in enumerate(clean):
        if not keep_in_link[idx]:
            if seg:
                out.append(f"[{''.join(seg)}]({url})")
                seg.clear()
                has_link = True
            out.append(r"\[" if ch == "[" else r"\]")
            saw_pair_without_nonspace = False
            continue
        if inner_open[idx] and saw_pair_without_nonspace and seg:
            out.append(f"[{''.join(seg)}]({url})")
            seg.clear()
            has_link = True
            saw_pair_without_nonspace = False
        seg.append(ch)
        if inner_close[idx]:
            saw_pair_without_nonspace = True
        elif not ch.isspace() and not inner_open[idx]:
            saw_pair_without_nonspace = False
    if seg:
        out.append(f"[{''.join(seg)}]({url})")
        has_link = True
    if not has_link:
        return f"{''.join(out)} {url}" if out else url
    return "".join(out)


class _NewsHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.parts = []
        self.skip = 0
        self.anchor = None
        self.bold_depth = 0
        self.bold_open = False
        self.italic_depth = 0
        self.italic_open = False
        self.strike_depth = 0
        self.strike_open = False
        self.list_prefix_pending = False

    def _lift_trailing_ws(self) -> str:
        trailing = ""
        if self.parts:
            last = self.parts[-1]
            trailing = last[len(last.rstrip(" \t")):]
            self.parts[-1] = last.rstrip(" \t")
        return trailing

    def close_inline(self):
        if self.italic_open:
            self.parts.append("_" + self._lift_trailing_ws())
            self.italic_open = False
        if self.strike_open:
            self.parts.append("~~" + self._lift_trailing_ws())
            self.strike_open = False
        self.close_bold()

    def close_bold(self):
        if self.bold_open:
            self.parts.append("**" + self._lift_trailing_ws())
            self.bold_open = False

    def flush(self):
        self.close_inline()
        value = clean_text("".join(self.parts))
        # Adjacent HTML spans carrying the same emphasis form one run.
        value = _coalesce_bold(value)
        self.parts = []
        if value:
            self.blocks.append({"kind": "text", "text": value})

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        if self.skip:
            return
        if tag == "hr":
            self.close_inline()
            self.parts.append("\n──────────\n")
            return
        if tag in _HTMLText.BLOCK and not self.list_prefix_pending:
            self.close_inline()
            self.parts.append("\n")
        if tag in ("b", "strong", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.close_inline()
            self.bold_depth += 1
        elif tag in ("i", "em"):
            self.close_inline()
            self.italic_depth += 1
        elif tag in ("s", "strike", "del"):
            self.close_inline()
            self.strike_depth += 1
        elif tag == "li":
            self.close_inline()
            self.parts.append("- ")
            self.list_prefix_pending = True
        if tag == "a":
            self.anchor = (a.get("href"), len(self.parts), self.bold_open)
        if tag in ("img", "video", "source", "iframe"):
            url = _http_media_url(a.get("src") or a.get("data-src"))
            if url:
                self.flush()
                self.blocks.append({"kind": "image" if tag == "img" else "link" if tag == "iframe" else "video",
                                    "url": url})

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif not self.skip:
            if tag in ("i", "em"):
                self.italic_depth = max(0, self.italic_depth - 1)
                if not self.italic_depth:
                    self.close_inline()
            elif tag in ("s", "strike", "del"):
                self.strike_depth = max(0, self.strike_depth - 1)
                if not self.strike_depth:
                    self.close_inline()
            if tag in ("b", "strong", "h1", "h2", "h3", "h4", "h5", "h6"):
                self.bold_depth = max(0, self.bold_depth - 1)
                if not self.bold_depth:
                    self.close_inline()
            if tag == "a" and self.anchor:
                url, start, was_bold_open = self.anchor
                self.close_inline()
                label = "".join(self.parts[start:])
                rendered = news_link(label, url)
                self.parts[start:] = []
                self.bold_open = was_bold_open
                if _BARE_HTTP_URL_RE.fullmatch(rendered.strip()):
                    self.close_bold()
                    self.parts.append(rendered)
                elif self.bold_depth or was_bold_open:
                    m_lead = re.match(r"^([ \t]*)(.*)$", rendered, re.S)
                    lead_ws, rest = m_lead.groups()
                    if lead_ws:
                        self.parts.append(lead_ws)
                    if not self.bold_open and rest.strip():
                        self.parts.append("**")
                        self.bold_open = True
                    self.parts.append(re.sub(r"(?<!\\)\*\*", "", rest))
                else:
                    self.parts.append(rendered)
                    self.bold_open = False
                self.anchor = None
            if tag == "li":
                self.list_prefix_pending = False
            if tag in _HTMLText.BLOCK:
                self.close_inline()
                self.parts.append("\n")

    def handle_data(self, value):
        if not self.skip:
            if self.list_prefix_pending:
                if not value.strip():
                    return
                self.list_prefix_pending = False
            value = _escape_cms_stars(value)
            need_open = (
                (self.bold_depth and not self.bold_open)
                or (self.strike_depth and not self.strike_open)
                or (self.italic_depth and not self.italic_open)
            )
            if need_open and value.strip():
                m_ws = re.match(r"^([ \t\r\n]+)(.*)$", value, re.S)
                if m_ws:
                    self.parts.append(m_ws.group(1))
                    value = m_ws.group(2)
                if self.bold_depth and not self.bold_open:
                    self.parts.append("**")
                    self.bold_open = True
                if self.strike_depth and not self.strike_open:
                    self.parts.append("~~")
                    self.strike_open = True
                if self.italic_depth and not self.italic_open:
                    self.parts.append("_")
                    self.italic_open = True
            self.parts.append(value)


def news_blocks(content: str, structured: str = "", videos: dict[str, str] | None = None) -> list[dict]:
    """Full HTML/Quill article in source order, including linked external players.

    Prefer Quill when provided: it retains media that HoYoLAB's plain content can omit.
    Unknown structured embeds are not silently represented as a complete article.
    """
    if structured:
        ops = json.loads(structured)
        if isinstance(ops, dict):
            ops = ops.get("ops")
        if not isinstance(ops, list):
            raise ValueError("unrecognised article structure")
        blocks: list[dict] = []
        last_was_str = False
        for op in ops:
            if not isinstance(op, dict) or "insert" not in op:
                continue
            value = op["insert"]
            if isinstance(value, str):
                attributes = op.get("attributes") or {}
                if value == "\n" and (attributes.get("header") or attributes.get("list")) and blocks and blocks[-1]["kind"] == "text":
                    previous = blocks[-1]["text"]
                    prefix, delimiter, line = previous.rpartition("\n")
                    if attributes.get("header") and line.strip():
                        line = _bold_text_run(line)
                    elif attributes.get("list") and line.strip() and not line.lstrip().startswith(("- ", "• ")):
                        line = "- " + line.lstrip()
                    blocks[-1]["text"] = prefix + delimiter + line
                else:
                    link = attributes.get("link")
                    is_bold = bool(attributes.get("bold") and value.strip())
                    is_italic = bool(attributes.get("italic") and value.strip())
                    is_strike = bool(attributes.get("strike") and value.strip())
                    value = _escape_cms_stars(value)
                    if is_italic:
                        value = _wrap_inline_marker(value, "_")
                    if is_strike:
                        value = _wrap_inline_marker(value, "~~")
                    if link:
                        # Never let the following newline/heading become part of the link.
                        parts_out = []
                        for line in value.splitlines(keepends=True):
                            core = line.rstrip("\r\n")
                            nl = line[len(core):]
                            if core:
                                linked = news_link(_bold_text_run(core) if is_bold else core, str(link))
                                parts_out.append(linked + nl)
                            else:
                                parts_out.append(line)
                        value = "".join(parts_out)
                    elif is_bold:
                        value = _bold_text_run(value)
                if last_was_str and blocks and blocks[-1]["kind"] == "text":
                    blocks[-1]["text"] += value
                else:
                    blocks.append({"kind": "text", "text": value})
                last_was_str = True
            elif isinstance(value, dict):
                last_was_str = False
                if "divider" in value:
                    blocks.append({"kind": "text", "text": "\n──────────\n"})
                    continue
                if isinstance(value.get("link_card"), dict):
                    lc = value["link_card"]
                    lc_url = _http_media_url(lc.get("origin_url") or lc.get("landing_url") or lc.get("url"))
                    if not lc_url:
                        blocks.append({"kind": "text", "text": "\n[Embedded media unavailable — open the original article.]\n"})
                        continue
                    lc_label = clean_text(str(lc.get("title") or value.get("backup_text") or "")).replace("\n", " ").strip()
                    blocks.append({"kind": "text", "text": f"\n{news_link(lc_label or lc_url, lc_url)}\n"})
                    continue
                kind = next((k for k in ("image", "video", "vod") if k in value), None)
                if not kind:
                    blocks.append({"kind": "text", "text": "\n[Unsupported embedded content — open the original article.]\n"})
                    continue
                url = value[kind]
                if isinstance(url, dict):
                    url = (url.get("url") or url.get("src")
                           or (videos or {}).get(str(url.get("id") or url.get("video_id") or url.get("vod_id") or "")))
                elif kind == "vod":
                    url = (videos or {}).get(str(url))
                if not isinstance(url, str) or not _http_media_url(url):
                    blocks.append({"kind": "text", "text": "\n[Embedded media unavailable — open the original article.]\n"})
                    continue
                blocks.append({"kind": "video" if kind == "vod" else kind, "url": _http_media_url(url)})
        for block in blocks:
            if block["kind"] == "text":
                block["text"] = _coalesce_bold(block["text"])
        return blocks
    if re.fullmatch(r"[a-z]{2}-[a-z]{2}", (content or "").strip()):
        raise ValueError("article body unavailable")
    parser = _NewsHTML()
    parser.feed(content or "")
    parser.close()
    parser.flush()
    return parser.blocks
