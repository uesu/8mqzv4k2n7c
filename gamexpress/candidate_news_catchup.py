"""Attended, paced candidate catch-up on the single authoritative branch.

This is a live sender, not a preview. It stops on any hold, uncertain Git operation,
operator deadline or lack of progress. It never initializes or resets the ledger.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from types import SimpleNamespace

from . import candidate_news_delivery as delivery
from . import candidate_news_recovery as recovery
from . import candidate_news_runner as runner
from .candidate_news_checkpoint import IsolatedGitLedger
from .config import ROOT, load_settings

SOURCES = ("endfield", "nte", "silverpalace")
MIN_SOURCE_INTERVAL = 5
# Leave a generous margin before the GitHub-hosted six-hour job limit.
MAX_RUNTIME_SECONDS = 5 * 60 * 60


class CatchupHold(ValueError):
    """Stop the attended catch-up; never automatically retry an uncertain send."""


async def catch_up(inspect, run_one, *, clock=time.monotonic, wait=asyncio.sleep,
                   deadline_seconds=MAX_RUNTIME_SECONDS):
    """Run one bounded source at a time, stopping on uncertainty or no progress.

    `inspect` must read the authoritative ledger, validate all routes, and reject
    any in-flight page. `run_one` must raise on *any* warning, never retry it.
    """
    if deadline_seconds <= 0:
        raise CatchupHold("invalid time budget")
    deadline = clock() + deadline_seconds
    before = await inspect()  # all routes and held pages checked before any send
    while True:
        for source in SOURCES:
            if clock() >= deadline:
                raise CatchupHold("time budget reached; review before resuming")
            await run_one(source)
            await wait(MIN_SOURCE_INTERVAL)
        after = await inspect()
        if all(after[s]["pending"] == 0 and not after[s]["partial"] for s in SOURCES):
            return after
        if all(after[s] == before[s] for s in SOURCES):
            raise CatchupHold("no progress; review before resuming")
        before = after


def _refresh(branch):
    """Fast-forward the clean monitor checkout after detached-ledger checkpoints."""
    try:
        status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=30, check=False)
        if status.returncode or status.stdout:
            return False
        result = subprocess.run(["git", "pull", "--ff-only", "origin", branch], cwd=ROOT,
                                capture_output=True, stdin=subprocess.DEVNULL, timeout=60, check=False)
        return result.returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


async def run_from_environment():
    """Require explicit approval, primary identity and a previously bound ledger."""
    settings = load_settings()
    if (os.environ.get("CATCHUP_WRITERS_PAUSED") != "true"
            or os.environ.get("CATCHUP_CONFIRM") != "START_CANDIDATE_BACKFILL"
            or not runner.CANDIDATE_NEWS_READY or settings.dry_run or settings.test_mode
            or not settings.news_enabled or "news" not in settings.features
            or settings.env.get("NEWS_CANDIDATES_BACKFILL", "").strip() != "1"
            or not set(SOURCES) <= settings.news_games or settings.games_filter):
        raise CatchupHold("catch-up not explicitly configured and approved")
    branch = runner.authoritative_branch(settings, ROOT)
    if branch is None or not await asyncio.to_thread(_refresh, branch):
        raise CatchupHold("primary authority or clean checkout unavailable")
    targets = {source: delivery.destination(settings, source) for source in SOURCES}
    if any(target is None for target in targets.values()) or len({target[1] for target in targets.values()}) != len(SOURCES):
        raise CatchupHold("three distinct dedicated routes required")

    async def inspect():
        if not await asyncio.to_thread(_refresh, branch):
            raise CatchupHold("authoritative branch update not confirmed")
        ledger = IsolatedGitLedger(ROOT, branch)
        if not await ledger.open():
            raise CatchupHold("authoritative ledger unavailable")
        try:
            result = {}
            for source in SOURCES:
                record = recovery.inspect_record(ledger, source, targets[source][1])
                if record.get("inflight"):
                    raise CatchupHold("ambiguous page requires attended recovery")
                result[source] = record
            return result
        finally:
            await ledger.close()

    import aiohttp

    from .http import BOT_UA, Fetcher

    async with aiohttp.ClientSession(headers={"User-Agent": BOT_UA}) as session:
        fetcher = Fetcher(session, timeout=settings.http_timeout)

        async def run_one(source):
            if not await asyncio.to_thread(_refresh, branch):
                raise CatchupHold("authoritative branch update not confirmed")
            settings.games_filter = {source}
            ctx = SimpleNamespace(settings=settings, active=True, now=int(time.time()), fetcher=fetcher,
                                  webhook=SimpleNamespace(session=session), report=[], warnings=[])
            await runner.run_candidate_news(ctx)
            if ctx.warnings or not ctx.report:
                raise CatchupHold(f"{source}: held; inspect ledger before resuming")
            print(f"{source}: {ctx.report[-1]}", flush=True)

        result = await catch_up(inspect, run_one)
    print("All three sources caught up to their checked snapshots; no more backlog was seen in this pass.")
    for source in SOURCES:
        print(f"{source}: recorded={result[source]['posted']} pending=0")


if __name__ == "__main__":
    try:
        asyncio.run(run_from_environment())
    except Exception:  # noqa: BLE001 — no secret URLs or raw errors in logs
        raise SystemExit("Catch-up held. Inspect the authoritative ledger and Discord before any retry.") from None
