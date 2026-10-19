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
    r"(?:youtube\.com/(?:watch\?(?:[^\s]*&)?v=|live/|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")


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
        if tag == "img" and a.get("src"):
            self.images.append(a["src"])
        if tag in ("iframe", "video") and a.get("src"):
            self.links.append(a["src"])

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
            if ins.get("image"):
                images.append(ins["image"])
            if ins.get("video"):
                links.append(ins["video"])
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
def news_link(label: str, url: str) -> str:
    """One Discord masked link, with its label and destination kept separate."""
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
    self_labelled = label.strip() == url
    # Parentheses terminate a Markdown destination unless escaped/encoded.
    url = url.replace("(", "%28").replace(")", "%29")
    if self_labelled:
        return url  # preserve bare/self-labelled URLs, matching the existing link policy
    label = label.replace("[", r"\[").replace("]", r"\]")
    return f"[{label}]({url})"


class _NewsHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.parts = []
        self.skip = 0
        self.anchor = None
        self.bold_depth = 0
        self.bold_open = False
        self.list_prefix_pending = False

    def close_bold(self):
        if self.bold_open:
            trailing = ""
            if self.parts:
                last = self.parts[-1]
                trailing = last[len(last.rstrip(" \t")):]
                self.parts[-1] = last.rstrip(" \t")
            self.parts.append("**" + trailing)
            self.bold_open = False

    def flush(self):
        self.close_bold()
        value = clean_text("".join(self.parts))
        # Adjacent HTML spans carrying the same emphasis form one bold run.
        value = re.sub(r"\*\*([ \t]*)\*\*", r"\1", value)
        self.parts = []
        if value:
            self.blocks.append({"kind": "text", "text": value})

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        if self.skip:
            return
        if tag in _HTMLText.BLOCK and not self.list_prefix_pending:
            self.parts.append("\n")
        if tag in ("b", "strong", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.bold_depth += 1
        elif tag == "li":
            self.parts.append("- ")
            self.list_prefix_pending = True
        if tag == "a":
            if self.bold_depth and not self.bold_open:
                self.parts.append("**")
                self.bold_open = True
            self.anchor = (a.get("href"), len(self.parts))
        if tag in ("img", "video", "source", "iframe"):
            url = a.get("src") or a.get("data-src")
            if url:
                self.flush()
                self.blocks.append({"kind": "image" if tag == "img" else "link" if tag == "iframe" else "video",
                                    "url": html.unescape(url)})

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif not self.skip:
            if tag in ("b", "strong", "h1", "h2", "h3", "h4", "h5", "h6"):
                self.bold_depth = max(0, self.bold_depth - 1)
                if not self.bold_depth:
                    self.close_bold()
            if tag == "a" and self.anchor:
                url, start = self.anchor
                label = "".join(self.parts[start:])
                self.parts[start:] = [news_link(label, url)]
                self.anchor = None
            if tag == "li":
                self.list_prefix_pending = False
            if tag in _HTMLText.BLOCK:
                self.parts.append("\n")

    def handle_data(self, value):
        if not self.skip:
            if self.list_prefix_pending:
                if not value.strip():
                    return
                self.list_prefix_pending = False
            if self.bold_depth and not self.bold_open and value.strip():
                self.parts.append("**")
                self.bold_open = True
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
        blocks = []
        for op in ops:
            if not isinstance(op, dict) or "insert" not in op:
                continue
            value = op["insert"]
            if isinstance(value, str):
                attributes = op.get("attributes") or {}
                if value == "\n" and (attributes.get("header") or attributes.get("list")) and blocks and blocks[-1]["kind"] == "text":
                    previous = blocks[-1]["text"]
                    prefix, delimiter, line = previous.rpartition("\n")
                    if attributes.get("header") and line and not line.startswith("**"):
                        line = "**" + line + "**"
                    elif attributes.get("list") and line and not line.startswith(("- ", "• ")):
                        line = "- " + line
                    blocks[-1]["text"] = prefix + delimiter + line
                elif attributes.get("bold") and value.strip():
                    value = "**" + value.strip() + "**" + ("\n" if value.endswith("\n") else "")
                link = attributes.get("link")
                if link:
                    # Never let the following newline/heading become part of the link.
                    value = "".join(news_link(line.rstrip("\r\n"), link) + line[len(line.rstrip("\r\n")):]
                                    if line.rstrip("\r\n") else line for line in value.splitlines(keepends=True))
                blocks.append({"kind": "text", "text": value})
            elif isinstance(value, dict):
                if "divider" in value:
                    blocks.append({"kind": "text", "text": "\n──────────\n"})
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
                if not isinstance(url, str) or not url:
                    blocks.append({"kind": "text", "text": "\n[Embedded media unavailable — open the original article.]\n"})
                    continue
                blocks.append({"kind": "video" if kind == "vod" else kind, "url": html.unescape(url)})
        return blocks
    if re.fullmatch(r"[a-z]{2}-[a-z]{2}", (content or "").strip()):
        raise ValueError("article body unavailable")
    parser = _NewsHTML()
    parser.feed(content or "")
    parser.close()
    parser.flush()
    return parser.blocks
