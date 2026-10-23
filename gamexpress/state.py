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


def _as_dict_slot(container: dict, key: str) -> dict:
    cur = container.get(key)
    if not isinstance(cur, dict):
        cur = {}
        container[key] = cur
    return cur


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


class State:
    def __init__(self, path: Path, data: dict | None = None) -> None:
        self.path = path
        self.data = data if isinstance(data, dict) else empty_state()
        for k, v in empty_state().items():
            if not isinstance(self.data.get(k), type(v)):
                self.data[k] = copy.deepcopy(v)
        self._original = stable_hash(self._comparable())

    @classmethod
    def load(cls, path: Path) -> State:
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
                return cls(path, loaded if isinstance(loaded, dict) else {})
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
        return bool(_as_dict_slot(self.data, "bootstrapped").get(f"{feature}:{game}"))

    def mark_bootstrapped(self, feature: str, game: str) -> None:
        _as_dict_slot(self.data, "bootstrapped")[f"{feature}:{game}"] = int(time.time())

    # ------------------------------------------------------------------ schedule
    def schedule_records(self, game: str) -> dict:
        sched = _as_dict_slot(self.data, "schedule")
        return _as_dict_slot(sched, game)

    def schedule_record(self, game: str, version: str) -> dict | None:
        recs = self.schedule_records(game)
        val = recs.get(version)
        return val if isinstance(val, dict) else None

    # ------------------------------------------------------------------ codes
    def code_records(self, game: str) -> dict:
        codes = _as_dict_slot(self.data, "codes")
        return _as_dict_slot(codes, game)

    # ------------------------------------------------------------------ heartbeat
    def heartbeat(self, instance: str, every_min: int, now: int) -> None:
        hb = _as_dict_slot(self.data, "heartbeat")
        if (now - _safe_int(hb.get("last_success"), 0) >= every_min * 60 or hb.get("instance") != instance
                or hb.get("every_min") != every_min):
            hb.update({"last_success": now, "instance": instance, "every_min": every_min})

    # ------------------------------------------------------------------ peer merge
    def merge_peer(self, peer: dict, *, include_news: bool = False,
                   include_maintenance: bool = False) -> int:
        """Import posted/seeded keys (and message ids) from the other instance.
        Returns how many keys were imported."""
        if not isinstance(peer, dict):
            return 0
        imported = 0
        peer_sched = peer.get("schedule")
        for game, recs in (peer_sched.items() if isinstance(peer_sched, dict) else ()):
            if not isinstance(recs, dict):
                continue
            mine = self.schedule_records(game)
            for ver, rec in recs.items():
                if not isinstance(rec, dict):
                    continue
                cur = mine.get(ver)
                if ver not in mine and rec.get("status") in ("posted", "seeded", "live"):
                    mine[ver] = copy.deepcopy(rec)
                    mine[ver]["imported_from_peer"] = True
                    imported += 1
                elif isinstance(cur, dict) and not cur.get("message_id") and rec.get("message_id"):
                    cur["message_id"] = rec["message_id"]
                    cur["status"] = rec.get("status", cur.get("status"))
                    imported += 1
        peer_codes = peer.get("codes")
        for game, codes in (peer_codes.items() if isinstance(peer_codes, dict) else ()):
            if not isinstance(codes, dict):
                continue
            mine = self.code_records(game)
            for code, rec in codes.items():
                if not isinstance(rec, dict):
                    continue
                if code not in mine and rec.get("status") in ("posted", "seeded"):
                    mine[code] = copy.deepcopy(rec)
                    imported += 1
        peer_boot = peer.get("bootstrapped")
        boot_slot = _as_dict_slot(self.data, "bootstrapped")
        for key, ts in (peer_boot.items() if isinstance(peer_boot, dict) else ()):
            boot_slot.setdefault(key, ts)
        if include_news:
            peer_news = peer.get("news")
            for game, other in (peer_news.items() if isinstance(peer_news, dict) else ()):
                if not isinstance(other, dict):
                    continue
                mine = self.news_record(game)
                if mine.get("route") and other.get("route") and mine["route"] != other["route"]:
                    continue
                if other.get("route"):
                    mine.setdefault("route", other["route"])
                if "floor" in other:
                    mine["floor"] = max(_safe_int(mine.get("floor"), 0), _safe_int(other.get("floor"), 0))
                records = _as_dict_slot(mine, "records")
                other_records = other.get("records")
                for key, rec in (other_records.items() if isinstance(other_records, dict) else ()):
                    if not isinstance(rec, dict) or rec.get("status") not in ("posted", "partial"):
                        continue
                    current = records.get(key)
                    if isinstance(current, dict) and current.get("status") == "posted":
                        continue
                    if isinstance(current, dict) and current.get("ambiguous") and not rec.get("ambiguous"):
                        continue
                    if isinstance(current, dict) and rec.get("status") == "partial" and (
                            current.get("signature") != rec.get("signature")
                            or current.get("messages") != rec.get("messages", [])[:len(current.get("messages", []))]):
                        continue
                    if key in records or len(records) < 500:
                        records[key] = copy.deepcopy(rec)
                        imported += 1
        if include_maintenance:
            peer_maint = peer.get("maintenance")
            for game, other in (peer_maint.items() if isinstance(peer_maint, dict) else ()):
                if not isinstance(other, dict):
                    continue
                mine = self.maintenance_record(game)
                if mine.get("route") and other.get("route") and mine["route"] != other["route"]:
                    continue
                if other.get("route"):
                    mine.setdefault("route", other["route"])
                if "seeded_at" in other:
                    mine["seeded_at"] = max(_safe_int(mine.get("seeded_at"), 0), _safe_int(other.get("seeded_at"), 0))
                versions = _as_dict_slot(mine, "versions")
                other_versions = other.get("versions")
                for ver, vrec in (other_versions.items() if isinstance(other_versions, dict) else ()):
                    if not isinstance(vrec, dict):
                        continue
                    dst = _as_dict_slot(versions, ver)
                    if "launch_media" in vrec and "launch_media" not in dst:
                        dst["launch_media"] = copy.deepcopy(vrec["launch_media"])
                    if isinstance(vrec.get("seen_media"), list) and vrec["seen_media"]:
                        merged_seen = list(dst.get("seen_media") or [])
                        for loc in vrec["seen_media"]:
                            if isinstance(loc, str) and loc and loc not in merged_seen:
                                merged_seen.append(loc)
                        dst["seen_media"] = merged_seen[-12:]
                    for kind in ("preload", "maintenance"):
                        src_a = vrec.get(kind)
                        if not isinstance(src_a, dict):
                            continue
                        if src_a.get("status") not in ("posted", "seeded", "ambiguous"):
                            continue
                        cur_a = dst.get(kind)
                        if isinstance(cur_a, dict) and cur_a.get("status") in ("posted", "ambiguous"):
                            continue
                        dst[kind] = copy.deepcopy(src_a)
                        imported += 1
        return imported

    def prune(self, now: int, keep_versions: int = 6, code_days: int = 120) -> None:
        sched = _as_dict_slot(self.data, "schedule")
        for recs in sched.values():
            if isinstance(recs, dict) and len(recs) > keep_versions:
                from .textutil import version_key
                for ver in sorted(recs, key=version_key)[:-keep_versions]:
                    recs.pop(ver, None)
        codes_all = _as_dict_slot(self.data, "codes")
        for codes in codes_all.values():
            if not isinstance(codes, dict):
                continue
            for code in [
                c for c, r in codes.items()
                if isinstance(r, dict)
                and now - _safe_int(r.get("last_seen") or r.get("first_seen"), now) > code_days * 86400
            ]:
                codes.pop(code, None)

    def maintenance_record(self, game: str) -> dict:
        maint = _as_dict_slot(self.data, "maintenance")
        rec = _as_dict_slot(maint, game)
        _as_dict_slot(rec, "versions")
        return rec

    def prune_maintenance(self, keep_versions: int = 8) -> None:
        maint = self.data.get("maintenance")
        if not isinstance(maint, dict):
            return
        for game in maint.values():
            if not isinstance(game, dict):
                continue
            versions = game.get("versions")
            if isinstance(versions, dict) and len(versions) > keep_versions:
                from .textutil import version_key
                for ver in sorted(versions, key=version_key)[:-keep_versions]:
                    versions.pop(ver, None)

    def news_record(self, game: str) -> dict:
        news = _as_dict_slot(self.data, "news")
        rec = _as_dict_slot(news, game)
        _as_dict_slot(rec, "records")
        if "floor" in rec and not isinstance(rec["floor"], int):
            rec["floor"] = _safe_int(rec.get("floor"), 0)
        return rec

    def prune_news(self, now: int) -> None:
        """30-day retention / 500 completed entries; never evict inside the 48h eligibility window.

        Admission in the runner enforces a 500-record bound. Safe eviction advances an
        irreversible publication cutoff so an old feed entry cannot become new again.
        """
        news = self.data.get("news")
        if not isinstance(news, dict):
            return
        for game in news.values():
            if not isinstance(game, dict):
                continue
            records = game.get("records")
            if not isinstance(records, dict):
                continue
            for key, rec in list(records.items()):
                if not isinstance(rec, dict):
                    continue
                rec_ts = _safe_int(rec.get("ts"), now)
                if rec.get("status") == "posted" and now - rec_ts > 30 * 86400:
                    game["floor"] = max(_safe_int(game.get("floor"), 0), rec_ts)
                    records.pop(key)
            if len(records) >= 500:
                for key, rec in sorted(
                    [(k, r) for k, r in records.items() if isinstance(r, dict)],
                    key=lambda pair: _safe_int(pair[1].get("ts"), now),
                ):
                    if len(records) < 500:
                        break
                    rec_ts = _safe_int(rec.get("ts"), now)
                    if rec.get("status") == "posted" and rec_ts < now - 48 * 3600:
                        game["floor"] = max(_safe_int(game.get("floor"), 0), rec_ts)
                        records.pop(key)
