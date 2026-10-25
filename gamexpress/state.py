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
BETA_FORMAT_EPOCH = 2


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
                   include_maintenance: bool = False, include_beta: bool = False) -> int:
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
        if include_beta:
            peer_beta = peer.get("beta")
            for game, other in (peer_beta.items() if isinstance(peer_beta, dict) else ()):
                if not isinstance(other, dict):
                    continue
                mine = self.beta_record(game)
                if mine.get("route") and other.get("route") and mine["route"] != other["route"]:
                    continue
                if other.get("route"):
                    mine.setdefault("route", other["route"])
                other_epoch = _safe_int(other.get("format_epoch"), 0)
                mine_epoch = _safe_int(mine.get("format_epoch"), 0)
                if other_epoch < BETA_FORMAT_EPOCH and mine_epoch >= BETA_FORMAT_EPOCH:
                    continue
                if other_epoch >= BETA_FORMAT_EPOCH and mine_epoch < BETA_FORMAT_EPOCH:
                    mine["revisions"] = {}
                    mine["snapshots"] = {}
                    mine["locked_images"] = {}
                    mine.pop("latest_rev", None)
                    mine.pop("source_version", None)
                    mine.pop("source_revision", None)
                    mine["last_poll"] = 0
                    mine.pop("paused", None)
                    mine["format_epoch"] = other_epoch
                elif other_epoch > mine_epoch:
                    mine["format_epoch"] = other_epoch
                if "seeded_at" in other:
                    mine["seeded_at"] = max(_safe_int(mine.get("seeded_at"), 0), _safe_int(other.get("seeded_at"), 0))
                if other.get("latest_rev") and not mine.get("latest_rev"):
                    mine["latest_rev"] = str(other["latest_rev"])
                revs = _as_dict_slot(mine, "revisions")
                other_revs = other.get("revisions")
                for rev_key, rrec in (other_revs.items() if isinstance(other_revs, dict) else ()):
                    if not isinstance(rrec, dict):
                        continue
                    if rrec.get("status") not in ("posted", "seeded", "partial", "ambiguous", "silent"):
                        continue
                    cur_r = revs.get(rev_key)
                    if not isinstance(cur_r, dict):
                        revs[rev_key] = copy.deepcopy(rrec)
                        imported += 1
                        continue
                    # An ambiguous outcome always wins: a conflict must hold rather than make a
                    # standby replay a possibly-delivered Discord page.
                    if cur_r.get("status") == "ambiguous":
                        continue
                    if rrec.get("status") == "ambiguous":
                        cur_r.update(copy.deepcopy(rrec))
                        imported += 1
                        continue
                    if cur_r.get("status") in ("posted", "silent"):
                        continue

                    changed = False
                    mine_processed = {str(k) for k in cur_r.get("processed", [])}
                    peer_processed = {str(k) for k in rrec.get("processed", [])}
                    if peer_processed - mine_processed:
                        cur_r["processed"] = sorted(mine_processed | peer_processed)
                        changed = True
                    mine_batches = _as_dict_slot(cur_r, "batches")
                    peer_batches = rrec.get("batches")
                    for batch_key, peer_batch in (peer_batches.items()
                                                   if isinstance(peer_batches, dict) else ()):
                        if not isinstance(peer_batch, dict):
                            continue
                        local_batch = mine_batches.get(batch_key)
                        if not isinstance(local_batch, dict):
                            mine_batches[batch_key] = copy.deepcopy(peer_batch)
                            changed = True
                            continue
                        local_msgs = list(local_batch.get("messages") or [])
                        peer_msgs = list(peer_batch.get("messages") or [])
                        if ((local_msgs or peer_msgs) and
                                local_batch.get("signature") != peer_batch.get("signature")):
                            local_batch["ambiguous"] = True
                            cur_r["status"] = "ambiguous"
                            changed = True
                        elif peer_msgs[:len(local_msgs)] == local_msgs and len(peer_msgs) > len(local_msgs):
                            local_batch.update(copy.deepcopy(peer_batch))
                            changed = True
                        elif local_msgs[:len(peer_msgs)] != peer_msgs:
                            local_batch["ambiguous"] = True
                            cur_r["status"] = "ambiguous"
                            changed = True
                    for field in ("label", "source_version", "source_revision", "more"):
                        if field in rrec and (field not in cur_r or rrec.get(field) != cur_r.get(field)):
                            cur_r[field] = copy.deepcopy(rrec[field])
                            changed = True
                    # A peer may finish a later batch while this instance has also processed
                    # work the peer has never seen. Only adopt its terminal state when its
                    # cursor covers our pre-merge cursor; otherwise remain partial and safe.
                    peer_covers_mine = mine_processed <= peer_processed
                    if (rrec.get("status") in ("posted", "seeded", "silent")
                            and not rrec.get("more") and peer_covers_mine
                            and cur_r.get("status") != "ambiguous"):
                        cur_r["status"] = rrec["status"]
                        changed = True
                    if changed:
                        imported += 1
                if other.get("source_version") and not mine.get("source_version"):
                    mine["source_version"] = str(other["source_version"])
                if other.get("source_revision") and not mine.get("source_revision"):
                    mine["source_revision"] = str(other["source_revision"])
                snaps = _as_dict_slot(mine, "snapshots")
                other_snaps = other.get("snapshots")
                for skey, srec in (other_snaps.items() if isinstance(other_snaps, dict) else ()):
                    if not isinstance(srec, dict):
                        continue
                    cur_s = snaps.get(skey)
                    if not isinstance(cur_s, dict) or _safe_int(srec.get("ts"), 0) > _safe_int(cur_s.get("ts"), 0):
                        snaps[skey] = copy.deepcopy(srec)
                imgs = _as_dict_slot(mine, "locked_images")
                other_imgs = other.get("locked_images")
                for ikey, irec in (other_imgs.items() if isinstance(other_imgs, dict) else ()):
                    if not isinstance(irec, dict):
                        continue
                    cur_i = imgs.get(ikey)
                    if not isinstance(cur_i, dict) or (irec.get("locked") and not cur_i.get("locked")):
                        imgs[ikey] = copy.deepcopy(irec)
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

    def beta_record(self, game: str) -> dict:
        beta = _as_dict_slot(self.data, "beta")
        is_fresh = not isinstance(beta.get(game), dict)
        rec = _as_dict_slot(beta, game)
        if is_fresh:
            rec["format_epoch"] = BETA_FORMAT_EPOCH
        _as_dict_slot(rec, "revisions")
        _as_dict_slot(rec, "snapshots")
        _as_dict_slot(rec, "locked_images")
        return rec

    def prune_beta(self, keep_revisions: int = 16, keep_snapshots: int = 80) -> None:
        beta = self.data.get("beta")
        if not isinstance(beta, dict):
            return
        for game in beta.values():
            if not isinstance(game, dict):
                continue
            revs = game.get("revisions")
            if isinstance(revs, dict) and len(revs) > keep_revisions:
                ordered = sorted(
                    revs.keys(),
                    key=lambda k: (_safe_int((revs.get(k) or {}).get("ts"), 0), str(k)),
                )
                for k in ordered[:-keep_revisions]:
                    revs.pop(k, None)
            snaps = game.get("snapshots")
            if isinstance(snaps, dict) and len(snaps) > keep_snapshots:
                ordered_s = sorted(
                    snaps.keys(),
                    key=lambda k: (_safe_int((snaps.get(k) or {}).get("ts"), 0), str(k)),
                )
                for k in ordered_s[:-keep_snapshots]:
                    snaps.pop(k, None)
            imgs = game.get("locked_images")
            if isinstance(imgs, dict) and len(imgs) > keep_snapshots:
                ordered_i = sorted(
                    imgs.keys(),
                    key=lambda k: (_safe_int((imgs.get(k) or {}).get("ts"), 0), str(k)),
                )
                for k in ordered_i[:-keep_snapshots]:
                    imgs.pop(k, None)

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
