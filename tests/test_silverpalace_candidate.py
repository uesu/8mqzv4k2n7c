"""Offline synthetic tests for the inactive Silver Palace listing snapshot."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gamexpress.cards import news_payloads  # noqa: E402
from gamexpress.config import Game  # noqa: E402
from gamexpress.models import Item  # noqa: E402
from gamexpress.sources import silverpalace  # noqa: E402


def row(cid, day):
    return {"id": cid, "title": f"Official {cid}", "publish_date": day}


def page(number, total, rows):
    return {"code": 0, "data": {"current_page": str(number), "total_page": total, "list": rows}}


class Fetcher:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    async def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0) if self.responses else None


def run(responses):
    f = Fetcher(responses)
    return asyncio.run(silverpalace.snapshot(f)), f


class SilverCandidateTests(unittest.TestCase):
    def test_unsorted_day_only_listing_scans_every_page_twice(self):
        p1 = page(1, 2, [row("01", "2026-10-01"), row("2", "2026-09-01")])
        p2 = page(2, 2, [row("3", "2026-11-01")])
        items, fetcher = run([p1, p2, p1, p2])
        self.assertEqual(items, [{"id": "01", "title": "Official 01", "date": "2026-10-01"},
                                 {"id": "2", "title": "Official 2", "date": "2026-09-01"},
                                 {"id": "3", "title": "Official 3", "date": "2026-11-01"}])
        self.assertEqual([call[1]["params"]["page"] for call in fetcher.calls], [1, 2, 1, 2])
        self.assertNotIn("ts", items[0])

    def test_detail_preserves_id_day_order_and_rejects_mismatches(self):
        candidate = {"id": "01", "title": "Official 01", "date": "2026-10-09"}
        data = {"id": "01", "title": "Official 01", "publish_date": "2026-10-09",
                "cover": "https://s.elementagames.com/official/image/a.jpg",
                "content": '<p>First</p><img src="https://s.elementagames.com/a.png"><p>Last</p>'}
        f = Fetcher([{"code": 0, "data": data}])
        found = asyncio.run(silverpalace.detail(f, candidate))
        self.assertEqual((found["id"], found["date"]), ("01", "2026-10-09"))
        self.assertEqual([b["kind"] for b in found["blocks"]], ["text", "image", "text"])
        self.assertIn("First", found["blocks"][0]["text"])
        self.assertIn("Last", found["blocks"][2]["text"])
        self.assertNotIn("ts", found)
        self.assertEqual(found["url"], "https://silverpalace.elementagames.com/en-us/newsDetail?id=01")
        self.assertEqual(f.calls[0][0], silverpalace.DETAIL)
        self.assertEqual(f.calls[0][1]["params"], {"lang": "en-us", "id": "01"})
        bad = (
            None, {"code": False, "data": data}, {"code": 1, "data": data},
            {"code": 0, "data": {**data, "id": "1"}},
            {"code": 0, "data": {**data, "publish_date": "2026-10-10"}},
            {"code": 0, "data": {**data, "title": "Different"}},
            {"code": 0, "data": {**data, "content": ""}},
            {"code": 0, "data": {**data, "content": "x" * (1024 * 1024 + 1)}},
            *({"code": 0, "data": {**data, "cover": url}} for url in (
                "https://evil.invalid/image.jpg", "https://s.elementagames.com.evil.invalid/x",
                "https://u@s.elementagames.com/x", "http://s.elementagames.com/x",
                "https://s.elementagames.com:444/x", "https://s.elementa\ngames.com/x",
                "https://s.elementagames.com/x#fragment")),
        )
        for response in bad:
            with self.subTest(response=str(response)[:160]):
                self.assertIsNone(asyncio.run(silverpalace.detail(Fetcher([response]), candidate)))
        f = Fetcher([])
        self.assertIsNone(asyncio.run(silverpalace.detail(f, {**candidate, "id": "../1"})))
        self.assertEqual(f.calls, [])

    def test_item_adapter_allows_no_cover_but_not_a_foreign_one(self):
        candidate = {"id": "1", "title": "Official 1", "date": "2026-10-09"}
        data = {"id": "1", "title": "Official 1", "publish_date": "2026-10-09",
                "cover": "", "content": '<p>Watch https://youtu.be/ELC4r7fbKEQ</p>'}
        game = Game(key="silverpalace", name="Silver Palace", short="Silver", enabled=False, color=0)
        fetcher = Fetcher([{"code": 0, "data": data}])
        item = asyncio.run(silverpalace.news_article(fetcher, game, candidate))
        self.assertEqual(item.images, [])
        self.assertEqual(item.published_ts, 0)
        self.assertEqual(item.source, "silverpalace")
        self.assertFalse(fetcher.calls[0][1]["allow_redirects"])
        data["cover"] = "https://evil.example/x.jpg"
        self.assertIsNone(asyncio.run(silverpalace.news_article(Fetcher([{"code": 0, "data": data}]), game, candidate)))

    def test_identical_cover_bytes_are_removed_but_body_and_different_media_are_preserved(self):
        cover = "https://s.elementagames.com/official/image/cover.jpg"
        body = "https://s.elementagames.com/official/image/body.jpg"
        game = Game(key="silverpalace", name="Silver Palace", short="Silver", enabled=False, color=0)
        item = Item(source="silverpalace", game="silverpalace", id="7", title="Official",
                    url="https://silverpalace.elementagames.com/en-us/newsDetail?id=7", text="", published_ts=0,
                    images=[cover], article_blocks=[{"kind": "image", "url": body},
                                                   {"kind": "text", "text": "Article text"}])

        class Response:
            def __init__(self, content, status=200, mime="image/jpeg"):
                self.content = self
                self.data, self.status, self.headers = content, status, {"Content-Type": mime}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def iter_chunked(self, size):
                yield self.data

        class Session:
            def __init__(self, bodies):
                self.bodies, self.calls = bodies, []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return self.bodies[url]

        same = Session({cover: Response(b"same contents"), body: Response(b"same contents")})
        result = asyncio.run(silverpalace.without_duplicate_leading_cover(type("F", (), {"session": same})(), item))
        self.assertEqual(result.images, [])
        self.assertEqual(item.images, [cover])  # no mutation of the source item
        self.assertEqual(result.article_blocks, item.article_blocks)
        self.assertEqual(len(same.calls), 2)
        self.assertTrue(all(kwargs["allow_redirects"] is False for _, kwargs in same.calls))
        pages = news_payloads(game, result, expand_full=True)
        galleries = [entry["media"]["url"] for page in pages for box in page["components"]
                     for child in box["components"] if child["type"] == 12 for entry in child["items"]]
        self.assertEqual(galleries, [body])
        for response in (Response(b"different"), Response(b"same contents", status=302),
                         Response(b"same contents", mime="text/html"), Response(b"x" * (8 * 1024 * 1024 + 1))):
            with self.subTest(status=response.status, size=len(response.data)):
                different = Session({cover: Response(b"same contents"), body: response})
                result = asyncio.run(silverpalace.without_duplicate_leading_cover(
                    type("F", (), {"session": different})(), item))
                self.assertIs(result, item)

    def test_duplicate_check_is_scoped_and_never_fetches_untrusted_or_nonleading_media(self):
        cover = "https://s.elementagames.com/official/image/cover.jpg"
        item = Item(source="silverpalace", game="silverpalace", id="7", title="Official",
                    url="https://silverpalace.elementagames.com/en-us/newsDetail?id=7", text="", published_ts=0,
                    images=[cover], article_blocks=[{"kind": "image", "url": "https://example.invalid/other.jpg"}])
        self.assertIs(asyncio.run(silverpalace.without_duplicate_leading_cover(object(), item)), item)
        item.article_blocks = [{"kind": "text", "text": "Intro"}, {"kind": "image", "url": cover}]
        self.assertIs(asyncio.run(silverpalace.without_duplicate_leading_cover(object(), item)), item)
        item.article_blocks = [{"kind": "image", "url": cover}]
        self.assertIs(asyncio.run(silverpalace.without_duplicate_leading_cover(object(), item)), item)

    def test_invalid_incomplete_or_mutating_walks_fail_closed(self):
        one = page(1, 2, [row("1", "2026-01-01")])
        two = page(2, 2, [row("2", "2026-01-02")])
        cases = (
            [], [None], [{"code": False, "data": {}}],
            [page(1, 31, [row("1", "2026-01-01")])],
            [page(1, 1, [])],
            [page(1, 1, [row("1", "2026-02-30")])],
            [page(1, 1, [row("../1", "2026-01-01")])],
            [one], [one, page(2, 3, [row("2", "2026-01-02")])],
            [one, page(2, 2, [row("1", "2026-01-02")])],
            [one, two, one, page(2, 2, [row("3", "2026-01-02")])],
            [page(1, 2, [row("1", "2026-01-01"), row("2", "2026-01-01")]),
             page(2, 2, [row("3", "2026-01-01"), row("4", "2026-01-01"), row("5", "2026-01-01")])],
        )
        for responses in cases:
            with self.subTest(responses=responses):
                self.assertIsNone(run(responses)[0])


if __name__ == "__main__":
    unittest.main()
