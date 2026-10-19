"""JSON state (committed back to the repo by the workflow) — dedup + card message ids.

Design rules (lessons from News-Express):
  * the file only changes when something meaningful happens (post / edit / new
    code seen / hourly heartbeat) -> no commit-per-run noise;
  * atomic writes (tmp file + os.replace) so a killed run never corrupts it;
  * peer merge: an instance can import another instance's posted keys so a
    fail-over standby never double-posts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from pathlib import Path

SCHEMA = 1


def empty_state() -> dict:
    return {"schema": SCHEMA, "heartbeat": {}, "bootstrapped": {}, "schedule": {}, "codes": {},
            "icons": {}}


def stable_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


class State:
    def __init__(self, path: Path, data: dict | None = None) -> None:
        self.path = path
        self.data = data or empty_state()
        for k, v in empty_state().items():
            self.data.setdefault(k, copy.deepcopy(v))
        self._original = stable_hash(self._comparable())

    @classmethod
    def load(cls, path: Path) -> State:
        if path.exists():
            try:
                return cls(path, json.loads(path.read_text(encoding="utf-8") or "{}"))
            except json.JSONDecodeError:
                backup = path.with_suffix(".corrupt.json")
                path.replace(backup)
        return cls(path)

    def _comparable(self) -> dict:
        return self.data

    @property
    def changed(self) -> bool:
        return stable_hash(self._comparable()) != self._original

    def save(self) -> bool:
        if not self.changed:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)
        self._original = stable_hash(self._comparable())
        return True

    # ------------------------------------------------------------------ bootstrap
    def is_bootstrapped(self, feature: str, game: str) -> bool:
        return bool(self.data["bootstrapped"].get(f"{feature}:{game}"))

    def mark_bootstrapped(self, feature: str, game: str) -> None:
        self.data["bootstrapped"][f"{feature}:{game}"] = int(time.time())

    # ------------------------------------------------------------------ schedule
    def schedule_records(self, game: str) -> dict:
        return self.data["schedule"].setdefault(game, {})

    def schedule_record(self, game: str, version: str) -> dict | None:
        return self.data["schedule"].get(game, {}).get(version)

    # ------------------------------------------------------------------ codes
    def code_records(self, game: str) -> dict:
        return self.data["codes"].setdefault(game, {})

    # ------------------------------------------------------------------ heartbeat
    def heartbeat(self, instance: str, every_min: int, now: int) -> None:
        hb = self.data["heartbeat"]
        if (now - int(hb.get("last_success") or 0) >= every_min * 60 or hb.get("instance") != instance
                or hb.get("every_min") != every_min):
            hb.update({"last_success": now, "instance": instance, "every_min": every_min})

    # ------------------------------------------------------------------ peer merge
    def merge_peer(self, peer: dict, *, include_news: bool = False) -> int:
        """Import posted/seeded keys (and message ids) from the other instance.
        Returns how many keys were imported."""
        imported = 0
        for game, recs in (peer.get("schedule") or {}).items():
            mine = self.schedule_records(game)
            for ver, rec in recs.items():
                if ver not in mine and rec.get("status") in ("posted", "seeded", "live"):
                    mine[ver] = copy.deepcopy(rec)
                    mine[ver]["imported_from_peer"] = True
                    imported += 1
                elif ver in mine and not mine[ver].get("message_id") and rec.get("message_id"):
                    mine[ver]["message_id"] = rec["message_id"]
                    mine[ver]["status"] = rec.get("status", mine[ver].get("status"))
                    imported += 1
        for game, codes in (peer.get("codes") or {}).items():
            mine = self.code_records(game)
            for code, rec in codes.items():
                if code not in mine and rec.get("status") in ("posted", "seeded"):
                    mine[code] = copy.deepcopy(rec)
                    imported += 1
        for key, ts in (peer.get("bootstrapped") or {}).items():
            self.data["bootstrapped"].setdefault(key, ts)
        if include_news:
            for game, other in (peer.get("news") or {}).items():
                mine = self.news_record(game)
                if mine.get("route") and other.get("route") and mine["route"] != other["route"]:
                    continue
                if other.get("route"):
                    mine.setdefault("route", other["route"])
                if "floor" in other:
                    mine["floor"] = max(mine.get("floor", 0), other["floor"])
                records = mine.setdefault("records", {})
                for key, rec in (other.get("records") or {}).items():
                    if rec.get("status") not in ("posted", "partial"):
                        continue
                    current = records.get(key)
                    if current and current.get("status") == "posted":
                        continue
                    if current and rec.get("status") == "partial" and (
                            current.get("signature") != rec.get("signature")
                            or current.get("messages") != rec.get("messages", [])[:len(current.get("messages", []))]):
                        continue
                    if key in records or len(records) < 500:
                        records[key] = copy.deepcopy(rec)
                        imported += 1
        return imported

    def prune(self, now: int, keep_versions: int = 6, code_days: int = 120) -> None:
        for recs in self.data["schedule"].values():
            if len(recs) > keep_versions:
                from .textutil import version_key
                for ver in sorted(recs, key=version_key)[:-keep_versions]:
                    recs.pop(ver, None)
        for codes in self.data["codes"].values():
            for code in [c for c, r in codes.items()
                         if now - int(r.get("last_seen") or r.get("first_seen") or now) > code_days * 86400]:
                codes.pop(code, None)


    def news_record(self, game: str) -> dict:
        return self.data.setdefault("news", {}).setdefault(game, {})

    def prune_news(self, now: int) -> None:
        """30-day retention / 500 completed entries; never evict inside the 48h eligibility window.

        Admission in the runner enforces a 500-record bound. Safe eviction advances an
        irreversible publication cutoff so an old feed entry cannot become new again.
        """
        for game in (self.data.get("news") or {}).values():
            records = game.get("records") or {}
            for key, rec in list(records.items()):
                if rec.get("status") == "posted" and now - rec.get("ts", now) > 30 * 86400:
                    game["floor"] = max(game.get("floor", 0), rec["ts"])
                    records.pop(key)
            if len(records) >= 500:
                for key, rec in sorted(list(records.items()), key=lambda pair: pair[1].get("ts", now)):
                    if len(records) < 500:
                        break
                    if rec.get("status") == "posted" and rec.get("ts", now) < now - 48 * 3600:
                        game["floor"] = max(game.get("floor", 0), rec["ts"])
                        records.pop(key)
