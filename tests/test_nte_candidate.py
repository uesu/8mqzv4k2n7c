"""Synthetic one-page tests for the inactive NTE listing parser.

The date/path mismatch is modelled on article 265053 observed in the publisher's
listing and #173 capture, not a raw HTML fixture or an assumed publication time.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gamexpress.cards import news_payloads, validate_payload  # noqa: E402
from gamexpress.config import Game  # noqa: E402
from gamexpress.models import Item  # noqa: E402
from gamexpress.sources import nte_candidate  # noqa: E402
from gamexpress.textutil import news_blocks  # noqa: E402


def article(href, title="Official &amp; news", day="2026-11-20"):
    return f'<a href="{href}"><div><h2 class="title">{title}</h2><p class="date">{day}</p></div></a>'


def listing(content):
    return f'<html><div class="other">ignored</div><div class="listNews">{content}</div></html>'


class NteCandidateTests(unittest.TestCase):
    def test_complete_double_walk_rejects_partial_and_listing_drift(self):
        path1 = "/en/article/news/gamenews/20240830/252469.html"
        path2 = "/en/article/news/gamebroad/20261113/265053.html"
        pager = '<ul class="pagination"><li><a href="#">1</a></li><li><a href="index1.html">2</a></li></ul>'
        first = listing(article(path1, "Trailer", "2024-08-30")) + pager
        second = listing(article(path2, "Guidelines", "2026-11-20"))

        class Fetcher:
            def __init__(self, firsts, last=second):
                self.firsts, self.last, self.calls = iter(firsts), last, []

            async def get_text(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return next(self.firsts, None) if url.endswith("index.html") else self.last

        f = Fetcher([first, first, first, first])
        rows = asyncio.run(nte_candidate.snapshot(f))
        self.assertEqual([r["id"] for r in rows], ["252469", "265053"])
        self.assertEqual(rows[1]["date"], "2026-11-20")
        self.assertEqual(rows[1]["url"], "https://nte.perfectworld.com" + path2)
        self.assertEqual(len(f.calls), 6)
        self.assertTrue(all(call[1]["allow_redirects"] is False for call in f.calls))
        self.assertIsNone(asyncio.run(nte_candidate.snapshot(Fetcher([first, first], last=None))))
        changed = first.replace("Trailer", "Changed")
        self.assertIsNone(asyncio.run(nte_candidate.snapshot(Fetcher([first, changed]))))
        self.assertIsNone(asyncio.run(nte_candidate.snapshot(Fetcher([first, first, changed, changed]))))

    def test_detail_reader_preserves_body_and_rejects_untrusted_or_incomplete(self):
        row = {"id": "252469", "title": "Trailer", "date": "2024-08-30",
               "url": "https://nte.perfectworld.com/en/article/news/gamenews/20240830/252469.html"}
        html = ('<div class="articleContent"><p>Before</p><iframe '
                'src="https://www.youtube.com/embed/tdZN6H1N3PU"></iframe><p>After</p></div>')

        class Fetcher:
            def __init__(self, body):
                self.body, self.calls = body, []

            async def get_text(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return self.body

        game = Game(key="nte", name="NTE", short="NTE", enabled=False, color=0)
        fetcher = Fetcher(html)
        result = asyncio.run(nte_candidate.news_article(fetcher, game, row))
        self.assertEqual(result.published_ts, 0)
        self.assertEqual([b["kind"] for b in result.article_blocks], ["text", "link", "text"])
        self.assertEqual(fetcher.calls[0][0], row["url"])
        self.assertFalse(fetcher.calls[0][1]["allow_redirects"])
        for bad in (None, html.replace("</div>", ""), html + html, "<p>unavailable</p>"):
            self.assertIsNone(asyncio.run(nte_candidate.news_article(Fetcher(bad), game, row)))
        for url in (row["url"].replace("nte.perfectworld.com", "nte.perfectworld.com.evil.test"),
                    row["url"].replace("252469", "999"), row["url"] + "?redirect=1"):
            fetcher = Fetcher(html)
            self.assertIsNone(asyncio.run(nte_candidate.news_article(fetcher, game, {**row, "url": url})))
            self.assertFalse(fetcher.calls)

    def test_table_cells_keep_distinct_masked_ids_and_rows(self):
        html = ('<div class="articleContent"><p>Accounts:</p><table>'
                '<tr><td><p>2009****9731</p></td><td><span>2005****4351</span></td></tr>'
                '<tr><td>2003****7685</td><td>2009****4596</td><td>2012****5282</td></tr>'
                '</table><p>After</p></div>')
        result = nte_candidate._body(html)
        text = result[0]["text"]
        self.assertIn(r"2009\*\*\*\*9731, 2005\*\*\*\*4351", text)
        self.assertIn(r"2003\*\*\*\*7685, 2009\*\*\*\*4596, 2012\*\*\*\*5282", text)
        self.assertIn("After", text)
        bad = '<div class="articleContent"><table><tr><td><img src="https://example.test/a.jpg"></td></tr></table></div>'
        self.assertIsNone(nte_candidate._body(bad))

    def test_official_media_path_spaces_are_encoded_without_relaxing_url_validation(self):
        url = "https://ntevmg.perfectworld.com/webops/nte/art EN%20name.jpg"
        html = f'<div class="articleContent"><img src="{url}"></div>'
        self.assertEqual(nte_candidate._body(html)[0]["url"], url.replace("art EN", "art%20EN"))
        for bad in ("https://evil.test/art EN.jpg", "https://ntevmg.perfectworld.com.evil.test/art EN.jpg",
                    "https://user@ntevmg.perfectworld.com/art EN.jpg", "https://ntevmg.perfectworld.com/art EN.jpg?q=bad value",
                    "https://ntevmg.perfectworld.com/art\n EN.jpg"):
            self.assertEqual(nte_candidate._encode_official_media_spaces(bad), bad)

    def test_literal_tildes_do_not_strike_through_article_and_real_strike_is_kept(self):
        html = ('<div class="articleContent"><p>Taygedo~~taygedo!</p><p>Text in between</p>'
                '<p>Taygedo~~taygedo!</p><p><del>obsolete</del> '
                '<a href="https://example.test/~user">visit</a></p></div>')
        blocks = nte_candidate._body(html)
        text = "\n".join(b["text"] for b in blocks if b["kind"] == "text")
        self.assertEqual(text.count(r"Taygedo\~\~taygedo!"), 2)
        self.assertIn("~~obsolete~~", text)
        self.assertIn("https://example.test/~user", text)
        self.assertIn("Text in between", text)
        self.assertEqual(news_blocks("<p>A~~B</p>")[0]["text"], "A~~B")  # other sources unchanged

    def test_synthetic_short_trailer_keeps_watch_link_in_same_news_card(self):
        # Contract experiment only: no NTE detail reader or delivery is registered.
        game = Game(key="nte", name="Neverness to Everness", short="NTE", enabled=False, color=0)
        item = Item(source="nte", game="nte", id="252469",
                    url="https://nte.perfectworld.com/en/article/news/gamenews/20240830/252469.html",
                    title="Trailer", text="", published_ts=0,
                    article_blocks=news_blocks('<p>Before</p><iframe src="https://www.youtube.com/embed/tdZN6H1N3PU"></iframe><p>After</p>'))
        payloads = news_payloads(game, item)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(validate_payload(payloads[0]), [])
        texts = [child.get("content", "") for container in payloads[0]["components"]
                 for child in container.get("components", []) if child["type"] == 10]
        link = "https://www.youtube.com/watch?v=tdZN6H1N3PU"
        self.assertIn(link, texts)
        self.assertLess(texts.index("Before"), texts.index(link))
        self.assertLess(texts.index(link), texts.index("After"))

    def test_observed_path_date_mismatch_uses_displayed_day(self):
        result = nte_candidate.parse_list_page(listing(article(
            "/en/article/news/gamebroad/20261113/265053.html", "Fan Content Guidelines", "2026-11-20")))
        self.assertEqual(result, [{"id": "265053", "title": "Fan Content Guidelines", "date": "2026-11-20"}])
        self.assertNotIn("ts", result[0])

    def test_canonical_links_and_source_order(self):
        html = listing(article("https://nte.perfectworld.com/en/article/news/gamenews/20260101/001.html") +
                       article("/en/article/news/gameevent/20261201/5.html", "News", "2026-10-01"))
        self.assertEqual(nte_candidate.parse_list_page(html), [
            {"id": "001", "title": "Official & news", "date": "2026-11-20"},
            {"id": "5", "title": "News", "date": "2026-10-01"}])

    def test_untrusted_host_malformed_or_incomplete_is_failure_not_empty(self):
        path = "/en/article/news/gamenews/20261113/123.html"
        invalid = [None, "<html>temporary error</html>", listing(""),
                   listing(article("https://evil.invalid" + path)),
                   listing(article("https://nte.perfectworld.com.evil.invalid" + path)),
                   listing(article("https://nte.perfectworld.com:443" + path)),
                   listing(article("https://u@nte.perfectworld.com" + path)),
                   listing(article(path + "?x=1")), listing(article(path, day="2026-02-30")),
                   listing(article(path, title="")), listing(article(path) + article(path)),
                   '<div class="listNews">' + article(path),
                   '<div class="listNews"><a href="' + path + '"><h2 class="title">broken</h2></div>',
                   listing(article(path)) + listing(article(path)), "x" * (1024 * 1024 + 1)]
        for value in invalid:
            with self.subTest(value=str(value)[:100]):
                self.assertIsNone(nte_candidate.parse_list_page(value))


if __name__ == "__main__":
    unittest.main()
