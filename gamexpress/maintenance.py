"""Independent fourth feature: Pre-Load Started and Maintenance Started announcements.

Design guarantees:
  * Disabled by default (`MAINTENANCE_ENABLED=0`, `MAINTENANCE_GAMES=""`), with per-game opt-in,
    dedicated per-game webhooks (`DISCORD_WEBHOOK_MAINTENANCE_<GAME>`), and optional per-game
    pings (`PING_MAINTENANCE_<GAME>`).
  * Works with or without schedule, codes, or news enabled, keeping the existing 5-minute
    `cron-job.org` cadence unchanged.
  * Precedence: official notices/tweets > explicit `config/overrides.json` entries > persisted
    first-observed launcher pre-download signal (explicitly labelled observed, never moving `now`)
    > documented cadence predictions (never invented for HNA or unknown maintenance end times).
  * Separate `preload` and `maintenance` state records per `(game, version)` with silent seeding
    on first enable / upgrade so settled historical versions never repost.
  * Structured version-verified media selection with priority:
      1. Matching-version launch video
      2. Matching-version launch artwork / WebP
      3. Matching-version official X / HoYoLAB Special Program artwork
      4. Omit gallery cleanly
  * Launch/version identity is separated from selected media tier:
      - Matching-version launch video upgrades an initial artwork/fallback post silently via
        webhook `PATCH` inside `LATE_MEDIA_EDIT_WINDOW_S` (`6 h`) without reposting or re-pinging.
      - Expiring/unusable Tier 1..3 URLs support corroborated same-asset signed URL renewal
        (never bare host/path equality alone) or safe fallback to matching-version artwork /
        omitted media within `LATE_MEDIA_EDIT_WINDOW_S` (`6 h`), capped at `MAX_LATE_MEDIA_ATTEMPTS = 3`
        consecutive failed `PATCH` attempts (reset on `res.ok`) and `MAX_LATE_EDITS = 6` total
        succeeded `PATCH` updates per posted milestone.
      - Failed edits never persist false replacement state or emit duplicate announcements.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from . import schedule
from .cards import maintenance_payload
from .config import CONFIG_DIR, Game
from .discord import webhook_fingerprint
from .http import Probe
from .media import twimg_orig, upgrade
from .sources import hoyolab, kuro, launcher
from .state import stable_hash
from .textutil import find_version, version_key

log = logging.getLogger("gamexpress.maintenance")

ACTIVE_WINDOW_S = 12 * 3600           # deliver announcements within 12h of milestone start
LATE_MEDIA_EDIT_WINDOW_S = 6 * 3600   # post-delivery check/edit window: 6h (21,600s) from posted_at
MAX_LATE_MEDIA_ATTEMPTS = 3           # max consecutive failed PATCH attempts (`edit_attempts`, reset on res.ok)
MAX_LATE_EDITS = 6                    # hard cap on total succeeded silent PATCH updates (`edit_count`)
LAUNCH_MEDIA_MAX_AFTER_S = 7 * 86400  # >7d after maintenance start belongs to mid-patch / Phase II
LAUNCH_MEDIA_MAX_BEFORE_S = 14 * 86400

_CDN_DATE_RE = re.compile(r"/(20\d{2})[/-](0[1-9]|1[0-2])[/-](0[1-9]|[12]\d|3[01])/")
_VIDEO_EXT_RE = re.compile(r"\.(?:mp4|webm)(?:[?#]|$)", re.I)


def _program_pins() -> dict:
    path = CONFIG_DIR / "program_announcements.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _asset_upload_ts(url: str) -> int | None:
    """Extract embedded UTC date timestamp `/YYYY/MM/DD/` from CDN paths when present."""
    m = _CDN_DATE_RE.search(url or "")
    if not m:
        return None
    try:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, 0, tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def _auth_key_expiry(url: str) -> int | None:
    """Parse `<unix_expiry>-...` from Kuro/CDN `auth_key` query parameter without stripping it."""
    if not url or "auth_key=" not in url:
        return None
    params = parse_qs(urlsplit(url).query, keep_blank_values=True)
    raw_list = params.get("auth_key") or []
    if not raw_list:
        return None
    head = raw_list[0].split("-", 1)[0].strip()
    if head.isdigit() and len(head) >= 9:
        return int(head)
    return None


def _same_url_locator(url_a: str, url_b: str) -> bool:
    """True when scheme, host, and path match (necessary, NEVER sufficient on its own)."""
    pa, pb = urlsplit(url_a or ""), urlsplit(url_b or "")
    return bool(
        pa.scheme
        and pa.scheme == pb.scheme
        and (pa.hostname or "").lower() == (pb.hostname or "").lower()
        and pa.path == pb.path
    )


def _build_asset_meta(
    url: str,
    version: str,
    *,
    explicit_version_link: bool = False,
    extra: dict | None = None,
) -> dict:
    """Assemble structured identity metadata binding `url` to a concrete version/asset."""
    raw = dict(extra or {})
    path_ver = find_version(urlsplit(url or "").path) or ""
    upload_ts = _asset_upload_ts(url)
    meta = {
        "version": str(raw.get("version") or version or "").strip(),
        "full_version": str(raw.get("full_version") or "").strip(),
        "hash": str(raw.get("hash") or "").strip(),
        "size": str(raw.get("size") or "").strip(),
        "asset_id": str(raw.get("asset_id") or "").strip(),
        "slot": str(raw.get("slot") or "").strip(),
        "path_version": path_ver if path_ver == version else "",
        "explicit_link": bool(explicit_version_link or raw.get("explicit_link")),
        "upload_ts": int(upload_ts) if upload_ts is not None else 0,
    }
    return {k: v for k, v in meta.items() if v}


def _corroborated_same_asset(
    cand_url: str,
    cand_meta: dict,
    locked_url: str,
    locked_meta: dict,
    expected_version: str,
) -> bool:
    """Return True ONLY when a renewed URL is corroborated as the exact same launch asset.

    Host + pathname equality is required, never sufficient by itself: mutable publisher
    background paths (e.g. generic Kuro `bg.mp4`) can serve new bytes in Phase II or later
    versions. Renewal is approved only when:
      1. `scheme + host + path` match `locked_url`.
      2. Version matches `expected_version` and zero metadata fields conflict with `locked_meta`.
      3. At least one positive asset binder corroborates identity (`hash`, `asset_id`,
         version-stamped path `/3.7/...`, matching CDN upload date path `/YYYY/MM/DD/`,
         `explicit_link` version tag, or matching `full_version` + `size`).
    """
    if not _same_url_locator(cand_url, locked_url):
        return False
    c_ver = str(cand_meta.get("version") or "")
    l_ver = str(locked_meta.get("version") or expected_version)
    if c_ver != expected_version or l_ver != expected_version:
        return False

    # Any conflicting binding attribute immediately disqualifies renewal.
    for key in ("full_version", "hash", "size", "asset_id", "upload_ts", "path_version"):
        lv = locked_meta.get(key)
        cv = cand_meta.get(key)
        if lv and cv and lv != cv:
            return False
        # If the locked asset had an explicit content hash or asset_id and the candidate dropped
        # or changed it, refuse to assume the bytes stayed identical on a mutable path.
        if key in ("hash", "asset_id") and lv and not cv:
            return False

    if cand_meta.get("hash") and cand_meta.get("hash") == locked_meta.get("hash"):
        return True
    if cand_meta.get("asset_id") and cand_meta.get("asset_id") == locked_meta.get("asset_id"):
        return True
    if cand_meta.get("path_version") == expected_version:
        return True
    if cand_meta.get("upload_ts") and cand_meta.get("upload_ts") == locked_meta.get("upload_ts"):
        return True
    if cand_meta.get("explicit_link") and locked_meta.get("explicit_link"):
        return True
    return bool(
        cand_meta.get("full_version")
        and cand_meta.get("size")
        and cand_meta.get("full_version") == locked_meta.get("full_version")
        and cand_meta.get("size") == locked_meta.get("size")
    )


def _is_video_url(url: str) -> bool:
    return bool(_VIDEO_EXT_RE.search(url or ""))


def _is_launch_window_compatible(
    url: str,
    *,
    now: int,
    preinstall_ts: int | None,
    maint_start_ts: int | None,
    explicit_version_link: bool = False,
) -> bool:
    """Reject stale prior-patch backgrounds and Phase II mid-patch background swaps."""
    upload_ts = _asset_upload_ts(url)
    anchor_early = preinstall_ts or maint_start_ts
    anchor_maint = maint_start_ts or preinstall_ts
    if upload_ts is not None:
        if anchor_maint and upload_ts > anchor_maint + LAUNCH_MEDIA_MAX_AFTER_S:
            return False
        if anchor_early and upload_ts < anchor_early - LAUNCH_MEDIA_MAX_BEFORE_S:
            return False
    elif not explicit_version_link and maint_start_ts and now > maint_start_ts + LAUNCH_MEDIA_MAX_AFTER_S:
        return False
    return True


async def _verify_url(
    fetcher,
    url: str,
    now: int,
    *,
    cache: dict[str, bool] | None = None,
) -> bool:
    """Validate URL safety, signed-query non-expiry, and per-run memoized CDN preflight."""
    if not url or not url.startswith(("https://", "http://")):
        return False
    exp = _auth_key_expiry(url)
    if exp is not None and exp <= now:
        if cache is not None:
            cache[url] = False
        return False
    if cache is not None and url in cache:
        return cache[url]
    verify = getattr(fetcher, "verify_media", None)
    ok = bool(await verify(url)) if callable(verify) else True
    if cache is not None:
        cache[url] = ok
    return ok


def _repost_matches(repost: str, game_key: str, version: str, kind: str) -> bool:
    """Support explicit operator `REPOST` targets (`zzz:3.3`, `zzz:3.3:preload`, `all`)."""
    if not repost:
        return False
    tokens = {tok.strip().lower() for tok in repost.split(",") if tok.strip()}
    gk = game_key.lower()
    ver = version.lower()
    return bool(
        tokens & {
            "all",
            gk,
            f"{gk}:{ver}",
            f"{gk}:{ver}:{kind}",
        }
    )


def resolve_version_plans(
    game: Game,
    schedule_records: dict,
    items: list,
    versions_info: dict,
    game_overrides: dict,
    now: int,
    *,
    maintenance_versions: dict | None = None,
) -> dict[str, dict]:
    """Build resolved timing plans `{version: plan}` strictly obeying precedence:

      1. Official published notices / tweets (`rank >= 40`)
      2. Explicit `config/overrides.json` entries (including calendar/holiday shifts)
      3. Stable first-observed launcher availability (`observed_preinstall_ts`, never moving `now`)
      4. Documented `Cadence` predictions (never invented for HNA or unknown end times)
    """
    plans: dict[str, dict] = {}
    maint_vers = maintenance_versions if isinstance(maintenance_versions, dict) else {}
    by_version: dict[str, list] = {}
    for it in items or []:
        ver = find_version(f"{it.title}\n{it.text}")
        if ver:
            by_version.setdefault(ver, []).append(it)

    candidate_versions: set[str] = set()
    for ver in (schedule_records or {}):
        if version_key(ver) != (0, 0):
            candidate_versions.add(ver)
    for ver in by_version:
        if version_key(ver) != (0, 0):
            candidate_versions.add(ver)
    for ver in (game_overrides or {}):
        if not str(ver).startswith("_") and isinstance(game_overrides[ver], dict) and version_key(ver) != (0, 0):
            candidate_versions.add(str(ver))
    for ver, m_entry in maint_vers.items():
        if isinstance(m_entry, dict) and m_entry.get("observed_preinstall_ts") and version_key(ver) != (0, 0):
            candidate_versions.add(str(ver))
    for field_name in ("live", "pre"):
        ver = (versions_info or {}).get(field_name)
        if ver and version_key(ver) != (0, 0):
            candidate_versions.add(str(ver))

    lead_h = schedule.observed_lead_h(schedule_records) if game.cadence is not None else None

    for ver in sorted(candidate_versions, key=version_key):
        existing_rec = copy.deepcopy((schedule_records or {}).get(ver) or {})
        ov = copy.deepcopy((game_overrides or {}).get(ver) or {})
        ver_items = by_version.get(ver, [])
        persisted_vstate = maint_vers.get(ver) if isinstance(maint_vers.get(ver), dict) else {}

        raw_prev_data = existing_rec.get("data") if isinstance(existing_rec.get("data"), dict) else {}
        raw_prev_prov = existing_rec.get("prov") if isinstance(existing_rec.get("prov"), dict) else {}
        raw_prev_est = set(raw_prev_data.get("estimated") or [])
        official_seed_data: dict = {}
        official_seed_prov: dict = {}
        for k_field in ("preinstall_ts", "maint_start_ts", "maint_end_ts", "images", "version"):
            rank_val = (raw_prev_prov.get(k_field) or [0])[0]
            if k_field in ("images", "version") and raw_prev_data.get(k_field):
                official_seed_data[k_field] = copy.deepcopy(raw_prev_data[k_field])
            elif k_field not in raw_prev_est and 40 <= rank_val < 100 and raw_prev_data.get(k_field):
                official_seed_data[k_field] = raw_prev_data[k_field]
                official_seed_prov[k_field] = list(raw_prev_prov[k_field])
        official_rec = {"data": official_seed_data, "prov": official_seed_prov}

        # Merge official items ONLY onto official provenance so prior rank-100 overrides can never
        # shadow a newly published official notice, and estimates remain cleanly separated.
        ver_extracts = [schedule.extract(game, it) for it in ver_items]
        merged_official = schedule.merge(
            game, ver, ver_extracts, official_rec, {}, {}, now, [], lead_h=lead_h
        )
        est_fields = set(merged_official.get("estimated") or [])
        prov = official_rec.get("prov") or {}

        pre_ts: int | None = None
        pre_est = False
        pre_obs = False
        pre_prov = ""
        pre_src = ""
        start_ts: int | None = None
        end_ts: int | None = None
        maint_est = False
        maint_prov = ""
        maint_src = ""

        # 1. Official published values (rank >= 40 / non-estimated from official posts).
        raw_pre = merged_official.get("preinstall_ts")
        if raw_pre and "preinstall_ts" not in est_fields and prov.get("preinstall_ts", [50])[0] >= 40:
            pre_ts = int(raw_pre)
            pre_prov = "official"

        raw_start = merged_official.get("maint_start_ts")
        if raw_start and "maint_start_ts" not in est_fields and prov.get("maint_start_ts", [50])[0] >= 40:
            start_ts = int(raw_start)
            maint_prov = "official"
            raw_end = merged_official.get("maint_end_ts")
            if raw_end and "maint_end_ts" not in est_fields and int(raw_end) > start_ts:
                end_ts = int(raw_end)

        # 2. Explicit overrides (beat launcher observation & cadence predictions; Official wins
        #    when published, while overrides fill missing fields or shift holiday dates).
        if isinstance(ov, dict) and ov:
            if pre_ts is None and ov.get("preinstall_ts"):
                pre_ts = int(ov["preinstall_ts"])
                pre_est = False
                pre_obs = False
                pre_prov = "override"
            if start_ts is None and ov.get("maint_start_ts"):
                start_ts = int(ov["maint_start_ts"])
                maint_est = False
                maint_prov = "override"
                if ov.get("maint_end_ts"):
                    end_ts = int(ov["maint_end_ts"])
                elif ov.get("maint_hours"):
                    end_ts = int(start_ts + float(ov["maint_hours"]) * 3600)
                else:
                    # Unknown maintenance end time in override must never be invented.
                    end_ts = None
            elif start_ts is not None and end_ts is None:
                if ov.get("maint_end_ts"):
                    end_ts = int(ov["maint_end_ts"])
                elif ov.get("maint_hours"):
                    end_ts = int(start_ts + float(ov["maint_hours"]) * 3600)

        # 3. Launcher availability fallback: persist and reuse the FIRST-OBSERVED timestamp
        #    rather than a moving `now`, and mark provenance explicitly as `observed`.
        persisted_obs = int(persisted_vstate.get("observed_preinstall_ts") or 0)
        if pre_ts is None and ((versions_info or {}).get("pre") == ver or persisted_obs > 0):
            pre_ts = persisted_obs if persisted_obs > 0 else int(now)
            pre_obs = True
            pre_est = False
            pre_prov = "observed"
            pre_src = "launcher observation"

        # 4. Documented cadence fallback for preload when maintenance start is known and game has cadence.
        if (
            pre_ts is None
            and start_ts is not None
            and game.cadence is not None
            and raw_pre
            and "preinstall_ts" in est_fields
        ):
            pre_ts = int(raw_pre)
            pre_est = True
            pre_obs = False
            pre_prov = "estimated"
            pre_src = "version cadence"

        plans[ver] = {
            "version": ver,
            "preinstall_ts": pre_ts,
            "preinstall_estimated": pre_est,
            "preinstall_observed": pre_obs,
            "preinstall_provenance": pre_prov,
            "preinstall_source": pre_src,
            "maint_start_ts": start_ts,
            "maint_end_ts": end_ts if (end_ts and start_ts and end_ts > start_ts) else None,
            "maint_estimated": maint_est,
            "maint_provenance": maint_prov,
            "maint_source": maint_src,
            "schedule_images": list(merged_official.get("images") or []),
            "override": ov if isinstance(ov, dict) else {},
        }

    # 5. Full cycle prediction (`predict_cycle`) for the upcoming version when no official/override
    #    record exists yet — evaluated at both `now - ACTIVE_WINDOW_S` (so a cycle whose milestone
    #    just crossed `now` is not rolled +42d past the active trigger window) and `now` (so the
    #    successor cycle is projected immediately once maintenance starts, without a 12h blind spot).
    if game.cadence is not None:
        seen_guesses: set[str] = set()
        for ref_now in (max(1, now - ACTIVE_WINDOW_S), now):
            guess = schedule.predict_cycle(game, schedule_records, ref_now, lead_h=lead_h)
            if not guess or not guess.get("version"):
                continue
            raw_ver = str(guess["version"])
            if raw_ver in seen_guesses:
                continue
            seen_guesses.add(raw_ver)
            major_rollover = f"{version_key(raw_ver)[0] + 1}.0"
            gver = major_rollover if major_rollover in plans else raw_ver
            ov = copy.deepcopy((game_overrides or {}).get(gver) or {})
            existing = plans.get(gver)
            if existing is None:
                plans[gver] = {
                    "version": gver,
                    "preinstall_ts": int(guess["preinstall_ts"]) if guess.get("preinstall_ts") else None,
                    "preinstall_estimated": True,
                    "preinstall_observed": False,
                    "preinstall_provenance": "estimated",
                    "preinstall_source": "version cadence",
                    "maint_start_ts": int(guess["maint_start_ts"]) if guess.get("maint_start_ts") else None,
                    "maint_end_ts": int(guess["maint_end_ts"]) if guess.get("maint_end_ts") else None,
                    "maint_estimated": True,
                    "maint_provenance": "estimated",
                    "maint_source": "version cadence",
                    "schedule_images": [],
                    "override": ov if isinstance(ov, dict) else {},
                }
            else:
                if existing["preinstall_ts"] is None and "preinstall_ts" not in ov and guess.get("preinstall_ts"):
                    existing["preinstall_ts"] = int(guess["preinstall_ts"])
                    existing["preinstall_estimated"] = True
                    existing["preinstall_observed"] = False
                    existing["preinstall_provenance"] = "estimated"
                    existing["preinstall_source"] = "version cadence"
                if existing["maint_start_ts"] is None and "maint_start_ts" not in ov and guess.get("maint_start_ts"):
                    existing["maint_start_ts"] = int(guess["maint_start_ts"])
                    existing["maint_end_ts"] = int(guess["maint_end_ts"]) if guess.get("maint_end_ts") else None
                    existing["maint_estimated"] = True
                    existing["maint_provenance"] = "estimated"
                    existing["maint_source"] = "version cadence"

    return plans


async def resolve_launch_media(
    ctx,
    game: Game,
    version: str,
    plan: dict,
    version_state: dict,
    *,
    basic_info: dict[str, list[dict]],
    program_pins: dict,
    verified_cache: dict[str, bool] | None = None,
) -> tuple[int, str | None]:
    """Select and verify media strictly following priority, identity-lock, and renewal rules:

      Priority 1: Matching-version launch video
      Priority 2: Matching-version launch artwork / WebP
      Priority 3: Matching-version official X / HoYoLAB Special Program artwork
      Priority 4: Omit gallery (`(4, None)`)

    Launch/version identity (`lock["version"]`) is separated from selected media tier:
      * Locking launch artwork (`tier 2`) binds the card to `version` and rejects Phase II or
        other-version assets, while STILL permitting a matching-version launch video (`tier 1`)
        to upgrade the card inside the bounded late-edit window.
      * When a locked Tier 1 video URL expires (e.g. signed WuWa `auth_key`), same-tier renewal
        is approved ONLY when `_corroborated_same_asset` proves identical launch asset identity;
        otherwise selection falls back cleanly to matching-version artwork (`tier 2`/`3`) or
        safe omission (`(4, None)`).
    """
    now = ctx.now
    pre_ts = plan.get("preinstall_ts")
    start_ts = plan.get("maint_start_ts")
    ov = plan.get("override") or {}
    vinfo = ctx.versions.get(game.key) or {}
    lock = version_state.setdefault("launch_media", {})

    locked_ver = str(lock.get("version") or "").strip()
    if locked_ver and locked_ver != version:
        return 4, None

    launcher_matches = (vinfo.get("pre") == version) or (vinfo.get("live") == version)

    # Gather structured fresh candidates with binding identity metadata.
    fresh_videos: list[tuple[str, dict]] = []
    fresh_artworks: list[tuple[str, dict]] = []

    if ov.get("launch_video"):
        u = str(ov["launch_video"]).strip()
        fresh_videos.append(
            (u, _build_asset_meta(u, version, explicit_version_link=True, extra={"asset_id": f"override:{version}:video"}))
        )
    if ov.get("launch_image"):
        u = str(ov["launch_image"]).strip()
        fresh_artworks.append(
            (u, _build_asset_meta(u, version, explicit_version_link=True, extra={"asset_id": f"override:{version}:image"}))
        )

    if game.launcher.get("type") == "hoyoplay":
        gid = str(game.launcher.get("game_id") or "")
        for entry in basic_info.get(gid) or []:
            links = entry.get("links") or []
            link_versions = [find_version(u) for u in links if find_version(u)]
            if link_versions and version not in link_versions:
                continue
            explicit_match = version in link_versions
            if not explicit_match and not launcher_matches:
                continue
            entry_id = str(entry.get("id") or "").strip()
            vid = entry.get("video") or ""
            if vid and _is_launch_window_compatible(
                vid,
                now=now,
                preinstall_ts=pre_ts,
                maint_start_ts=start_ts,
                explicit_version_link=explicit_match,
            ):
                fresh_videos.append((
                    vid,
                    _build_asset_meta(
                        vid,
                        version,
                        explicit_version_link=explicit_match,
                        extra={"asset_id": entry_id, "hash": entry.get("video_md5") or ""},
                    ),
                ))
            art = entry.get("background") or ""
            if art and _is_launch_window_compatible(
                art,
                now=now,
                preinstall_ts=pre_ts,
                maint_start_ts=start_ts,
                explicit_version_link=explicit_match,
            ):
                fresh_artworks.append((
                    art,
                    _build_asset_meta(
                        art,
                        version,
                        explicit_version_link=explicit_match,
                        extra={"asset_id": entry_id, "hash": entry.get("background_md5") or ""},
                    ),
                ))
    elif game.launcher.get("type") == "kuro":
        for slot_key in ("pre_media", "live_media"):
            slot = vinfo.get(slot_key)
            if not isinstance(slot, dict) or slot.get("version") != version:
                continue
            vid = slot.get("video") or ""
            vmeta_raw = dict(slot.get("video_meta")) if isinstance(slot.get("video_meta"), dict) else {}
            if slot.get("video_md5") and not vmeta_raw.get("hash"):
                vmeta_raw["hash"] = str(slot["video_md5"]).strip()
            if vid and _is_launch_window_compatible(
                vid, now=now, preinstall_ts=pre_ts, maint_start_ts=start_ts
            ):
                fresh_videos.append((vid, _build_asset_meta(vid, version, extra=vmeta_raw)))
            art = slot.get("artwork") or ""
            ameta_raw = dict(slot.get("artwork_meta")) if isinstance(slot.get("artwork_meta"), dict) else {}
            if slot.get("artwork_md5") and not ameta_raw.get("hash"):
                ameta_raw["hash"] = str(slot["artwork_md5"]).strip()
            if art and _is_launch_window_compatible(
                art, now=now, preinstall_ts=pre_ts, maint_start_ts=start_ts
            ):
                fresh_artworks.append((art, _build_asset_meta(art, version, extra=ameta_raw)))

    # --- Priority 1: Matching-version launch video ---
    locked_video = str(lock.get("video") or "").strip()
    locked_vmeta = lock.get("video_meta") if isinstance(lock.get("video_meta"), dict) else {}
    if locked_video:
        if await _verify_url(ctx.fetcher, locked_video, now, cache=verified_cache):
            return 1, locked_video
        # Locked video expired / unreachable: approve ONLY a corroborated renewal for the exact
        # same launch asset (never bare host/path equality alone).
        for cand_url, cand_meta in fresh_videos:
            if _corroborated_same_asset(cand_url, cand_meta, locked_video, locked_vmeta, version) and await _verify_url(
                ctx.fetcher, cand_url, now, cache=verified_cache
            ):
                lock["video"] = cand_url
                lock["video_meta"] = cand_meta
                lock["renewed_at"] = now
                return 1, cand_url
    else:
        # No video locked yet: allow matching-version launch video even if Tier 2 artwork was
        # locked earlier, provided the candidate matches this launch identity and launch window.
        locked_ameta = lock.get("artwork_meta") if isinstance(lock.get("artwork_meta"), dict) else {}
        locked_aid = str(locked_ameta.get("asset_id") or "").strip()
        for cand_url, cand_meta in fresh_videos:
            cand_aid = str(cand_meta.get("asset_id") or "").strip()
            # Reject an unrelated mid-patch carousel entry if an explicit HoYoPlay background ID
            # was already locked at launch and the new entry neither shares that launch ID nor
            # carries an explicit version link for `version`.
            if locked_aid and cand_aid and cand_aid != locked_aid and not cand_meta.get("explicit_link"):
                continue
            if await _verify_url(ctx.fetcher, cand_url, now, cache=verified_cache):
                lock["version"] = version
                lock["video"] = cand_url
                lock["video_meta"] = cand_meta
                if not lock.get("artwork"):
                    for art_url, art_meta in fresh_artworks:
                        if await _verify_url(ctx.fetcher, art_url, now, cache=verified_cache):
                            lock["artwork"] = art_url
                            lock["artwork_meta"] = art_meta
                            break
                lock["locked"] = True
                lock.setdefault("locked_at", now)
                return 1, cand_url

    # --- Priority 2: Matching-version launch artwork / WebP ---
    locked_art = str(lock.get("artwork") or "").strip()
    locked_ameta = lock.get("artwork_meta") if isinstance(lock.get("artwork_meta"), dict) else {}
    if locked_art:
        if await _verify_url(ctx.fetcher, locked_art, now, cache=verified_cache):
            return 2, locked_art
        for cand_url, cand_meta in fresh_artworks:
            if _corroborated_same_asset(cand_url, cand_meta, locked_art, locked_ameta, version) and await _verify_url(
                ctx.fetcher, cand_url, now, cache=verified_cache
            ):
                lock["artwork"] = cand_url
                lock["artwork_meta"] = cand_meta
                lock["renewed_at"] = now
                return 2, cand_url
    else:
        for cand_url, cand_meta in fresh_artworks:
            if await _verify_url(ctx.fetcher, cand_url, now, cache=verified_cache):
                lock["version"] = version
                lock["artwork"] = cand_url
                lock["artwork_meta"] = cand_meta
                lock["locked"] = True
                lock.setdefault("locked_at", now)
                return 2, cand_url

    # --- Priority 3: Matching-version official X / HoYoLAB Special Program artwork ---
    program_candidates: list[str] = []
    pinned_entry = ((program_pins.get(game.key) or {}).get(version) or {})
    if isinstance(pinned_entry, dict) and pinned_entry.get("image"):
        program_candidates.append(twimg_orig(str(pinned_entry["image"]).strip()))
    for img in plan.get("schedule_images") or []:
        if img and not _is_video_url(img):
            program_candidates.append(upgrade(str(img).strip()))
    if not program_candidates and callable(getattr(ctx.fetcher, "get_json", None)):
        from .runner import find_program

        hit = await find_program(ctx, game, version)
        if hit:
            for img in hit.get("images") or []:
                if img and not _is_video_url(img):
                    program_candidates.append(upgrade(str(img).strip()))

    seen_prog: set[str] = set()
    for prog_url in program_candidates:
        if not prog_url or prog_url in seen_prog:
            continue
        seen_prog.add(prog_url)
        if await _verify_url(ctx.fetcher, prog_url, now, cache=verified_cache):
            return 3, prog_url

    # --- Priority 4: Omit gallery ---
    return 4, None


async def _ensure_inputs_for_games(ctx, target_games: list[Game]) -> None:
    """Gather ONLY the launcher + official announcement inputs needed by `target_games`.

    When `schedule` or `codes` already gathered them earlier in the same run, reuses
    `ctx.versions` and `ctx.items` with zero duplicate requests. In maintenance-only
    mode, avoids all unrelated sources (`CodeSources`, countdown sites, banner feeds,
    wikis, calendars, and unconfigured games).
    """
    missing_ver_hyp = any(
        g.launcher.get("type") == "hoyoplay" and g.key not in ctx.versions for g in target_games
    )
    missing_ver_kuro = any(
        g.launcher.get("type") == "kuro" and g.key not in ctx.versions for g in target_games
    )
    if missing_ver_hyp or missing_ver_kuro:
        async def _empty() -> dict:
            return {}

        hyp, kr = await asyncio.gather(
            launcher.hoyoplay_versions(ctx.fetcher) if missing_ver_hyp else _empty(),
            kuro.launcher_versions(ctx.fetcher) if missing_ver_kuro else _empty(),
        )
        for g in target_games:
            t = g.launcher.get("type")
            if t == "hoyoplay" and g.launcher.get("game_id") in hyp:
                ctx.versions[g.key] = hyp[g.launcher["game_id"]]
            elif t == "kuro" and kr:
                ctx.versions[g.key] = kr

    since = ctx.now - ctx.settings.lookback_hours * 3600
    missing_item_games = [g for g in target_games if g.key not in ctx.items]
    if missing_item_games:
        async def _collect_one(g: Game) -> None:
            want = schedule.wants(g)
            probe = Probe(ctx.fetcher)
            x_client = getattr(ctx, "x", None)
            parts = await asyncio.gather(
                hoyolab.fetch_items(probe, g, want, since),
                kuro.fetch_items(probe, g, want, since),
                x_client.items(g, want, since) if x_client is not None else asyncio.sleep(0, result=[]),
            )
            items = [it for part in parts for it in part]
            x_reachable = getattr(x_client, "reachable", None) if x_client is not None else ()
            x_ok = True if x_reachable is None else any(a.lower() in x_reachable for a in g.x_accounts)
            has_primary_api = bool(g.hoyolab_gid or g.kuro_news)
            primary_ok = (not has_primary_api) or probe.ok > 0
            if getattr(ctx, "reddit", None) is not None and ctx.settings.reddit_enabled and (not x_ok or not primary_ok):
                items.extend(await ctx.reddit.items(g, want, since))
            ctx.items[g.key] = items

        await asyncio.gather(*(_collect_one(g) for g in missing_item_games))


async def run(ctx) -> None:
    """Execute one maintenance-announcement pass across opted-in games."""
    if "maintenance" not in ctx.settings.features or not ctx.active:
        return
    if ctx.settings.test_mode:
        ctx.warnings.append(
            "Maintenance test-mode posting is disabled; use preview --kind maintenance or run --only maintenance --dry-run"
        )
        return

    ctx.state.prune_maintenance()
    for key, record in (ctx.state.data.get("maintenance") or {}).items():
        if key not in ctx.settings.maintenance_games:
            record["paused"] = True

    routed_games: list[tuple[Game, str, str]] = []
    for game in ctx.games:
        if game.key not in ctx.settings.maintenance_games:
            continue
        hook = ctx.settings.webhook("maintenance", game.key)
        if not hook:
            ctx.warnings.append(f"{game.short} maintenance: missing dedicated webhook; no request or bootstrap")
            continue
        fp = stable_hash([webhook_fingerprint(hook), hook.partition("?")[2]])
        existing = (ctx.state.data.get("maintenance") or {}).get(game.key) or {}
        if existing.get("route") and existing["route"] != fp:
            ctx.warnings.append(
                f"{game.short} maintenance: destination changed; review maintenance state before resuming"
            )
            continue
        routed_games.append((game, hook, fp))

    if not routed_games:
        return

    await _ensure_inputs_for_games(ctx, [g for g, _, _ in routed_games])

    ephemeral = ctx.settings.dry_run or ctx.settings.test_mode
    program_pins = _program_pins()
    basic_info: dict[str, list[dict]] | None = None
    verified_cache: dict[str, bool] = {}

    async def get_basic_info() -> dict[str, list[dict]]:
        nonlocal basic_info
        if basic_info is None:
            basic_info = await launcher.hoyoplay_basic_info(ctx.fetcher)
        return basic_info

    for game, hook, fp in routed_games:
        record = ctx.state.maintenance_record(game.key)
        versions_state = record.setdefault("versions", {})
        sched_records = ctx.state.data.get("schedule", {}).get(game.key, {})
        plans = resolve_version_plans(
            game,
            sched_records,
            ctx.items.get(game.key, []),
            ctx.versions.get(game.key, {}),
            (ctx.overrides or {}).get(game.key, {}),
            ctx.now,
            maintenance_versions=versions_state,
        )

        # Persist first-observed launcher availability timestamps immediately so repeated runs
        # and restarts never move an observation timestamp forward with `ctx.now`.
        obs_changed = False
        for ver, plan in plans.items():
            if plan.get("preinstall_observed") and plan.get("preinstall_ts"):
                vstate = versions_state.setdefault(ver, {})
                if not vstate.get("observed_preinstall_ts"):
                    vstate["observed_preinstall_ts"] = int(plan["preinstall_ts"])
                    obs_changed = True

        # Silent baseline / upgrade migration: mark every historical version & already-elapsed
        # milestone as `seeded` without posting or pinging, leaving future milestones active.
        if "seeded_at" not in record or record.get("paused"):
            record.update(seeded_at=ctx.now, route=fp, paused=False)
            for ver, plan in plans.items():
                vstate = versions_state.setdefault(ver, {})
                for kind, field_name in (("preload", "preinstall_ts"), ("maintenance", "maint_start_ts")):
                    if kind in vstate:
                        continue
                    ev_ts = plan.get(field_name)
                    if ev_ts is not None:
                        if ev_ts <= ctx.now:
                            vstate[kind] = {
                                "status": "seeded",
                                "ts": int(ev_ts),
                                "provenance": plan.get("preinstall_provenance") if kind == "preload" else plan.get("maint_provenance", ""),
                            }
                    elif (
                        (ctx.versions.get(game.key) or {}).get("live") == ver
                        or (kind == "preload" and (ctx.versions.get(game.key) or {}).get("pre") == ver)
                    ):
                        vstate[kind] = {"status": "seeded", "ts": ctx.now}
            if not ephemeral:
                ctx.state.save()
            ctx.report.append(f"🌱 {game.short} maintenance: silent baseline; settled milestones not reposted")
            if not ctx.settings.repost:
                continue

        if obs_changed and not ephemeral:
            ctx.state.save()

        record.setdefault("route", fp)
        seeded_at = int(record.get("seeded_at") or 0)
        ping = ctx.settings.ping("maintenance", game.key)

        for ver, plan in sorted(plans.items(), key=lambda pair: version_key(pair[0])):
            vstate = versions_state.setdefault(ver, {})
            milestones = (
                (
                    "preload",
                    plan.get("preinstall_ts"),
                    None,
                    bool(plan.get("preinstall_estimated")),
                    plan.get("preinstall_source") or "",
                    bool(plan.get("preinstall_observed")),
                    plan.get("preinstall_provenance") or "",
                ),
                (
                    "maintenance",
                    plan.get("maint_start_ts"),
                    plan.get("maint_end_ts"),
                    bool(plan.get("maint_estimated")),
                    plan.get("maint_source") or "",
                    False,
                    plan.get("maint_provenance") or "",
                ),
            )
            for kind, start_ts, end_ts, is_est, est_src, is_obs, prov_tag in milestones:
                force = _repost_matches(ctx.settings.repost, game.key, ver, kind)
                entry = vstate.get(kind)

                if entry and not force:
                    status = entry.get("status")
                    if status == "ambiguous":
                        ctx.warnings.append(
                            f"{game.short} {ver} {kind}: held due to prior ambiguous webhook outcome"
                        )
                        continue
                    if status in ("posted", "seeded"):
                        posted_at = int(entry.get("posted_at") or 0)
                        cur_tier = int(entry.get("media_tier") or 4)
                        cur_url = str(entry.get("media_url") or "")
                        msg_id = entry.get("message_id")
                        attempts = int(entry.get("edit_attempts") or 0)
                        edit_count = int(entry.get("edit_count") or 0)
                        if (
                            status == "posted"
                            and msg_id
                            and attempts < MAX_LATE_MEDIA_ATTEMPTS
                            and edit_count < MAX_LATE_EDITS
                            and 0 <= ctx.now - posted_at <= LATE_MEDIA_EDIT_WINDOW_S
                        ):
                            cur_url_unusable = False
                            if cur_tier in (1, 2, 3) and cur_url:
                                cur_url_unusable = not await _verify_url(
                                    ctx.fetcher, cur_url, ctx.now, cache=verified_cache
                                )
                            expected_end = int(end_ts) if (kind == "maintenance" and end_ts) else None
                            end_ts_shifted = (
                                kind == "maintenance"
                                and prov_tag in ("official", "override")
                                and "end_ts" in entry
                                and entry.get("end_ts") != expected_end
                            )
                            auth_ts_upgrade = (
                                (
                                    entry.get("provenance") in ("observed", "estimated")
                                    and prov_tag in ("official", "override")
                                    and start_ts is not None
                                )
                                or end_ts_shifted
                            )
                            if cur_tier > 1 or cur_url_unusable or auth_ts_upgrade:
                                binfo = await get_basic_info() if game.launcher.get("type") == "hoyoplay" else {}
                                staged_vstate = copy.deepcopy(vstate)
                                new_tier, new_url = await resolve_launch_media(
                                    ctx,
                                    game,
                                    ver,
                                    plan,
                                    staged_vstate,
                                    basic_info=binfo,
                                    program_pins=program_pins,
                                    verified_cache=verified_cache,
                                )
                                should_edit = (
                                    (bool(new_url) and new_tier < cur_tier)
                                    or (cur_url_unusable and (new_url or "") != cur_url)
                                    or auth_ts_upgrade
                                )
                                if should_edit:
                                    effective_ts = (
                                        int(start_ts)
                                        if auth_ts_upgrade and start_ts is not None
                                        else int(entry.get("ts") or start_ts or ctx.now)
                                    )
                                    edit_card = maintenance_payload(
                                        game,
                                        ver,
                                        kind,
                                        start_ts=effective_ts,
                                        end_ts=end_ts if kind == "maintenance" else None,
                                        ping=ping,
                                        media_url=new_url,
                                        estimated=is_est,
                                        estimate_source=est_src,
                                        observed=is_obs and not auth_ts_upgrade,
                                        silent=True,
                                    )
                                    res = await ctx.webhook.edit(hook, msg_id, edit_card)
                                    if res.ok:
                                        vstate["launch_media"] = staged_vstate.get("launch_media", {})
                                        entry["media_tier"] = new_tier
                                        entry["media_url"] = new_url or ""
                                        entry["ts"] = effective_ts
                                        if auth_ts_upgrade:
                                            entry["provenance"] = prov_tag
                                            if kind == "maintenance":
                                                entry["end_ts"] = expected_end
                                        entry["edited_at"] = ctx.now
                                        entry["edit_attempts"] = 0
                                        entry["edit_count"] = int(entry.get("edit_count") or 0) + 1
                                        if not ephemeral:
                                            ctx.state.save()
                                        ctx.report.append(
                                            f"🖼️ {game.short} {ver} {kind}: updated card silently (tier {cur_tier}->{new_tier})"
                                        )
                                    else:
                                        entry["edit_attempts"] = attempts + 1
                                        if not ephemeral:
                                            ctx.state.save()
                                        ctx.warnings.append(
                                            f"{game.short} {ver} {kind}: silent edit failed ({res.status}); preserving prior card state"
                                        )
                        continue

                if start_ts is None:
                    continue
                due = force or (seeded_at < start_ts <= ctx.now and ctx.now - start_ts <= ACTIVE_WINDOW_S)
                if not due:
                    continue

                binfo = await get_basic_info() if game.launcher.get("type") == "hoyoplay" else {}
                staged_vstate = copy.deepcopy(vstate)
                media_tier, media_url = await resolve_launch_media(
                    ctx,
                    game,
                    ver,
                    plan,
                    staged_vstate,
                    basic_info=binfo,
                    program_pins=program_pins,
                    verified_cache=verified_cache,
                )
                payload = maintenance_payload(
                    game,
                    ver,
                    kind,
                    start_ts=int(start_ts),
                    end_ts=int(end_ts) if (kind == "maintenance" and end_ts) else None,
                    ping=ping,
                    media_url=media_url,
                    estimated=is_est,
                    estimate_source=est_src,
                    observed=is_obs,
                )
                result = await ctx.webhook.send(hook, payload, stop_on_ambiguous=True)
                if result.ambiguous or (result.ok and not result.message_id):
                    vstate[kind] = {
                        "status": "ambiguous",
                        "ts": int(start_ts),
                        "provenance": prov_tag,
                        "attempted_at": ctx.now,
                        "media_tier": media_tier,
                        "media_url": media_url or "",
                    }
                    if not ephemeral:
                        ctx.state.save()
                    ctx.warnings.append(
                        f"{game.short} {ver} {kind}: ambiguous Discord webhook outcome; held to prevent duplicate repost"
                    )
                    continue
                if not result.ok:
                    ctx.warnings.append(
                        f"{game.short} {ver} {kind}: Discord send failed ({result.status}); will retry within active window"
                    )
                    continue

                vstate["launch_media"] = staged_vstate.get("launch_media", {})
                vstate[kind] = {
                    "status": "posted",
                    "ts": int(start_ts),
                    "end_ts": int(end_ts) if (kind == "maintenance" and end_ts) else None,
                    "provenance": prov_tag,
                    "posted_at": ctx.now,
                    "message_id": result.message_id,
                    "media_tier": media_tier,
                    "media_url": media_url or "",
                    "edit_attempts": 0,
                    "edit_count": 0,
                }
                if not ephemeral:
                    ctx.state.save()
                ctx.report.append(
                    f"🚀 {game.short} {ver} {kind}: posted ({result.message_id}, media tier {media_tier})"
                )
