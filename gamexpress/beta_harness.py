"""Deterministic, no-network full-pipeline harness for the opt-in beta feature.

This module deliberately lives beside the feature instead of importing the test suite.  The
``benchmark --beta-enabled`` command can therefore exercise the actual enabled monitor path in
an installed checkout too: manifest/changelog discovery, the four namespace parsers, image
preflight, Components V2 rendering, webhook delivery, paging, state settlement, revision diffs,
and a quiet revision all run against local in-memory collaborators.

It is not a live-source claim and it never owns a real URL, file, or webhook.  Every collaborator
below records its work so callers can prove that no external request or physical state write took
place.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from . import beta
from .cards import validate_payload
from .config import Game, load_settings
from .discord import SendResult
from .models import BetaCharacter, BetaPatch, BetaSectionEntry
from .runner import Ctx
from .state import State, stable_hash

HARNESS_GAMES = ("genshin", "starrail", "zzz", "wuwa")
_HOOK = "https://discord.com/api/webhooks/100000000000000001/beta_harness"
_NOW = 1_793_000_000


class _MemoryState(State):
    """State semantics without a filesystem side effect (including normal save boundaries)."""

    def __init__(self) -> None:
        super().__init__(Path("/nonexistent/game-express-beta-harness.json"))
        self.save_calls = 0
        self.disk_writes = 0

    def save(self) -> bool:
        if not self.changed:
            return False
        self.save_calls += 1
        # Match State.save's committed-baseline behavior, but intentionally never mkdir/write.
        self._original = stable_hash(self._comparable())
        return True


class _HarnessSleeper:
    """Records intended page waits in place of touching wall-clock time."""

    def __init__(self, timeline: list[tuple[str, Any]]) -> None:
        self.calls: list[float] = []
        self.timeline = timeline

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.timeline.append(("sleep", seconds))


class _HarnessWebhook:
    """A webhook protocol double that validates the exact payload before accepting it."""

    def __init__(self, timeline: list[tuple[str, Any]]) -> None:
        self.timeline = timeline
        self.sent: list[dict] = []
        self._responses: list[SendResult] = []

    def responses(self, *items: SendResult) -> None:
        self._responses.extend(items)

    async def send(self, _url: str, payload: dict, *, stop_on_ambiguous: bool = False) -> SendResult:
        problems = validate_payload(payload)
        if problems:
            return SendResult(False, 0, error="invalid payload: " + "; ".join(problems))
        index = len(self.sent) + 1
        self.sent.append(copy.deepcopy(payload))
        self.timeline.append(("send", index))
        if self._responses:
            return self._responses.pop(0)
        return SendResult(True, 200, message_id=f"harness-{index}")


class _HarnessFetcher:
    """Fixture server for the production fetcher protocol; never opens a socket."""

    def __init__(self) -> None:
        self.phase = 1
        self.calls: list[tuple[str, str]] = []
        self.media_calls: list[str] = []
        self._versions = {
            "gi": ("7.1.53+71053", "7.1.54+71054", "71053", "71054"),
            "hsr": ("4.6.54+46054", "4.6.55+46055", "46054", "46055"),
            "zzz": ("3.4.1+19836465", "3.4.1+19850203", "19836465", "19850203"),
            "ww": ("3.8.1+38100", "3.8.2+38200", "38100", "38200"),
        }
        self._quiet_zzz = ("3.4.1+19851435", "19851435")
        self._ids = {
            "gi": {"character": "10000136", "weapon": "115001", "relic": "500001"},
            "hsr": {"character": "1511", "weapon": "24011", "relic": "329"},
            "zzz": {"character": "1601", "weapon": "14166", "relic": "34300"},
            "ww": {"character": "1512", "weapon": "210001", "relic": "600001"},
        }

    @property
    def fixture_source_calls(self) -> int:
        return len(self.calls) + len(self.media_calls)

    def advance_revision(self) -> None:
        self.phase = 2

    def advance_quiet(self) -> None:
        self.phase = 3

    def _version(self, ns: str) -> str:
        one, two, _one_build, _two_build = self._versions[ns]
        if self.phase == 3 and ns == "zzz":
            return self._quiet_zzz[0]
        return one if self.phase == 1 else two

    def _manifest(self) -> dict:
        mapping = {
            "gi": ("character", "weapon", "artifact"),
            "hsr": ("character", "lightcone", "relicset"),
            "zzz": ("character", "weapon", "equipment"),
            "ww": ("character", "weapon", "echo"),
        }
        out: dict[str, dict] = {}
        for ns, keys in mapping.items():
            one, two, _one_build, _two_build = self._versions[ns]
            version = one if self.phase == 1 else two
            available = [one] if self.phase == 1 else [one, two]
            if self.phase == 3 and ns == "zzz":
                version = self._quiet_zzz[0]
                available.append(version)
            ids = self._ids[ns]
            out[ns] = {
                "latest": version,
                "available": available,
                "new": {
                    keys[0]: [ids["character"]],
                    keys[1]: [ids["weapon"]],
                    keys[2]: [ids["relic"]],
                },
            }
        return out

    @staticmethod
    def _source_url(ns: str, role: str, item_id: str) -> str:
        paths = {
            "gi": {"character": "characters", "weapon": "weapons", "relic": "artifacts"},
            "hsr": {"character": "characters", "weapon": "light-cones", "relic": "relicsets"},
            "zzz": {"character": "agents", "weapon": "w-engines", "relic": "drive-discs"},
        }
        return f"https://{ns}.gachabase.net/{paths[ns][role]}/{item_id}/harness/beta?lang=en"

    def _changelog(self, ns: str) -> str:
        # WW intentionally has no configured beta changelog. Its snapshot fallback is an
        # independently important production path and is exercised on phase two.
        if self.phase == 3 and ns == "zzz":
            quiet_version, quiet_build = self._quiet_zzz
            return (
                f"## v{quiet_version.split('+', 1)[0]} Rev. {quiet_build} | harness quiet revision\n\n"
                "No tracked changes detected for this revision.\n"
            )
        version = self._version(ns)
        build = self._versions[ns][2 if self.phase == 1 else 3]
        ids = self._ids[ns]
        role_titles = (("Characters", "character"), ("Weapons", "weapon"), ("Equipment", "relic"))
        lines = [f"## v{version.split('+', 1)[0]} Rev. {build} | harness revision"]
        for heading, role in role_titles:
            item_id = ids[role]
            url = self._source_url(ns, role, item_id)
            image = f"https://fixture.invalid/{ns}/{role}/{item_id}.webp"
            diff = f"https://fixture.invalid/{ns}/{role}/{item_id}/diff"
            lines += ["", f"### {heading}", "", f"[![]({image})\\", "\\",
                      f"Harness {ns.upper()} {role.title()}]({url}) [See Changes]({diff})"]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _long_description(multiplier: int) -> str:
        # The newline-preserving parsers turn this into a real multi-page Components V2 card.
        lines = [f"Deals {multiplier}% Benchmark DMG to the target."]
        lines.extend(
            f"Harness mechanic {index:02d}: this deterministic source line verifies complete kit text "
            "is preserved and safely split between beta-card pages."
            for index in range(1, 66)
        )
        return "\n".join(lines)

    def _character(self, ns: str) -> dict:
        multiplier = 100 if self.phase == 1 else 120
        desc = self._long_description(multiplier)
        names = {
            "gi": "Harness Traveler", "hsr": "Harness Trailblazer",
            "zzz": "Harness Agent", "ww": "Harness Resonator",
        }
        if ns == "gi":
            return {
                "name": names[ns], "rarity": 5, "element": "Pyro", "weapon": "WEAPON_SWORD_ONE_HAND",
                "chara_info": {"title": "Deterministic beta fixture", "constellation": "Harness"},
                "skills": [{"name": "Normal Attack: Harness Strike", "desc": desc,
                            "promote": {"10": {"param": [multiplier / 100],
                                                "desc": ["DMG Multiplier|{param1:P}"]}}}],
                "passives": [{"name": "Harness Passive", "desc": "A readable passive fixture."}],
                "constellations": [{"name": "Harness Constellation", "desc": "A readable constellation fixture."}],
                "crit_rate": 0.05, "crit_dmg": 0.5,
            }
        if ns == "hsr":
            return {
                "name": names[ns], "rarity": 5, "damage_type": "Fire", "base_type": "Destruction",
                "skills": {"100": {"type_name": "Skill", "name": "Harness Skill", "desc": desc,
                                     "tag": "Blast", "param_list": []}},
                "stats": {"1": {"hp_base": 1000, "hp_add": 10, "attack_base": 500,
                                   "attack_add": 5, "defence_base": 400, "defence_add": 4,
                                   "speed_base": 100}},
                "sp_need": 120,
            }
        if ns == "zzz":
            return {
                "name": names[ns], "rarity": 4, "element_type": {"1": "Ether"},
                "weapon_type": {"1": "Attack"}, "camp": {"1": "Harness"},
                "skill": {"basic": {"description": [{"name": "Basic Attack: Harness", "desc": desc,
                                                         "param": []}]}},
            }
        return {
            "name": names[ns], "rarity": 5, "element": 5, "weapon": 2,
            "skill_trees": {"1": {"skill": {"name": "Harness Strike", "type": "Normal Attack",
                                                "desc": desc, "level": {}}}},
        }

    def _equipment(self, ns: str, role: str) -> dict:
        amount = 20 if self.phase == 1 else 24
        names = {"gi": "Harness Sword", "hsr": "Harness Light Cone", "zzz": "Harness W-Engine",
                 "ww": "Harness Weapon"}
        if role == "relic":
            if ns == "gi":
                return {"name": "Harness Artifact", "rarity": 5,
                        "set": {"1": {"name": {"en": "Harness Set"},
                                       "desc": {"en": f"2-Piece: Increases Benchmark DMG by {amount}%."}}}}
            if ns == "hsr":
                return {"name": "Harness Relic", "rarity": 5,
                        "require_num": {"2": {"desc": f"Increases Benchmark DMG by {amount}%."}}}
            if ns == "zzz":
                return {"name": "Harness Drive Disc", "rarity": 5,
                        "desc2": f"Increases Benchmark DMG by {amount}%.",
                        "desc4": "When a squadmate acts, gain one Harness stack."}
            return {"name": "Harness Echo", "rarity": 5,
                    "skill": {"desc": f"Deals {amount}% Benchmark Echo DMG."},
                    "group": {"1": {"name": "Harness Sonata", "set": {"2": {"desc": "Gain a Harness bonus."}}}}}
        if ns == "gi":
            return {"name": names[ns], "rarity": 5,
                    "weapon_prop": [{"prop_type": "FIGHT_PROP_BASE_ATTACK", "init_value": 608}],
                    "refinement": {"1": {"name": "Harness Effect", "desc": f"Increases Benchmark DMG by {amount}%."}}}
        if ns == "hsr":
            return {"name": names[ns], "rarity": 5,
                    "stats": {"1": {"base_hp": 1000, "base_attack": 500, "base_defence": 300}},
                    "refinements": {"name": "Harness Effect", "desc": f"Increases Benchmark DMG by {amount}%."}}
        if ns == "zzz":
            return {"name": names[ns], "rarity": 4, "base_property": {},
                    "rand_property": {"name": "CRIT Rate"},
                    "talents": {"1": {"name": "Harness Effect", "desc": f"Increases Benchmark DMG by {amount}%."}}}
        return {"name": names[ns], "rarity": 5, "effect_name": "Harness Effect",
                "effect": f"Increases Benchmark DMG by {amount}%.",
                "stats": {"1": {"90": [{"name": "ATK", "value": 500}]}}}

    def _listing(self, ns: str, role: str) -> dict:
        item_id = self._ids[ns][role]
        name = f"Harness {ns.upper()} {role.title()}"
        out = {item_id: {"en": name}}
        if ns == "zzz" and role == "weapon":
            out[item_id]["icon"] = "HarnessWeaponIcon"
        if ns == "zzz" and role == "weapon":
            out[item_id]["atk"] = 700
        return out

    async def get_json(self, url: str, **_kw: Any) -> dict | None:
        self.calls.append(("json", url))
        if url.endswith("/manifest.json"):
            return copy.deepcopy(self._manifest())
        ns = next((name for name in ("gi", "hsr", "zzz", "ww") if f"/{name}/" in url), "")
        if not ns:
            return None
        # Listings lack a locale segment and an item JSON suffix.
        if url.endswith("/weapon.json"):
            return copy.deepcopy(self._listing(ns, "weapon"))
        if url.endswith(("/artifact.json", "/relicset.json", "/equipment.json", "/echo.json")):
            return copy.deepcopy(self._listing(ns, "relic"))
        if "/character/" in url:
            return copy.deepcopy(self._character(ns))
        if any(f"/{kind}/" in url for kind in ("weapon", "lightcone")):
            return copy.deepcopy(self._equipment(ns, "weapon"))
        if any(f"/{kind}/" in url for kind in ("artifact", "relicset", "equipment", "echo")):
            return copy.deepcopy(self._equipment(ns, "relic"))
        return None

    async def get_text(self, url: str, **_kw: Any) -> str:
        self.calls.append(("text", url))
        ns = next((name for name in ("gi", "hsr", "zzz") if f"{name}.gachabase.net" in url), "")
        return self._changelog(ns) if ns else ""

    async def verify_media(self, url: str) -> bool:
        self.media_calls.append(url)
        return url.startswith("https://")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"beta enabled harness: {message}")


def _ctx(games: dict[str, Game], fetcher: _HarnessFetcher, state: _MemoryState,
         webhook: _HarnessWebhook, sleeper: _HarnessSleeper) -> Ctx:
    settings = load_settings({
        "BETA_ENABLED": "1",
        "BETA_GAMES": ",".join(HARNESS_GAMES),
        "BETA_MIN_POLL_MINUTES": "0",
        "BETA_MAX_REQUESTS": "120",
        "DISCORD_WEBHOOK_BETA": _HOOK,
    })
    _assert(settings.beta_enabled and settings.beta_games == set(HARNESS_GAMES), "opt-in settings lost")
    return Ctx(
        settings=settings, games=[games[key] for key in HARNESS_GAMES], state=state,
        fetcher=fetcher, webhook=webhook, x=None, code_sources=None, overrides={}, now=_NOW,
        sleep=sleeper,
    )


def _edge_patch() -> BetaPatch:
    # Three true rendered pages, rather than mocked payloads, make failure/resume assertions
    # cover beta_payloads_with_map and _deliver together.
    lines = [f"Edge delivery line {index}: a safe multi-page beta delivery fixture." for index in range(160)]
    character = BetaCharacter(
        id="zzz:edge", name="Edge Harness Agent", rarity=5, element="Ether", role="Attack",
        sections=[BetaSectionEntry(title="Basic Attack: Edge", category="Basic Attack", tag="[NEW]", lines=lines)],
        is_new=True,
    )
    return BetaPatch(game_key="zzz", beta_version="0.0", rev_label="v0.0 Rev. edge", rev_id="edge-delivery",
                     characters=[character], item_keys=["character:zzz:edge"], batch_id="edge-delivery")


async def run_enabled_beta_harness(games: dict[str, Game]) -> dict:
    """Run the complete BETA_ENABLED=1 fixture monitor and return auditable evidence.

    The caller supplies the loaded production game configuration.  Any mismatch in a live
    namespace/configuration is therefore surfaced by this harness rather than hidden in a
    duplicate game definition.
    """
    _assert(set(HARNESS_GAMES) <= set(games), "released beta games are missing from config")
    _assert(all(games[key].beta for key in HARNESS_GAMES), "a released beta game lacks beta config")
    fetcher = _HarnessFetcher()
    state = _MemoryState()
    timeline: list[tuple[str, Any]] = []
    webhook = _HarnessWebhook(timeline)
    sleeper = _HarnessSleeper(timeline)
    ctx = _ctx(games, fetcher, state, webhook, sleeper)

    # Phase 1: source discovery + every parser + all three item roles + actual paged delivery.
    await beta.run(ctx)
    initial_sends = len(webhook.sent)
    initial_delays = list(sleeper.calls)
    _assert(not ctx.errors and not ctx.warnings, f"initial warnings/errors: {ctx.warnings} {ctx.errors}")
    _assert(initial_sends > len(HARNESS_GAMES), "long full-kit fixtures did not paginate")
    _assert(initial_delays == [beta.BETA_PAGE_GAP_SECONDS] * (initial_sends - len(HARNESS_GAMES)),
            "each initial multi-page hand-off must have exactly one five-second gap")

    initial: dict[str, dict] = {}
    for key in HARNESS_GAMES:
        record = state.beta_record(key)
        revision = record["revisions"].get(record.get("latest_rev")) or {}
        _assert(revision.get("status") == "posted", f"{key} initial revision did not settle")
        _assert(len(record["snapshots"]) >= 3, f"{key} did not retain character + weapon + relic baselines")
        messages = list(revision.get("messages") or [])
        _assert(len(messages) >= 2, f"{key} initial full kit was not paged")
        initial[key] = {
            "source_version": record.get("source_version"),
            "pages": len(messages),
            "snapshots": len(record["snapshots"]),
            "status": revision.get("status"),
        }

    # Phase 2: same IDs, later revisions. Existing retained baselines become safe diffs rather
    # than a duplicate reveal. All real configured changelog paths plus WW's snapshot fallback run.
    fetcher.advance_revision()
    ctx.now += 60
    sends_before_revision = len(webhook.sent)
    sleeps_before_revision = len(sleeper.calls)
    await beta.run(ctx)
    revision_sends = len(webhook.sent) - sends_before_revision
    revision_delays = len(sleeper.calls) - sleeps_before_revision
    _assert(not ctx.errors and not ctx.warnings, f"revision warnings/errors: {ctx.warnings} {ctx.errors}")
    _assert(revision_sends >= len(HARNESS_GAMES), "all games must deliver a retained-baseline revision")

    revisions: dict[str, dict] = {}
    for key in HARNESS_GAMES:
        record = state.beta_record(key)
        current = record["revisions"].get(record.get("latest_rev")) or {}
        _assert(current.get("status") == "posted", f"{key} revision did not settle")
        _assert(len(record["revisions"]) >= 2, f"{key} did not retain both revisions")
        revisions[key] = {
            "source_version": record.get("source_version"),
            "pages": len(current.get("messages") or []),
            "status": current.get("status"),
            "retained_revisions": len(record["revisions"]),
        }
    _assert(revision_delays == max(0, revision_sends - len(HARNESS_GAMES)),
            "revision page gaps must only occur between pages of a delivered game")

    # A third identical run proves settled cursor/dedup behavior: no new source cards or sends.
    sends_before_dedup = len(webhook.sent)
    await beta.run(ctx)
    dedup_sends = len(webhook.sent) - sends_before_dedup
    _assert(dedup_sends == 0, "a settled revision was delivered twice")

    # Phase 3: an upstream-declared quiet ZZZ hotfix advances state without an empty card/data read.
    fetcher.advance_quiet()
    ctx.games = [games["zzz"]]
    ctx.now += 60
    quiet_before = len(webhook.sent)
    data_before = len(fetcher.calls)
    await beta.run(ctx)
    quiet_sends = len(webhook.sent) - quiet_before
    quiet_calls = fetcher.calls[data_before:]
    quiet_record = state.beta_record("zzz")
    quiet_revision = quiet_record["revisions"].get(quiet_record.get("latest_rev")) or {}
    _assert(quiet_sends == 0, "explicit quiet revision emitted a card")
    _assert(quiet_revision.get("status") == "silent", "quiet revision did not settle silently")
    _assert(all(kind != "json" or "/character/" not in url for kind, url in quiet_calls),
            "quiet revision fetched character data")

    # Delivery edges are separate from the happy-path monitor so one failed/ambiguous test does
    # not obscure all-game evidence. They still render actual Components V2 beta payloads.
    edge_state = _MemoryState()
    edge_timeline: list[tuple[str, Any]] = []
    edge_webhook = _HarnessWebhook(edge_timeline)
    edge_sleeper = _HarnessSleeper(edge_timeline)
    edge_ctx = _ctx(games, fetcher, edge_state, edge_webhook, edge_sleeper)
    edge_game = games["zzz"]
    edge_record = edge_state.beta_record("zzz")
    edge_webhook.responses(SendResult(False, 500, error="fixture first-page failure"))
    await beta._deliver(edge_ctx, edge_game, _edge_patch(), edge_record, ephemeral=False)
    _assert(edge_sleeper.calls == [], "failed first page created a delay")
    _assert(len(edge_webhook.sent) == 1, "failed first page was retried in-place")

    # An indeterminate POST is also a hard stop: its outcome is held rather than retried or
    # paced into another page that could make an operator-facing duplicate harder to diagnose.
    ambiguous_state = _MemoryState()
    ambiguous_timeline: list[tuple[str, Any]] = []
    ambiguous_webhook = _HarnessWebhook(ambiguous_timeline)
    ambiguous_sleeper = _HarnessSleeper(ambiguous_timeline)
    ambiguous_ctx = _ctx(games, fetcher, ambiguous_state, ambiguous_webhook, ambiguous_sleeper)
    ambiguous_webhook.responses(SendResult(False, 0, error="fixture indeterminate POST", ambiguous=True))
    ambiguous_patch = _edge_patch()
    await beta._deliver(ambiguous_ctx, edge_game, ambiguous_patch,
                        ambiguous_state.beta_record("zzz"), ephemeral=False)
    ambiguous_revision = ambiguous_state.beta_record("zzz")["revisions"].get(ambiguous_patch.rev_id) or {}
    _assert(ambiguous_sleeper.calls == [], "ambiguous first page created a delay")
    _assert(ambiguous_revision.get("status") == "ambiguous", "ambiguous outcome was not held")
    ambiguous_sends = len(ambiguous_webhook.sent)
    await beta._deliver(ambiguous_ctx, edge_game, ambiguous_patch,
                        ambiguous_state.beta_record("zzz"), ephemeral=False)
    _assert(len(ambiguous_webhook.sent) == ambiguous_sends,
            "an ambiguous batch was retried without operator reconciliation")
    _assert(any("prior ambiguous Discord outcome is held" in warning
                for warning in ambiguous_ctx.warnings),
            "an ambiguous replay hold was not reported")

    # Seed page one as a previous invocation would. Resuming starts immediately at page two;
    # it only pauses after a newly successful page that has another new page following it.
    resume_patch = _edge_patch()
    payloads, _char_map = beta.beta_payloads_with_map(edge_game, resume_patch, edge_ctx.settings, None)
    _assert(len(payloads) >= 3, "edge fixture must render at least three pages")
    revision, batch = beta._batch_record(edge_record, resume_patch, edge_ctx.now)
    batch["signature"] = stable_hash(payloads)
    batch["messages"] = ["prior-page-1"]
    revision["messages"] = ["prior-page-1"]
    edge_webhook.responses(*[SendResult(True, 200, message_id=f"edge-{i}") for i in range(2, len(payloads) + 1)])
    timeline_start = len(edge_timeline)
    sleeps_start = len(edge_sleeper.calls)
    await beta._deliver(edge_ctx, edge_game, resume_patch, edge_record, ephemeral=False)
    resume_events = edge_timeline[timeline_start:]
    resume_sleeps = edge_sleeper.calls[sleeps_start:]
    _assert(resume_events and resume_events[0][0] == "send", "resume waited before its first unsent page")
    _assert(resume_sleeps == [beta.BETA_PAGE_GAP_SECONDS] * max(0, len(payloads) - 2),
            "resume gap count is not exactly between newly sent pages")
    _assert(resume_events[-1][0] == "send", "a trailing post-final-page delay was added")

    all_payloads_valid = all(validate_payload(payload) == [] for payload in webhook.sent + edge_webhook.sent)
    _assert(all_payloads_valid, "the harness webhook accepted an invalid Components V2 payload")
    _assert(state.disk_writes == 0 and edge_state.disk_writes == 0 and ambiguous_state.disk_writes == 0,
            "harness touched disk")
    _assert(state.save_calls > 0 and edge_state.save_calls > 0 and ambiguous_state.save_calls > 0,
            "state settlement/save transitions were not exercised")

    return {
        "enabled": True,
        "mode": "deterministic BETA_ENABLED=1 full pipeline",
        "settings": {"BETA_ENABLED": "1", "BETA_GAMES": list(HARNESS_GAMES)},
        "in_memory_only": True,
        "live_source_requests": 0,
        "live_webhook_posts": 0,
        "physical_state_writes": 0,
        "fixture_source_calls": fetcher.fixture_source_calls,
        "fixture_webhook_posts": len(webhook.sent) + len(edge_webhook.sent) + len(ambiguous_webhook.sent),
        "state_save_transitions": state.save_calls + edge_state.save_calls + ambiguous_state.save_calls,
        "payloads_validated": all_payloads_valid,
        "initial_full_kit": initial,
        "retained_revision": revisions,
        "dedup_replay_posts": dedup_sends,
        "quiet_revision": {
            "game": "zzz", "posts": quiet_sends, "status": quiet_revision.get("status"),
            "character_detail_reads": sum(1 for kind, url in quiet_calls if kind == "json" and "/character/" in url),
        },
        "delivery_pacing": {
            "gap_seconds": beta.BETA_PAGE_GAP_SECONDS,
            "initial_recorded_gaps": len(initial_delays),
            "revision_recorded_gaps": revision_delays,
            "failed_first_page_gaps": 0,
            "ambiguous_first_page_gaps": 0,
            "ambiguous_outcome_held": ambiguous_revision.get("status") == "ambiguous",
            "ambiguous_replay_blocked": len(ambiguous_webhook.sent) == ambiguous_sends,
            "resume_started_with_send": bool(resume_events and resume_events[0][0] == "send"),
            "resume_recorded_gaps": len(resume_sleeps),
            "resume_has_no_trailing_gap": bool(resume_events and resume_events[-1][0] == "send"),
        },
    }
