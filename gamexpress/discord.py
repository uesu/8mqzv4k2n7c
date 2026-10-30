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
WEBHOOK_SPACING = 1.2   # local conservative spacing, not a published Discord quota


@dataclass
class SendResult:
    ok: bool
    status: int
    message_id: str | None = None
    error: str = ""
    ambiguous: bool = False


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


def _redact_webhook(value: object) -> str:
    """Redact `/webhooks/<id>/<token>` path secrets in logs and persisted error strings."""
    return re.sub(r"/webhooks/[^/\s?#]+/[^\s/?#)'\"]+", "/webhooks/…", str(value))


def _is_non_retryable_pre_send_error(exc: BaseException) -> bool:
    """True when URL validation fails locally before opening any network connection."""
    return isinstance(exc, (aiohttp.InvalidURL, aiohttp.NonHttpUrlClientError)) and not isinstance(
        exc, aiohttp.RedirectClientError
    )


def _is_definite_pre_send_error(exc: BaseException) -> bool:
    """True only when failure occurred strictly during connection/TLS/tunnel setup before
    any HTTP request bytes could reach the target origin."""
    return isinstance(
        exc,
        (
            aiohttp.ClientConnectorError,
            aiohttp.ConnectionTimeoutError,
            aiohttp.ServerFingerprintMismatch,
            aiohttp.ClientHttpProxyError,
        ),
    )


MENTION_PARSE = ("roles", "users", "everyone")


def _edit_allowed_mentions(source: object) -> dict:
    """The mention permissions the card was POSTED with, re-sanitised for a PATCH.

    Editing never notifies — Discord decides that from the request type, not from
    `allowed_mentions` — so the only thing this field changes on a PATCH is whether a mention
    still RENDERS. Blanking it to `{"parse": []}` demoted the card's `<@&…>` to plain text on its
    first edit and silently dropped the role-colour highlight it had carried since it was posted.
    Seen live on WuWa 3.8, 2026-10-30: posted 11:00, edited 15:25 to add the Watch button, and the
    orange highlight was gone while the mention text stayed.

    Deliberately not "allow everything": `parse` is filtered to the three documented keys and only
    ids the card itself declared are carried, so a scraped `@everyone`, `@here` or `@username`
    inside a title can never become a live mention by way of an edit.
    """
    am = source if isinstance(source, dict) else {}
    out: dict = {"parse": [p for p in (am.get("parse") or []) if p in MENTION_PARSE]}
    for key in ("roles", "users"):
        raw = am.get(key)
        ids = [str(i) for i in (raw if isinstance(raw, (list, tuple)) else [])
               if str(i).strip().isdigit()]
        if ids:
            out[key] = ids[:100]
    return out


class WebhookClient:
    def __init__(self, session: aiohttp.ClientSession, dry_run: bool = False) -> None:
        self.session = session
        self.dry_run = dry_run
        self.sent: list[dict] = []   # dry-run / audit log
        self._next_slot = 0.0        # monotonic time the next request is allowed to go out

    async def _pace(self) -> None:
        """Space requests out WITHOUT paying for the gap after the last one.

        This local conservative pacer is not a guarantee of Discord capacity.
        Route/resource limits can change; 429 handling remains necessary. There used to
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

    async def _request(
        self,
        method: str,
        url: str,
        payload: dict,
        *,
        stop_on_ambiguous: bool = False,
    ) -> SendResult:
        problems = validate_payload(payload)
        if problems:
            return SendResult(False, 0, error="invalid payload: " + "; ".join(problems))
        if self.dry_run:
            # The fingerprint, never the URL: it is enough to tell two destinations apart (which
            # is what the fan-out tests assert) and it cannot be turned back into a secret.
            self.sent.append({"method": method, "payload": payload, "fp": webhook_fingerprint(url)})
            log.info("DRY RUN %s %s\n%s", method, _redact_webhook(url),
                     json.dumps(payload, ensure_ascii=False)[:1500])
            return SendResult(True, 200, message_id="dry-run")
        await self._pace()
        delay = 1.0
        for attempt in range(5):
            try:
                async with self.session.request(method, url, json=payload,
                                                timeout=aiohttp.ClientTimeout(total=30)) as r:
                    status = int(getattr(r, "status", 0) or 0)
                    try:
                        body = await r.text()
                    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError) as e:
                        safe_err = _redact_webhook(e)
                        if stop_on_ambiguous and method == "POST" and (status == 0 or 200 <= status < 400):
                            log.warning(
                                "Discord POST response body read failed after HTTP %s (%s)"
                                " — holding as ambiguous without retry",
                                status,
                                safe_err,
                            )
                            return SendResult(
                                False,
                                status,
                                error=f"response body read failed after HTTP {status}: {safe_err}",
                                ambiguous=True,
                            )
                        if isinstance(e, UnicodeDecodeError):
                            body = ""
                        else:
                            raise
                    if status in (200, 204):
                        mid = None
                        if body:
                            try:
                                mid = str(json.loads(body).get("id") or "") or None
                            except json.JSONDecodeError:
                                mid = None
                        return SendResult(
                            True,
                            status,
                            message_id=mid,
                            ambiguous=(stop_on_ambiguous and method == "POST" and mid is None),
                        )
                    if status == 429:
                        try:
                            retry = float(json.loads(body).get("retry_after", 1.0))
                        except (json.JSONDecodeError, TypeError, ValueError):
                            try:
                                retry = float(r.headers.get("Retry-After", 1.0))
                            except (TypeError, ValueError):
                                retry = 1.0
                        log.warning("Discord 429 — retrying in %.2fs", retry)
                        await asyncio.sleep(min(retry, 30) + 0.25)
                        continue
                    if status >= 500:
                        log.warning("Discord %s — retry %d", status, attempt + 1)
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
                    return SendResult(False, status, error=_redact_webhook(body)[:500])
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                safe_err = _redact_webhook(e)
                if stop_on_ambiguous:
                    if _is_non_retryable_pre_send_error(e):
                        return SendResult(False, 0, error=safe_err, ambiguous=False)
                    if method == "POST" and not _is_definite_pre_send_error(e):
                        log.warning(
                            "Discord POST outcome indeterminate (%s) — holding as ambiguous without retry",
                            safe_err,
                        )
                        return SendResult(
                            False,
                            0,
                            error=f"indeterminate POST outcome: {safe_err}",
                            ambiguous=True,
                        )
                log.warning("Discord request error %s — retry %d", safe_err, attempt + 1)
                await asyncio.sleep(delay)
                delay *= 2
        return SendResult(False, 0, error="gave up after retries")

    async def send(
        self,
        webhook_url: str,
        payload: dict,
        *,
        stop_on_ambiguous: bool = False,
    ) -> SendResult:
        base, keep = _split(webhook_url)
        q = urlencode({"wait": "true", "with_components": "true", **keep})
        return await self._request(
            "POST",
            f"{base}?{q}",
            payload,
            stop_on_ambiguous=stop_on_ambiguous,
        )

    async def edit(self, webhook_url: str, message_id: str, payload: dict) -> SendResult:
        base, keep = _split(webhook_url)
        q = urlencode({"with_components": "true", **keep})
        body = {k: v for k, v in payload.items() if k in ("components", "flags")}
        # The permissions the card was POSTED with, not a blank. An edit cannot notify (that is
        # Discord's rule, independent of `allowed_mentions`), so all this decides is whether the
        # mention still renders -- see `_edit_allowed_mentions`.
        body["allowed_mentions"] = _edit_allowed_mentions(payload.get("allowed_mentions"))
        return await self._request("PATCH", f"{base}/messages/{message_id}?{q}", body)
