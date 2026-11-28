"""Offline state and routing tests; no real webhook, persistence or source access."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gamexpress import candidate_news_catchup as catchup  # noqa: E402
from gamexpress import candidate_news_delivery as delivery  # noqa: E402
from gamexpress import candidate_news_recovery as recovery  # noqa: E402
from gamexpress import candidate_news_runner as runner  # noqa: E402
from gamexpress.candidate_news_checkpoint import GitCheckpoint, IsolatedGitLedger, reconcile_checkpointed  # noqa: E402
from gamexpress.cards import news_payloads  # noqa: E402
from gamexpress.config import Game, load_settings  # noqa: E402
from gamexpress.models import Item  # noqa: E402
from gamexpress.state import State  # noqa: E402

ROUTE = "a" * 16
NOW = 1795824000
HOOK = "https://discord.com/api/webhooks/123456789012345678/example_token_123456789"
INITIAL_ROUTES = {"nte": "a" * 16, "endfield": "b" * 16, "silverpalace": "c" * 16}


def empty_record(source, route):
    return {"version": 1, "source": source, "route": route, "pending": [], "posted": {}, "partial": None}


def initialized_ledger(routes):
    return {"schema": 1, "candidate_news": {source: empty_record(source, route) for source, route in routes.items()}}


def row(cid, date="2026-11-27"):
    return {"id": cid, "title": "Official " + cid, "date": date}


def payload(cid):
    game = Game(key="silverpalace", name="Silver Palace", short="Silver", enabled=False, color=0)
    item = Item(source="silverpalace", game="silverpalace", id=cid, title="Official " + cid,
                url="https://silverpalace.elementagames.com/en-us/newsDetail?id=" + cid,
                text="", published_ts=0, article_blocks=[{"kind": "text", "text": "Hello"}])
    return news_payloads(game, item)[0]


def test_long_comma_list_splits_without_loss_or_markdown_corruption():
    from gamexpress.cards import _split_expanded_text

    content = (r"171137\*\*\*\*\*\*0311553、" * 5000) + "end"
    pieces = _split_expanded_text(content, 3000)
    assert pieces and len(pieces) > 12
    assert "".join(pieces) == content
    assert all(len(piece) <= 3000 for piece in pieces)
    assert _split_expanded_text("a" * 5000, 100) is None
    assert _split_expanded_text("[" + content + "](https://example.test/)", 100) is None
    assert _split_expanded_text("**" + content + "**", 100) is None


class Storage:
    def __init__(self, fail_at=0):
        self.states, self.calls, self.fail_at = [], 0, fail_at

    async def checkpoint(self, state):
        self.calls += 1
        if self.calls == self.fail_at:
            return False
        self.states.append(copy.deepcopy(state))
        return True


class CandidateDeliveryTests(unittest.TestCase):
    def test_git_checkpoint_persists_to_same_remote_branch_and_rejects_unpushed_work(self):
        branch = "arena/98b5f4e1-game-express"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work"
            remote = Path(tmp) / "remote.git"
            root.mkdir()

            def git(*args, cwd=root):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)

            git("init", "--bare", str(remote))
            git("init", "-b", branch)
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            (root / "state").mkdir()
            path = root / "state/state.json"
            path.write_text('{"schema":1}\n', encoding="utf-8")
            git("add", "state/state.json")
            git("commit", "-m", "initial")
            git("remote", "add", "origin", str(remote))
            git("push", "origin", "HEAD:refs/heads/" + branch)
            state = State.load(path)
            data = delivery.ingest("silverpalace", ROUTE, None, [row("1")])
            checkpoint = GitCheckpoint(root, state, "silverpalace", branch)
            self.assertTrue(asyncio.run(checkpoint(data)))
            raw = git("--git-dir", str(remote), "show", "refs/heads/" + branch + ":state/state.json").stdout
            self.assertEqual(json.loads(raw)["candidate_news"]["silverpalace"], data)
            self.assertTrue(asyncio.run(checkpoint(data)))  # identical state is still remote-confirmed

            async def render(article):
                return [payload(article["id"])]

            async def send(page):
                saved = git("--git-dir", str(remote), "show", "refs/heads/" + branch + ":state/state.json").stdout
                record = json.loads(saved)["candidate_news"]["silverpalace"]
                self.assertTrue(record["partial"]["inflight"])
                return SimpleNamespace(ok=True, message_id="101", ambiguous=False)

            posted = asyncio.run(delivery.deliver_oldest(data, "silverpalace", ROUTE, as_of=NOW,
                                                        checkpoint=checkpoint, render=render, send=send))
            raw = git("--git-dir", str(remote), "show", "refs/heads/" + branch + ":state/state.json").stdout
            self.assertEqual(json.loads(raw)["candidate_news"]["silverpalace"], posted)
            self.assertEqual(posted["posted"]["1"]["messages"], ["101"])
            self.assertFalse(asyncio.run(GitCheckpoint(root, state, "silverpalace", "other")(posted)))
            # A lost or modified local candidate record is not an empty first run.
            lost_state = State(path, {"schema": 1})
            self.assertFalse(asyncio.run(GitCheckpoint(root, lost_state, "silverpalace", branch)(data)))
            self.assertEqual(json.loads(git("--git-dir", str(remote), "show", "refs/heads/" + branch + ":state/state.json").stdout)
                             ["candidate_news"]["silverpalace"], posted)
            (root / "unrelated.txt").write_text("keep", encoding="utf-8")
            git("add", "unrelated.txt")
            self.assertFalse(asyncio.run(checkpoint(posted)))  # never commit unrelated staged files
            self.assertEqual(git("diff", "--cached", "--name-only").stdout.strip(), b"unrelated.txt")

    def test_initialize_ledger_is_first_install_only_and_does_not_touch_checkout(self):
        branch = "arena/98b5f4e1-game-express"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            remote, seed = base / "remote.git", base / "seed"
            seed.mkdir()

            def git(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout

            git(seed, "init", "--bare", str(remote))
            git(seed, "init", "-b", branch)
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            (seed / "state").mkdir()
            (seed / "state/state.json").write_text('{"live":"untouched"}\n', encoding="utf-8")
            git(seed, "add", "state")
            git(seed, "commit", "-m", "seed")
            git(seed, "remote", "add", "origin", str(remote))
            git(seed, "push", "origin", f"HEAD:refs/heads/{branch}")
            stale = base / "stale"
            git(base, "clone", "-b", branch, str(remote), str(stale))
            head = git(seed, "rev-parse", "HEAD")
            (seed / "local-uncommitted.txt").write_text("preserved", encoding="utf-8")
            for bad in ({}, {**INITIAL_ROUTES, "nte": INITIAL_ROUTES["endfield"]},
                        {**INITIAL_ROUTES, "nte": "not-a-fingerprint"}):
                self.assertFalse(asyncio.run(IsolatedGitLedger(seed, branch).initialize_empty(bad)))
            self.assertEqual(git(seed, "rev-parse", "HEAD"), head)
            self.assertTrue(asyncio.run(IsolatedGitLedger(seed, branch).initialize_empty(INITIAL_ROUTES)))
            self.assertEqual(git(seed, "rev-parse", "HEAD"), head)
            self.assertFalse((seed / "state/candidate_news.json").exists())
            self.assertEqual((seed / "state/state.json").read_text(), '{"live":"untouched"}\n')
            blob = git(seed, "--git-dir", str(remote), "show", f"refs/heads/{branch}:state/candidate_news.json")
            self.assertEqual(json.loads(blob), initialized_ledger(INITIAL_ROUTES))
            writer = IsolatedGitLedger(stale, branch)
            self.assertFalse(asyncio.run(writer.initialize_empty(INITIAL_ROUTES)))
            self.assertTrue(writer.poisoned)
            git(seed, "pull", "--ff-only", "origin", branch)
            self.assertFalse(asyncio.run(IsolatedGitLedger(seed, branch).initialize_empty(INITIAL_ROUTES)))
            git(seed, "rm", "state/candidate_news.json")
            git(seed, "commit", "-m", "simulate missing ledger")
            git(seed, "push", "origin", f"HEAD:refs/heads/{branch}")
            self.assertFalse(asyncio.run(IsolatedGitLedger(seed, branch).initialize_empty(INITIAL_ROUTES)))
            self.assertEqual(git(seed, "worktree", "list", "--porcelain").count(b"worktree "), 1)
            self.assertEqual((seed / "local-uncommitted.txt").read_text(), "preserved")

    def test_simultaneous_initializers_have_one_winner_and_shallow_checkout_holds(self):
        branch = "arena/98b5f4e1-game-express"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            remote, seed = base / "remote.git", base / "seed"
            seed.mkdir()

            def git(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout

            git(seed, "init", "--bare", str(remote))
            git(seed, "init", "-b", branch)
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            (seed / "state").mkdir()
            (seed / "state/state.json").write_text('{}\n', encoding="utf-8")
            git(seed, "add", "state")
            git(seed, "commit", "-m", "seed")
            git(seed, "remote", "add", "origin", str(remote))
            git(seed, "push", "origin", f"HEAD:refs/heads/{branch}")
            a, b, shallow = base / "a", base / "b", base / "shallow"
            git(base, "clone", "-b", branch, str(remote), str(a))
            git(base, "clone", "-b", branch, str(remote), str(b))
            git(base, "clone", "--depth=1", "-b", branch, remote.as_uri(), str(shallow))
            self.assertFalse(asyncio.run(IsolatedGitLedger(shallow, branch).initialize_empty(INITIAL_ROUTES)))

            async def initialize_both():
                return await asyncio.gather(IsolatedGitLedger(a, branch).initialize_empty(INITIAL_ROUTES),
                                            IsolatedGitLedger(b, branch).initialize_empty(INITIAL_ROUTES))

            results = asyncio.run(initialize_both())
            self.assertEqual(sorted(results), [False, True])
            blob = git(seed, "--git-dir", str(remote), "show", f"refs/heads/{branch}:state/candidate_news.json")
            self.assertEqual(json.loads(blob), initialized_ledger(INITIAL_ROUTES))

    def test_uncertain_initialization_push_holds_and_cannot_reinitialize(self):
        branch = "arena/98b5f4e1-game-express"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            remote, seed = base / "remote.git", base / "seed"
            seed.mkdir()

            def git(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout

            git(seed, "init", "--bare", str(remote))
            git(seed, "init", "-b", branch)
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            (seed / "state").mkdir()
            (seed / "state/state.json").write_text('{}\n', encoding="utf-8")
            git(seed, "add", "state")
            git(seed, "commit", "-m", "seed")
            git(seed, "remote", "add", "origin", str(remote))
            git(seed, "push", "origin", f"HEAD:refs/heads/{branch}")
            ledger = IsolatedGitLedger(seed, branch)
            original_git = ledger._git

            def uncertain_push(cwd, *args):
                result = original_git(cwd, *args)
                if args and args[0] == "push" and result.returncode == 0:
                    return subprocess.CompletedProcess(result.args, 1, b"", b"ambiguous")
                return result

            with patch.object(ledger, "_git", side_effect=uncertain_push):
                self.assertFalse(asyncio.run(ledger.initialize_empty(INITIAL_ROUTES)))
            self.assertTrue(ledger.poisoned)
            self.assertFalse(asyncio.run(ledger.initialize_empty(INITIAL_ROUTES)))
            blob = git(seed, "--git-dir", str(remote), "show", f"refs/heads/{branch}:state/candidate_news.json")
            self.assertEqual(json.loads(blob), initialized_ledger(INITIAL_ROUTES))
            git(seed, "pull", "--ff-only", "origin", branch)
            self.assertFalse(asyncio.run(IsolatedGitLedger(seed, branch).initialize_empty(INITIAL_ROUTES)))

    def test_initialize_requires_explicit_attestations_and_distinct_destinations(self):
        class FakeLedger:
            calls = 0

            def __init__(self, root, branch):
                pass

            async def initialize_empty(self, routes):
                assert routes == {source: target[1] for source, target in destinations.items()}
                FakeLedger.calls += 1
                return True

        env = {"RECOVERY_MODE": "initialize", "RECOVERY_WRITERS_PAUSED": "true",
               "RECOVERY_INIT_CONFIRM": "FIRST_INSTALL_BACKFILL"}
        destinations = {"nte": (HOOK, "a" * 16), "endfield": (HOOK, "b" * 16),
                        "silverpalace": (HOOK, "c" * 16)}
        out = io.StringIO()
        with (patch.dict("os.environ", env, clear=True), patch.object(recovery, "load_settings", return_value=object()),
              patch.object(recovery, "authoritative_branch", return_value="main"),
              patch.object(recovery, "destination", side_effect=lambda settings, source: destinations[source]),
              patch.object(recovery, "IsolatedGitLedger", FakeLedger), contextlib.redirect_stdout(out)):
            asyncio.run(recovery.run_from_environment())
            self.assertEqual(FakeLedger.calls, 1)
            self.assertNotIn(HOOK, out.getvalue())
            for key in ("RECOVERY_WRITERS_PAUSED", "RECOVERY_INIT_CONFIRM"):
                with patch.dict("os.environ", {key: "false"}), self.assertRaises(delivery.DeliveryHold):
                    asyncio.run(recovery.run_from_environment())
            destinations["nte"] = destinations["endfield"]
            with self.assertRaises(delivery.DeliveryHold):
                asyncio.run(recovery.run_from_environment())
            destinations["nte"] = None
            with self.assertRaises(delivery.DeliveryHold):
                asyncio.run(recovery.run_from_environment())
            self.assertEqual(FakeLedger.calls, 1)

    def test_isolated_ledger_holds_stale_writer_and_leaves_monitor_checkout_alone(self):
        branch = "arena/98b5f4e1-game-express"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            remote, seed = base / "remote.git", base / "seed"
            seed.mkdir()

            def git(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout

            git(seed, "init", "--bare", str(remote))
            git(seed, "init", "-b", branch)
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            (seed / "state").mkdir()
            (seed / "state/state.json").write_text('{"schema":1}\n', encoding="utf-8")
            (seed / "state/candidate_news.json").write_text('{"schema":1,"candidate_news":{}}\n', encoding="utf-8")
            git(seed, "add", "state")
            git(seed, "commit", "-m", "initial")
            git(seed, "remote", "add", "origin", str(remote))
            git(seed, "push", "origin", "HEAD:refs/heads/" + branch)
            other = base / "other"
            git(base, "clone", "-b", branch, str(remote), str(other))
            original = git(seed, "rev-parse", "HEAD")
            first, stale = IsolatedGitLedger(seed, branch), IsolatedGitLedger(other, branch)
            self.assertTrue(asyncio.run(first.open()))
            self.assertTrue(asyncio.run(stale.open()))
            try:
                queued = delivery.ingest("silverpalace", ROUTE, None, [row("1")])
                self.assertTrue(asyncio.run(first.checkpoint("silverpalace", queued)))
                self.assertFalse(asyncio.run(stale.checkpoint("silverpalace", queued)))
                self.assertTrue(stale.poisoned)
                self.assertFalse(asyncio.run(stale.checkpoint("silverpalace", queued)))
                self.assertEqual(git(seed, "rev-parse", "HEAD"), original)
                self.assertEqual(git(seed, "status", "--porcelain"), b"")
                self.assertEqual(json.loads((seed / "state/candidate_news.json").read_text())["candidate_news"], {})
                # The existing final monitor-state commit can rebase over the separate ledger.
                (seed / "state/state.json").write_text('{"schema":1,"existing":"preserved"}\n', encoding="utf-8")
                git(seed, "add", "state/state.json")
                git(seed, "commit", "-m", "monitor state")
                git(seed, "pull", "--rebase", "origin", branch)
                git(seed, "push", "origin", f"HEAD:refs/heads/{branch}")
                persisted = json.loads(git(seed, "--git-dir", str(remote), "show",
                                           f"refs/heads/{branch}:state/candidate_news.json"))
                self.assertEqual(persisted["candidate_news"]["silverpalace"], queued)
                self.assertFalse(asyncio.run(first.checkpoint("silverpalace", queued)))
                self.assertTrue(first.poisoned)
            finally:
                asyncio.run(first.close())
                asyncio.run(stale.close())
            self.assertEqual(git(seed, "worktree", "list", "--porcelain").count(b"worktree "), 1)

    def test_simultaneous_identical_git_checkpoints_cannot_both_win(self):
        branch = "arena/98b5f4e1-game-express"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            remote, seed = base / "remote.git", base / "seed"
            seed.mkdir()

            def git(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout

            git(seed, "init", "--bare", str(remote))
            git(seed, "init", "-b", branch)
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            (seed / "state").mkdir()
            (seed / "state/candidate_news.json").write_text('{"schema":1,"candidate_news":{}}\n', encoding="utf-8")
            git(seed, "add", "state")
            git(seed, "commit", "-m", "initial")
            git(seed, "remote", "add", "origin", str(remote))
            git(seed, "push", "origin", "HEAD:refs/heads/" + branch)
            other = base / "other"
            git(base, "clone", "-b", branch, str(remote), str(other))
            first, second = IsolatedGitLedger(seed, branch), IsolatedGitLedger(other, branch)
            self.assertTrue(asyncio.run(first.open()))
            self.assertTrue(asyncio.run(second.open()))
            barrier = threading.Barrier(2, timeout=20)
            for ledger in (first, second):
                original = ledger._git

                def coordinated(root, *args, _original=original):
                    if args and args[0] == "push":
                        barrier.wait()
                    return _original(root, *args)

                ledger._git = coordinated
            queued = delivery.ingest("silverpalace", ROUTE, None, [row("1")])

            async def compete():
                return await asyncio.gather(first.checkpoint("silverpalace", queued),
                                            second.checkpoint("silverpalace", queued))

            # A fixed clock reproduces the dangerous identical-tree, identical-parent,
            # identical-author/timestamp case: independent checkpoints need unique IDs.
            try:
                with patch.dict("os.environ", {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
                                               "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000"}):
                    results = asyncio.run(compete())
                self.assertEqual(sorted(results), [False, True])
                self.assertNotEqual(first._head(first.worktree), second._head(second.worktree))
                self.assertEqual(sum(ledger.poisoned for ledger in (first, second)), 1)
                saved = json.loads(git(seed, "--git-dir", str(remote), "show",
                                       f"refs/heads/{branch}:state/candidate_news.json"))
                self.assertEqual(saved["candidate_news"]["silverpalace"], queued)
            finally:
                asyncio.run(first.close())
                asyncio.run(second.close())

    def test_competing_delivery_coordinators_only_one_posts(self):
        branch = "arena/98b5f4e1-game-express"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            remote, seed = base / "remote.git", base / "seed"
            seed.mkdir()

            def git(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout

            git(seed, "init", "--bare", str(remote))
            git(seed, "init", "-b", branch)
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            queued = delivery.ingest("silverpalace", ROUTE, None, [row("1")])
            (seed / "state").mkdir()
            (seed / "state/candidate_news.json").write_text(
                json.dumps({"schema": 1, "candidate_news": {"silverpalace": queued}}), encoding="utf-8")
            git(seed, "add", "state")
            git(seed, "commit", "-m", "initial")
            git(seed, "remote", "add", "origin", str(remote))
            git(seed, "push", "origin", "HEAD:refs/heads/" + branch)
            other = base / "other"
            git(base, "clone", "-b", branch, str(remote), str(other))
            ledgers = [IsolatedGitLedger(seed, branch), IsolatedGitLedger(other, branch)]
            for ledger in ledgers:
                self.assertTrue(asyncio.run(ledger.open()))
            barrier = threading.Barrier(2, timeout=20)
            for ledger in ledgers:
                original = ledger._git
                paused = [False]

                def coordinated(root, *args, _original=original, _paused=paused):
                    if args and args[0] == "push" and not _paused[0]:
                        _paused[0] = True
                        barrier.wait()
                    return _original(root, *args)

                ledger._git = coordinated
            sends = []

            async def render(candidate):
                return [payload(candidate["id"])]

            async def send(card):
                sends.append(card)
                return SimpleNamespace(ok=True, ambiguous=False, message_id="101")

            async def compete():
                async def run_one(ledger):
                    async def checkpoint(state):
                        return await ledger.checkpoint("silverpalace", state)
                    return await delivery.deliver_oldest(queued, "silverpalace", ROUTE, as_of=NOW,
                                                        checkpoint=checkpoint, render=render, send=send)
                return await asyncio.gather(*(run_one(ledger) for ledger in ledgers), return_exceptions=True)

            try:
                with patch.dict("os.environ", {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
                                               "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000"}):
                    results = asyncio.run(compete())
                self.assertEqual(len(sends), 1)
                self.assertEqual(sum(isinstance(x, delivery.DeliveryHold) for x in results), 1)
                saved = json.loads(git(seed, "--git-dir", str(remote), "show",
                                       f"refs/heads/{branch}:state/candidate_news.json"))
                self.assertEqual(saved["candidate_news"]["silverpalace"]["posted"]["1"]["messages"], ["101"])
            finally:
                for ledger in ledgers:
                    asyncio.run(ledger.close())

    def test_isolated_ledger_requires_existing_tracked_valid_file(self):
        branch = "arena/98b5f4e1-game-express"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work"
            remote = Path(tmp) / "remote.git"
            root.mkdir()

            def git(*args):
                return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout

            git("init", "--bare", str(remote))
            git("init", "-b", branch)
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            (root / "state").mkdir()
            (root / "state/state.json").write_text('{"schema":1}\n', encoding="utf-8")
            git("add", "state")
            git("commit", "-m", "initial")
            git("remote", "add", "origin", str(remote))
            git("push", "origin", f"HEAD:refs/heads/{branch}")
            ledger = IsolatedGitLedger(root, branch)
            self.assertFalse(asyncio.run(ledger.open()))  # no ledger is not a fresh empty ledger
            self.assertTrue(ledger.poisoned)
            (root / "state/candidate_news.json").write_text('{"schema":true,"candidate_news":{}}\n', encoding="utf-8")
            git("add", "state/candidate_news.json")
            git("commit", "-m", "malformed ledger")
            git("push", "origin", f"HEAD:refs/heads/{branch}")
            ledger = IsolatedGitLedger(root, branch)
            self.assertFalse(asyncio.run(ledger.open()))
            self.assertTrue(ledger.poisoned)
            self.assertIsNone(ledger.data)

    def test_reconciliation_is_durably_checkpointed_without_resending(self):
        branch = "arena/98b5f4e1-game-express"
        with tempfile.TemporaryDirectory() as tmp:
            root, remote = Path(tmp) / "work", Path(tmp) / "remote.git"
            root.mkdir()

            def git(*args):
                return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout

            git("init", "--bare", str(remote))
            git("init", "-b", branch)
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            queued = delivery.ingest("silverpalace", ROUTE, None, [row("1")])
            queued["partial"] = {"id": "1", "signature": "b" * 64, "pages": 1, "messages": [], "inflight": True}
            (root / "state").mkdir()
            (root / "state/candidate_news.json").write_text(
                json.dumps({"schema": 1, "candidate_news": {"silverpalace": queued}}), encoding="utf-8")
            git("add", "state/candidate_news.json")
            git("commit", "-m", "held page")
            git("remote", "add", "origin", str(remote))
            git("push", "origin", f"HEAD:refs/heads/{branch}")
            ledger = IsolatedGitLedger(root, branch)
            self.assertTrue(asyncio.run(ledger.open()))
            try:
                with self.assertRaises(delivery.DeliveryHold):
                    asyncio.run(reconcile_checkpointed(ledger, "silverpalace", ROUTE, article_id="1", page=2,
                                                       signature="b" * 64, resolution="delivered", message_id="101"))
                self.assertTrue(ledger.data["candidate_news"]["silverpalace"]["partial"]["inflight"])
                before = git("--git-dir", str(remote), "rev-parse", f"refs/heads/{branch}")
                summary = recovery.inspect_record(ledger, "silverpalace", ROUTE)
                self.assertEqual((summary["article_id"], summary["held_page"], summary["inflight"]), ("1", 1, True))
                self.assertEqual(summary["signature"], "b" * 64)
                for paused, evidence in ((False, False), (True, False), (False, True)):
                    with self.assertRaises(delivery.DeliveryHold):
                        asyncio.run(recovery.reviewed_resolution(ledger, "silverpalace", ROUTE, article_id="1", page=1,
                            signature="b" * 64, resolution="delivered", message_id="101",
                            writers_paused=paused, evidence_verified=evidence))
                self.assertEqual(git("--git-dir", str(remote), "rev-parse", f"refs/heads/{branch}"), before)
                with self.assertRaises(delivery.DeliveryHold):
                    recovery.inspect_record(ledger, "silverpalace", "c" * 16)
                result = asyncio.run(recovery.reviewed_resolution(ledger, "silverpalace", ROUTE, article_id="1", page=1,
                                                           signature="b" * 64, resolution="delivered", message_id="101",
                                                           writers_paused=True, evidence_verified=True))
                self.assertFalse(result["partial"]["inflight"])
                saved = json.loads(git("--git-dir", str(remote), "show", f"refs/heads/{branch}:state/candidate_news.json"))
                self.assertEqual(saved["candidate_news"]["silverpalace"], result)
                self.assertEqual(result["partial"]["messages"], ["101"])
            finally:
                asyncio.run(ledger.close())

    def test_preview_reads_renders_and_holds_without_checkpoint_or_send(self):
        import hashlib

        initial = empty_record("silverpalace", ROUTE)
        data = {"schema": 1, "candidate_news": {"silverpalace": initial}}
        ledger = SimpleNamespace(data=data, poisoned=False)
        original = copy.deepcopy(data)
        settings = load_settings({})
        calls = []
        async def discover(fetcher, source):
            calls.append("discover")
            return [row("2"), row("1")]
        async def render(fetcher, conf, source, candidate):
            calls.append("render")
            self.assertEqual(candidate["id"], "1")
            return [payload("1"), payload("1")], {"animation.gif": b"synthetic"}
        with (patch.object(delivery, "discover", discover), patch.object(delivery, "render_candidate_animated", render)):
            result = asyncio.run(recovery.preview_record(ledger, settings, "silverpalace", ROUTE, None, as_of=NOW))
            self.assertEqual(result, {"source": "silverpalace", "pending": 2, "posted": 0, "sends": 0,
                "checkpoints": 0, "article_id": "1", "pages": 2, "confirmed_pages": 0,
                "pages_next_run_limit": 2, "gif_attachments": 1})
            self.assertEqual(data, original)
            self.assertEqual(calls, ["discover", "render"])
            pages = [payload("1"), payload("1")]
            signature = hashlib.sha256(json.dumps(pages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            partial = delivery.ingest("silverpalace", ROUTE, initial, [row("1")])
            partial["partial"] = {"id": "1", "signature": signature, "pages": 2, "messages": ["101"], "inflight": False}
            ledger.data = {"candidate_news": {"silverpalace": partial}}
            result = asyncio.run(recovery.preview_record(ledger, settings, "silverpalace", ROUTE, None, as_of=NOW))
            self.assertEqual(result["confirmed_pages"], 1)
            self.assertEqual(result["pages_next_run_limit"], 1)
            partial["partial"]["signature"] = "f" * 64
            with self.assertRaises(delivery.DeliveryHold):
                asyncio.run(recovery.preview_record(ledger, settings, "silverpalace", ROUTE, None, as_of=NOW))
            partial["partial"]["inflight"] = True
            with patch.object(delivery, "discover", side_effect=AssertionError("held page must not fetch")), self.assertRaises(delivery.DeliveryHold):
                asyncio.run(recovery.preview_record(ledger, settings, "silverpalace", ROUTE, None, as_of=NOW))
            with patch.object(delivery, "discover", side_effect=AssertionError("route mismatch must not fetch")), self.assertRaises(delivery.DeliveryHold):
                asyncio.run(recovery.preview_record(ledger, settings, "silverpalace", "b" * 16, None, as_of=NOW))
            ledger.data = {"candidate_news": {"silverpalace": initial}}
            async def future(fetcher, source):
                return [row("1", "2099-01-01")]
            with patch.object(delivery, "discover", future), patch.object(delivery, "render_candidate_animated", side_effect=AssertionError("future must not render")), self.assertRaises(delivery.DeliveryHold):
                asyncio.run(recovery.preview_record(ledger, settings, "silverpalace", ROUTE, None, as_of=NOW))

    def test_preview_environment_uses_validated_authority_without_send(self):
        class FakeLedger:
            def __init__(self, root, branch):
                self.closed = False
            async def open(self):
                return True
            async def close(self):
                self.closed = True
        ledger = FakeLedger(None, None)
        settings = SimpleNamespace(http_timeout=1)
        async def preview(actual, conf, source, route, fetcher, *, as_of):
            self.assertIs(actual, ledger)
            self.assertEqual((source, route), ("silverpalace", ROUTE))
            return {"sends": 0, "checkpoints": 0}
        env = {"RECOVERY_MODE": "preview", "RECOVERY_SOURCE": "silverpalace", "RECOVERY_WRITERS_PAUSED": "true"}
        out = io.StringIO()
        with (patch.dict("os.environ", env, clear=True), patch.object(recovery, "load_settings", return_value=settings),
              patch.object(recovery, "authoritative_branch", return_value="main"),
              patch.object(recovery, "destination", return_value=(HOOK, ROUTE)),
              patch.object(recovery, "IsolatedGitLedger", return_value=ledger),
              patch.object(recovery, "preview_record", preview), contextlib.redirect_stdout(out)):
            asyncio.run(recovery.run_from_environment())
            with patch.dict("os.environ", {"RECOVERY_WRITERS_PAUSED": "false"}), self.assertRaises(delivery.DeliveryHold):
                asyncio.run(recovery.run_from_environment())
        self.assertEqual(json.loads(out.getvalue()), {"sends": 0, "checkpoints": 0})
        self.assertNotIn(HOOK, out.getvalue())
        self.assertTrue(ledger.closed)

    def test_recovery_environment_requires_authority_and_never_exposes_route(self):
        state = delivery.ingest("silverpalace", ROUTE, None, [row("1")])
        state["partial"] = {"id": "1", "signature": "b" * 64, "pages": 1, "messages": [], "inflight": True}

        class FakeLedger:
            instances = []

            def __init__(self, root, branch):
                self.data, self.poisoned, self.closed = {"candidate_news": {"silverpalace": state}}, False, False
                self.instances.append(self)

            async def open(self):
                return True

            async def close(self):
                self.closed = True

        env = {"RECOVERY_MODE": "inspect", "RECOVERY_SOURCE": "silverpalace"}
        out = io.StringIO()
        with (patch.dict("os.environ", env, clear=True), patch.object(recovery, "load_settings", return_value=object()),
              patch.object(recovery, "authoritative_branch", return_value="arena/98b5f4e1-game-express"),
              patch.object(recovery, "destination", return_value=(HOOK, ROUTE)),
              patch.object(recovery, "IsolatedGitLedger", FakeLedger), contextlib.redirect_stdout(out)):
            asyncio.run(recovery.run_from_environment())
        info = json.loads(out.getvalue())
        self.assertTrue(info["inflight"])
        self.assertEqual(info["held_page"], 1)
        self.assertNotIn(HOOK, out.getvalue())
        self.assertTrue(FakeLedger.instances[-1].closed)
        with (patch.dict("os.environ", env, clear=True), patch.object(recovery, "load_settings", return_value=object()),
              patch.object(recovery, "authoritative_branch", return_value=None),
              patch.object(recovery, "destination", return_value=(HOOK, ROUTE)),
              patch.object(recovery, "IsolatedGitLedger", side_effect=AssertionError("no ledger access")),
              self.assertRaises(delivery.DeliveryHold)):
            asyncio.run(recovery.run_from_environment())

    def test_recovery_workflow_shares_monitor_concurrency_and_has_no_send_step(self):
        import yaml

        wf = ROOT / ".github/workflows"
        recovery_path = wf / "candidate_news_recovery.yml"
        if not recovery_path.exists() and not (ROOT / "tools").is_dir():
            self.skipTest("optional recovery workflow not present in this production-shaped checkout")
        monitor = yaml.safe_load((wf / "monitor.yml").read_text())
        recovery_wf = yaml.safe_load(recovery_path.read_text())
        self.assertEqual(recovery_wf["concurrency"], monitor["concurrency"])
        # GitHub workflow_dispatch accepts at most ten top-level inputs.
        self.assertLessEqual(len(recovery_wf[True]["workflow_dispatch"]["inputs"]), 10)
        checkout = recovery_wf["jobs"]["recovery"]["steps"][0]["with"]
        self.assertEqual(checkout["fetch-depth"], 0)
        self.assertFalse(checkout["persist-credentials"])
        self.assertNotIn("env", recovery_wf["jobs"]["recovery"])
        self.assertFalse(recovery_wf["concurrency"]["cancel-in-progress"])
        steps = recovery_wf["jobs"]["recovery"]["steps"]
        commands = "\n".join(str(step.get("run", "")) for step in steps)
        self.assertIn("python -m gamexpress.candidate_news_recovery", commands)
        self.assertNotIn("${{", commands)
        self.assertNotIn("curl", commands)
        self.assertNotIn("discord", commands.lower())

    def test_stale_writer_and_crash_hold_until_reviewed_git_reconciliation(self):
        branch = "arena/98b5f4e1-game-express"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            remote, seed = base / "remote.git", base / "seed"
            seed.mkdir()

            def git(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout

            git(seed, "init", "--bare", str(remote))
            git(seed, "init", "-b", branch)
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            (seed / "state").mkdir()
            (seed / "state/state.json").write_text('{"schema":1}\n', encoding="utf-8")
            git(seed, "add", "state/state.json")
            git(seed, "commit", "-m", "initial")
            git(seed, "remote", "add", "origin", str(remote))
            git(seed, "push", "origin", "HEAD:refs/heads/" + branch)
            writer_a, writer_b, restarted = base / "a", base / "b", base / "restarted"
            for dest in (writer_a, writer_b):
                git(base, "clone", "-b", branch, str(remote), str(dest))
            state_a, state_b = (State.load(p / "state/state.json") for p in (writer_a, writer_b))
            queued = delivery.ingest("silverpalace", ROUTE, None, [row("1")])
            sends = []

            async def render(article):
                return [payload(article["id"])]

            async def ambiguous(card):
                sends.append("ambiguous")
                raise TimeoutError("lost after send")

            async def unexpected(card):
                sends.append("unexpected")
                return SimpleNamespace(ok=True, message_id="999", ambiguous=False)

            with self.assertRaises(delivery.DeliveryHold):
                asyncio.run(delivery.deliver_oldest(queued, "silverpalace", ROUTE, as_of=NOW,
                            checkpoint=GitCheckpoint(writer_a, state_a, "silverpalace", branch),
                            render=render, send=ambiguous))
            # A writer with the old HEAD must not send, even though it started before the first one.
            with self.assertRaises(delivery.DeliveryHold):
                asyncio.run(delivery.deliver_oldest(queued, "silverpalace", ROUTE, as_of=NOW,
                            checkpoint=GitCheckpoint(writer_b, state_b, "silverpalace", branch),
                            render=render, send=unexpected))
            git(base, "clone", "-b", branch, str(remote), str(restarted))
            state_c = State.load(restarted / "state/state.json")
            held = state_c.data["candidate_news"]["silverpalace"]
            self.assertTrue(held["partial"]["inflight"])
            checkpoint = GitCheckpoint(restarted, state_c, "silverpalace", branch)
            with self.assertRaises(delivery.DeliveryHold):
                asyncio.run(delivery.deliver_oldest(held, "silverpalace", ROUTE, as_of=NOW,
                            checkpoint=checkpoint, render=render, send=unexpected))
            reviewed = delivery.reconcile_inflight(held, "silverpalace", ROUTE, article_id="1", page=1,
                                                   signature=held["partial"]["signature"],
                                                   resolution="delivered", message_id="101")
            self.assertTrue(asyncio.run(checkpoint(reviewed)))
            result = asyncio.run(delivery.deliver_oldest(reviewed, "silverpalace", ROUTE, as_of=NOW,
                                 checkpoint=checkpoint, render=render, send=unexpected))
            self.assertEqual(sends, ["ambiguous"])
            self.assertEqual(result["posted"]["1"]["messages"], ["101"])
            persisted = json.loads(git(base, "--git-dir", str(remote), "show", f"refs/heads/{branch}:state/state.json"))
            self.assertEqual(persisted["candidate_news"]["silverpalace"], result)

    def test_attended_catchup_runs_until_all_sources_are_empty(self):
        state = {source: {"pending": 2, "posted": 0, "partial": False} for source in catchup.SOURCES}
        calls, waits = [], []

        async def inspect():
            return copy.deepcopy(state)

        async def run_one(source):
            calls.append(source)
            state[source]["pending"] -= 1
            state[source]["posted"] += 1

        async def wait(seconds):
            waits.append(seconds)

        result = asyncio.run(catchup.catch_up(inspect, run_one, wait=wait, clock=lambda: 0))
        self.assertEqual(calls, list(catchup.SOURCES) * 2)
        self.assertEqual(waits, [catchup.MIN_SOURCE_INTERVAL] * 6)
        self.assertTrue(all(result[source]["pending"] == 0 for source in catchup.SOURCES))

    def test_attended_catchup_stops_on_any_hold_or_no_progress(self):
        state = {source: {"pending": 1, "posted": 0, "partial": False} for source in catchup.SOURCES}
        calls = []

        async def inspect(): return copy.deepcopy(state)
        async def wait(_seconds): pass
        async def held(source):
            calls.append(source)
            raise catchup.CatchupHold("uncertain send")

        with self.assertRaises(catchup.CatchupHold):
            asyncio.run(catchup.catch_up(inspect, held, wait=wait, clock=lambda: 0))
        self.assertEqual(calls, [catchup.SOURCES[0]])
        calls.clear()

        async def no_progress(source): calls.append(source)
        with self.assertRaises(catchup.CatchupHold):
            asyncio.run(catchup.catch_up(inspect, no_progress, wait=wait, clock=lambda: 0))
        self.assertEqual(calls, list(catchup.SOURCES))
        calls.clear()
        times = iter((0, 1))
        with self.assertRaises(catchup.CatchupHold):
            asyncio.run(catchup.catch_up(inspect, no_progress, wait=wait, clock=lambda: next(times), deadline_seconds=1))
        self.assertEqual(calls, [])

    def test_attended_catchup_rejects_missing_approval_before_network(self):
        env = {"NEWS_GAMES": "nte,endfield,silverpalace", "NEWS_CANDIDATES_BACKFILL": "1"}
        with (patch.dict("os.environ", env, clear=True),
              patch.object(catchup.runner, "authoritative_branch", side_effect=AssertionError("must not access git")),
              self.assertRaises(catchup.CatchupHold)):
            asyncio.run(catchup.run_from_environment())

    def test_attended_catchup_integrates_runner_and_local_git_without_discord(self):
        branch = "arena/98b5f4e1-game-express"
        hooks = {"nte": HOOK,
                 "endfield": "https://discord.com/api/webhooks/222222222222222222/bbbbbbbbbbbbbbbbbbbb",
                 "silverpalace": "https://discord.com/api/webhooks/333333333333333333/cccccccccccccccccccc"}
        env = {"NEWS_GAMES": ",".join(catchup.SOURCES), "NEWS_CANDIDATES_BACKFILL": "1",
               "CATCHUP_WRITERS_PAUSED": "true", "CATCHUP_CONFIRM": "START_CANDIDATE_BACKFILL"}
        env.update({f"DISCORD_WEBHOOK_NEWS_{source.upper()}": url for source, url in hooks.items()})
        settings = load_settings(env)
        rows = {
            "endfield": [{"id": str(i), "title": "Official " + str(i), "ts": NOW - 10 + i} for i in (1, 2)],
            "silverpalace": [row(str(i)) for i in (1, 2)],
            "nte": [{**row(str(i)), "url": "https://nte.perfectworld.com/en/article/news/gamenews/20261127/" + str(i) + ".html"}
                    for i in (1, 2)],
        }
        sends = []
        ambiguous = [False]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            remote, seed = base / "remote.git", base / "seed"
            seed.mkdir()

            def git(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout

            git(base, "init", "--bare", str(remote))
            git(seed, "init", "-b", branch)
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            (seed / "state").mkdir()
            records = {source: empty_record(source, delivery.destination(settings, source)[1]) for source in hooks}
            (seed / "state/candidate_news.json").write_text(json.dumps({"schema": 1, "candidate_news": records}))
            git(seed, "add", "state")
            git(seed, "commit", "-m", "synthetic initialized ledger")
            git(seed, "remote", "add", "origin", str(remote))
            git(seed, "push", "origin", f"HEAD:refs/heads/{branch}")

            async def discover(_fetcher, source): return copy.deepcopy(rows[source])
            async def render(_fetcher, _settings, _source, candidate): return [payload(candidate["id"])], {}
            async def send(_session, _settings, source, _payload, *, files=None):
                saved = json.loads(git(base, "--git-dir", str(remote), "show", f"refs/heads/{branch}:state/candidate_news.json"))
                self.assertTrue(saved["candidate_news"][source]["partial"]["inflight"])
                sends.append(source)
                if ambiguous[0]:
                    return SimpleNamespace(ok=False, ambiguous=True, message_id=None)
                return SimpleNamespace(ok=True, ambiguous=False, message_id=str(1000 + len(sends)))

            def no_post(*args, **kwargs): raise AssertionError("real network POST forbidden")
            with (patch.dict("os.environ", env, clear=True), patch.object(catchup, "ROOT", seed),
                  patch.object(runner, "ROOT", seed), patch.object(runner, "authoritative_branch", return_value=branch),
                  patch.object(catchup, "MIN_SOURCE_INTERVAL", 0), patch.object(delivery, "discover", discover),
                  patch.object(delivery, "render_candidate_animated", render), patch.object(delivery, "send_candidate_once", send),
                  patch("aiohttp.ClientSession.post", no_post), contextlib.redirect_stdout(io.StringIO())):
                asyncio.run(catchup.run_from_environment())
                self.assertEqual(sends, list(catchup.SOURCES) * 2)
                final = json.loads(git(base, "--git-dir", str(remote), "show", f"refs/heads/{branch}:state/candidate_news.json"))
                self.assertTrue(all(len(final["candidate_news"][source]["posted"]) == 2 for source in hooks))
                rows["endfield"].append({"id": "3", "title": "Official 3", "ts": NOW - 7})
                ambiguous[0] = True
                with self.assertRaises(catchup.CatchupHold):
                    asyncio.run(catchup.run_from_environment())
                self.assertEqual(sends[-1], "endfield")
                self.assertEqual(len(sends), 7)
                with self.assertRaises(catchup.CatchupHold):
                    asyncio.run(catchup.run_from_environment())
                self.assertEqual(len(sends), 7)  # a new attempt holds before any send

    def test_attended_catchup_workflow_is_manual_locked_and_explicitly_live(self):
        import yaml
        wf = ROOT / ".github" / "workflows"
        catchup_wf = yaml.safe_load((wf / "candidate_news_catchup.yml").read_text())
        monitor_wf = yaml.safe_load((wf / "monitor.yml").read_text())
        self.assertEqual(catchup_wf["concurrency"], monitor_wf["concurrency"])
        self.assertFalse(catchup_wf["concurrency"]["cancel-in-progress"])
        self.assertEqual(set(catchup_wf[True]), {"workflow_dispatch"})
        steps = catchup_wf["jobs"]["catchup"]["steps"]
        self.assertFalse(steps[0]["with"]["persist-credentials"])
        self.assertEqual(steps[0]["with"]["fetch-depth"], 0)
        self.assertIn("python -m gamexpress.candidate_news_catchup", steps[-1]["run"])
        self.assertNotIn("${{", steps[-1]["run"])
        self.assertIn("CATCHUP_CONFIRM", steps[-1]["env"])
        self.assertIn("CATCHUP_WRITERS_PAUSED", steps[-1]["env"])

    def test_crashed_process_holds_after_restart_and_reviewed_resolution(self):
        """A fresh process sees a durable intent after a simulated crash; no network POSTs."""
        branch = "arena/98b5f4e1-game-express"
        script = r"""
import asyncio, os, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from gamexpress import candidate_news_delivery as delivery, candidate_news_recovery as recovery, candidate_news_runner as runner
from gamexpress.candidate_news_checkpoint import IsolatedGitLedger
from gamexpress.cards import news_payloads
from gamexpress.config import Game, load_settings
from gamexpress.models import Item
root, branch, marker, mode = Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), sys.argv[5]
hook = "https://discord.com/api/webhooks/123456789012345678/example_token_123456789"
settings = load_settings({"ENABLED_FEATURES": "news", "NEWS_GAMES": "silverpalace",
    "NEWS_CANDIDATES_BACKFILL": "1", "DISCORD_WEBHOOK_NEWS_SILVERPALACE": hook})
route = delivery.destination(settings, "silverpalace")[1]
row = {"id": "1", "title": "Official 1", "date": "2026-11-27"}
async def discover(fetcher, source): return [row]
async def render(fetcher, settings, source, candidate):
    game = Game(key="silverpalace", name="Silver Palace", short="Silver", enabled=False, color=0)
    item = Item(source="silverpalace", game="silverpalace", id="1", title="Official 1",
                url="https://silverpalace.elementagames.com/en-us/newsDetail?id=1", text="",
                published_ts=0, article_blocks=[{"kind": "text", "text": "Hello"}])
    return news_payloads(game, item), {}
async def send(*args, **kwargs):
    with marker.open("a") as handle:
        handle.write("synthetic send called\n")
        handle.flush()
        os.fsync(handle.fileno())
    os._exit(71 if mode == "crash" else 72)
async def no_sleep(_): pass
async def main():
    if mode == "review":
        ledger = IsolatedGitLedger(root, branch)
        assert await ledger.open()
        try:
            inspected = recovery.inspect_record(ledger, "silverpalace", route)
            assert inspected["inflight"] and inspected["held_page"] == 1
            await recovery.reviewed_resolution(ledger, "silverpalace", route,
                article_id="1", page=1, signature=inspected["signature"], resolution="delivered",
                message_id="101", writers_paused=True, evidence_verified=True)
        finally:
            await ledger.close()
        return
    ctx = SimpleNamespace(settings=settings, active=True, now=1795824000, fetcher=object(),
                          webhook=SimpleNamespace(session=object()), report=[], warnings=[])
    with (patch.object(runner, "ROOT", root), patch.object(runner, "CANDIDATE_NEWS_READY", True),
          patch.object(runner, "authoritative_branch", return_value=branch),
          patch.object(delivery, "discover", discover), patch.object(delivery, "render_candidate_animated", render),
          patch.object(delivery, "send_candidate_once", send), patch.object(runner.asyncio, "sleep", no_sleep)):
        await runner.run_candidate_news(ctx)
    if mode == "restart":
        assert any("held (DeliveryHold)" in warning for warning in ctx.warnings), ctx.warnings
    elif mode == "finish":
        assert not ctx.warnings, ctx.warnings
    else:
        raise AssertionError("crash sender was not called")
asyncio.run(main())
"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            remote, seed, marker = base / "remote.git", base / "seed", base / "synthetic-sends.txt"
            seed.mkdir()

            def git(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout

            git(seed, "init", "--bare", str(remote))
            git(seed, "init", "-b", branch)
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            (seed / "state").mkdir()
            settings = load_settings({"DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK})
            route = delivery.destination(settings, "silverpalace")[1]
            (seed / "state/candidate_news.json").write_text(json.dumps({"schema": 1, "candidate_news": {
                "silverpalace": empty_record("silverpalace", route)}}), encoding="utf-8")
            git(seed, "add", "state")
            git(seed, "commit", "-m", "initial synthetic ledger")
            git(seed, "remote", "add", "origin", str(remote))
            git(seed, "push", "origin", f"HEAD:refs/heads/{branch}")

            def run(mode, checkout):
                result = subprocess.run([sys.executable, "-c", script, str(ROOT), str(checkout), branch,
                                         str(marker), mode], capture_output=True, text=True, timeout=60,
                                        env={**os.environ, "TMPDIR": str(base)})
                return result

            result = run("crash", seed)
            self.assertEqual(result.returncode, 71, result.stdout + result.stderr)
            held_blob = git(base, "--git-dir", str(remote), "show", f"refs/heads/{branch}:state/candidate_news.json")
            held = json.loads(held_blob)["candidate_news"]["silverpalace"]
            self.assertTrue(held["partial"]["inflight"])
            self.assertEqual(marker.read_text(), "synthetic send called\n")
            for mode in ("restart", "review", "finish"):
                checkout = base / mode
                git(base, "clone", "-b", branch, str(remote), str(checkout))
                result = run(mode, checkout)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(marker.read_text(), "synthetic send called\n")
            final_blob = git(base, "--git-dir", str(remote), "show", f"refs/heads/{branch}:state/candidate_news.json")
            final = json.loads(final_blob)["candidate_news"]["silverpalace"]
            self.assertEqual(final["posted"]["1"]["messages"], ["101"])
            self.assertIsNone(final["partial"])

    def test_validation_accepts_only_opted_in_candidate_news_and_checks_dedicated_route(self):
        from gamexpress.__main__ import cmd_validate
        base = {"NEWS_GAMES": "silverpalace", "NEWS_CANDIDATES_BACKFILL": "1",
                "DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK}
        output = io.StringIO()
        self.assertTrue(runner.CANDIDATE_NEWS_READY)
        with patch.dict("os.environ", base, clear=True), contextlib.redirect_stdout(output):
            self.assertEqual(cmd_validate(None), 0)
        self.assertNotIn("delivery is not released", output.getvalue())
        self.assertIn("candidate news silverpalace: dedicated route configured", output.getvalue())
        self.assertNotIn(HOOK, output.getvalue())
        output = io.StringIO()
        with patch.dict("os.environ", {"NEWS_GAMES": "silverpalace"}, clear=True), contextlib.redirect_stdout(output):
            self.assertEqual(cmd_validate(None), 1)
            self.assertIn("unknown game", output.getvalue())
        output = io.StringIO()
        with patch.dict("os.environ", {"NEWS_GAMES": "silverpalace", "NEWS_CANDIDATES_BACKFILL": "1"}, clear=True), contextlib.redirect_stdout(output):
            self.assertEqual(cmd_validate(None), 1)
            self.assertIn("missing/invalid dedicated route", output.getvalue())

    def test_opt_in_runner_routes_oldest_and_persists_without_touching_other_news(self):
        settings = load_settings({"ENABLED_FEATURES": "news", "NEWS_GAMES": "silverpalace",
                                  "NEWS_CANDIDATES_BACKFILL": "1", "DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK,
                                  "NEWS_CANDIDATES_AUTHORITY_REPOSITORY": "uesu/Game-Express",
                                  "NEWS_CANDIDATES_AUTHORITY_BRANCH": "arena/98b5f4e1-game-express"})
        state = SimpleNamespace(data={"news": {"genshin": {"unchanged": True}}})
        ctx = SimpleNamespace(settings=settings, active=True, state=state, now=NOW,
                              fetcher=object(), webhook=SimpleNamespace(session=object()), report=[], warnings=[])
        rendered, sent = [], []

        async def discover(fetcher, source):
            self.assertEqual(source, "silverpalace")
            return [row("2", "2026-01-02"), row("1", "2026-01-01")]

        async def render(fetcher, conf, source, candidate):
            rendered.append(candidate["id"])
            return [payload(candidate["id"])]

        async def sleep(seconds):
            self.assertEqual(seconds, 1.5)

        ledgers = []

        class Ledger:
            def __init__(self, root, branch):
                fingerprint = delivery.destination(settings, "silverpalace")[1]
                self.data, self.poisoned = {"candidate_news": {"silverpalace": empty_record("silverpalace", fingerprint)}}, False
                if branch != "arena/98b5f4e1-game-express":
                    raise AssertionError("wrong branch")
                ledgers.append(self)

            async def open(self):
                return True

            async def checkpoint(self, source, snapshot):
                self.data["candidate_news"][source] = copy.deepcopy(snapshot)
                return True

            async def close(self):
                pass

        # The send callback observes the ledger, not the monitor state.
        async def send(session, conf, source, card):
            self.assertEqual(source, "silverpalace")
            self.assertTrue(ledgers[0].data["candidate_news"][source]["partial"]["inflight"])
            sent.append(card)
            return SimpleNamespace(ok=True, message_id="101", ambiguous=False)

        with (patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_REF_NAME": "arena/98b5f4e1-game-express",
                                       "GITHUB_REF": "refs/heads/arena/98b5f4e1-game-express",
                                       "GITHUB_REPOSITORY": "uesu/Game-Express", "GITHUB_EVENT_NAME": "workflow_dispatch"}),
              patch.object(runner, "_origin_is_authority", return_value=True),
              patch.object(runner, "CANDIDATE_NEWS_READY", True), patch.object(runner, "IsolatedGitLedger", Ledger),
              patch.object(runner.asyncio, "sleep", sleep), patch.object(delivery, "discover", discover),
              patch.object(delivery, "render_candidate", render), patch.object(delivery, "send_candidate_once", send)):
            asyncio.run(runner.run_candidate_news(ctx))
        result = ledgers[0].data["candidate_news"]["silverpalace"]
        self.assertEqual(rendered, ["1"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(result["posted"]["1"]["messages"], ["101"])
        self.assertEqual([r["id"] for r in result["pending"]], ["2"])
        self.assertEqual(state.data["news"], {"genshin": {"unchanged": True}})
        self.assertFalse(ctx.warnings)

    def test_gated_runner_uses_real_isolated_ledger_without_touching_monitor_state(self):
        branch = "arena/98b5f4e1-game-express"
        with tempfile.TemporaryDirectory() as tmp:
            root, remote = Path(tmp) / "work", Path(tmp) / "remote.git"
            root.mkdir()

            def git(*args):
                return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout

            git("init", "--bare", str(remote))
            git("init", "-b", branch)
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            (root / "state").mkdir()
            (root / "state/state.json").write_text('{"schema":1,"news":{"old":"unchanged"}}\n', encoding="utf-8")
            route = delivery.destination(load_settings({"DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK}), "silverpalace")[1]
            (root / "state/candidate_news.json").write_text(json.dumps(initialized_ledger({"silverpalace": route})), encoding="utf-8")
            git("add", "state")
            git("commit", "-m", "initial")
            git("remote", "add", "origin", str(remote))
            git("push", "origin", f"HEAD:refs/heads/{branch}")
            before = git("rev-parse", "HEAD")
            settings = load_settings({"ENABLED_FEATURES": "news", "NEWS_GAMES": "silverpalace",
                                      "NEWS_CANDIDATES_BACKFILL": "1", "DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK,
                                      "NEWS_CANDIDATES_AUTHORITY_REPOSITORY": "uesu/Game-Express",
                                      "NEWS_CANDIDATES_AUTHORITY_BRANCH": branch})
            ctx = SimpleNamespace(settings=settings, active=True, state=State.load(root / "state/state.json"), now=NOW,
                                  fetcher=None, webhook=SimpleNamespace(session=None), report=[], warnings=[])
            sends = []

            async def discover(fetcher, source):
                return [row("1"), row("2")]

            async def render(fetcher, conf, source, article):
                return [payload(article["id"])]

            async def send(session, conf, source, card):
                recorded = json.loads(git("--git-dir", str(remote), "show", f"refs/heads/{branch}:state/candidate_news.json"))
                self.assertTrue(recorded["candidate_news"][source]["partial"]["inflight"])
                sends.append(source)
                return SimpleNamespace(ok=True, message_id="101", ambiguous=False)

            async def sleep(seconds):
                pass

            with (patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_REF_NAME": branch,
                                           "GITHUB_REF": f"refs/heads/{branch}", "GITHUB_REPOSITORY": "uesu/Game-Express",
                                           "GITHUB_EVENT_NAME": "workflow_dispatch"}),
                  patch.object(runner, "_origin_is_authority", return_value=True), patch.object(runner, "ROOT", root), patch.object(runner, "CANDIDATE_NEWS_READY", True),
                  patch.object(runner.asyncio, "sleep", sleep), patch.object(delivery, "discover", discover),
                  patch.object(delivery, "render_candidate", render), patch.object(delivery, "send_candidate_once", send)):
                asyncio.run(runner.run_candidate_news(ctx))
            self.assertEqual(sends, ["silverpalace"])
            self.assertFalse(ctx.warnings)
            self.assertEqual(git("rev-parse", "HEAD"), before)
            self.assertEqual(git("status", "--porcelain"), b"")
            self.assertEqual(ctx.state.data["news"], {"old": "unchanged"})
            self.assertNotIn("candidate_news", ctx.state.data)
            recorded = json.loads(git("--git-dir", str(remote), "show", f"refs/heads/{branch}:state/candidate_news.json"))
            self.assertEqual(recorded["candidate_news"]["silverpalace"]["posted"]["1"]["messages"], ["101"])
            self.assertEqual(recorded["candidate_news"]["silverpalace"]["pending"][0]["id"], "2")

    def test_integrated_runner_real_render_planner_multipart_and_git_checkpoint(self):
        import base64

        branch = "arena/98b5f4e1-game-express"
        gif = base64.b64decode("R0lGODlhBAAEAIEAAP8AAAAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQACgAAACwAAAAABAAEAAAICQABCBxIsCCAgAAh+QQBCgABACwAAAAABAAEAIEAAP8AAAAAAAAAAAAICQABCBxIsCCAgAA7")
        urls = ("https://s.elementagames.com/a.jpg", "https://s.elementagames.com/b.jpg")
        contents = {urls[0]: gif, urls[1]: gif.replace(b"NETSCAPE2.0", b"ANIMEXTS1.0")}
        with tempfile.TemporaryDirectory() as tmp:
            root, remote = Path(tmp) / "work", Path(tmp) / "remote.git"
            root.mkdir()

            def git(*args):
                return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout

            git("init", "--bare", str(remote))
            git("init", "-b", branch)
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            (root / "state").mkdir()
            (root / "state/state.json").write_text('{"schema":1}\n', encoding="utf-8")
            route = delivery.destination(load_settings({"DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK}), "silverpalace")[1]
            (root / "state/candidate_news.json").write_text(json.dumps(initialized_ledger({"silverpalace": route})), encoding="utf-8")
            git("add", "state")
            git("commit", "-m", "initial")
            git("remote", "add", "origin", str(remote))
            git("push", "origin", f"HEAD:refs/heads/{branch}")
            game = delivery.candidate_game("silverpalace")
            item = Item(source="silverpalace", game="silverpalace", id="1", title="Official 1",
                        url="https://silverpalace.elementagames.com/en-us/newsDetail?id=1", text="", published_ts=0,
                        article_blocks=[{"kind": "text", "text": "Before"}, {"kind": "image", "url": urls[0]},
                                        {"kind": "text", "text": "Middle"}, {"kind": "image", "url": urls[1]}])

            class Fetcher:
                def __init__(self):
                    self.media_prefixes = {}

                async def verify_media(self, url):
                    self.media_prefixes[url] = (contents[url][:32], "image/jpeg")
                    return True

                async def media_dimensions(self, url):
                    return None

            class Response:
                status = 200
                content = None

                def __init__(self):
                    self.content = self

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return False

                async def iter_chunked(self, size):
                    yield b'{"id":"101"}'

            class Session:
                def __init__(self):
                    self.calls = []

                def post(self, url, **kwargs):
                    current = json.loads(git("--git-dir", str(remote), "show",
                                             f"refs/heads/{branch}:state/candidate_news.json"))
                    self_test.assertTrue(current["candidate_news"]["silverpalace"]["partial"]["inflight"])
                    self_test.assertFalse(kwargs["allow_redirects"])
                    form = kwargs["data"]
                    fields = [field[0].get("name") for field in form._fields]
                    self_test.assertEqual(fields, ["payload_json", "files[0]", "files[1]"])
                    self.calls.append(kwargs)
                    return Response()

            self_test = self
            session, fetcher = Session(), Fetcher()
            settings = load_settings({"ENABLED_FEATURES": "news", "NEWS_GAMES": "silverpalace",
                                      "NEWS_CANDIDATES_BACKFILL": "1", "DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK,
                                      "NEWS_CANDIDATES_AUTHORITY_REPOSITORY": "uesu/Game-Express",
                                      "NEWS_CANDIDATES_AUTHORITY_BRANCH": branch})
            ctx = SimpleNamespace(settings=settings, active=True, now=NOW, fetcher=fetcher,
                                  webhook=SimpleNamespace(session=session), report=[], warnings=[])

            async def discover(fetcher, source):
                return [row("1")]

            async def article(fetcher, actual_game, candidate):
                self.assertEqual(actual_game, game)
                return item

            async def fetch_gif(fetcher, url):
                return contents[url]

            async def sleep(seconds):
                pass

            with (patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_REF_NAME": branch,
                                            "GITHUB_REF": f"refs/heads/{branch}", "GITHUB_REPOSITORY": "uesu/Game-Express",
                                            "GITHUB_EVENT_NAME": "workflow_dispatch"}),
                  patch.object(runner, "_origin_is_authority", return_value=True), patch.object(runner, "ROOT", root),
                  patch.object(runner, "CANDIDATE_NEWS_READY", True), patch.object(runner.asyncio, "sleep", sleep),
                  patch.object(delivery, "discover", discover), patch.object(delivery.silverpalace, "news_article", article),
                  patch("gamexpress.news_animation.fetch_gif", fetch_gif)):
                asyncio.run(runner.run_candidate_news(ctx))
            self.assertEqual(len(session.calls), 1)
            self.assertFalse(ctx.warnings)
            saved = json.loads(git("--git-dir", str(remote), "show", f"refs/heads/{branch}:state/candidate_news.json"))
            self.assertEqual(saved["candidate_news"]["silverpalace"]["posted"]["1"]["messages"], ["101"])
            self.assertEqual(git("status", "--porcelain"), b"")

    def test_standby_authority_requires_exact_opt_in_caller_and_current_checkout(self):
        authority = "uesu/Authority"
        standby_repo, standby_branch, authority_branch = "uesu/Standby", "main", "main"
        config = {"INSTANCE_ROLE": "standby", "NEWS_CANDIDATES_STANDBY_ENABLED": "1",
                  "NEWS_CANDIDATES_AUTHORITY_REPOSITORY": authority,
                  "NEWS_CANDIDATES_AUTHORITY_BRANCH": authority_branch,
                  "NEWS_CANDIDATES_STANDBY_REPOSITORY": standby_repo,
                  "NEWS_CANDIDATES_STANDBY_BRANCH": standby_branch,
                  "NEWS_CANDIDATES_LEDGER_TOKEN": "github_pat_" + "x" * 30}
        actions = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
                   "GITHUB_REPOSITORY": standby_repo, "GITHUB_REF_NAME": standby_branch,
                   "GITHUB_REF": f"refs/heads/{standby_branch}"}
        with (tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", actions, clear=True),
              patch.object(runner, "_origin_is_authority", return_value=True),
              patch.object(runner, "_checkout_is_current", return_value=True)):
            root = Path(tmp)
            self.assertEqual(runner.standby_authority(load_settings(config), root),
                             (authority, authority_branch, config["NEWS_CANDIDATES_LEDGER_TOKEN"]))
            printable_token = "token.with+punctuation/" + "x" * 24
            self.assertEqual(runner.standby_authority(load_settings({**config, "NEWS_CANDIDATES_LEDGER_TOKEN": printable_token}), root)[2], printable_token)
            for key, bad in (("INSTANCE_ROLE", "primary"), ("NEWS_CANDIDATES_STANDBY_ENABLED", "0"),
                             ("NEWS_CANDIDATES_LEDGER_TOKEN", ""), ("NEWS_CANDIDATES_LEDGER_TOKEN", "bad\nsecret"),
                             ("NEWS_CANDIDATES_AUTHORITY_REPOSITORY", "../Authority"),
                             ("NEWS_CANDIDATES_AUTHORITY_REPOSITORY", standby_repo),
                             ("NEWS_CANDIDATES_AUTHORITY_BRANCH", "-bad"),
                             ("NEWS_CANDIDATES_STANDBY_BRANCH", "other")):
                self.assertIsNone(runner.standby_authority(load_settings({**config, key: bad}), root), key)
            for key, bad in (("GITHUB_REPOSITORY", "uesu/Other"), ("GITHUB_ACTIONS", "false"),
                             ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_REF", "refs/tags/main")):
                with patch.dict("os.environ", {key: bad}):
                    self.assertIsNone(runner.standby_authority(load_settings(config), root), key)
            with patch.object(runner, "_origin_is_authority", return_value=False):
                self.assertIsNone(runner.standby_authority(load_settings(config), root))
            with patch.object(runner, "_checkout_is_current", return_value=False):
                self.assertIsNone(runner.standby_authority(load_settings(config), root))

    def test_standby_git_auth_is_scoped_and_never_in_args_or_url(self):
        token = "github_pat_" + "x" * 30
        with patch.dict("os.environ", {"GIT_TRACE_CURL": "1", "GH_TOKEN": "other-secret",
                                      "GE_SECRETS_JSON": "private", "PATH": "/bin"}, clear=True):
            env = runner._standby_git_environment(token)
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GE_SECRETS_JSON", env)
        self.assertNotIn("GIT_TRACE_CURL", env)
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "http.followRedirects")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], "false")
        self.assertNotIn(token, repr({k: v for k, v in env.items() if k != "GIT_CONFIG_VALUE_0"}))
        checkout = runner.StandbyLedgerCheckout("uesu/Authority", "main", token)
        seen = []

        def fake_git(*args):
            seen.append(args)
            return True

        with patch.object(checkout, "_git", side_effect=fake_git):
            self.assertTrue(asyncio.run(checkout.open()))
            root = checkout.root
            self.assertTrue(root.is_dir())
            self.assertEqual(seen[1], ("remote", "add", "origin", "https://github.com/uesu/Authority.git"))
            self.assertNotIn(token, repr(seen))
            asyncio.run(checkout.close())
            self.assertFalse(root.exists())
            self.assertEqual(checkout.env, {})
        failed = runner.StandbyLedgerCheckout("uesu/Authority", "main", token)
        with patch.object(failed, "_git", return_value=False):
            self.assertFalse(asyncio.run(failed.open()))
        self.assertIsNone(failed.root)
        self.assertEqual(failed.env, {})

    def test_standby_never_binds_new_routes_and_gate_prevents_checkout(self):
        settings = load_settings({"INSTANCE_ROLE": "standby", "NEWS_CANDIDATES_STANDBY_ENABLED": "1",
                                  "NEWS_CANDIDATES_BACKFILL": "1", "NEWS_GAMES": "silverpalace",
                                  "DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK})
        class FakeCheckout:
            closed = False
            def __init__(self, repository, branch, token):
                self.root, self.env = Path("/tmp"), {}
            async def open(self):
                return True
            async def close(self):
                self.closed = True
        class FakeLedger:
            def __init__(self, root, branch, *, git_env):
                self.data, self.poisoned = {"candidate_news": {}}, False
            async def open(self):
                return True
            async def close(self):
                pass
        def context():
            return SimpleNamespace(settings=settings, active=True, now=NOW, fetcher=None,
                                   webhook=SimpleNamespace(session=None), report=[], warnings=[])
        with (patch.object(runner, "authoritative_branch", return_value=None),
              patch.object(runner, "standby_authority", return_value=("uesu/Auth", "main", "x" * 30)),
              patch.object(runner, "StandbyLedgerCheckout", FakeCheckout),
              patch.object(runner, "IsolatedGitLedger", FakeLedger),
              patch.object(delivery, "discover", side_effect=AssertionError("must not discover"))):
            gated = context()
            with patch.object(runner, "CANDIDATE_NEWS_READY", False), patch.object(FakeCheckout, "open", side_effect=AssertionError("no checkout")):
                asyncio.run(runner.run_candidate_news(gated))
            self.assertIn("not released", gated.warnings[0])
            active = context()
            with patch.object(runner, "CANDIDATE_NEWS_READY", True):
                asyncio.run(runner.run_candidate_news(active))
            self.assertIn("initialized route missing", active.warnings[0])

    def test_route_mismatch_holds_before_discovery_on_primary_and_standby(self):
        settings = load_settings({"NEWS_CANDIDATES_BACKFILL": "1", "NEWS_GAMES": "silverpalace",
                                  "DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK})
        class Ledger:
            def __init__(self, root, branch, **kwargs):
                self.data = {"candidate_news": {"silverpalace": empty_record("silverpalace", "f" * 16)}}
                self.poisoned = False
            async def open(self):
                return True
            async def close(self):
                pass
        class Checkout:
            def __init__(self, *args):
                self.root, self.env = Path("/tmp"), {}
            async def open(self):
                return True
            async def close(self):
                pass
        def ctx():
            return SimpleNamespace(settings=settings, active=True, now=NOW, fetcher=None,
                                   webhook=SimpleNamespace(session=None), report=[], warnings=[])
        with (patch.object(runner, "CANDIDATE_NEWS_READY", True), patch.object(runner, "IsolatedGitLedger", Ledger),
              patch.object(runner, "StandbyLedgerCheckout", Checkout),
              patch.object(runner, "standby_authority", return_value=("uesu/Auth", "main", "x" * 30)),
              patch.object(delivery, "discover", side_effect=AssertionError("route mismatch must hold first"))):
            for branch in ("main", None):
                with patch.object(runner, "authoritative_branch", return_value=branch):
                    result = ctx()
                    asyncio.run(runner.run_candidate_news(result))
                    self.assertIn("held (DeliveryHold)", result.warnings[0])

    def test_standby_failover_holds_ambiguous_primary_and_resumes_only_after_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            authority, seed = base / "authority.git", base / "seed"
            standby_remote, standby_root = base / "standby.git", base / "standby"
            seed.mkdir()
            standby_root.mkdir()

            def git(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout

            git(base, "init", "--bare", str(authority))
            git(seed, "init", "-b", "main")
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            (seed / "state").mkdir()
            (seed / "state/candidate_news.json").write_text('{"schema":1,"candidate_news":{}}\n', encoding="utf-8")
            git(seed, "add", "state")
            git(seed, "commit", "-m", "seed")
            git(seed, "remote", "add", "origin", str(authority))
            git(seed, "push", "origin", "HEAD:refs/heads/main")
            git(base, "init", "--bare", str(standby_remote))
            git(standby_root, "init", "-b", "main")
            git(standby_root, "config", "user.name", "Test")
            git(standby_root, "config", "user.email", "test@example.invalid")
            (standby_root / "example").write_text("standby", encoding="utf-8")
            git(standby_root, "add", "example")
            git(standby_root, "commit", "-m", "standby")
            git(standby_root, "remote", "add", "origin", str(standby_remote))
            git(standby_root, "push", "origin", "HEAD:refs/heads/main")
            settings = load_settings({"INSTANCE_ROLE": "standby", "NEWS_CANDIDATES_STANDBY_ENABLED": "1",
                "NEWS_CANDIDATES_AUTHORITY_REPOSITORY": "uesu/Authority", "NEWS_CANDIDATES_AUTHORITY_BRANCH": "main",
                "NEWS_CANDIDATES_STANDBY_REPOSITORY": "uesu/Standby", "NEWS_CANDIDATES_STANDBY_BRANCH": "main",
                "NEWS_CANDIDATES_LEDGER_TOKEN": "github_pat_" + "x" * 30,
                "NEWS_CANDIDATES_BACKFILL": "1", "NEWS_GAMES": "silverpalace",
                "DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK})
            route = delivery.destination(settings, "silverpalace")[1]
            attempts = []
            primary = IsolatedGitLedger(seed, "main")
            self.assertTrue(asyncio.run(primary.open()))
            async def checkpoint(state):
                return await primary.checkpoint("silverpalace", state)
            async def render_old(candidate):
                return [payload(candidate["id"])]
            async def ambiguous(card):
                attempts.append("primary")
                return SimpleNamespace(ok=False, message_id=None, ambiguous=True)
            initial = delivery.ingest("silverpalace", route, None, [row("1")])
            with self.assertRaises(delivery.DeliveryHold):
                asyncio.run(delivery.deliver_oldest(initial, "silverpalace", route, as_of=NOW,
                            checkpoint=checkpoint, render=render_old, send=ambiguous))
            asyncio.run(primary.close())

            class TestCheckout(runner.StandbyLedgerCheckout):
                # Replace only the HTTPS transport with a local bare Git repository.
                def __init__(self, repository, branch, token):
                    super().__init__(repository, branch, token)
                    self.env["GIT_CONFIG_VALUE_2"] = "always"
                def _git(self, *args):
                    if args[:3] == ("remote", "add", "origin"):
                        args = (*args[:3], str(authority))
                    return super()._git(*args)

            async def discover(fetcher, source):
                return [row("1")]
            async def render(fetcher, conf, source, candidate):
                return [payload(candidate["id"])], {}
            async def send(session, conf, source, card):
                attempts.append("standby")
                return SimpleNamespace(ok=True, message_id="101", ambiguous=False)
            def context(active=True):
                return SimpleNamespace(settings=settings, active=active, now=NOW, fetcher=None,
                                       webhook=SimpleNamespace(session=None), report=[], warnings=[])
            actions = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
                       "GITHUB_REPOSITORY": "uesu/Standby", "GITHUB_REF_NAME": "main", "GITHUB_REF": "refs/heads/main"}
            with (patch.dict("os.environ", actions, clear=True), patch.object(runner, "ROOT", standby_root),
                  patch.object(runner, "_origin_is_authority", return_value=True),
                  patch.object(runner, "CANDIDATE_NEWS_READY", True), patch.object(runner, "StandbyLedgerCheckout", TestCheckout),
                  patch.object(delivery, "discover", discover), patch.object(delivery, "render_candidate_animated", render),
                  patch.object(delivery, "send_candidate_once", send)):
                passive = context(False)
                asyncio.run(runner.run_candidate_news(passive))
                self.assertEqual(attempts, ["primary"])
                held = context()
                asyncio.run(runner.run_candidate_news(held))
                self.assertEqual(attempts, ["primary"])
                self.assertTrue(held.warnings)
                git(seed, "pull", "--ff-only", "origin", "main")
                ledger = IsolatedGitLedger(seed, "main")
                self.assertTrue(asyncio.run(ledger.open()))
                info = recovery.inspect_record(ledger, "silverpalace", route)
                self.assertTrue(info["inflight"])
                asyncio.run(recovery.reviewed_resolution(ledger, "silverpalace", route, article_id="1", page=1,
                    signature=info["signature"], resolution="absent", writers_paused=True, evidence_verified=True))
                asyncio.run(ledger.close())
                resumed = context()
                asyncio.run(runner.run_candidate_news(resumed))
                self.assertFalse(resumed.warnings)
                self.assertEqual(attempts, ["primary", "standby"])
                again = context()
                asyncio.run(runner.run_candidate_news(again))
                self.assertEqual(attempts, ["primary", "standby"])
            persisted = json.loads(git(seed, "--git-dir", str(authority), "show", "refs/heads/main:state/candidate_news.json"))
            self.assertEqual(persisted["candidate_news"]["silverpalace"]["posted"]["1"]["messages"], ["101"])
            self.assertFalse((standby_root / "state/candidate_news.json").exists())

    def test_primary_and_standby_runners_race_on_one_shared_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, branch = Path(tmp), "main"
            remote, seed = base / "authority.git", base / "primary"
            seed.mkdir()
            def git(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout
            git(base, "init", "--bare", str(remote))
            git(seed, "init", "-b", branch)
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            config = {"NEWS_CANDIDATES_BACKFILL": "1", "NEWS_GAMES": "silverpalace",
                      "DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK}
            primary_settings = load_settings({**config, "INSTANCE_ROLE": "primary"})
            standby_settings = load_settings({**config, "INSTANCE_ROLE": "standby"})
            route = delivery.destination(primary_settings, "silverpalace")[1]
            queued = delivery.ingest("silverpalace", route, None, [row("1")])
            (seed / "state").mkdir()
            (seed / "state/candidate_news.json").write_text(json.dumps({"schema": 1, "candidate_news": {"silverpalace": queued}}), encoding="utf-8")
            git(seed, "add", "state")
            git(seed, "commit", "-m", "seed")
            git(seed, "remote", "add", "origin", str(remote))
            git(seed, "push", "origin", "HEAD:refs/heads/main")

            class LocalCheckout(runner.StandbyLedgerCheckout):
                def __init__(self, repository, branch, token):
                    super().__init__(repository, branch, token)
                    self.env["GIT_CONFIG_VALUE_2"] = "always"
                def _git(self, *args):
                    if args[:3] == ("remote", "add", "origin"):
                        args = (*args[:3], str(remote))
                    return super()._git(*args)

            barrier = threading.Barrier(2, timeout=20)
            original_git = IsolatedGitLedger._git
            def coordinate(ledger, root, *args):
                if args and args[0] == "push" and not getattr(ledger, "_race_started", False):
                    ledger._race_started = True
                    barrier.wait()
                return original_git(ledger, root, *args)
            sends = []
            async def discover(fetcher, source):
                return [row("1")]
            async def render(fetcher, settings, source, candidate):
                return [payload(candidate["id"])], {}
            async def send(session, settings, source, card):
                sends.append(settings.instance_role)
                return SimpleNamespace(ok=True, message_id="101", ambiguous=False)
            async def compete():
                def ctx(settings):
                    return SimpleNamespace(settings=settings, active=True, now=NOW, fetcher=None,
                                           webhook=SimpleNamespace(session=None), report=[], warnings=[])
                a, b = ctx(primary_settings), ctx(standby_settings)
                await asyncio.gather(runner.run_candidate_news(a), runner.run_candidate_news(b))
                return a, b
            with (patch.object(runner, "ROOT", seed), patch.object(runner, "CANDIDATE_NEWS_READY", True),
                  patch.object(runner, "authoritative_branch", side_effect=lambda settings, root: "main" if settings.instance_role == "primary" else None),
                  patch.object(runner, "standby_authority", return_value=("uesu/Authority", "main", "x" * 30)),
                  patch.object(runner, "StandbyLedgerCheckout", LocalCheckout),
                  patch.object(IsolatedGitLedger, "_git", coordinate),
                  patch.object(delivery, "discover", discover), patch.object(delivery, "render_candidate_animated", render),
                  patch.object(delivery, "send_candidate_once", send)):
                a, b = asyncio.run(compete())
            self.assertEqual(len(sends), 1)
            self.assertEqual(sum(bool(ctx.warnings) for ctx in (a, b)), 1)
            result = json.loads(git(seed, "--git-dir", str(remote), "show", "refs/heads/main:state/candidate_news.json"))
            self.assertEqual(result["candidate_news"]["silverpalace"]["posted"]["1"]["messages"], ["101"])

    def test_candidate_authority_restricts_repo_branch_origin_role_and_event(self):
        branch = "arena/98b5f4e1-game-express"
        env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": "uesu/Game-Express",
               "GITHUB_REF_NAME": branch, "GITHUB_REF": f"refs/heads/{branch}"}
        config = {"NEWS_CANDIDATES_AUTHORITY_REPOSITORY": "uesu/Game-Express", "NEWS_CANDIDATES_AUTHORITY_BRANCH": branch}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-b", branch], cwd=root, capture_output=True, check=True)
            subprocess.run(["git", "remote", "add", "origin", "https://github.com/uesu/Game-Express.git"],
                           cwd=root, capture_output=True, check=True)
            with patch.dict("os.environ", env, clear=True):
                self.assertEqual(runner.authoritative_branch(load_settings(config), root), branch)
                self.assertEqual(runner.authoritative_branch(load_settings({**config, "PEER_STATE_URL": "https://peer.invalid"}), root), branch)
                self.assertIsNone(runner.authoritative_branch(load_settings({**config, "INSTANCE_ROLE": "standby"}), root))
                self.assertIsNone(runner.authoritative_branch(load_settings({}), root))
                for key, value in (("GITHUB_REPOSITORY", "uesu/Other"), ("GITHUB_REF", "refs/tags/" + branch),
                                   ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_REF_NAME", "other")):
                    with patch.dict("os.environ", {key: value}):
                        self.assertIsNone(runner.authoritative_branch(load_settings(config), root))
                subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/uesu/Other.git"],
                               cwd=root, capture_output=True, check=True)
                self.assertIsNone(runner.authoritative_branch(load_settings(config), root))
                subprocess.run(["git", "remote", "set-url", "origin", "https://secret@github.com/uesu/Game-Express.git"],
                               cwd=root, capture_output=True, check=True)
                self.assertIsNone(runner.authoritative_branch(load_settings(config), root))

    def test_runner_stays_inert_without_explicit_opt_in_and_on_peer_config(self):
        settings = load_settings({"ENABLED_FEATURES": "news", "NEWS_GAMES": "silverpalace",
                                  "DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK})
        ctx = SimpleNamespace(settings=settings, active=True, state=SimpleNamespace(data={}), now=NOW,
                              fetcher=None, webhook=None, report=[], warnings=[])
        with patch.object(delivery, "discover", side_effect=AssertionError("unexpected network")):
            asyncio.run(runner.run_candidate_news(ctx))
            self.assertEqual(ctx.state.data, {})
            settings = load_settings({"ENABLED_FEATURES": "news", "NEWS_GAMES": "silverpalace",
                                      "NEWS_CANDIDATES_BACKFILL": "1", "PEER_STATE_URL": "https://example.org/state"})
            ctx.settings = settings
            with patch.object(runner, "CANDIDATE_NEWS_READY", False):
                asyncio.run(runner.run_candidate_news(ctx))
            self.assertIn("not released", ctx.warnings[-1])
            asyncio.run(runner.run_candidate_news(ctx))
            self.assertIn("authority unavailable", ctx.warnings[-1])
            self.assertEqual(ctx.state.data, {})

    def test_scoped_renderer_calls_correct_reader_preflight_and_fallback(self):
        game = delivery.candidate_game("silverpalace")
        article = Item(source="silverpalace", game="silverpalace", id="1", title="Official 1",
                       url="https://silverpalace.elementagames.com/en-us/newsDetail?id=1", text="", published_ts=0,
                       article_blocks=[{"kind": "text", "text": "Watch https://youtu.be/ELC4r7fbKEQ"}])

        class Fetcher:
            async def media_dimensions(self, url):
                return (1280, 720)

            async def verify_media(self, url):
                return url == "https://i.ytimg.com/vi/ELC4r7fbKEQ/maxresdefault.jpg"

        async def read(fetcher, gm, candidate):
            self.assertEqual(gm, game)
            self.assertEqual(candidate, row("1"))
            return article

        with patch.object(delivery.silverpalace, "news_article", read):
            pages = asyncio.run(delivery.render_candidate(Fetcher(), load_settings({}), "silverpalace", row("1")))
        children = pages[0]["components"][0]["components"]
        self.assertTrue(any(c.get("type") == 12 and c["items"][0]["media"]["url"].endswith("/maxresdefault.jpg")
                            for c in children))
        self.assertTrue(any("https://youtu.be/ELC4r7fbKEQ" in c.get("content", "") for c in children))
        self.assertEqual(article.images, [])

    def test_candidate_full_article_is_required_even_when_default_mode_is_off(self):
        article = Item(source="silverpalace", game="silverpalace", id="1", title="Official 1",
                       url="https://silverpalace.elementagames.com/en-us/newsDetail?id=1", text="", published_ts=0,
                       article_blocks=[{"kind": "text", "text": "Full text " * 1500}])

        async def read(fetcher, game, candidate):
            return article

        settings = load_settings({})
        self.assertFalse(settings.news_expand_full)
        with patch.object(delivery.silverpalace, "news_article", read):
            pages = asyncio.run(delivery.render_candidate(None, settings, "silverpalace", row("1")))
        self.assertGreater(len(pages), 1)
        self.assertNotIn("**Excerpt — this article", json.dumps(pages, ensure_ascii=False))
        # Existing-news page settings do not truncate candidate catch-up; candidates have
        # their own hard safety bound, which still holds instead of sending an excerpt.
        settings = load_settings({"NEWS_EXPAND_MAX_PAGES": "1"})
        with patch.object(delivery.silverpalace, "news_article", read):
            pages = asyncio.run(delivery.render_candidate(None, settings, "silverpalace", row("1")))
        self.assertGreater(len(pages), 1)
        with (patch.object(delivery.silverpalace, "news_article", read),
              patch.object(delivery, "MAX_CANDIDATE_PAGES", 1), self.assertRaises(delivery.DeliveryHold)):
            asyncio.run(delivery.render_candidate(None, settings, "silverpalace", row("1")))

    def test_private_destination_sends_once_without_redirects_or_retries(self):
        class Content:
            def __init__(self, raw):
                self.raw = raw

            async def iter_chunked(self, size):
                yield self.raw

        class Response:
            def __init__(self, status, raw):
                self.status, self.content = status, Content(raw)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class Session:
            def __init__(self, status=200, raw=b'{"id":"12345"}'):
                self.calls, self.status, self.raw = [], status, raw

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response(self.status, self.raw)

        settings = load_settings({"DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK + "?thread_id=123"})
        session = Session()
        result = asyncio.run(delivery.send_candidate_once(session, settings, "silverpalace", payload("1")))
        self.assertTrue(result.ok)
        self.assertEqual(result.message_id, "12345")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][0], HOOK)
        self.assertFalse(session.calls[0][1]["allow_redirects"])
        self.assertEqual(session.calls[0][1]["params"], {"wait": "true", "with_components": "true", "thread_id": "123"})
        for status, raw in ((500, b'{}'), (200, b'{}'), (200, b'{bad'), (200, b'x' * 65537)):
            session = Session(status, raw)
            result = asyncio.run(delivery.send_candidate_once(session, settings, "silverpalace", payload("1")))
            self.assertFalse(result.ok)
            self.assertTrue(result.ambiguous)
            self.assertEqual(len(session.calls), 1)
        session = Session()
        result = asyncio.run(delivery.send_candidate_once(session, settings, "nte", payload("1")))
        self.assertFalse(result.ok)
        self.assertFalse(session.calls)

    def test_one_shot_gif_attachment_requires_exact_digest_and_matching_reference(self):
        import base64
        import hashlib

        from gamexpress.cards import IS_COMPONENTS_V2, container, text

        data = base64.b64decode("R0lGODlhBAAEAIEAAP8AAAAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQACgAAACwAAAAABAAEAAAICQABCBxIsCCAgAAh+QQBCgABACwAAAAABAAEAIEAAP8AAAAAAAAAAAAICQABCBxIsCCAgAA7")
        name = "animation-" + hashlib.sha256(data).hexdigest() + ".gif"
        page = {"flags": IS_COMPONENTS_V2, "allowed_mentions": {"parse": []},
                "attachments": [{"id": 0, "filename": name}],
                "components": [container([text("Official"), {"type": 12, "items": [{"media": {"url": "attachment://" + name}}]}])]}

        class Content:
            async def iter_chunked(self, size):
                yield b'{"id":"12345"}'

        class Response:
            status = 200
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append(kwargs)
                return Response()

        settings = load_settings({"DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK})
        session = Session()
        result = asyncio.run(delivery.send_candidate_once(session, settings, "silverpalace", page, files={name: data}))
        self.assertTrue(result.ok)
        self.assertEqual(len(session.calls), 1)
        self.assertIn("data", session.calls[0])
        self.assertNotIn("json", session.calls[0])
        for files in ({name: data + b"x"}, {}, {"wrong.gif": data}, None):
            session = Session()
            result = asyncio.run(delivery.send_candidate_once(session, settings, "silverpalace", page, files=files))
            self.assertFalse(result.ok)
            self.assertFalse(session.calls)

    def test_dedicated_routing_only_and_no_secret_in_fingerprint(self):
        settings = load_settings({"DISCORD_WEBHOOK_NEWS_SILVERPALACE": HOOK,
                                  "DISCORD_WEBHOOK_URL": "https://wrong.example/secret"})
        result = delivery.destination(settings, "silverpalace")
        self.assertEqual(result[0], HOOK)
        self.assertEqual(len(result[1]), 16)
        self.assertNotIn("example_token", result[1])
        self.assertIsNone(delivery.destination(settings, "nte"))
        self.assertIsNone(delivery.destination(settings, "hoyolab"))
        for value in ("https://evil.example/api/webhooks/123/example_token_123456789",
                      HOOK + "?thread_id=1&other=secret", HOOK + "#fragment", HOOK.replace("https:", "http:")):
            self.assertIsNone(delivery.destination(load_settings({"DISCORD_WEBHOOK_NEWS_NTE": value}), "nte"))
        a = delivery.destination(load_settings({"DISCORD_WEBHOOK_NEWS_NTE": HOOK}), "nte")
        b = delivery.destination(load_settings({"DISCORD_WEBHOOK_NEWS_NTE": HOOK + "?thread_id=123"}), "nte")
        self.assertNotEqual(a[1], b[1])

    def test_initial_backfill_and_new_articles_sort_without_pruning(self):
        state = delivery.ingest("silverpalace", ROUTE, None,
                                [row("8", "2026-03-01"), row("3", "2025-01-01"), row("1", "2026-03-01")])
        self.assertEqual([r["id"] for r in state["pending"]], ["3", "1", "8"])
        state = delivery.ingest("silverpalace", ROUTE, state, [row("4", "2024-01-01"), row("8")])
        self.assertEqual([r["id"] for r in state["pending"]], ["4", "3", "1", "8"])
        with self.assertRaises(delivery.DeliveryHold):
            delivery.ingest("silverpalace", "b" * 16, state, [])
        with self.assertRaises(delivery.DeliveryHold):
            delivery.ingest("silverpalace", ROUTE, state, None)
        with self.assertRaises(delivery.DeliveryHold):
            delivery.ingest("silverpalace", ROUTE, None, [row("1"), row("1")])
        end = delivery.ingest("endfield", ROUTE, None, [{"id": "2", "title": "two", "ts": 20},
                                                        {"id": "1", "title": "one", "ts": 10}])
        self.assertEqual([r["id"] for r in end["pending"]], ["1", "2"])

    def test_future_article_is_held_before_persist_or_send(self):
        state = delivery.ingest("silverpalace", ROUTE, None, [row("1", "2099-01-01")])
        store = Storage()
        calls = []
        async def render(article):
            calls.append(article)
            return [payload(article["id"])]
        async def send(page):
            calls.append(page)
        with self.assertRaises(delivery.DeliveryHold):
            asyncio.run(delivery.deliver_oldest(state, "silverpalace", ROUTE, as_of=NOW,
                                                checkpoint=store.checkpoint, render=render, send=send))
        self.assertEqual(store.calls, 0)
        self.assertEqual(calls, [])
        with self.assertRaises(delivery.DeliveryHold):
            delivery.ingest("silverpalace", ROUTE, None, [])

    def test_each_page_has_persisted_intent_and_confirmed_id_before_next(self):
        state = delivery.ingest("silverpalace", ROUTE, None, [row("1"), row("2")])
        store = Storage()
        sent = []
        pages = [payload("1"), payload("1")]
        pages[1] = copy.deepcopy(pages[1])
        pages[1]["components"][0]["components"][0]["content"] += " continued"

        async def render(article):
            self.assertEqual(article["id"], "1")
            return pages

        async def send(page):
            persisted = store.states[-1]
            self.assertTrue(persisted["partial"]["inflight"])
            self.assertEqual(len(persisted["partial"]["messages"]), len(sent))
            sent.append(page)
            return SimpleNamespace(ok=True, message_id=str(len(sent) + 100), ambiguous=False)

        result = asyncio.run(delivery.deliver_oldest(state, "silverpalace", ROUTE, as_of=NOW, checkpoint=store.checkpoint,
                                                     render=render, send=send))
        self.assertEqual(sent, pages)
        self.assertEqual(result["posted"]["1"]["messages"], ["101", "102"])
        self.assertEqual(result["pending"], [row("2")])
        self.assertIsNone(result["partial"])
        self.assertEqual(store.states[-1], result)
        self.assertEqual(state["posted"], {})
        self.assertNotIn("webhook", str(result).lower())
        after = delivery.ingest("silverpalace", ROUTE, result, [row("1"), row("2")])
        self.assertEqual(after["pending"], [row("2")])

    def test_reviewed_reconciliation_prevents_blind_resend_and_rejects_stale_evidence(self):
        state = delivery.ingest("silverpalace", ROUTE, None, [row("1")])
        store = Storage()
        calls = []

        async def render(candidate):
            return [payload("1"), payload("1")]

        async def ambiguous(card):
            calls.append(card)
            return SimpleNamespace(ok=False, ambiguous=True, message_id=None)

        with self.assertRaises(delivery.DeliveryHold):
            asyncio.run(delivery.deliver_oldest(state, "silverpalace", ROUTE, as_of=NOW,
                                               checkpoint=store.checkpoint, render=render, send=ambiguous))
        held = store.states[-1]
        original = copy.deepcopy(held)
        signature = held["partial"]["signature"]
        for kwargs in (
            {"article_id": "2", "page": 1, "signature": signature, "resolution": "delivered", "message_id": "101"},
            {"article_id": "1", "page": 2, "signature": signature, "resolution": "delivered", "message_id": "101"},
            {"article_id": "1", "page": 1, "signature": "b" * 64, "resolution": "delivered", "message_id": "101"},
            {"article_id": "1", "page": 1, "signature": signature, "resolution": "delivered", "message_id": None},
            {"article_id": "1", "page": 1, "signature": signature, "resolution": "absent", "message_id": "101"},
        ):
            with self.assertRaises(delivery.DeliveryHold):
                delivery.reconcile_inflight(held, "silverpalace", ROUTE, **kwargs)
        self.assertEqual(held, original)
        corrected = delivery.reconcile_inflight(held, "silverpalace", ROUTE, article_id="1", page=1,
                                                signature=signature, resolution="delivered", message_id="101")
        self.assertEqual(corrected["partial"]["messages"], ["101"])
        self.assertTrue(held["partial"]["inflight"])  # no silent state mutation
        self.assertTrue(asyncio.run(store.checkpoint(corrected)))

        async def confirmed(card):
            calls.append(card)
            return SimpleNamespace(ok=True, ambiguous=False, message_id="102")

        posted = asyncio.run(delivery.deliver_oldest(corrected, "silverpalace", ROUTE, as_of=NOW,
                                                     checkpoint=store.checkpoint, render=render, send=confirmed))
        self.assertEqual(posted["posted"]["1"]["messages"], ["101", "102"])
        self.assertEqual(len(calls), 2)  # initial ambiguous page and only the second page
        self.assertFalse(posted["pending"])

    def test_reconcile_absence_requires_explicit_resolution_and_preserves_page(self):
        state = delivery.ingest("silverpalace", ROUTE, None, [row("1")])
        store = Storage()

        async def render(candidate):
            return [payload("1")]

        async def lost(card):
            raise TimeoutError("unknown")

        with self.assertRaises(delivery.DeliveryHold):
            asyncio.run(delivery.deliver_oldest(state, "silverpalace", ROUTE, as_of=NOW,
                                               checkpoint=store.checkpoint, render=render, send=lost))
        held = store.states[-1]
        fixed = delivery.reconcile_inflight(held, "silverpalace", ROUTE, article_id="1", page=1,
                                            signature=held["partial"]["signature"], resolution="absent")
        self.assertFalse(fixed["partial"]["inflight"])
        self.assertEqual(fixed["partial"]["messages"], [])
        self.assertTrue(held["partial"]["inflight"])

    def test_bounded_pages_resume_without_marking_incomplete_article_posted(self):
        state = delivery.ingest("silverpalace", ROUTE, None, [row("1"), row("2")])
        store = Storage()
        pages = [payload("1"), payload("1"), payload("1")]
        calls = []
        async def render(article):
            return pages
        async def send(page):
            calls.append(page)
            return SimpleNamespace(ok=True, message_id=str(100 + len(calls)), ambiguous=False)
        state = asyncio.run(delivery.deliver_oldest(state, "silverpalace", ROUTE, as_of=NOW,
                            checkpoint=store.checkpoint, render=render, send=send, max_pages=2))
        self.assertEqual(state["partial"]["messages"], ["101", "102"])
        self.assertFalse(state["partial"]["inflight"])
        self.assertEqual(state["posted"], {})
        state = asyncio.run(delivery.deliver_oldest(state, "silverpalace", ROUTE, as_of=NOW,
                            checkpoint=store.checkpoint, render=render, send=send, max_pages=2))
        self.assertEqual(state["posted"]["1"]["messages"], ["101", "102", "103"])
        self.assertEqual([r["id"] for r in state["pending"]], ["2"])

    def test_ambiguous_result_or_failed_checkpoint_never_reposts(self):
        initial = delivery.ingest("silverpalace", ROUTE, None, [row("1")])

        async def render(article):
            return [payload(article["id"])]

        calls = []

        async def ambiguous(page):
            calls.append(page)
            return SimpleNamespace(ok=True, message_id=None, ambiguous=True)

        store = Storage()
        with self.assertRaises(delivery.DeliveryHold):
            asyncio.run(delivery.deliver_oldest(initial, "silverpalace", ROUTE, as_of=NOW, checkpoint=store.checkpoint,
                                                render=render, send=ambiguous))
        self.assertEqual(len(calls), 1)
        saved = store.states[-1]
        self.assertTrue(saved["partial"]["inflight"])
        with self.assertRaises(delivery.DeliveryHold):
            asyncio.run(delivery.deliver_oldest(saved, "silverpalace", ROUTE, as_of=NOW, checkpoint=store.checkpoint,
                                                render=render, send=ambiguous))
        self.assertEqual(len(calls), 1)
        store = Storage(fail_at=3)  # intent checkpoint fails, before sending
        with self.assertRaises(delivery.DeliveryHold):
            asyncio.run(delivery.deliver_oldest(initial, "silverpalace", ROUTE, as_of=NOW, checkpoint=store.checkpoint,
                                                render=render, send=ambiguous))
        self.assertEqual(len(calls), 1)

    def test_partial_resume_holds_changed_content_and_finishes_original(self):
        state = delivery.ingest("silverpalace", ROUTE, None, [row("1")])
        store = Storage(fail_at=5)  # confirmed page 1 persisted; page 2 intent fails
        pages = [payload("1"), payload("1")]

        async def render(article):
            return pages

        count = []
        async def send(page):
            count.append(page)
            return SimpleNamespace(ok=True, message_id=str(100 + len(count)), ambiguous=False)

        with self.assertRaises(delivery.DeliveryHold):
            asyncio.run(delivery.deliver_oldest(state, "silverpalace", ROUTE, as_of=NOW, checkpoint=store.checkpoint,
                                                render=render, send=send))
        saved = store.states[-1]
        self.assertEqual(saved["partial"]["messages"], ["101"])
        self.assertFalse(saved["partial"]["inflight"])
        async def changed(article):
            return [payload("2")]
        with self.assertRaises(delivery.DeliveryHold):
            asyncio.run(delivery.deliver_oldest(saved, "silverpalace", ROUTE, as_of=NOW, checkpoint=Storage().checkpoint,
                                                render=changed, send=send))
        clean = Storage()
        result = asyncio.run(delivery.deliver_oldest(saved, "silverpalace", ROUTE, as_of=NOW, checkpoint=clean.checkpoint,
                                                     render=render, send=send))
        self.assertEqual(result["posted"]["1"]["messages"], ["101", "102"])


if __name__ == "__main__":
    unittest.main()


def test_heading_moves_with_following_paragraph_without_losing_text():
    from gamexpress.cards import _keep_headings_with_following_text, news_payloads, text, validate_payload
    from gamexpress.config import Game
    from gamexpress.models import Item

    fragments = [text("Before\n\n**Important Notes**\n\n"), text("1. Only one work link.\n\n"), text("After")]
    joined = _keep_headings_with_following_text(fragments)
    assert "".join(x["content"] for x in joined) == "".join(x["content"] for x in fragments)
    assert joined[1]["content"] == "**Important Notes**\n\n1. Only one work link.\n\n"
    body = "x" * 3650 + "\n\n**Important Notes**\n\n1. Only one work link may be submitted. " + "word " * 45
    game = Game(key="nte", name="NTE", short="NTE", enabled=False, color=0)
    item = Item(source="nte", game="nte", id="1", url="https://example.com/a", title="Test", text="",
                published_ts=0, article_blocks=[{"kind": "text", "text": body}])
    pages = news_payloads(game, item, expand_full=True)
    assert len(pages) == 2 and all(not validate_payload(page) for page in pages)
    texts = ["\n".join(c.get("content", "") for box in page["components"] for c in box["components"])
             for page in pages]
    assert "**Important Notes**" not in texts[0]
    assert "**Important Notes**\n\n1. Only one work link" in texts[1]
    assert all("Excerpt —" not in part for part in texts)
