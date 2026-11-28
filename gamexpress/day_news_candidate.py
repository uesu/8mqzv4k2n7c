"""Unregistered ID-based planning for day-only news listings.

This pure prototype is not wired into ``run_news`` or persisted anywhere. A caller
must provide a *verified complete* snapshot, persist each returned state atomically,
and mark an item complete only after confirmed delivery. Calendar days are never
converted to instants. No webhook URL belongs in this state, only its fingerprint.
"""
from __future__ import annotations

import copy
import re
from datetime import date

MAX_IDS = 10000
_ID = re.compile(r"[0-9]{1,32}\Z")
_ROUTE = re.compile(r"[a-f0-9]{16}\Z")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


class InvalidSnapshot(ValueError):
    """The snapshot/state cannot safely be used; caller must not persist a transition."""


def _valid_id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _row(value):
    if not isinstance(value, dict) or set(value) != {"id", "date", "title"}:
        raise InvalidSnapshot("invalid row")
    cid, day, title = value.get("id"), value.get("date"), value.get("title")
    if (not _valid_id(cid) or not isinstance(day, str) or not _DAY.fullmatch(day)
            or not isinstance(title, str) or not title.strip() or len(title) > 512):
        raise InvalidSnapshot("invalid row")
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise InvalidSnapshot("invalid date")
    except ValueError as exc:
        raise InvalidSnapshot("invalid date") from exc
    return {"id": cid, "date": day, "title": title.strip()}


def _validate_state(state, route):
    if (not isinstance(route, str) or not _ROUTE.fullmatch(route)
            or not isinstance(state, dict) or set(state) != {"version", "route", "known", "pending"}
            or type(state.get("version")) is not int
            or state["version"] != 1 or state.get("route") != route):
        raise InvalidSnapshot("state version or destination mismatch")
    known, pending = state.get("known"), state.get("pending")
    if not isinstance(known, list) or not isinstance(pending, list):
        raise InvalidSnapshot("invalid state")
    if (len(known) > MAX_IDS or any(not _valid_id(cid) for cid in known)
            or len(set(known)) != len(known) or len(pending) > MAX_IDS):
        raise InvalidSnapshot("invalid known IDs")
    clean_pending = [_row(row) for row in pending]
    ids = [row["id"] for row in clean_pending]
    if len(ids) != len(set(ids)) or not set(ids).issubset(known):
        raise InvalidSnapshot("invalid pending IDs")
    return {"version": 1, "route": route, "known": list(known), "pending": clean_pending}


def plan(state, snapshot, *, route, bootstrap_backfill=False):
    """Plan baseline/discovery without mutating input; ``None`` snapshot fails closed.

    Default remains a silent baseline. Explicit backfill instead queues the entire
    initial snapshot in ascending displayed day, with an ID tie-breaker (not a
    claim of time-of-day order). Caller must persist pending before any send,
    stop on failure, and reconcile partial/ambiguous deliveries before advancing.
    Later newly observed IDs are queued regardless of day, including backdated
    articles. Pending survives source failures or disappearance. This is not
    production wiring or proof of a complete, atomic source snapshot.
    """
    if not isinstance(route, str) or not _ROUTE.fullmatch(route):
        raise InvalidSnapshot("destination fingerprint required")
    if not isinstance(snapshot, list) or len(snapshot) > MAX_IDS:
        raise InvalidSnapshot("complete snapshot required")
    rows = [_row(row) for row in snapshot]
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise InvalidSnapshot("duplicate snapshot IDs")
    if type(bootstrap_backfill) is not bool:
        raise InvalidSnapshot("invalid backfill setting")
    if state is None:
        pending = sorted(rows, key=lambda row: (row["date"], int(row["id"]), row["id"])) if bootstrap_backfill else []
        return {"version": 1, "route": route, "known": ids, "pending": pending}
    next_state = _validate_state(state, route)
    known = set(next_state["known"])
    new = [row for row in rows if row["id"] not in known]
    if len(known) + len(new) > MAX_IDS:
        raise InvalidSnapshot("ID capacity reached; operator review required")
    next_state["known"].extend(row["id"] for row in new)
    next_state["pending"].extend(new)
    next_state["pending"].sort(key=lambda row: (row["date"], int(row["id"]), row["id"]))
    return next_state


def confirm_delivered(state, cid, *, route):
    """Return a copy with one pending ID removed after caller confirms delivery.

    This cannot solve the external webhook-accepted/state-save-failed ambiguity or
    partial multi-message delivery. Those require the real runner's delivery protocol.
    """
    next_state = _validate_state(state, route)
    if not _valid_id(cid) or not next_state["pending"] or next_state["pending"][0]["id"] != cid:
        raise InvalidSnapshot("ID is not the oldest pending item")
    result = copy.deepcopy(next_state)
    result["pending"] = [row for row in result["pending"] if row["id"] != cid]
    return result
