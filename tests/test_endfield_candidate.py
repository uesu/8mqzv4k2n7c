"""Offline contract tests for the unregistered Endfield reader (synthetic fixtures)."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gamexpress.cards import news_payloads, validate_payload  # noqa: E402
from gamexpress.config import Game  # noqa: E402
from gamexpress.sources import endfield  # noqa: E402


def row(cid, timestamp):
    return {"cid": cid, "title": "Official notice " + cid, "displayTime": timestamp}


def page(number, rows, total):
    return {"code": 0, "data": {"current": number, "pageSize": 20, "total": total, "list": rows}}


def test_unmatched_source_brackets_are_literal_but_links_remain_valid():
    from gamexpress.cards import _split_expanded_text
    from gamexpress.sources.endfield import _escape_unmatched_brackets

    source = "[A](https://example.test/) then Details] and [unfinished"
    escaped = _escape_unmatched_brackets(source)
    assert escaped == r"[A](https://example.test/) then Details\] and \[unfinished"
    assert _escape_unmatched_brackets(escaped) == escaped
    assert _escape_unmatched_brackets(r"\[literal\] [ok]") == r"\[literal\] [ok]"
    assert _split_expanded_text(("normal paragraph\n\n" * 300) + escaped, 3500) is not None


class Fetcher:
    def __init__(self, pages, detail=None):
        self.pages, self.detail, self.calls = pages, detail, []

    async def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == endfield.BASE:
            return self.pages.get(kwargs["params"]["page"])
        return self.detail


def run(coro):
    return asyncio.run(coro)


class EndfieldCandidateTests(unittest.TestCase):
    def test_complete_paginated_listing_does_not_stop_at_old_or_sticky(self):
        first = [row(f"{i:04d}", 100 if i == 0 else 300) for i in range(20)]
        second = [row("0020", 400)]
        fetcher = Fetcher({1: page(1, first, 21), 2: page(2, second, 21)})
        found = run(endfield.discover_news(fetcher, SimpleNamespace(key="endfield"), 200))
        self.assertEqual(len(found), 20)
        self.assertEqual(found[-1], {"id": "0020", "ts": 400, "title": "Official notice 0020"})
        self.assertEqual([call[1]["params"]["page"] for call in fetcher.calls], [1, 2])
        self.assertEqual(fetcher.calls[0][1]["params"]["code"], "arknights_endfield_official")

    def test_fail_closed_on_incomplete_drift_duplicates_and_bad_shapes(self):
        first = [row(str(i), 300) for i in range(20)]
        for pages in (
            {1: page(1, first, 21)},
            {1: page(1, first, 21), 2: page(2, [row("20", 300)], 22)},
            {1: page(1, first, 21), 2: page(2, [row("0", 300)], 21)},
            {1: page(1, [row("1", True)], 1)},
            {1: page(1, [row("../a", 300)], 1)},
            {1: page(1, [], 1)},
            {1: page(1, first, 401)},
            {1: {"code": False, "data": {}}},
        ):
            with self.subTest(pages=pages):
                self.assertIsNone(run(endfield.discover_news(Fetcher(pages), None, 0)))
        self.assertEqual(run(endfield.discover_news(Fetcher({1: page(1, [], 0)}), None, 0)), [])

    def test_complete_snapshot_rechecks_and_holds_on_drift(self):
        class ChangingFetcher:
            def __init__(self, pages):
                self.pages = iter(pages)
                self.calls = []

            async def get_json(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return next(self.pages, None)

        first = page(1, [row("001", 300)], 1)
        fetcher = ChangingFetcher([first, first])
        result = run(endfield.snapshot(fetcher, None))
        self.assertEqual(result, [{"id": "001", "ts": 300, "title": "Official notice 001"}])
        self.assertTrue(all(call[1]["allow_redirects"] is False for call in fetcher.calls))
        changed = page(1, [row("002", 300)], 1)
        self.assertIsNone(run(endfield.snapshot(ChangingFetcher([first, changed]), None)))
        self.assertIsNone(run(endfield.snapshot(ChangingFetcher([first, None]), None)))

    def test_reader_and_renderer_preserve_trailer_and_media_order_in_synthetic_examples(self):
        game = Game(key="endfield", name="Endfield", short="Endfield", enabled=False, color=0)
        url = "https://web-static.hg-cdn.com/upload/image/"
        cases = (
            ("0766", '<p>Before</p><iframe src="https://www.youtube.com/embed/Yt7eUS1X0Ec"></iframe><p>After</p>'),
            ("1340", f'<p>Before</p><img src="{url}example.gif"><p>Middle</p>'
                     f'<img src="{url}example.png"><p>After</p>'),
            ("3840", "".join(f'<p>Caption {i}</p><img src="{url}{i}.png">' for i in range(50))),
        )
        for cid, html in cases:
            with self.subTest(cid=cid):
                detail = {"code": 0, "data": {"cid": cid, "displayTime": 300,
                          "title": "Example", "data": html}}
                item = run(endfield.news_article(Fetcher({}, detail), game, {"id": cid, "ts": 300}))
                pages = news_payloads(game, item)
                self.assertTrue(all(not validate_payload(p) for p in pages))
                children = [c for p in pages for container in p["components"]
                            for c in container.get("components", [])]
                texts = [c.get("content", "") for c in children if c["type"] == 10]
                media = [i["media"]["url"] for c in children if c["type"] == 12 for i in c["items"]]
                if cid == "0766":
                    self.assertEqual(len(pages), 1)
                    self.assertIn("https://www.youtube.com/watch?v=Yt7eUS1X0Ec", texts)
                    self.assertLess(texts.index("Before"), texts.index("https://www.youtube.com/watch?v=Yt7eUS1X0Ec"))
                    self.assertLess(texts.index("https://www.youtube.com/watch?v=Yt7eUS1X0Ec"), texts.index("After"))
                elif cid == "1340":
                    self.assertEqual(media, [url + "example.gif", url + "example.png"])
                else:
                    self.assertEqual(media, [url + str(i) + ".png" for i in range(50)])
                    self.assertGreater(len(pages), 1)
                    self.assertFalse(any("**Excerpt" in t for t in texts))

    def test_detail_preserves_leading_zero_body_order_and_rejects_mismatch(self):
        detail = {"code": 0, "data": {"cid": "0740", "displayTime": 300, "title": "Update",
                 "cover": "https://web-static.hg-cdn.com/test.jpg",
                 "data": '<p>Hello <b>friend</b></p><img src="https://web-static.hg-cdn.com/a.png"><p>Goodbye</p>'}}
        fetcher = Fetcher({}, detail)
        item = run(endfield.news_article(fetcher, SimpleNamespace(key="endfield"), {"id": "0740", "ts": 300}))
        self.assertEqual(item.url, "https://endfield.gryphline.com/en-us/news/0740")
        self.assertEqual(item.images, ["https://web-static.hg-cdn.com/test.jpg"])
        self.assertEqual([b["kind"] for b in item.article_blocks], ["text", "image", "text"])
        self.assertIn("Hello", item.article_blocks[0]["text"])
        self.assertIn("Goodbye", item.article_blocks[2]["text"])
        self.assertEqual(fetcher.calls[0][0], endfield.BASE + "/0740")
        self.assertIsNone(run(endfield.news_article(fetcher, None, {"id": "0740", "ts": 301})))
        self.assertIsNone(run(endfield.news_article(fetcher, None, {"id": "../0740", "ts": 300})))
        detail["data"]["data"] = ""
        self.assertIsNone(run(endfield.news_article(fetcher, None, {"id": "0740", "ts": 300})))


if __name__ == "__main__":
    unittest.main()
