"""Offline tests for the official-news YouTube cover fallback."""
from __future__ import annotations

import asyncio
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gamexpress.cards import news_payloads, validate_payload  # noqa: E402
from gamexpress.config import Game  # noqa: E402
from gamexpress.models import Item  # noqa: E402
from gamexpress.sources.candidate_thumbnail import fallback_url, with_verified_fallback  # noqa: E402


class Fetcher:
    def __init__(self, ok=True, sizes=None):
        self.ok, self.calls = ok, []
        self.sizes = sizes if sizes is not None else {"maxresdefault.jpg": (1280, 720)}

    async def media_dimensions(self, url):
        self.calls.append(("dimensions", url))
        return self.sizes.get(url.rsplit("/", 1)[-1])

    async def verify_media(self, url):
        self.calls.append(("verify", url))
        return self.ok


def item(source="nte"):
    return Item(source=source, game=source, id="1", url="https://example.org/article/1", title="Trailer",
                text="", published_ts=0, article_blocks=[{"kind": "text", "text": "Before"},
                    {"kind": "link", "url": "https://www.youtube.com/embed/tdZN6H1N3PU"},
                    {"kind": "text", "text": "After"}])


class CandidateThumbnailTests(unittest.TestCase):
    def test_youtube_fallback_is_verified_and_link_stays_inline(self):
        original = item()
        before = deepcopy(original)
        fetcher = Fetcher()
        result = asyncio.run(with_verified_fallback(fetcher, original))
        url = "https://i.ytimg.com/vi/tdZN6H1N3PU/maxresdefault.jpg"
        self.assertEqual(fetcher.calls, [("dimensions", url), ("verify", url)])
        self.assertEqual(result.images, [url])
        self.assertEqual(original, before)
        game = Game(key="nte", name="NTE", short="NTE", enabled=False, color=0)
        payload = news_payloads(game, result)[0]
        self.assertEqual(validate_payload(payload), [])
        children = payload["components"][0]["components"]
        texts = [c.get("content", "") for c in children if c["type"] == 10]
        self.assertIn("https://www.youtube.com/watch?v=tdZN6H1N3PU", texts)
        self.assertTrue(any(c["type"] == 12 and c["items"][0]["media"]["url"] == url for c in children))

    def test_existing_media_unofficial_sources_and_bad_links_never_guess(self):
        variants = []
        for source in ("hoyolab", "kuro", "nte"):
            a = item(source)
            a.images = ["https://example.org/existing.jpg"]
            variants.append(a)
        a = item("hoyolab")
        a.official = False
        variants.append(a)
        a = item("hoyolab")
        a.article_blocks.append({"kind": "text", "text": "[Native video unavailable — open the original article.]"})
        variants.append(a)
        for kind in ("image", "video"):
            a = item()
            a.article_blocks.append({"kind": kind, "url": "https://example.org/source"})
            variants.append(a)
        a = item()
        a.article_blocks[1]["url"] = "https://youtube.com.evil.example/embed/tdZN6H1N3PU"
        variants.append(a)
        a = item()
        a.game = "wuwa"
        variants.append(a)
        for a in variants:
            with self.subTest(source=a.source):
                f = Fetcher()
                self.assertIsNone(fallback_url(a))
                self.assertIs(asyncio.run(with_verified_fallback(f, a)), a)
                self.assertEqual(f.calls, [])
        failed = item()
        f = Fetcher(False)
        self.assertIs(asyncio.run(with_verified_fallback(f, failed)), failed)
        self.assertEqual(failed.images, [])
        self.assertEqual(len(f.calls), 4)

    def test_structured_youtube_video_is_a_link_not_native_video(self):
        a = item("hoyolab")
        a.game = "zzz"
        a.article_blocks[1]["kind"] = "video"
        f = Fetcher()
        self.assertIsNot(asyncio.run(with_verified_fallback(f, a)), a)
        a.article_blocks.append({"kind": "video", "url": "https://cdn.example.org/native.mp4"})
        f = Fetcher()
        self.assertIs(asyncio.run(with_verified_fallback(f, a)), a)
        self.assertFalse(f.calls)

    def test_official_hoyolab_and_kuro_without_native_media_get_verified_thumbnail(self):
        for source, game in (("hoyolab", "zzz"), ("kuro", "wuwa")):
            a = item(source)
            a.game = game
            result = asyncio.run(with_verified_fallback(Fetcher(), a))
            self.assertEqual(result.images, ["https://i.ytimg.com/vi/tdZN6H1N3PU/maxresdefault.jpg"])
            self.assertEqual(result.article_blocks, a.article_blocks)

    def test_plain_and_markdown_video_links_are_detected_but_not_spoofed(self):
        for content in ("Watch https://youtu.be/tdZN6H1N3PU.",
                        "[Trailer](https://www.youtube.com/watch?v=tdZN6H1N3PU)"):
            a = item()
            a.article_blocks = [{"kind": "text", "text": content}]
            with self.subTest(content=content):
                f = Fetcher()
                result = asyncio.run(with_verified_fallback(f, a))
                self.assertTrue(result.images[0].endswith("/tdZN6H1N3PU/maxresdefault.jpg"))
                self.assertEqual(result.article_blocks, a.article_blocks)
        for content in ("https://youtube.com.evil.test/watch?v=tdZN6H1N3PU",
                        "https://www.youtube.com/@channel", "tdZN6H1N3PU"):
            a = item()
            a.article_blocks = [{"kind": "text", "text": content}]
            self.assertIsNone(fallback_url(a))

    def test_missing_or_placeholder_maxres_uses_best_verified_fallback(self):
        original = item()
        f = Fetcher(sizes={"maxresdefault.jpg": (120, 90), "sddefault.jpg": (640, 480)})
        result = asyncio.run(with_verified_fallback(f, original))
        self.assertTrue(result.images[0].endswith("/sddefault.jpg"))
        self.assertEqual([x[0] for x in f.calls], ["dimensions", "dimensions", "verify"])
        f = Fetcher(sizes={})
        result = asyncio.run(with_verified_fallback(f, original))
        self.assertTrue(result.images[0].endswith("/hqdefault.jpg"))
        self.assertEqual([x[0] for x in f.calls], ["dimensions", "dimensions", "verify"])
        f = Fetcher(sizes={"maxresdefault.jpg": (720, 1280)})
        result = asyncio.run(with_verified_fallback(f, original))
        self.assertTrue(result.images[0].endswith("/maxresdefault.jpg"))


if __name__ == "__main__":
    unittest.main()
