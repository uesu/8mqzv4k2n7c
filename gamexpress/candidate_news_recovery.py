"""Attended candidate-news reconciliation; never sends to Discord.

The Actions workflow must serialize with the monitor, and the operator must pause
all schedulers and writers including any other repository. The tool cannot prove
Discord delivery or that external writers are paused. A failed/ambiguous Git
operation requires inspecting the remote before attempting another correction.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import UTC, datetime

from . import candidate_news_delivery as delivery
from .candidate_news_checkpoint import IsolatedGitLedger, reconcile_checkpointed
from .candidate_news_delivery import SOURCES, DeliveryHold, _validate, destination
from .candidate_news_runner import MAX_PAGES_PER_SOURCE_RUN, authoritative_branch
from .config import ROOT, load_settings


def inspect_record(ledger, source, route):
    """Describe only a validated ledger record; never use a webhook or change state."""
    if ledger.poisoned or not isinstance(ledger.data, dict):
        raise DeliveryHold("authoritative ledger unavailable")
    record = ledger.data.get("candidate_news", {}).get(source)
    if record is None:
        raise DeliveryHold("candidate record unavailable")
    record = _validate(record, source, route)
    partial = record["partial"]
    result = {"source": source, "pending": len(record["pending"]), "posted": len(record["posted"]),
              "partial": partial is not None}
    if partial:
        result.update({"article_id": partial["id"], "pages": partial["pages"],
                       "confirmed_pages": len(partial["messages"]), "inflight": partial["inflight"],
                       "signature": partial["signature"]})
        if partial["inflight"]:
            result["held_page"] = len(partial["messages"]) + 1
    return result


async def reviewed_resolution(ledger, source, route, *, article_id, page, signature, resolution,
                              message_id=None, writers_paused=False, evidence_verified=False):
    """Apply only an explicitly attested, independently verified operator decision."""
    if writers_paused is not True or evidence_verified is not True:
        raise DeliveryHold("operator attestations required")
    inspect_record(ledger, source, route)
    return await reconcile_checkpointed(ledger, source, route, article_id=article_id, page=page,
                                        signature=signature, resolution=resolution, message_id=message_id)


async def preview_record(ledger, settings, source, route, fetcher, *, as_of):
    """Read a live snapshot and render the next candidate without checkpointing or posting."""
    summary = inspect_record(ledger, source, route)
    if summary.get("inflight"):
        raise DeliveryHold("held page requires reviewed reconciliation before preview")
    if type(as_of) is not int or not 0 < as_of <= 253402300799:
        raise DeliveryHold("invalid observation time")
    prior = ledger.data["candidate_news"][source]
    rows = await delivery.discover(fetcher, source)
    state = delivery.ingest(source, route, prior, rows)
    result = {"source": source, "pending": len(state["pending"]), "posted": len(state["posted"]),
              "sends": 0, "checkpoints": 0}
    if not state["pending"]:
        return result
    row = state["pending"][0]
    today = datetime.fromtimestamp(as_of, UTC).date().isoformat()
    if (source == "endfield" and row["ts"] > as_of) or (source != "endfield" and row["date"] > today):
        raise DeliveryHold("future-dated article held")
    pages, files = await delivery.render_candidate_animated(fetcher, settings, source, row)
    if not pages:
        raise DeliveryHold("no preview pages")
    signature = hashlib.sha256(json.dumps(pages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    partial = state["partial"]
    if partial and (partial["signature"] != signature or partial["pages"] != len(pages)):
        raise DeliveryHold("partial article changed; reconciliation required")
    confirmed = len(partial["messages"]) if partial else 0
    result.update({"article_id": row["id"], "pages": len(pages), "confirmed_pages": confirmed,
                   "pages_next_run_limit": min(MAX_PAGES_PER_SOURCE_RUN, len(pages) - confirmed),
                   "gif_attachments": len(files)})
    return result


async def run_from_environment():
    """Run only on the explicitly configured primary Actions repository and branch."""
    settings = load_settings()
    source = os.environ.get("RECOVERY_SOURCE", "")
    mode = os.environ.get("RECOVERY_MODE", "")
    if mode not in ("inspect", "preview", "resolve", "initialize") or (mode != "initialize" and source not in SOURCES):
        raise DeliveryHold("invalid recovery request")
    branch = authoritative_branch(settings, ROOT)
    if branch is None:
        raise DeliveryHold("authority unavailable")
    ledger = IsolatedGitLedger(ROOT, branch)
    if mode == "initialize":
        if (os.environ.get("RECOVERY_WRITERS_PAUSED") != "true"
                or os.environ.get("RECOVERY_INIT_CONFIRM") != "FIRST_INSTALL_BACKFILL"):
            raise DeliveryHold("first-install attestations required")
        targets = {candidate: destination(settings, candidate) for candidate in sorted(SOURCES)}
        if any(target is None for target in targets.values()) or len({target[1] for target in targets.values()}) != len(SOURCES):
            raise DeliveryHold("distinct dedicated destinations required")
        routes = {candidate: target[1] for candidate, target in targets.items()}
        if not await ledger.initialize_empty(routes):
            raise DeliveryHold("ledger initialization not confirmed")
        print("Empty, route-bound ledger checkpointed. No discovery or Discord messages sent; keep writers paused until setup is reviewed.")
        return
    target = destination(settings, source)
    if target is None:
        raise DeliveryHold("dedicated destination unavailable")
    route = target[1]
    if not await ledger.open():
        raise DeliveryHold("authoritative ledger unavailable")
    try:
        if mode == "inspect":
            print(json.dumps(inspect_record(ledger, source, route), sort_keys=True))
            return
        if mode == "preview":
            if os.environ.get("RECOVERY_WRITERS_PAUSED") != "true":
                raise DeliveryHold("writer-pause attestation required")
            import aiohttp

            from .http import BOT_UA, Fetcher

            async with aiohttp.ClientSession(headers={"User-Agent": BOT_UA}) as session:
                fetcher = Fetcher(session, timeout=settings.http_timeout)
                result = await preview_record(ledger, settings, source, route, fetcher, as_of=int(time.time()))
            print(json.dumps(result, sort_keys=True))
            return
        page_text = os.environ.get("RECOVERY_PAGE", "")
        if not page_text.isascii() or not page_text.isdecimal() or len(page_text) > 3:
            raise DeliveryHold("invalid held page")
        mid = os.environ.get("RECOVERY_MESSAGE_ID", "") or None
        await reviewed_resolution(
            ledger, source, route,
            article_id=os.environ.get("RECOVERY_ARTICLE_ID", ""), page=int(page_text),
            signature=os.environ.get("RECOVERY_SIGNATURE", ""),
            resolution=os.environ.get("RECOVERY_RESOLUTION", ""), message_id=mid,
            writers_paused=os.environ.get("RECOVERY_WRITERS_PAUSED") == "true",
            evidence_verified=os.environ.get("RECOVERY_EVIDENCE_VERIFIED") == "true",
        )
        print("Reviewed correction checkpointed. No Discord messages sent; keep writers paused until reviewed.")
    finally:
        await ledger.close()


if __name__ == "__main__":
    try:
        asyncio.run(run_from_environment())
    except Exception:  # noqa: BLE001 — no secrets, remote response or exceptions in logs
        raise SystemExit("Recovery held. Inspect the authoritative ledger and destination before any retry.") from None
