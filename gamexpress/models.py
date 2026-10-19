"""Normalized items shared by every source."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Item:
    """One official post (HoYoLAB article, tweet, Kuro news article)."""
    source: str                      # hoyolab | x | kuro
    game: str
    id: str
    url: str
    title: str
    text: str
    published_ts: int
    images: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    official: bool = True
    # Only the news feature uses ordered content blocks; existing source consumers stay unchanged.
    article_blocks: list[dict] = field(default_factory=list)
    author: str = ""

    @property
    def full(self) -> str:
        return f"{self.title}\n{self.text}" if self.title and self.title not in self.text else self.text

    @property
    def source_label(self) -> str:
        return {"hoyolab": "HoYoLAB", "x": "X Post", "kuro": "Official News"}.get(self.source, "Source")


@dataclass
class CodeHit:
    """One code as reported by one source."""
    code: str
    source: str                      # hoyolab | seria | humbao | ogc | ennead | fandom | codehub | wuthering.gg | x
    rewards: str | list | None = None
    verified: bool = False           # redeem-validated (seria OK / humbao) or the official module
    url: str = ""
    expired: bool = False            # the source explicitly lists this code as EXPIRED / invalid
    expires_at: int | None = None    # explicit "valid until" time given by the source (unix seconds)
    origin: str = ""                 # aggregators only: the upstream they copied it from (seria / fandom)
