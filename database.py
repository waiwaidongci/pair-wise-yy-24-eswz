from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime


class DomainError(ValueError):
    """A business-rule violation that should be shown to the API caller."""


def _minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _overlap(a_start: str, a_duration: int, b_start: str, b_duration: int) -> bool:
    start_a, start_b = _minutes(a_start), _minutes(b_start)
    return start_a < start_b + b_duration and start_b < start_a + a_duration


SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  root_id INTEGER,
  version INTEGER NOT NULL DEFAULT 1,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  sponsor TEXT,
  cooldown_minutes INTEGER NOT NULL DEFAULT 0 CHECK(cooldown_minutes >= 0),
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  created_at TEXT NOT NULL DEFAULT '',
  UNIQUE(root_id, version)
);
CREATE TABLE IF NOT EXISTS program_regions (
  program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
  region TEXT NOT NULL,
  PRIMARY KEY(program_id, region)
);
CREATE TABLE IF NOT EXISTS blocked_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  region TEXT NOT NULL,
  weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  reason TEXT NOT NULL,
  CHECK(start_time < end_time)
);
CREATE TABLE IF NOT EXISTS sponsor_policies (
  sponsor TEXT PRIMARY KEY,
  min_gap_minutes INTEGER NOT NULL CHECK(min_gap_minutes >= 0)
);
CREATE TABLE IF NOT EXISTS slots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  air_date TEXT NOT NULL,
  start_time TEXT NOT NULL,
  duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
  program_id INTEGER NOT NULL REFERENCES programs(id),
  region TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned'
    CHECK(status IN ('planned','pending','replaced','cancelled')),
  replaced_from INTEGER REFERENCES programs(id),
  review_reason TEXT,
  next_playable_start TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slots_date_region ON slots(air_date, region);
CREATE TABLE IF NOT EXISTS playout_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
  actual_start TEXT NOT NULL,
  actual_duration_minutes INTEGER NOT NULL CHECK(actual_duration_minutes >= 0),
  actual_program_id INTEGER REFERENCES programs(id),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconciliation_exceptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  air_date TEXT NOT NULL,
  slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(air_date, slot_id, kind)
);
"""


class Database:
    """SQLite connection, schema and migrations. No business rules here."""

    def __init__(self, path: str = "radio.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._tx_depth = 0
        self._schema()
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        """One atomic unit of work; nested calls join the outer transaction."""
        outermost = self._tx_depth == 0
        if outermost:
            self.conn.execute("BEGIN IMMEDIATE")
        self._tx_depth += 1
        try:
            yield
        except Exception:
            self._tx_depth -= 1
            if outermost:
                self.conn.rollback()
            raise
        else:
            self._tx_depth -= 1
            if outermost:
                self.conn.commit()

    def _schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _migrate(self) -> None:
        """Rebuild legacy tables in place so older radio.db files keep working."""
        program_cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(programs)")}
        slot_cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(slots)")}
        rebuild_programs = "version" not in program_cols
        rebuild_slots = "review_reason" not in slot_cols
        if not (rebuild_programs or rebuild_slots):
            return
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.transaction():
                if rebuild_programs:
                    self.conn.execute(
                        """
                        CREATE TABLE programs_new (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          root_id INTEGER,
                          version INTEGER NOT NULL DEFAULT 1,
                          title TEXT NOT NULL,
                          kind TEXT NOT NULL,
                          duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
                          start_date TEXT NOT NULL,
                          end_date TEXT NOT NULL,
                          sponsor TEXT,
                          cooldown_minutes INTEGER NOT NULL DEFAULT 0 CHECK(cooldown_minutes >= 0),
                          active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                          created_at TEXT NOT NULL DEFAULT '',
                          UNIQUE(root_id, version)
                        )
                        """
                    )
                    self.conn.execute(
                        "INSERT INTO programs_new(id,root_id,version,title,kind,duration_minutes,start_date,end_date,"
                        "sponsor,cooldown_minutes,active,created_at) "
                        "SELECT id,id,1,title,kind,duration_minutes,start_date,end_date,sponsor,cooldown_minutes,active,'' "
                        "FROM programs"
                    )
                    self.conn.execute("DROP TABLE programs")
                    self.conn.execute("ALTER TABLE programs_new RENAME TO programs")
                if rebuild_slots:
                    self.conn.execute(
                        """
                        CREATE TABLE slots_new (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          air_date TEXT NOT NULL,
                          start_time TEXT NOT NULL,
                          duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
                          program_id INTEGER NOT NULL REFERENCES programs(id),
                          region TEXT NOT NULL,
                          status TEXT NOT NULL DEFAULT 'planned'
                            CHECK(status IN ('planned','pending','replaced','cancelled')),
                          replaced_from INTEGER REFERENCES programs(id),
                          review_reason TEXT,
                          next_playable_start TEXT,
                          created_at TEXT NOT NULL
                        )
                        """
                    )
                    self.conn.execute(
                        "INSERT INTO slots_new(id,air_date,start_time,duration_minutes,program_id,region,status,"
                        "replaced_from,review_reason,next_playable_start,created_at) "
                        "SELECT id,air_date,start_time,duration_minutes,program_id,region,status,replaced_from,NULL,NULL,created_at "
                        "FROM slots"
                    )
                    self.conn.execute("DROP TABLE slots")
                    self.conn.execute("ALTER TABLE slots_new RENAME TO slots")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_slots_date_region ON slots(air_date, region)")
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")


class RadioDB:
    """组合入口：节目维护（ProgramService）与排期判定（SchedulingService）共用同一存储。"""

    def __init__(self, path: str = "radio.db") -> None:
        from programs import ProgramService
        from scheduling import SchedulingService

        self.store = Database(path)
        self.programs = ProgramService(self.store)
        self.scheduling = SchedulingService(self.store)

    def close(self) -> None:
        self.store.close()

    # ---- 节目维护 ----
    def add_program(self, *args, **kwargs):
        return self.programs.add_program(*args, **kwargs)

    def update_program(self, program_id: int, **changes) -> dict:
        """节目修改生成新版本，并在同一事务内重查未播排期。"""
        with self.store.transaction():
            version = self.programs.update_program(program_id, **changes)
            report = self.scheduling.recheck_after_version_change(version["supersedes"], version["id"])
        return {"program": version, "recheck": report}

    def authorize_region(self, program_id: int, region: str) -> dict:
        """追加地区授权也算一次修改：生成新版本并重查未播排期。"""
        with self.store.transaction():
            version = self.programs.authorize_region(program_id, region)
            report = None
            if version["supersedes"] != version["id"]:
                report = self.scheduling.recheck_after_version_change(version["supersedes"], version["id"])
        return {"program": version, "recheck": report}

    # ---- 排期判定 ----
    def schedule_slot(self, *args, **kwargs):
        return self.scheduling.schedule_slot(*args, **kwargs)

    def replace_slot(self, *args, **kwargs):
        return self.scheduling.replace_slot(*args, **kwargs)

    def reschedule_slot(self, *args, **kwargs):
        return self.scheduling.reschedule_slot(*args, **kwargs)

    def record_playout(self, *args, **kwargs):
        return self.scheduling.record_playout(*args, **kwargs)

    def reconcile_date(self, *args, **kwargs):
        return self.scheduling.reconcile_date(*args, **kwargs)

    def get_slot(self, *args, **kwargs):
        return self.scheduling.get_slot(*args, **kwargs)

    def get_exceptions(self, *args, **kwargs):
        return self.scheduling.get_exceptions(*args, **kwargs)

    def snapshot(self, *args, **kwargs):
        return self.scheduling.snapshot(*args, **kwargs)

    def add_sponsor_policy(self, *args, **kwargs):
        return self.scheduling.add_sponsor_policy(*args, **kwargs)

    def add_blocked_window(self, *args, **kwargs):
        return self.scheduling.add_blocked_window(*args, **kwargs)

    def seed_demo(self) -> None:
        existing = self.store.conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
        if existing:
            return
        music = self.add_program("晨间轻音乐", "music", 30, "2026-01-01", "2026-12-31", "青柠饮品", 45, ["华东"])
        news = self.add_program("城市早报", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        ad = self.add_program("青柠饮品广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠饮品", 60, ["华东"])
        self.add_sponsor_policy("青柠饮品", 90)
        self.add_blocked_window("华东", 0, "08:00", "08:30", "周一设备检修")
        self.schedule_slot("2026-09-28", "09:00", music, "华东")
        self.schedule_slot("2026-09-28", "10:00", news, "华东")
        self.schedule_slot("2026-09-28", "11:00", ad, "华东")
