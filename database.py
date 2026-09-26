from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime

from errors import DomainError
from catalog import ProgramCatalog, PROGRAM_KINDS
from scheduling import Scheduler

_SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  current_version_id INTEGER,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  UNIQUE(title)
);
CREATE TABLE IF NOT EXISTS program_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
  version_no INTEGER NOT NULL CHECK(version_no >= 1),
  duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  sponsor TEXT,
  cooldown_minutes INTEGER NOT NULL DEFAULT 0 CHECK(cooldown_minutes >= 0),
  created_at TEXT NOT NULL,
  UNIQUE(program_id, version_no)
);
CREATE TABLE IF NOT EXISTS program_regions (
  program_version_id INTEGER NOT NULL REFERENCES program_versions(id) ON DELETE CASCADE,
  region TEXT NOT NULL,
  PRIMARY KEY(program_version_id, region)
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
  program_version_id INTEGER NOT NULL REFERENCES program_versions(id),
  region TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned'
    CHECK(status IN ('planned','replaced','cancelled','needs_revision')),
  replaced_from_version_id INTEGER REFERENCES program_versions(id),
  revision_air_date TEXT,
  revision_start_time TEXT,
  revision_duration_minutes INTEGER,
  revision_reason TEXT,
  revision_candidate_version_id INTEGER REFERENCES program_versions(id),
  revision_earliest_start TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slots_date_region ON slots(air_date, region);
CREATE TABLE IF NOT EXISTS playout_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
  actual_start TEXT NOT NULL,
  actual_duration_minutes INTEGER NOT NULL CHECK(actual_duration_minutes >= 0),
  actual_program_version_id INTEGER REFERENCES program_versions(id),
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

# Migration from the pre-versioning layout (programs carried mutable columns and
# slots referenced programs directly). One historical row becomes version 1.
_LEGACY_COLUMNS = {"duration_minutes", "end_date"}


class RadioDB:
    """SQLite storage plus reconciliation.

    Program maintenance lives in ``ProgramCatalog``, slot adjudication in
    ``Scheduler``; this class owns the connection, schema and history-facing
    reads. Playout and reconciliation always use the version pinned on the
    slot, so revising a program never rewrites the past.
    """

    def __init__(self, path: str = "radio.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._schema()
        self.catalog = ProgramCatalog(self)
        self.scheduler = Scheduler(self)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(programs)").fetchall()}
        if columns and _LEGACY_COLUMNS <= columns:
            self._migrate_legacy()
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def _migrate_legacy(self) -> None:
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.executescript(
            """
            ALTER TABLE programs RENAME TO programs_old;
            ALTER TABLE slots RENAME TO slots_old;
            ALTER TABLE playout_logs RENAME TO playout_logs_old;
            ALTER TABLE program_regions RENAME TO program_regions_old;

            CREATE TABLE programs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              kind TEXT NOT NULL,
              current_version_id INTEGER,
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
              UNIQUE(title)
            );
            CREATE TABLE program_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              program_id INTEGER NOT NULL,
              version_no INTEGER NOT NULL,
              duration_minutes INTEGER NOT NULL,
              start_date TEXT NOT NULL,
              end_date TEXT NOT NULL,
              sponsor TEXT,
              cooldown_minutes INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              UNIQUE(program_id, version_no)
            );
            CREATE TABLE program_regions (
              program_version_id INTEGER NOT NULL,
              region TEXT NOT NULL,
              PRIMARY KEY(program_version_id, region)
            );
            CREATE TABLE slots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL,
              start_time TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL,
              program_version_id INTEGER NOT NULL,
              region TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'planned',
              replaced_from_version_id INTEGER,
              revision_air_date TEXT,
              revision_start_time TEXT,
              revision_duration_minutes INTEGER,
              revision_reason TEXT,
              revision_candidate_version_id INTEGER,
              revision_earliest_start TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE playout_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slot_id INTEGER NOT NULL,
              actual_start TEXT NOT NULL,
              actual_duration_minutes INTEGER NOT NULL,
              actual_program_version_id INTEGER,
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );

            INSERT INTO programs(id,title,kind,current_version_id,active)
            SELECT id,title,kind,id,active FROM programs_old;
            INSERT INTO program_versions(id,program_id,version_no,duration_minutes,start_date,end_date,sponsor,cooldown_minutes,created_at)
            SELECT id,id,1,duration_minutes,start_date,end_date,sponsor,cooldown_minutes,
                   COALESCE((SELECT MIN(created_at) FROM slots_old WHERE program_id=programs_old.id), '')
            FROM programs_old;
            INSERT INTO program_regions(program_version_id,region)
            SELECT program_id,region FROM program_regions_old;
            INSERT INTO slots(id,air_date,start_time,duration_minutes,program_version_id,region,status,
                              replaced_from_version_id,created_at)
            SELECT id,air_date,start_time,duration_minutes,program_id,region,status,replaced_from,created_at
            FROM slots_old;
            INSERT INTO playout_logs(id,slot_id,actual_start,actual_duration_minutes,
                                     actual_program_version_id,note,created_at)
            SELECT l.id,l.slot_id,l.actual_start,l.actual_duration_minutes,
                   COALESCE(l.actual_program_id, s.program_id), l.note, l.created_at
            FROM playout_logs_old l
            JOIN slots_old s ON s.id=l.slot_id;

            DROP TABLE playout_logs_old;
            DROP TABLE slots_old;
            DROP TABLE program_regions_old;
            DROP TABLE programs_old;
            """
        )
        self.conn.execute("PRAGMA foreign_keys = ON")

    def seed_demo(self) -> None:
        existing = self.conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
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

    # ---- program maintenance (delegated to the catalog layer) -------------
    def add_program(self, *args, **kwargs) -> int:
        return self.catalog.add_program(*args, **kwargs)

    def revise_program(self, *args, **kwargs) -> dict:
        return self.catalog.revise_program(*args, **kwargs)

    def authorize_region(self, program_id: int, region: str) -> dict:
        return self.catalog.authorize_region(program_id, region)

    def add_sponsor_policy(self, sponsor: str, min_gap_minutes: int) -> None:
        if not sponsor.strip() or min_gap_minutes < 0:
            raise DomainError("赞助商和最小间隔必须有效")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO sponsor_policies(sponsor,min_gap_minutes) VALUES(?,?) "
                "ON CONFLICT(sponsor) DO UPDATE SET min_gap_minutes=excluded.min_gap_minutes",
                (sponsor.strip(), min_gap_minutes),
            )

    def add_blocked_window(self, region: str, weekday: int, start_time: str, end_time: str, reason: str) -> int:
        from scheduling import _minutes
        if weekday not in range(7) or _minutes(start_time) >= _minutes(end_time):
            raise DomainError("禁播时段参数无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason) VALUES(?,?,?,?,?)",
                (region.strip(), weekday, start_time, end_time, reason.strip() or "禁播"),
            )
        return int(cur.lastrowid)

    # ---- scheduling (delegated to the scheduler layer) --------------------
    def schedule_slot(self, *args, **kwargs) -> int:
        return self.scheduler.schedule_slot(*args, **kwargs)

    def replace_slot(self, *args, **kwargs) -> dict:
        return self.scheduler.replace_slot(*args, **kwargs)

    def revise_slot(self, *args, **kwargs) -> dict:
        return self.scheduler.revise_slot(*args, **kwargs)

    def get_slot(self, slot_id: int) -> dict:
        return self.scheduler.get_slot(slot_id)

    def record_playout(self, slot_id: int, actual_start: str, actual_duration_minutes: int,
                       actual_program_id: int | None = None, note: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM slots WHERE id=?", (slot_id,)).fetchone():
            raise DomainError("排期不存在")
        if actual_duration_minutes < 0:
            raise DomainError("实际时长不能为负数")
        from scheduling import _minutes
        _minutes(actual_start)
        with self.transaction():
            slot = self.conn.execute("SELECT program_version_id FROM slots WHERE id=?", (slot_id,)).fetchone()
            actual_version_id = None
            if actual_program_id is not None:
                version = self.conn.execute(
                    "SELECT id FROM program_versions WHERE program_id=? ORDER BY version_no DESC LIMIT 1",
                    (actual_program_id,),
                ).fetchone()
                if not version:
                    raise DomainError("实播节目不存在")
                actual_version_id = version["id"]
            cur = self.conn.execute(
                "INSERT INTO playout_logs(slot_id,actual_start,actual_duration_minutes,"
                "actual_program_version_id,note,created_at) VALUES(?,?,?,?,?,?)",
                (slot_id, actual_start, actual_duration_minutes,
                 actual_version_id or slot["program_version_id"], note, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    # ---- reconciliation: every fact comes from the pinned version ---------
    def reconcile_date(self, air_date: str) -> list[dict]:
        """Compare the latest playout per slot with the plan and persist exceptions."""
        try:
            datetime.strptime(air_date, "%Y-%m-%d")
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        with self.transaction():
            self.conn.execute("DELETE FROM reconciliation_exceptions WHERE air_date=?", (air_date,))
            slots = self._planned_slots(air_date)
            exceptions: list[tuple[int, str, str]] = []
            for slot in slots:
                log = self.conn.execute(
                    "SELECT * FROM playout_logs WHERE slot_id=? ORDER BY id DESC LIMIT 1", (slot["id"],)
                ).fetchone()
                if not log:
                    exceptions.append((slot["id"], "missed", "没有实播记录"))
                    continue
                planned_program_id = slot["program_id"]
                actual_version = self.conn.execute(
                    "SELECT pv.*, p.title FROM program_versions pv JOIN programs p ON p.id=pv.program_id "
                    "WHERE pv.id=?", (log["actual_program_version_id"],)
                ).fetchone()
                actual_program_id = actual_version["program_id"] if actual_version else planned_program_id
                if actual_program_id != planned_program_id:
                    exceptions.append(
                        (slot["id"], "wrong_program",
                         f"计划节目 {slot['title']}#{planned_program_id}，实播节目 "
                         f"{actual_version['title'] if actual_version else '#'+str(actual_program_id)}"
                         f"#{actual_program_id}")
                    )
                delta = log["actual_duration_minutes"] - slot["duration_minutes"]
                if abs(delta) > 30:
                    kind = "overrun" if delta > 0 else "underrun"
                    exceptions.append((slot["id"], kind, f"与计划相差 {delta:+d} 分钟"))
                if actual_version:
                    region_ok = self.conn.execute(
                        "SELECT 1 FROM program_regions WHERE program_version_id=? AND region=?",
                        (actual_version["id"], slot["region"]),
                    ).fetchone()
                    if not region_ok or not (actual_version["start_date"] <= air_date <= actual_version["end_date"]):
                        exceptions.append((slot["id"], "out_of_license", "实播节目超出地区或日期授权"))
            for slot_id, kind, detail in exceptions:
                self.conn.execute(
                    "INSERT INTO reconciliation_exceptions(air_date,slot_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                    (air_date, slot_id, kind, detail, datetime.now().isoformat()),
                )
        return self.get_exceptions(air_date)

    def _planned_slots(self, air_date: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT s.*, pv.program_id, pv.duration_minutes AS version_duration, p.title, p.kind, pv.sponsor, "
            "pv.start_date AS license_start, pv.end_date AS license_end "
            "FROM slots s "
            "JOIN program_versions pv ON pv.id=s.program_version_id "
            "JOIN programs p ON p.id=pv.program_id "
            "WHERE s.air_date=? AND s.status!='cancelled' ORDER BY s.start_time", (air_date,)
        ).fetchall()

    def get_exceptions(self, air_date: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM reconciliation_exceptions WHERE air_date=? ORDER BY slot_id, kind", (air_date,)
        ).fetchall()]

    def snapshot(self) -> dict:
        programs = []
        for prog in self.conn.execute(
            """
            SELECT p.id,p.title,p.kind,p.active,p.current_version_id,
                   cv.version_no,cv.duration_minutes,cv.start_date,cv.end_date,
                   cv.sponsor,cv.cooldown_minutes,
                   (SELECT COUNT(*) FROM program_versions pv WHERE pv.program_id=p.id) AS version_count
            FROM programs p
            JOIN program_versions cv ON cv.id=p.current_version_id
            ORDER BY p.id
            """
        ).fetchall():
            item = dict(prog)
            item["regions"] = [r["region"] for r in self.conn.execute(
                "SELECT region FROM program_regions WHERE program_version_id=? ORDER BY region",
                (prog["current_version_id"],)
            ).fetchall()]
            item["versions"] = [dict(row) for row in self.conn.execute(
                "SELECT id AS version_id,version_no,duration_minutes,start_date,end_date,sponsor,cooldown_minutes,created_at "
                "FROM program_versions WHERE program_id=? ORDER BY version_no", (prog["id"],)
            ).fetchall()]
            programs.append(item)
        slots = [dict(row) for row in self.conn.execute(
            """
            SELECT s.*, p.id AS program_id, p.title, p.kind, pv.version_no,
                   cv.version_no AS current_version_no,
                   rc.version_no AS candidate_version_no,
                   rf.version_no AS replaced_from_version_no
            FROM slots s
            JOIN program_versions pv ON pv.id=s.program_version_id
            JOIN programs p ON p.id=pv.program_id
            LEFT JOIN program_versions cv ON cv.id=p.current_version_id
            LEFT JOIN program_versions rc ON rc.id=s.revision_candidate_version_id
            LEFT JOIN program_versions rf ON rf.id=s.replaced_from_version_id
            ORDER BY s.air_date,s.start_time,s.id
            """
        ).fetchall()]
        return {"programs": programs, "slots": slots, "exceptions": [dict(row) for row in self.conn.execute(
            "SELECT * FROM reconciliation_exceptions ORDER BY id DESC LIMIT 50"
        ).fetchall()]}
