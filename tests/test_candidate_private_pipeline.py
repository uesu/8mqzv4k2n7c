"""Private test harness tests: only local Git and mocked Discord, never network."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import candidate_private_backfill as historical  # noqa: E402
import candidate_private_backfill_control as control  # noqa: E402
import candidate_private_pipeline_test as private  # noqa: E402
import silver_dedup_test as silver_fix  # noqa: E402

from gamexpress.cards import news_payloads  # noqa: E402
from gamexpress.config import Game, load_settings  # noqa: E402
from gamexpress.models import Item  # noqa: E402

HOOK = "https://discord.com/api/webhooks/123456789012345678/example_token_123456789"


def card():
    game = Game(key="silverpalace", name="Silver Palace", short="Silver", enabled=False, color=0)
    item = Item(source="silverpalace", game="silverpalace", id="2", title="Private fixture",
                url="https://silverpalace.elementagames.com/en-us/newsDetail?id=2", text="", published_ts=0,
                article_blocks=[{"kind": "text", "text": "Hello"}])
    return news_payloads(game, item)[0]


class PrivateHarnessTests(unittest.TestCase):
    def test_real_local_git_checkpoint_and_restart_with_mock_discord(self):
        nte = {"id": "252475", "title": "Test", "date": "2024-08-30",
               "url": "https://nte.perfectworld.com/en/article/news/gamenews/20240830/252475.html"}
        end = {"id": "100", "title": "Test", "ts": 1700000000}
        silver = {"id": "2", "title": "Test", "date": "2025-01-01"}
        prepared = [("nte", nte, [card()]), ("endfield", end, [card(), card()]),
                    ("silverpalace", silver, [card()])]
        calls = []

        async def send(session, settings, source, payload):
            calls.append(source)
            return SimpleNamespace(ok=True, ambiguous=False, message_id=str(100 + len(calls)))

        async def sleep(seconds):
            self.assertEqual(seconds, 1.5)

        with (patch.object(private.delivery, "send_candidate_once", send), patch.object(private.asyncio, "sleep", sleep),
              contextlib.redirect_stdout(io.StringIO())):
            sent = asyncio.run(private.send_prepared(None, prepared, HOOK))
        self.assertEqual(sent, 4)
        self.assertEqual(calls, ["nte", "endfield", "endfield", "silverpalace"])

    def test_historical_supervisor_dispatches_one_run_and_requires_progress(self):
        seen = []
        run_record = {"id": 42, "head_sha": "abc", "event": "workflow_dispatch"}

        def command(*args, timeout=50):
            seen.append(args)
            return b"abc\n" if args[:3] == ("git", "rev-parse", "HEAD") else b""

        with (patch.object(control, "current_head", return_value="abc"), patch.object(control, "command", command),
              patch.object(control, "status", side_effect=[(266, 1, 0), (267, 0, 0)]),
              patch.object(control, "runs", side_effect=[{}, {42: run_record}]), patch.object(control, "watch") as watch,
              contextlib.redirect_stdout(io.StringIO())):
            control.run()
        watch.assert_called_once_with(42)
        self.assertEqual(sum(args[:3] == ("gh", "workflow", "run") for args in seen), 1)

        with (patch.object(control, "current_head", return_value="abc"), patch.object(control, "command", command),
              patch.object(control, "status", side_effect=[(266, 1, 0), (266, 1, 0)]),
              patch.object(control, "runs", side_effect=[{}, {42: run_record}]), patch.object(control, "watch"),
              contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError)):
            control.run()

    def test_historical_batch_is_ordered_persisted_and_hides_message_ids(self):
        from gamexpress import candidate_news_delivery as delivery

        settings = load_settings({f"DISCORD_WEBHOOK_NEWS_{source.upper()}": HOOK for source in historical.SOURCES})
        routes = {source: delivery.destination(settings, source)[1] for source in historical.SOURCES}
        rows = {
            "nte": [{"id": "252475", "title": "NTE", "date": "2024-08-30",
                     "url": "https://nte.perfectworld.com/en/article/news/gamenews/20240830/252475.html"}],
            "endfield": [{"id": "100", "title": "Endfield", "ts": 1700000000}],
            "silverpalace": [{"id": "2", "title": "Silver", "date": "2025-01-01"}],
        }
        comments, ids = {}, {source: 1000 + n for n, source in enumerate(historical.SOURCES)}
        for source in historical.SOURCES:
            state = delivery.ingest(source, routes[source], None, rows[source])
            comments[ids[source]] = {"id": ids[source], "user": {"login": "github-actions[bot]"},
                                     "issue_url": "https://api.github.com/repos/uesu/Game-Express/issues/174",
                                     "body": historical.encode(source, state)}
        sends = []

        def api(endpoint, *, method="GET", body=None, paginate=False):
            cid = int(endpoint.rsplit("/", 1)[-1])
            if method == "PATCH":
                comments[cid]["body"] = body["body"]
            return copy.deepcopy(comments[cid])

        async def render(fetcher, conf, source, row):
            return [card(), card()] if source == "endfield" else [card()]

        async def send(session, conf, source, payload):
            cid = ids[source]
            record = historical.decode(source, comments[cid]["body"])
            self.assertTrue(record["partial"]["inflight"])
            sends.append(source)
            return SimpleNamespace(ok=True, ambiguous=False, message_id=str(991234500000000000 + len(sends)))

        async def sleep(seconds):
            self.assertEqual(seconds, 1.5)

        with (patch.object(historical, "COMMENT_IDS", ids), patch.object(historical, "PREPARE_COMPLETE", True),
              patch.object(historical, "gh_api", api), patch.object(delivery, "render_candidate", render),
              patch.object(delivery, "send_candidate_once", send), patch.object(historical.asyncio, "sleep", sleep),
              contextlib.redirect_stdout(io.StringIO())):
            pending = asyncio.run(historical.send_batch(None, None, settings, routes))
        self.assertEqual(pending, 0)
        self.assertEqual(sends, ["endfield", "endfield", "nte", "silverpalace"])
        for source in historical.SOURCES:
            body = comments[ids[source]]["body"]
            self.assertNotIn(HOOK, body)
            self.assertNotIn("9912345000000000", body)
            self.assertEqual(len(historical.decode(source, body)["posted"]), 1)

    def test_historical_inflight_or_changed_journal_never_sends(self):
        from gamexpress import candidate_news_delivery as delivery

        state = delivery.ingest("silverpalace", "a" * 16, None, [{"id": "2", "title": "Silver", "date": "2025-01-01"}])
        state["partial"] = {"id": "2", "signature": "b" * 64, "pages": 1, "messages": [], "inflight": True}
        journals = {source: SimpleNamespace(state={"partial": None, "pending": []}) for source in historical.SOURCES}
        journals["silverpalace"].state = state
        with self.assertRaises(delivery.DeliveryHold):
            historical.oldest(journals)
        comment = {"id": 123, "user": {"login": "github-actions[bot]"},
                   "issue_url": "https://api.github.com/repos/uesu/Game-Express/issues/174",
                   "body": historical.encode("silverpalace", state)}
        journal = historical.CommentJournal("silverpalace", comment, "a" * 16)
        changed = copy.deepcopy(comment)
        changed["body"] += "changed"
        with patch.object(historical, "gh_api", return_value=changed):
            self.assertFalse(asyncio.run(journal.checkpoint(state)))
        self.assertNotEqual(historical.anonymize("123456", "secret-one"), historical.anonymize("123456", "secret-two"))

    def test_silver_fix_preparation_requires_one_article_image_and_full_text(self):
        async def discover(fetcher, source):
            return [{"id": "7", "title": "[Monotype] Beta Test Recruitment has officially started!"}]

        async def render(fetcher, settings, source, row):
            return [{"components": [{"components": [
                {"type": 12, "items": [{"media": {"url": silver_fix.BODY}}]},
                {"type": 10, "content": "How to Participate\nRecruitment Overview"}]}]}]

        with patch.object(silver_fix.delivery, "discover", discover), patch.object(silver_fix.delivery, "render_candidate", render):
            payload = asyncio.run(silver_fix.prepare(None))
        self.assertIn("components", payload)

        async def bad_render(fetcher, settings, source, row):
            result = await render(fetcher, settings, source, row)
            result[0]["components"][0]["components"].append({"type": 12, "items": [{"media": {"url": silver_fix.BODY}}]})
            return result

        with (patch.object(silver_fix.delivery, "discover", discover),
              patch.object(silver_fix.delivery, "render_candidate", bad_render), self.assertRaises(ValueError)):
            asyncio.run(silver_fix.prepare(None))

    def test_silver_fix_refuses_rerun_and_completed_test(self):
        with patch.dict("os.environ", {"GITHUB_RUN_ATTEMPT": "2"}), self.assertRaises(ValueError):
            asyncio.run(silver_fix.run())
        with (patch.dict("os.environ", {"GITHUB_RUN_ATTEMPT": "1"}),
              patch.object(silver_fix, "PRIVATE_TEST_COMPLETED", True), self.assertRaises(ValueError)):
            asyncio.run(silver_fix.run())

    def test_completed_private_test_is_locked_against_repeat(self):
        with (patch.object(private, "prepare", side_effect=AssertionError("unexpected discovery")),
              self.assertRaises(ValueError)):
            asyncio.run(private.main(prepare_only=False))

    def test_invalid_webhook_stops_before_any_send(self):
        with (patch.object(private.delivery, "send_candidate_once", side_effect=AssertionError("unexpected post")),
              self.assertRaises(ValueError)):
            asyncio.run(private.send_prepared(None, [], "https://example.org"))

    def test_prepare_skips_previously_sent_and_requires_multipage_endfield(self):
        data = {
            "nte": [{"id": "252469", "title": "Previously sent", "date": "2024-08-30",
                     "url": "https://nte.perfectworld.com/en/article/news/gamenews/20240830/252469.html"},
                    {"id": "252475", "title": "New", "date": "2024-08-31",
                     "url": "https://nte.perfectworld.com/en/article/news/gamenews/20240830/252475.html"}],
            "endfield": [{"id": "100", "title": "One page", "ts": 1700000000},
                         {"id": "101", "title": "Two pages", "ts": 1700000010}],
            "silverpalace": [{"id": "1", "title": "Previously sent", "date": "2025-01-01"},
                             {"id": "2", "title": "New", "date": "2025-01-02"}],
        }

        async def discover(fetcher, source):
            return data[source]

        async def render(fetcher, settings, source, row):
            return [card()] * (2 if row["id"] == "101" else 1)

        with (patch.object(private.delivery, "discover", discover), patch.object(private.delivery, "render_candidate", render),
              contextlib.redirect_stdout(io.StringIO())):
            chosen = asyncio.run(private.prepare(None, load_settings({})))
        self.assertEqual([row["id"] for _, row, _ in chosen], ["252475", "101", "2"])


class NewsAnimationPlanTests(unittest.TestCase):
    def test_pack_preserves_layout_and_splits_only_at_budget(self):
        from gamexpress.cards import IS_COMPONENTS_V2, container, gallery, text, validate_payload
        from gamexpress.news_animation import split_pages

        a, b, static = "https://s.elementagames.com/a.jpg", "https://s.elementagames.com/b.jpg", "https://s.elementagames.com/c.jpg"
        first, second = "animation-" + "a" * 64 + ".gif", "animation-" + "b" * 64 + ".gif"
        original = [{"flags": IS_COMPONENTS_V2, "allowed_mentions": {"parse": []}, "components": [container([
            text("Header and first text"), gallery([a]), text("Middle"), gallery([static, b]), text("Footer")])]}]
        snapshot = copy.deepcopy(original)
        files = {first: b"a" * 100, second: b"b" * 100}
        with patch("gamexpress.news_animation.MESSAGE_FILE_BUDGET", 200):
            packed = split_pages(original, {a: first, b: second}, files, max_pages=10)
        self.assertEqual(len(packed), 1)
        self.assertEqual([x["filename"] for x in packed[0]["attachments"]], [first, second])
        self.assertEqual([len(c["items"]) for c in packed[0]["components"][0]["components"] if c["type"] == 12], [1, 2])
        with patch("gamexpress.news_animation.MESSAGE_FILE_BUDGET", 199):
            planned = split_pages(original, {a: first, b: second}, files, max_pages=10)
            with self.assertRaises(ValueError):
                split_pages(original, {a: first, b: second}, files, max_pages=1)
        self.assertEqual(original, snapshot)
        self.assertEqual(len(planned), 2)
        children = [c for p in planned for box in p["components"] for c in box["components"]]
        self.assertEqual([c["content"] for c in children if c["type"] == 10], ["Header and first text", "Middle", "Footer"])
        media = [entry["media"]["url"] for c in children if c["type"] == 12 for entry in c["items"]]
        self.assertEqual(media, ["attachment://" + first, static, "attachment://" + second])
        self.assertTrue(all(len(p["attachments"]) == 1 and not validate_payload(p) for p in planned))
        self.assertEqual(planned[1]["components"][0]["components"][0]["content"], "Middle")
        self.assertIs(split_pages(original, {}, {}, max_pages=1), original)

    def test_attachment_count_and_duplicate_bytes_are_bounded(self):
        from gamexpress.cards import IS_COMPONENTS_V2, container, gallery, text
        from gamexpress.news_animation import split_pages

        urls = [f"https://s.elementagames.com/{i}.jpg" for i in range(11)]
        names = [f"animation-{i:064x}.gif" for i in range(11)]
        files = {name: b"x" for name in names}
        children = [item for i, url in enumerate(urls) for item in (text(f"Heading {i}"), gallery([url]))]
        original = [{"flags": IS_COMPONENTS_V2, "components": [container(children)]}]
        pages = split_pages(original, dict(zip(urls, names, strict=True)), files, max_pages=3)
        self.assertEqual([len(p["attachments"]) for p in pages], [10, 1])
        self.assertEqual(pages[1]["components"][0]["components"][0]["content"], "Heading 10")
        # Two different URLs with identical converted bytes share one upload on a page.
        same = [{"flags": IS_COMPONENTS_V2, "components": [container([gallery(urls[:2])])]}]
        page = split_pages(same, {urls[0]: names[0], urls[1]: names[0]}, files, max_pages=1)[0]
        self.assertEqual(len(page["attachments"]), 1)
        self.assertEqual(len(page["components"][0]["components"][0]["items"]), 2)

    def test_multi_file_multipart_validates_ids_hashes_and_references(self):
        import base64
        import hashlib

        from gamexpress.cards import IS_COMPONENTS_V2, container, gallery
        from gamexpress.news_animation import multipart_form

        raw = base64.b64decode("R0lGODlhBAAEAIEAAP8AAAAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQACgAAACwAAAAABAAEAAAICQABCBxIsCCAgAAh+QQBCgABACwAAAAABAAEAIEAAP8AAAAAAAAAAAAICQABCBxIsCCAgAA7")
        other = raw.replace(b"NETSCAPE2.0", b"ANIMEXTS1.0")
        names = ["animation-" + hashlib.sha256(x).hexdigest() + ".gif" for x in (raw, other)]
        payload = {"flags": IS_COMPONENTS_V2, "components": [container([gallery([])])],
                   "attachments": [{"id": i, "filename": name} for i, name in enumerate(names)]}
        payload["components"][0]["components"][0]["items"] = [{"media": {"url": "attachment://" + name}} for name in names]
        files = dict(zip(names, (raw, other), strict=True))
        multipart_form(payload, files)
        bad = copy.deepcopy(payload)
        bad["attachments"][1]["id"] = 0
        with self.assertRaises(ValueError):
            multipart_form(bad, files)
        with self.assertRaises(ValueError):
            multipart_form(payload, {names[0]: raw})
        with (patch("gamexpress.news_animation.MESSAGE_FILE_BUDGET", len(raw) + len(other) - 1),
              self.assertRaises(ValueError)):
            multipart_form(payload, files)

    def test_mislabelled_gif_is_attached_while_regular_gif_is_unchanged(self):
        import base64

        from gamexpress.cards import IS_COMPONENTS_V2, container, gallery
        from gamexpress.news_animation import prepare

        data = base64.b64decode("R0lGODlhBAAEAIEAAP8AAAAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQACgAAACwAAAAABAAEAAAICQABCBxIsCCAgAAh+QQBCgABACwAAAAABAAEAIEAAP8AAAAAAAAAAAAICQABCBxIsCCAgAA7")
        bad = "https://web-static.hg-cdn.com/image.jpg"
        good = "https://ntevmg.perfectworld.com/image.gif"
        pages = [{"flags": IS_COMPONENTS_V2, "components": [container([gallery([bad, good])])]}]

        class Fetcher:
            async def _news_media_prefix(self, url):
                return data[:32], "image/jpeg" if url == bad else "image/gif"

        async def fetch(fetcher, url):
            self.assertEqual(url, bad)
            return data

        with patch("gamexpress.news_animation.fetch_gif", fetch):
            result, files = asyncio.run(prepare(Fetcher(), pages))
        names = list(files)
        self.assertEqual(len(names), 1)
        self.assertEqual(files[names[0]], data)
        urls = [entry["media"]["url"] for p in result for c in p["components"][0]["components"]
                for entry in c.get("items", [])]
        self.assertEqual(urls, ["attachment://" + names[0], good])
        self.assertEqual(pages[0]["components"][0]["components"][0]["items"][0]["media"]["url"], bad)

    def test_conversion_never_upscales_small_source(self):
        import subprocess
        import tempfile

        import imageio_ffmpeg

        from gamexpress.news_animation import convert_gif, gif_info

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.gif"
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x180:rate=20:duration=1", "-loop", "0", "-y", str(path)],
                           check=True, timeout=30, capture_output=True)
            raw = path.read_bytes()
            with patch("gamexpress.news_animation.UPLOAD_LIMIT", int(len(raw) * 0.9)):
                result = convert_gif(raw)
            width, height, frames, duration, loop = gif_info(result, with_duration=True, with_loop=True)
            self.assertEqual((width, height), (320, 180))
            self.assertGreater(frames, 1)
            self.assertEqual((duration, loop), (100, 0))
            self.assertLess(len(result), len(raw))

    def test_allowlist_rejects_spoofed_and_non_https_hosts(self):
        from gamexpress.news_animation import trusted_url

        self.assertTrue(trusted_url("https://s.elementagames.com/a.jpg"))
        self.assertTrue(trusted_url("https://upload-os-bbs.hoyolab.com/a.jpg"))
        for url in ("http://s.elementagames.com/x", "https://s.elementagames.com.evil.test/x",
                    "https://evil@s.elementagames.com/x", "https://s.elementagames.com:444/x",
                    "https://s.elementagames.com/x#fragment"):
            self.assertFalse(trusted_url(url))

class SilverGifProbeTests(unittest.TestCase):
    def test_parser_counts_frames_and_rejects_incomplete_data(self):
        import base64

        from silver_animation_upload_probe import gif_info

        raw = base64.b64decode(
            "R0lGODlhBAAEAIEAAP8AAAAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQACgAAACwAAAAABAAEAAAICQABCBxIsCCAgAAh+QQBCgABACwAAAAABAAEAIEAAP8AAAAAAAAAAAAICQABCBxIsCCAgAA7"
        )
        self.assertEqual(gif_info(raw), (4, 4, 2))
        from gamexpress.news_animation import gif_info as runtime_gif_info

        self.assertEqual(runtime_gif_info(raw, with_duration=True, with_loop=True), (4, 4, 2, 20, 0))
        for invalid in (b"", b"GIF89a", raw[:-1], raw + b"extra", b"JPEG" + raw):
            with self.assertRaises(ValueError):
                gif_info(invalid)
        altered = bytearray(raw)
        altered[6:8] = (1921).to_bytes(2, "little")
        with self.assertRaises(ValueError):
            gif_info(bytes(altered))


if __name__ == "__main__":
    unittest.main()
