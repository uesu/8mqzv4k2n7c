"""Offline decision/state contract tests for the inactive day-only prototype."""
from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gamexpress import day_news_candidate as daynews  # noqa: E402
from gamexpress.sources import silverpalace  # noqa: E402

ROUTE = "a" * 16


def row(cid, day="2026-11-27"):
    return {"id": cid, "date": day, "title": "Official " + cid}


class DayNewsCandidateTests(unittest.TestCase):
    def test_silent_baseline_and_same_day_addition_without_timestamp_floor(self):
        first = daynews.plan(None, [row("01"), row("2")], route=ROUTE)
        self.assertEqual(first["known"], ["01", "2"])
        self.assertEqual(first["pending"], [])
        old = copy.deepcopy(first)
        updated = daynews.plan(first, [row("01"), row("2"), row("3")], route=ROUTE)
        self.assertEqual(first, old)
        self.assertEqual(updated["pending"], [row("3")])
        self.assertEqual(updated["known"], ["01", "2", "3"])
        self.assertNotIn("ts", updated["pending"][0])

    def test_explicit_initial_backfill_queues_every_id_oldest_day_first_and_resumes(self):
        snapshot = [row("30", "2026-11-03"), row("9", "2026-11-01"),
                    row("02", "2026-11-01"), row("1", "2026-10-31")]
        initial = copy.deepcopy(snapshot)
        state = daynews.plan(None, snapshot, route=ROUTE, bootstrap_backfill=True)
        self.assertEqual(snapshot, initial)
        self.assertEqual([r["id"] for r in state["pending"]], ["1", "02", "9", "30"])
        self.assertEqual(set(state["known"]), {"1", "02", "9", "30"})
        state = daynews.confirm_delivered(state, "1", route=ROUTE)
        resumed = daynews.plan(state, snapshot, route=ROUTE, bootstrap_backfill=True)
        self.assertEqual([r["id"] for r in resumed["pending"]], ["02", "9", "30"])
        before = copy.deepcopy(resumed)
        with self.assertRaises(daynews.InvalidSnapshot):
            daynews.plan(resumed, None, route=ROUTE, bootstrap_backfill=True)
        self.assertEqual(resumed, before)
        with self.assertRaises(daynews.InvalidSnapshot):
            daynews.plan(None, snapshot, route=ROUTE, bootstrap_backfill="1")

    def test_backdated_discovery_resorts_queue_and_out_of_order_confirmation_is_rejected(self):
        state = daynews.plan(None, [row("1", "2026-01-05"), row("2", "2026-01-06")],
                             route=ROUTE, bootstrap_backfill=True)
        state = daynews.plan(state, [row("2", "2026-01-06"), row("3", "2026-01-01")], route=ROUTE)
        self.assertEqual([r["id"] for r in state["pending"]], ["3", "1", "2"])
        with self.assertRaises(daynews.InvalidSnapshot):
            daynews.confirm_delivered(state, "2", route=ROUTE)
        state = daynews.confirm_delivered(state, "3", route=ROUTE)
        self.assertEqual([r["id"] for r in state["pending"]], ["1", "2"])

    def test_pending_survives_disappearance_restart_and_unavailable_snapshot(self):
        state = daynews.plan(None, [row("1")], route=ROUTE)
        state = daynews.plan(state, [row("1"), row("2", "2026-11-01")], route=ROUTE)
        previous = copy.deepcopy(state)
        with self.assertRaises(daynews.InvalidSnapshot):
            daynews.plan(state, None, route=ROUTE)
        self.assertEqual(state, previous)
        state = daynews.plan(state, [row("1")], route=ROUTE)
        self.assertEqual(state["pending"], [row("2", "2026-11-01")])
        changed = daynews.plan(state, [row("1"), row("2", "2026-11-27")], route=ROUTE)
        self.assertEqual(changed["pending"], [row("2", "2026-11-01")])
        settled = daynews.confirm_delivered(state, "2", route=ROUTE)
        self.assertEqual(state["pending"], [row("2", "2026-11-01")])
        self.assertEqual(settled["pending"], [])
        self.assertEqual(daynews.plan(settled, [row("1"), row("2")], route=ROUTE)["pending"], [])
        with self.assertRaises(daynews.InvalidSnapshot):
            daynews.confirm_delivered(settled, "2", route=ROUTE)

    def test_duplicate_bad_rows_route_change_and_state_corruption_fail_closed(self):
        baseline = daynews.plan(None, [row("1")], route=ROUTE)
        bad_snapshots = ([row("2"), row("2")], [row("../2")], [row("3", "2026-02-30")],
                         [{"id": "2", "date": "2026-11-27", "title": "  "}], [None], None)
        for bad in bad_snapshots:
            with self.subTest(bad=bad), self.assertRaises(daynews.InvalidSnapshot):
                daynews.plan(baseline, bad, route=ROUTE)
        with self.assertRaises(daynews.InvalidSnapshot):
            daynews.plan(baseline, [row("1")], route="b" * 16)
        with self.assertRaises(daynews.InvalidSnapshot):
            daynews.plan(None, [row("1")], route="https://secret.invalid/webhook")
        for corrupted in ({**baseline, "version": True}, {**baseline, "known": ["1", "1"]},
                          {**baseline, "pending": [row("9")]}, {**baseline, "pending": [None]},
                          {**baseline, "future_schema": "must not erase"},
                          {**baseline, "known": ["1", "2"], "pending": [{**row("2"), "message_ids": ["9"]}]}):
            with self.subTest(corrupted=corrupted), self.assertRaises(daynews.InvalidSnapshot):
                daynews.plan(corrupted, [row("1")], route=ROUTE)
        self.assertEqual(baseline["known"], ["1"])

    def test_silver_candidate_scans_feed_baseline_and_preserve_pending_on_failure(self):
        def page(rows):
            return {"code": 0, "data": {"current_page": "1", "total_page": 1,
                    "list": [{"id": r["id"], "title": r["title"], "publish_date": r["date"]}
                             for r in rows]}}

        class Fetcher:
            def __init__(self, responses):
                self.responses = iter(responses)

            async def get_json(self, url, **kwargs):
                return next(self.responses, None)

        initial = page([row("1")])
        same_day = page([row("1"), row("2")])
        first = asyncio.run(silverpalace.snapshot(Fetcher([initial, initial])))
        state = daynews.plan(None, first, route=ROUTE)
        latest = asyncio.run(silverpalace.snapshot(Fetcher([same_day, same_day])))
        state = daynews.plan(state, latest, route=ROUTE)
        self.assertEqual(state["pending"], [row("2")])
        before = copy.deepcopy(state)
        failed = asyncio.run(silverpalace.snapshot(Fetcher([same_day, None])))
        self.assertIsNone(failed)
        with self.assertRaises(daynews.InvalidSnapshot):
            daynews.plan(state, failed, route=ROUTE)
        self.assertEqual(state, before)

    def test_capacity_holds_without_pruning_or_mutating_state(self):
        with patch.object(daynews, "MAX_IDS", 2):
            state = daynews.plan(None, [row("1"), row("2")], route=ROUTE)
            original = copy.deepcopy(state)
            with self.assertRaises(daynews.InvalidSnapshot):
                daynews.plan(state, [row("1"), row("3")], route=ROUTE)
            self.assertEqual(state, original)


if __name__ == "__main__":
    unittest.main()
