import os
import tempfile
import unittest
from pathlib import Path

sys_path = str(Path(__file__).resolve().parents[1])
import sys
sys.path.insert(0, sys_path)

from database import RadioDB
from errors import DomainError


class ProgramVersioningTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        # 60-minute program licensed 华东, with an ad sharing the sponsor.
        self.pid = self.db.add_program("晨间脱口秀", "talk", 60, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        self.ad = self.db.add_program("青柠广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        self.db.add_sponsor_policy("青柠", 90)
        self.db.add_blocked_window("华东", 0, "10:00", "10:30", "周一设备检修")

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_revision_creates_new_version_and_keeps_history(self):
        v1 = self.db.snapshot()["programs"][0]
        self.assertEqual(1, v1["version_no"])
        report = self.db.revise_program(self.pid, duration_minutes=45)
        self.assertEqual(2, report["version_no"])
        snap = {p["id"]: p for p in self.db.snapshot()["programs"]}
        self.assertEqual(45, snap[self.pid]["duration_minutes"])
        self.assertEqual(2, snap[self.pid]["version_count"])
        slot = self.db.schedule_slot("2026-09-29", "09:00", self.pid, "华东")
        self.assertEqual(45, self.db.get_slot(slot)["duration_minutes"])
        self.assertEqual(2, self.db.get_slot(slot)["version_no"])
        with self.assertRaisesRegex(DomainError, "没有变化"):
            self.db.revise_program(self.pid, duration_minutes=45)

    def test_open_slot_upgraded_when_new_version_passes(self):
        slot = self.db.schedule_slot("2026-09-29", "09:00", self.pid, "华东")
        self.assertEqual(60, self.db.get_slot(slot)["duration_minutes"])
        report = self.db.revise_program(self.pid, duration_minutes=45)
        self.assertEqual([slot], report["upgraded"])
        refreshed = self.db.get_slot(slot)
        self.assertEqual("planned", refreshed["status"])
        self.assertEqual(2, refreshed["version_no"])
        self.assertEqual(45, refreshed["duration_minutes"])

    def test_region_removal_returns_slot_with_reason_and_earliest_start(self):
        # 2026-09-28 is Monday and has the 10:00-10:30 blocked window.
        slot = self.db.schedule_slot("2026-09-28", "10:30", self.pid, "华东")
        report = self.db.revise_program(self.pid, regions=["华北"])
        self.assertEqual([], report["upgraded"])
        row = report["needs_revision"][0]
        self.assertEqual(slot, row["slot_id"])
        self.assertEqual("2026-09-28", row["air_date"])
        self.assertEqual("10:30", row["start_time"])
        self.assertEqual(60, row["original_duration_minutes"])
        self.assertIn("未授权", row["reason"])
        self.assertIsNone(row["earliest_start"])  # 华东 no longer licensed at all
        pinned = self.db.get_slot(slot)
        self.assertEqual("needs_revision", pinned["status"])
        # The slot still shows the original time/material for the page.
        self.assertEqual(60, pinned["duration_minutes"])
        self.assertEqual(1, pinned["version_no"])
        self.assertEqual(2, pinned["candidate_version_no"])
        # Re-adding 华东 as a revision rechecks and upgrades automatically.
        second = self.db.revise_program(self.pid, regions=["华东", "华北"])
        self.assertEqual([slot], second["upgraded"])
        self.assertEqual(3, self.db.get_slot(slot)["version_no"])

    def test_duration_change_pushed_back_suggests_first_playable_start(self):
        # Original 60-minute slot 09:00 Monday clears the 10:00-10:30 blocked window.
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.pid, "华东")
        # Grow to 120 minutes: 09:00-11:00 crosses the 10:00-10:30 blocked window.
        report = self.db.revise_program(self.pid, duration_minutes=120)
        row = report["needs_revision"][0]
        self.assertEqual(slot, row["slot_id"])
        self.assertIn("禁播", row["reason"])
        self.assertEqual("10:30", row["earliest_start"])
        # Operator accepts the suggested start and the slot adopts the new version.
        fixed = self.db.revise_slot(slot, "10:30", "华东")
        self.assertEqual("planned", fixed["status"])
        self.assertEqual(120, fixed["duration_minutes"])
        self.assertEqual(2, fixed["version_no"])

    def test_failed_slot_revision_keeps_returned_with_new_reason(self):
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.pid, "华东")
        self.db.revise_program(self.pid, duration_minutes=120)
        # 10:15 still hits the blocked window; slot stays needs_revision.
        with self.assertRaisesRegex(DomainError, "禁播"):
            self.db.revise_slot(slot, "10:15")
        pinned = self.db.get_slot(slot)
        self.assertEqual("needs_revision", pinned["status"])
        self.assertEqual(1, pinned["version_no"])
        self.assertIn("禁播", pinned["revision_reason"])

    def test_only_planned_slots_can_be_revised(self):
        slot = self.db.schedule_slot("2026-09-29", "09:00", self.pid, "华东")
        with self.assertRaisesRegex(DomainError, "待改"):
            self.db.revise_slot(slot)

    def test_replaced_slot_stays_pinned_when_program_changes(self):
        slot = self.db.schedule_slot("2026-09-29", "09:00", self.pid, "华东")
        self.db.replace_slot(slot, self.ad)
        self.assertEqual(5, self.db.get_slot(slot)["duration_minutes"])
        report = self.db.revise_program(self.ad, duration_minutes=3)
        self.assertEqual([], report["upgraded"])
        locked_ids = [item["id"] for item in report["locked"]]
        self.assertIn(slot, locked_ids)
        pinned = self.db.get_slot(slot)
        self.assertEqual("replaced", pinned["status"])
        self.assertEqual(1, pinned["version_no"])  # replaced-to ad version, untouched
        self.assertEqual(5, pinned["duration_minutes"])

    def test_played_slot_stays_pinned_and_reconcile_uses_historical_version(self):
        slot = self.db.schedule_slot("2026-09-29", "09:00", self.pid, "华东")
        self.db.record_playout(slot, "09:00", 60, self.pid)
        report = self.db.revise_program(self.pid, duration_minutes=15)
        self.assertEqual([], report["upgraded"])
        self.assertIn(slot, [item["id"] for item in report["locked"]])
        pinned = self.db.get_slot(slot)
        self.assertEqual(1, pinned["version_no"])
        self.assertEqual(60, pinned["duration_minutes"])
        # Reconciliation compares against the pinned 60-minute version: 60 actual is fine.
        exceptions = self.db.reconcile_date("2026-09-29")
        self.assertEqual([], exceptions)

    def test_authorize_region_issues_version_and_rechecks(self):
        slot = self.db.schedule_slot("2026-09-29", "09:00", self.pid, "华东")
        # First drop 华东: the open slot is returned against the 华北-only version.
        self.db.revise_program(self.pid, regions=["华北"])
        self.assertEqual("needs_revision", self.db.get_slot(slot)["status"])
        # Granting 华东 issues another version and upgrades the returned slot.
        report = self.db.authorize_region(self.pid, "华东")
        self.assertEqual(3, report["version_no"])
        self.assertEqual([slot], report["upgraded"])
        self.assertEqual("planned", self.db.get_slot(slot)["status"])


if __name__ == "__main__":
    unittest.main()
