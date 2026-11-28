"""Opt-in, conservative candidate news backfill path; disabled unless explicitly configured.

The primary and an explicitly opted-in standby must use the SAME authoritative
Git branch and ledger. Each page requires a verified Git checkpoint and a one-shot
POST. Unknown outcomes hold. Separate-repository failover is experimental and not
production-validated. This path is not activated by adding a game to NEWS_GAMES alone.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import candidate_news_delivery as delivery
from .candidate_news_checkpoint import IsolatedGitLedger
from .config import ROOT

# Release switch: ordinary use still requires explicit backfill opt-in, primary
# authority, dedicated routes, and an attended route-bound ledger initialization.
CANDIDATE_NEWS_READY = True

# Backfill is intentionally slow: at most one article / three pages per source per run.
MAX_PAGES_PER_SOURCE_RUN = 3


def _valid_repository(value):
    """Accept only simple owner/repository names, not URL paths or traversal."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", value):
        return False
    return all(part not in (".", "..") for part in value.split("/"))


def _checkout_is_current(root, branch):
    """Do not let stale standby code act on a newer shared ledger."""
    try:
        def git(*args):
            return subprocess.run(["git", *args], cwd=root, stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=30, check=False)
        current = git("symbolic-ref", "--quiet", "--short", "HEAD")
        head = git("rev-parse", "--verify", "HEAD")
        remote = git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
        lines = remote.stdout.splitlines()
        return (not current.returncode and current.stdout.strip() == branch.encode()
                and not head.returncode and not remote.returncode and len(lines) == 1
                and lines[0].split() == [head.stdout.strip(), f"refs/heads/{branch}".encode()])
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _origin_is_authority(root, repository):
    """Require the checkout's actual origin to be the nominated GitHub repository."""
    try:
        result = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=10, check=False)
        url = result.stdout.decode("utf-8").strip()
        parts = urlsplit(url)
        return (result.returncode == 0 and parts.scheme == "https" and parts.hostname == "github.com"
                and not parts.username and not parts.password and parts.port in (None, 443)
                and not parts.query and not parts.fragment
                and parts.path.removeprefix("/").removesuffix(".git").lower() == repository.lower())
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return False


def authoritative_branch(settings, root):
    """Return only an explicitly designated primary Actions branch; no auto-failover."""
    repository = str(settings.env.get("NEWS_CANDIDATES_AUTHORITY_REPOSITORY", "") or "").strip()
    branch = str(settings.env.get("NEWS_CANDIDATES_AUTHORITY_BRANCH", "") or "").strip()
    if (settings.instance_role != "primary" or not _valid_repository(repository)
            or not branch or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_EVENT_NAME") not in ("workflow_dispatch", "schedule")
            or os.environ.get("GITHUB_REPOSITORY", "").lower() != repository.lower()
            or os.environ.get("GITHUB_REF_NAME", "") != branch
            or os.environ.get("GITHUB_REF", "") != f"refs/heads/{branch}"
            or not _origin_is_authority(root, repository)):
        return None
    return branch


def standby_authority(settings, root):
    """Validate both Actions checkout identity and explicit shared-ledger standby opt-in.

    This does not make the standby active: the caller must also enforce the existing
    failover heartbeat decision. Credentials never appear in URLs or error messages.
    """
    env = settings.env
    repository = str(env.get("NEWS_CANDIDATES_AUTHORITY_REPOSITORY", "") or "").strip()
    branch = str(env.get("NEWS_CANDIDATES_AUTHORITY_BRANCH", "") or "").strip()
    standby_repo = str(env.get("NEWS_CANDIDATES_STANDBY_REPOSITORY", "") or "").strip()
    standby_branch = str(env.get("NEWS_CANDIDATES_STANDBY_BRANCH", "") or "").strip()
    token = str(env.get("NEWS_CANDIDATES_LEDGER_TOKEN", "") or "").strip()
    if (settings.instance_role != "standby" or env.get("NEWS_CANDIDATES_STANDBY_ENABLED", "").strip() != "1"
            or not _valid_repository(repository) or not _valid_repository(standby_repo)
            or repository.lower() == standby_repo.lower() or not branch or not standby_branch
            or len(branch) > 240 or len(standby_branch) > 240
            or not re.fullmatch(r"[!-~]{20,2048}", token)
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_EVENT_NAME") not in ("workflow_dispatch", "schedule")
            or os.environ.get("GITHUB_REPOSITORY", "").lower() != standby_repo.lower()
            or os.environ.get("GITHUB_REF_NAME", "") != standby_branch
            or os.environ.get("GITHUB_REF", "") != f"refs/heads/{standby_branch}"
            or not _origin_is_authority(root, standby_repo)):
        return None
    try:
        for name in (branch, standby_branch):
            valid = subprocess.run(["git", "check-ref-format", "--branch", name], cwd=root,
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
            if valid.returncode:
                return None
    except (OSError, subprocess.SubprocessError):
        return None
    if not _checkout_is_current(root, standby_branch):
        return None
    return repository, branch, token


def _standby_git_environment(token):
    """Pass a scoped GitHub authorization header via environment, never on disk or argv."""
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("GIT_") and not re.search(r"TOKEN|SECRET|PASSWORD|WEBHOOK|API_KEY", name, re.I)}
    auth = base64.b64encode(("x-access-token:" + token).encode("ascii")).decode("ascii")
    env.update({"GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + auth,
                "GIT_CONFIG_KEY_1": "http.followRedirects", "GIT_CONFIG_VALUE_1": "false",
                "GIT_CONFIG_KEY_2": "protocol.file.allow", "GIT_CONFIG_VALUE_2": "never", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
    return env


class StandbyLedgerCheckout:
    """Disposable checkout of the one authoritative ledger, using step-scoped auth."""

    def __init__(self, repository, branch, token):
        self.repository = repository
        self.branch = branch
        self.env = _standby_git_environment(token)
        self.temp = None
        self.root = None

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=60, check=False).returncode == 0

    def _open(self):
        try:
            self.temp = tempfile.TemporaryDirectory(prefix="candidate-standby-ledger-")
            self.root = Path(self.temp.name)
            return (self._git("-c", "init.templateDir=/dev/null", "init", "--quiet")
                    and self._git("remote", "add", "origin", f"https://github.com/{self.repository}.git")
                    and self._git("fetch", "--no-tags", "--depth=1", "origin", f"refs/heads/{self.branch}")
                    and self._git("checkout", "--quiet", "-B", self.branch, "FETCH_HEAD"))
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def _close(self):
        try:
            if self.temp is not None:
                self.temp.cleanup()
        finally:
            self.root = self.temp = None
            self.env.clear()

    async def open(self):
        ok = await asyncio.to_thread(self._open)
        if not ok:
            await self.close()
        return ok

    async def close(self):
        await asyncio.to_thread(self._close)


async def run_candidate_news(ctx):
    settings = ctx.settings
    if (not ctx.active or "news" not in settings.features or not settings.news_enabled
            or settings.env.get("NEWS_CANDIDATES_BACKFILL", "").strip() != "1"):
        return
    selected = sorted(delivery.SOURCES & settings.news_games)
    if settings.games_filter:
        selected = [source for source in selected if source in settings.games_filter]
    if not selected:
        return
    if not CANDIDATE_NEWS_READY:
        ctx.warnings.append("Candidate news: delivery is not released; no discovery or posts")
        return
    if settings.dry_run or settings.test_mode:
        ctx.report.append("Candidate news: live backfill disabled in dry-run/test mode")
        return
    branch = authoritative_branch(settings, ROOT)
    standby_checkout = None
    if branch is not None:
        ledger = IsolatedGitLedger(ROOT, branch)
    else:
        standby = standby_authority(settings, ROOT)
        if standby is None:
            ctx.warnings.append("Candidate news: explicit primary repository/branch authority unavailable; no posts")
            return
        repository, branch, token = standby
        standby_checkout = StandbyLedgerCheckout(repository, branch, token)
        if not await standby_checkout.open():
            ctx.warnings.append("Candidate news: shared authority checkout unavailable; no posts")
            return
        ledger = IsolatedGitLedger(standby_checkout.root, branch, git_env=standby_checkout.env)
    if not await ledger.open():
        if standby_checkout is not None:
            await standby_checkout.close()
        ctx.warnings.append("Candidate news: authoritative ledger unavailable or stale; no posts")
        return
    try:
        for source in selected:
            if ledger.poisoned:
                ctx.warnings.append("Candidate news: checkpoint failed; remaining sources held")
                break
            target = delivery.destination(settings, source)
            if target is None:
                ctx.warnings.append(f"{source} news: dedicated destination missing or invalid; no discovery or posts")
                continue
            _, fingerprint = target
            try:
                # Read each record from the separately checkpointed authoritative ledger.
                prior = ledger.data["candidate_news"].get(source)
                if prior is None:
                    # Only attended first-install initialization can bind a source route.
                    # Neither primary nor standby may silently create missing records.
                    ctx.warnings.append(f"{source} news: initialized route missing; source held")
                    continue
                delivery._validate(prior, source, fingerprint)
                rows = await delivery.discover(ctx.fetcher, source)
                state = delivery.ingest(source, fingerprint, prior, rows)
                if not state["pending"]:
                    ctx.report.append(f"{source} news: caught up ({len(state['posted'])} recorded)")
                    continue

                animation_assets = {}

                async def checkpoint(snapshot, *, _source=source):
                    return await ledger.checkpoint(_source, snapshot)

                async def render(row, *, _source=source, _assets=animation_assets):
                    pages, files = await delivery.render_candidate_animated(ctx.fetcher, settings, _source, row)
                    _assets.clear()
                    _assets.update(files)
                    return pages

                async def send(payload, *, _source=source, _assets=animation_assets):
                    # Git checkpoints usually exceed this delay, but never rely on that for pacing.
                    await asyncio.sleep(1.5)
                    attachments = payload.get("attachments") or []
                    if attachments:
                        files = {entry["filename"]: _assets[entry["filename"]] for entry in attachments}
                        return await delivery.send_candidate_once(ctx.webhook.session, settings, _source, payload, files=files)
                    return await delivery.send_candidate_once(ctx.webhook.session, settings, _source, payload)

                state = await delivery.deliver_oldest(state, source, fingerprint, as_of=ctx.now,
                                                      checkpoint=checkpoint, render=render, send=send,
                                                      max_pages=MAX_PAGES_PER_SOURCE_RUN)
                pages = len(state["partial"]["messages"]) if state["partial"] else 0
                ctx.report.append(f"{source} news: queued={len(state['pending'])} recorded={len(state['posted'])} "
                                  f"partial-pages={pages}")
            except Exception as exc:  # noqa: BLE001 — hold this source without affecting unrelated news
                # Never interpolate exceptions: HTTP clients can include secret URLs.
                ctx.warnings.append(f"{source} news: held ({type(exc).__name__}); inspect before retry")
    finally:
        try:
            await ledger.close()
        finally:
            if standby_checkout is not None:
                await standby_checkout.close()
