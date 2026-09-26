import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import DomainError, RadioDB


class ProgramVersioningTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.music = self.db.add_program("晨间轻音乐", "music", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _other_program(self):
        return self.db.add_program("城市早报", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])

    def test_update_creates_new_version_and_migrates_clean_slots(self):
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.music, "华东")
        result = self.db.update_program(self.music, duration_minutes=45)
        new_version = result["program"]
        self.assertEqual(2, new_version["version"])
        self.assertEqual(self.music, new_version["supersedes"])
        self.assertIn(slot, result["recheck"]["migrated"])
        moved = self.db.get_slot(slot)
        self.assertEqual(new_version["id"], moved["program_id"])
        self.assertEqual(45, moved["duration_minutes"])
        self.assertEqual("planned", moved["status"])
        old = self.db.programs.get(self.music)
        self.assertEqual(0, old["active"])  # 旧版本只留历史
        with self.assertRaisesRegex(DomainError, "只读"):
            self.db.update_program(self.music, duration_minutes=60)
        with self.assertRaisesRegex(DomainError, "停用"):
            self.db.schedule_slot("2026-09-29", "09:00", self.music, "华东")

    def test_failed_recheck_sends_slot_back_with_reason_and_playable_start(self):
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.music, "华东")
        other = self._other_program()
        self.db.schedule_slot("2026-09-28", "09:30", other, "华东")
        result = self.db.update_program(self.music, duration_minutes=45)
        self.assertEqual([], result["recheck"]["migrated"])
        (entry,) = result["recheck"]["pending"]
        self.assertEqual(slot, entry["slot_id"])
        self.assertEqual("2026-09-28", entry["air_date"])
        self.assertEqual("09:00", entry["start_time"])  # 原时段
        self.assertIn("重叠", entry["reason"])  # 失败原因
        self.assertEqual("10:00", entry["next_playable_start"])  # 仍可播的起点
        row = self.db.get_slot(slot)
        self.assertEqual("pending", row["status"])
        self.assertEqual("09:00", row["start_time"])
        self.assertEqual(result["program"]["id"], row["program_id"])
        # 待改排期不再占用时段，原时段可以排新节目
        self.db.schedule_slot("2026-09-28", "09:00", other, "华东")

    def test_region_removal_sends_slot_pending_without_playable_start(self):
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.music, "华东")
        result = self.db.update_program(self.music, regions=["华北"])
        (entry,) = result["recheck"]["pending"]
        self.assertEqual(slot, entry["slot_id"])
        self.assertIn("未授权", entry["reason"])
        self.assertIsNone(entry["next_playable_start"])

    def test_aired_and_replaced_slots_keep_their_version(self):
        aired = self.db.schedule_slot("2026-09-28", "09:00", self.music, "华东")
        self.db.record_playout(aired, "09:00", 30, self.music)
        spare = self.db.add_program("备用广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        replaced = self.db.schedule_slot("2026-09-28", "10:00", self.music, "华东")
        self.db.replace_slot(replaced, spare)
        result = self.db.update_program(self.music, duration_minutes=45)
        kept = {entry["slot_id"] for entry in result["recheck"]["kept"]}
        self.assertEqual({aired}, kept)  # replaced 槽已改指 spare，不在本次重查范围
        self.assertEqual(self.music, self.db.get_slot(aired)["program_id"])
        self.assertEqual(spare, self.db.get_slot(replaced)["program_id"])
        self.assertEqual("replaced", self.db.get_slot(replaced)["status"])
        # 对账按当时资料：实播与旧版本一致，不报任何异常
        exceptions = self.db.reconcile_date("2026-09-28")
        self.assertEqual([], [e for e in exceptions if e["slot_id"] == aired])

    def test_replaced_slot_keeps_version_when_its_program_changes(self):
        spare = self.db.add_program("备用广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        replaced = self.db.schedule_slot("2026-09-28", "10:00", self.music, "华东")
        self.db.replace_slot(replaced, spare)
        result = self.db.update_program(spare, duration_minutes=10)
        (entry,) = result["recheck"]["kept"]
        self.assertEqual(replaced, entry["slot_id"])
        self.assertIn("已替换", entry["reason"])
        row = self.db.get_slot(replaced)
        self.assertEqual(spare, row["program_id"])  # 保持当时版本
        self.assertEqual(5, row["duration_minutes"])

    def test_reschedule_pending_slot_to_playable_start(self):
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.music, "华东")
        other = self._other_program()
        self.db.schedule_slot("2026-09-28", "09:30", other, "华东")
        self.db.update_program(self.music, duration_minutes=45)
        fixed = self.db.reschedule_slot(slot, "2026-09-28", "10:00")
        self.assertEqual("planned", fixed["status"])
        self.assertEqual("10:00", fixed["start_time"])
        self.assertIsNone(fixed["review_reason"])
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.reschedule_slot(slot, "2026-09-28", "09:15")

    def test_pending_slot_follows_latest_version_and_can_be_restored(self):
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.music, "华东")
        other = self._other_program()
        self.db.schedule_slot("2026-09-28", "09:30", other, "华东")
        v2 = self.db.update_program(self.music, duration_minutes=45)["program"]
        self.assertEqual("pending", self.db.get_slot(slot)["status"])
        result = self.db.update_program(v2["id"], duration_minutes=30)
        self.assertEqual(3, result["program"]["version"])
        self.assertIn(slot, result["recheck"]["restored"])  # v3 又放得下，自动恢复
        row = self.db.get_slot(slot)
        self.assertEqual("planned", row["status"])
        self.assertEqual(result["program"]["id"], row["program_id"])

    def test_history_version_shown_in_slot_and_snapshot(self):
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.music, "华东")
        self.db.record_playout(slot, "09:02", 30, self.music)
        self.db.update_program(self.music, duration_minutes=45, title="晨间轻音乐（加长版）")
        shown = self.db.get_slot(slot)
        self.assertEqual("晨间轻音乐", shown["title"])  # 页面按当时版本显示
        self.assertEqual(1, shown["program_version"])
        snapshot = self.db.snapshot()
        row = [s for s in snapshot["slots"] if s["id"] == slot][0]
        self.assertEqual("晨间轻音乐", row["title"])
        versions = [p["version"] for p in snapshot["programs"]]
        self.assertEqual([1, 2], versions)

    def test_authorize_region_versions_and_rechecks(self):
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.music, "华东")
        result = self.db.authorize_region(self.music, "华北")
        self.assertEqual(2, result["program"]["version"])
        self.assertIn("华北", result["program"]["regions"])
        self.assertIn(slot, result["recheck"]["migrated"])
        again = self.db.authorize_region(result["program"]["id"], "华北")
        self.assertIsNone(again["recheck"])  # 重复授权不再生成新版本


if __name__ == "__main__":
    unittest.main()
