"""Experimental Git checkpoint for candidate-news write-ahead state.

Each successful call saves state/state.json, creates a state-only commit and pushes
it to the SAME checked-out branch, then verifies the remote ref. An ahead/behind
branch, a dirty index, any failed/ambiguous Git operation or an unexpected state
path fails closed. This alone does not provide cross-repository failover locking.
"""
from __future__ import annotations

import asyncio
import copy
import json
import secrets
import subprocess
from pathlib import Path

from .candidate_news_delivery import SOURCES, _validate


class GitCheckpoint:
    """Async checkpoint callback for one source; no caller may POST unless it returns True."""

    def __init__(self, root: Path, state, source: str, branch: str):
        self.root = root.resolve()
        self.state = state
        self.source = source
        self.branch = branch

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=30, check=False)

    def _head(self):
        result = self._git("rev-parse", "--verify", "HEAD")
        return result.stdout.strip() if result.returncode == 0 else None

    def _remote_head(self):
        result = self._git("ls-remote", "--heads", "origin", f"refs/heads/{self.branch}")
        lines = result.stdout.splitlines()
        if result.returncode != 0 or len(lines) != 1:
            return None
        parts = lines[0].split()
        return parts[0] if len(parts) == 2 and parts[1] == f"refs/heads/{self.branch}".encode() else None

    def _save(self, snapshot):
        try:
            if (self.source not in SOURCES or not isinstance(snapshot, dict)
                    or self.state.path.resolve() != self.root / "state/state.json"
                    or not self.branch or not isinstance(self.branch, str)):
                return False
            _validate(snapshot, self.source, snapshot.get("route"))
            current = self._git("symbolic-ref", "--quiet", "--short", "HEAD")
            valid = self._git("check-ref-format", "--branch", self.branch)
            if (current.returncode != 0 or current.stdout.strip() != self.branch.encode() or valid.returncode != 0
                    or self._git("diff", "--cached", "--quiet").returncode != 0):
                return False
            head = self._head()
            if head is None or self._remote_head() != head:
                return False
            # State.load can recover a corrupt/missing file by constructing an empty
            # state. Never treat that as a fresh backlog: compare all candidate records
            # against the committed, authoritative JSON before modifying anything.
            committed_blob = self._git("show", "HEAD:state/state.json")
            if committed_blob.returncode != 0:
                return False
            committed = json.loads(committed_blob.stdout)
            if not isinstance(committed, dict):
                return False
            remote_records = committed.get("candidate_news", {})
            records = self.state.data.get("candidate_news", {})
            if (not isinstance(remote_records, dict) or not isinstance(records, dict)
                    or records != remote_records):
                return False
            self.state.data["candidate_news"] = records
            records[self.source] = copy.deepcopy(snapshot)
            self.state.save()
            if self._git("add", "--", "state/state.json").returncode != 0:
                return False
            staged = self._git("diff", "--cached", "--name-only", "-z")
            if staged.returncode != 0 or set(filter(None, staged.stdout.split(b"\0"))) - {b"state/state.json"}:
                return False
            if staged.stdout:
                commit = self._git("-c", "user.name=github-actions[bot]",
                                   "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                                   "commit", "-m", f"auto: checkpoint candidate news {secrets.token_hex(16)} [skip ci]")
                if commit.returncode != 0:
                    return False
                pushed = self._git("push", "origin", f"HEAD:refs/heads/{self.branch}")
                if pushed.returncode != 0:
                    return False
            head = self._head()
            return head is not None and self._remote_head() == head
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            return False

    async def __call__(self, snapshot):
        return await asyncio.to_thread(self._save, snapshot)


class IsolatedGitLedger:
    """Experimental separate-file ledger in a disposable detached worktree.

    It never modifies the monitor checkout/index/HEAD; failed or ambiguous pushes
    poison the instance. A pre-existing, tracked ledger is mandatory: silently
    recreating a lost ledger could replay an entire backlog. The candidate monitor
    uses this isolated ledger; the older GitCheckpoint class above is not used there.
    """

    LEDGER = "state/candidate_news.json"

    def __init__(self, root: Path, branch: str, *, git_env=None):
        self.root = root.resolve()
        self.branch = branch
        self.git_env = git_env
        self.worktree = None
        self._temp = None
        self.data = None
        self.poisoned = False

    def _git(self, root, *args):
        return subprocess.run(["git", *args], cwd=root, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=30, check=False, env=self.git_env)

    def _remote(self):
        result = self._git(self.root, "ls-remote", "--heads", "origin", f"refs/heads/{self.branch}")
        lines = result.stdout.splitlines()
        if result.returncode or len(lines) != 1:
            return None
        parts = lines[0].split()
        return parts[0] if len(parts) == 2 and parts[1] == f"refs/heads/{self.branch}".encode() else None

    def _head(self, root):
        result = self._git(root, "rev-parse", "--verify", "HEAD")
        return result.stdout.strip() if result.returncode == 0 else None

    def _parse(self, content):
        value = json.loads(content)
        if not isinstance(value, dict) or set(value) != {"schema", "candidate_news"} or type(value["schema"]) is not int or value["schema"] != 1:
            raise ValueError("invalid candidate ledger")
        records = value["candidate_news"]
        if not isinstance(records, dict) or not set(records) <= SOURCES:
            raise ValueError("invalid candidate ledger")
        for source, record in records.items():
            _validate(record, source, record.get("route") if isinstance(record, dict) else None)
        return value

    def _open(self):
        import tempfile

        try:
            if not isinstance(self.branch, str) or not self.branch:
                return False
            current = self._git(self.root, "symbolic-ref", "--quiet", "--short", "HEAD")
            if (current.returncode or current.stdout.strip() != self.branch.encode()
                    or self._git(self.root, "check-ref-format", "--branch", self.branch).returncode):
                return False
            head = self._head(self.root)
            if not head or self._remote() != head:
                return False
            tree = self._git(self.root, "ls-tree", "HEAD", "--", self.LEDGER)
            if tree.returncode or not tree.stdout.startswith(b"100644 blob "):
                return False
            blob = self._git(self.root, "show", f"HEAD:{self.LEDGER}")
            if blob.returncode:
                return False
            self.data = self._parse(blob.stdout)
            self._temp = tempfile.TemporaryDirectory(prefix="candidate-ledger-")
            self.worktree = Path(self._temp.name) / "worktree"
            added = self._git(self.root, "worktree", "add", "--detach", "--quiet", str(self.worktree), "HEAD")
            return added.returncode == 0
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            return False

    def _initialize_empty(self, routes):
        """Create a never-before-tracked empty ledger with each source route bound.

        The caller must require explicit first-install and writer-pause attestations.
        History checks cannot see discarded/force-pushed history or external copies.
        A failed push may have succeeded remotely and must be inspected, not retried.
        """
        import tempfile

        try:
            if (self.poisoned or self.worktree is not None or not isinstance(self.branch, str) or not self.branch
                    or not isinstance(routes, dict) or set(routes) != SOURCES or len(set(routes.values())) != len(SOURCES)):
                return False
            records = {source: {"version": 1, "source": source, "route": routes[source], "pending": [],
                                "posted": {}, "partial": None} for source in sorted(SOURCES)}
            for source, record in records.items():
                _validate(record, source, routes[source])
            new = {"schema": 1, "candidate_news": records}
            current = self._git(self.root, "symbolic-ref", "--quiet", "--short", "HEAD")
            shallow = self._git(self.root, "rev-parse", "--is-shallow-repository")
            if (current.returncode or current.stdout.strip() != self.branch.encode()
                    or self._git(self.root, "check-ref-format", "--branch", self.branch).returncode
                    or shallow.returncode or shallow.stdout.strip() != b"false"):
                return False
            head = self._head(self.root)
            if not head or self._remote() != head:
                return False
            directory = self._git(self.root, "ls-tree", "HEAD", "--", "state")
            existing = self._git(self.root, "ls-tree", "HEAD", "--", self.LEDGER)
            history = self._git(self.root, "log", "--all", "--format=%H", "--", self.LEDGER)
            if (directory.returncode or not directory.stdout.startswith(b"040000 tree ")
                    or existing.returncode or existing.stdout or history.returncode or history.stdout):
                return False
            self._temp = tempfile.TemporaryDirectory(prefix="candidate-ledger-init-")
            self.worktree = Path(self._temp.name) / "worktree"
            if self._git(self.root, "worktree", "add", "--detach", "--quiet", str(self.worktree), "HEAD").returncode:
                return False
            state_dir = self.worktree / "state"
            path = self.worktree / self.LEDGER
            if state_dir.is_symlink() or not state_dir.is_dir() or path.exists() or path.is_symlink():
                return False
            path.write_text(json.dumps(new, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if self._git(self.worktree, "add", "--", self.LEDGER).returncode:
                return False
            staged = self._git(self.worktree, "diff", "--cached", "--name-only", "-z")
            if staged.returncode or staged.stdout != self.LEDGER.encode() + b"\0":
                return False
            commit = self._git(self.worktree, "-c", "user.name=github-actions[bot]",
                               "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                               "commit", "-m", f"auto: initialize candidate ledger {secrets.token_hex(16)} [skip ci]")
            if commit.returncode or self._git(self.worktree, "push", "origin", f"HEAD:refs/heads/{self.branch}").returncode:
                return False
            new_head = self._head(self.worktree)
            if not new_head or self._remote() != new_head:
                return False
            self.data = new
            return True
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            return False

    async def initialize_empty(self, routes):
        ok = await asyncio.to_thread(self._initialize_empty, routes)
        if not ok:
            self.poisoned = True
        await asyncio.to_thread(self._close)
        return ok

    def _save(self, source, snapshot):
        try:
            if self.poisoned or self.worktree is None or self.data is None or source not in SOURCES:
                return False
            _validate(snapshot, source, snapshot.get("route") if isinstance(snapshot, dict) else None)
            head = self._head(self.worktree)
            if not head or self._remote() != head:
                return False
            blob = self._git(self.worktree, "show", f"HEAD:{self.LEDGER}")
            if blob.returncode or self._parse(blob.stdout) != self.data:
                return False
            new = copy.deepcopy(self.data)
            new["candidate_news"][source] = copy.deepcopy(snapshot)
            path = self.worktree / self.LEDGER
            path.write_text(json.dumps(new, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            if self._git(self.worktree, "add", "--", self.LEDGER).returncode:
                return False
            staged = self._git(self.worktree, "diff", "--cached", "--name-only", "-z")
            if staged.returncode or set(filter(None, staged.stdout.split(b"\0"))) - {self.LEDGER.encode()}:
                return False
            if staged.stdout:
                # Identical concurrent writers can otherwise create the same commit
                # (same parent/tree/author/second), causing both pushes to succeed.
                # A unique commit makes the branch update a meaningful CAS fence.
                commit = self._git(self.worktree, "-c", "user.name=github-actions[bot]",
                                   "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                                   "commit", "-m", f"auto: checkpoint candidate news {secrets.token_hex(16)} [skip ci]")
                if commit.returncode:
                    return False
                if self._git(self.worktree, "push", "origin", f"HEAD:refs/heads/{self.branch}").returncode:
                    return False
            head = self._head(self.worktree)
            if not head or self._remote() != head:
                return False
            self.data = new
            return True
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            return False

    def _close(self):
        try:
            if self.worktree is not None:
                self._git(self.root, "worktree", "remove", "--force", str(self.worktree))
        finally:
            if self._temp is not None:
                self._temp.cleanup()
            self.worktree = self._temp = None

    async def open(self):
        ok = await asyncio.to_thread(self._open)
        if not ok:
            self.poisoned = True
            await asyncio.to_thread(self._close)
        return ok

    async def checkpoint(self, source, snapshot):
        ok = await asyncio.to_thread(self._save, source, snapshot)
        if not ok:
            self.poisoned = True
        return ok

    async def close(self):
        await asyncio.to_thread(self._close)


async def reconcile_checkpointed(ledger, source, route, *, article_id, page, signature, resolution, message_id=None):
    """Persist a human-reviewed correction; never post or resume automatically.

    Every writer must be paused, and the operator must independently verify the
    Discord outcome. If the Git outcome is unclear, inspect the authoritative
    branch before doing anything else. This primitive is not an operator UI.
    """
    from .candidate_news_delivery import DeliveryHold, reconcile_inflight

    if ledger.poisoned or not isinstance(ledger.data, dict):
        raise DeliveryHold("authoritative ledger unavailable")
    records = ledger.data.get("candidate_news")
    if not isinstance(records, dict) or source not in records:
        raise DeliveryHold("candidate record unavailable")
    corrected = reconcile_inflight(records[source], source, route, article_id=article_id, page=page,
                                   signature=signature, resolution=resolution, message_id=message_id)
    if not await ledger.checkpoint(source, corrected):
        raise DeliveryHold("reconciliation checkpoint not confirmed; inspect the authoritative branch")
    return corrected
