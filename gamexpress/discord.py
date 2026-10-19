"""Discord webhook client for Components V2 payloads.

POST  /webhooks/{id}/{token}?wait=true&with_components=true      -> message (id kept for edits)
PATCH /webhooks/{id}/{token}/messages/{mid}?with_components=true -> in-place card update
`with_components=true` is REQUIRED for non-application webhooks to send (non-interactive)
components — link buttons are non-interactive, so channel webhooks work.
A ?thread_id=… already present in the webhook URL (forum / thread channels) is preserved.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

import aiohttp

from .cards import validate_payload

log = logging.getLogger("gamexpress.discord")

WEBHOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(\d+)/([\w-]+)")
WEBHOOK_SPACING = 1.2   # seconds between two requests — well under the 5 req / 2 s webhook bucket


@dataclass
class SendResult:
    ok: bool
    status: int
    message_id: str | None = None
    error: str = ""


def webhook_fingerprint(url: str) -> str:
    """Short, non-reversible id of a webhook (stored in state instead of the secret URL)."""
    m = WEBHOOK_RE.match(url or "")
    return hashlib.sha256((m.group(1) if m else url or "").encode()).hexdigest()[:12]


def _split(url: str) -> tuple[str, dict]:
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")
    params = dict(parse_qsl(parts.query))
    keep = {k: v for k, v in params.items() if k == "thread_id"}
    return base, keep


class WebhookClient:
    def __init__(self, session: aiohttp.ClientSession, dry_run: bool = False) -> None:
        self.session = session
        self.dry_run = dry_run
        self.sent: list[dict] = []   # dry-run / audit log
        self._next_slot = 0.0        # monotonic time the next request is allowed to go out

    async def _pace(self) -> None:
        """Space requests out WITHOUT paying for the gap after the last one.

        Discord's webhook bucket is 5 requests / 2 s, so posts have to be spaced. That used to
        be an unconditional `sleep(WEBHOOK_SPACING)` *after* every successful send — including
        after the final one, where there is nothing left to space against, so every posting run
        ended with 1.2 s of dead time (and n posts cost n gaps instead of n-1). Waiting *before*
        a request keeps exactly the same distance between two real requests and gives the rest
        of the time back. Deliberately one global pacer, matching the old behaviour: it is also
        correct when two games share a webhook.
        """
        now = time.monotonic()
        if now < self._next_slot:
            await asyncio.sleep(self._next_slot - now)
        self._next_slot = time.monotonic() + WEBHOOK_SPACING

    async def _request(self, method: str, url: str, payload: dict) -> SendResult:
        problems = validate_payload(payload)
        if problems:
            return SendResult(False, 0, error="invalid payload: " + "; ".join(problems))
        if self.dry_run:
            # The fingerprint, never the URL: it is enough to tell two destinations apart (which
            # is what the fan-out tests assert) and it cannot be turned back into a secret.
            self.sent.append({"method": method, "payload": payload, "fp": webhook_fingerprint(url)})
            log.info("DRY RUN %s %s\n%s", method, re.sub(r"/webhooks/\d+/[\w-]+", "/webhooks/…", url),
                     json.dumps(payload, ensure_ascii=False)[:1500])
            return SendResult(True, 200, message_id="dry-run")
        await self._pace()
        delay = 1.0
        for attempt in range(5):
            try:
                async with self.session.request(method, url, json=payload,
                                                timeout=aiohttp.ClientTimeout(total=30)) as r:
                    body = await r.text()
                    if r.status in (200, 204):
                        mid = None
                        if body:
                            try:
                                mid = str(json.loads(body).get("id") or "") or None
                            except json.JSONDecodeError:
                                mid = None
                        return SendResult(True, r.status, message_id=mid)
                    if r.status == 429:
                        try:
                            retry = float(json.loads(body).get("retry_after", 1.0))
                        except (json.JSONDecodeError, TypeError, ValueError):
                            retry = float(r.headers.get("Retry-After", 1.0))
                        log.warning("Discord 429 — retrying in %.2fs", retry)
                        await asyncio.sleep(min(retry, 30) + 0.25)
                        continue
                    if r.status >= 500:
                        log.warning("Discord %s — retry %d", r.status, attempt + 1)
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
                    return SendResult(False, r.status, error=body[:500])
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Discord request error %s — retry %d", e, attempt + 1)
                await asyncio.sleep(delay)
                delay *= 2
        return SendResult(False, 0, error="gave up after retries")

    async def send(self, webhook_url: str, payload: dict) -> SendResult:
        base, keep = _split(webhook_url)
        q = urlencode({"wait": "true", "with_components": "true", **keep})
        return await self._request("POST", f"{base}?{q}", payload)

    async def edit(self, webhook_url: str, message_id: str, payload: dict) -> SendResult:
        base, keep = _split(webhook_url)
        q = urlencode({"with_components": "true", **keep})
        body = {k: v for k, v in payload.items() if k in ("components", "flags", "allowed_mentions")}
        body["allowed_mentions"] = {"parse": []}   # edits never ping
        return await self._request("PATCH", f"{base}/messages/{message_id}?{q}", body)
