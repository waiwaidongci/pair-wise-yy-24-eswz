"""排期判定：排期校验、版本重查、实播登记与按日期对账。"""

from __future__ import annotations

from datetime import datetime

from database import Database, DomainError, _hm, _minutes, _overlap


class SchedulingService:
    def __init__(self, store: Database) -> None:
        self.store = store

    # ---- 规则数据 ----
    def add_sponsor_policy(self, sponsor: str, min_gap_minutes: int) -> None:
        if not sponsor.strip() or min_gap_minutes < 0:
            raise DomainError("赞助商和最小间隔必须有效")
        with self.store.transaction():
            self.store.conn.execute(
                "INSERT INTO sponsor_policies(sponsor,min_gap_minutes) VALUES(?,?) "
                "ON CONFLICT(sponsor) DO UPDATE SET min_gap_minutes=excluded.min_gap_minutes",
                (sponsor.strip(), min_gap_minutes),
            )

    def add_blocked_window(self, region: str, weekday: int, start_time: str, end_time: str, reason: str) -> int:
        if weekday not in range(7) or _minutes(start_time) >= _minutes(end_time):
            raise DomainError("禁播时段参数无效")
        with self.store.transaction():
            cur = self.store.conn.execute(
                "INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason) VALUES(?,?,?,?,?)",
                (region.strip(), weekday, start_time, end_time, reason.strip() or "禁播"),
            )
        return int(cur.lastrowid)

    # ---- 校验 ----
    def _validate_slot(self, air_date: str, start_time: str, duration: int, program_id: int,
                       region: str, ignore_slot_id: int | None = None) -> None:
        try:
            day = datetime.strptime(air_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("播出日期必须使用 YYYY-MM-DD") from exc
        try:
            _minutes(start_time)
        except ValueError as exc:
            raise DomainError("开始时间必须使用 HH:MM") from exc
        if duration <= 0:
            raise DomainError("排期时长必须大于0")
        program = self.store.conn.execute("SELECT * FROM programs WHERE id=?", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在")
        if not program["active"]:
            raise DomainError("节目版本已停用，请改用最新版本")
        if program["duration_minutes"] != duration:
            raise DomainError(f"排期时长必须等于节目时长 {program['duration_minutes']} 分钟")
        if not (program["start_date"] <= air_date <= program["end_date"]):
            raise DomainError("播出日期超出授权窗口")
        if not self.store.conn.execute(
            "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (program_id, region)
        ).fetchone():
            raise DomainError(f"节目未授权在{region}播出")
        end_minutes = _minutes(start_time) + duration
        blocked = self.store.conn.execute(
            "SELECT * FROM blocked_windows WHERE region=? AND weekday=?",
            (region, day.weekday()),
        ).fetchall()
        for window in blocked:
            if _minutes(window["start_time"]) < end_minutes and _minutes(start_time) < _minutes(window["end_time"]):
                raise DomainError(f"与禁播时段冲突: {window['reason']}")
        sql = "SELECT * FROM slots WHERE air_date=? AND region=? AND status NOT IN ('cancelled','pending')"
        params: list[object] = [air_date, region]
        if ignore_slot_id is not None:
            sql += " AND id!=?"
            params.append(ignore_slot_id)
        for existing in self.store.conn.execute(sql, params).fetchall():
            if _overlap(start_time, duration, existing["start_time"], existing["duration_minutes"]):
                raise DomainError(f"与排期 #{existing['id']} 时间重叠")
        if program["cooldown_minutes"]:
            previous = self.store.conn.execute(
                "SELECT * FROM slots WHERE air_date=? AND region=? AND program_id=? "
                "AND status NOT IN ('cancelled','pending') AND id!=? AND start_time < ? "
                "ORDER BY start_time DESC LIMIT 1",
                (air_date, region, program_id, ignore_slot_id or -1, start_time),
            ).fetchone()
            if previous:
                gap = _minutes(start_time) - (_minutes(previous["start_time"]) + previous["duration_minutes"])
                if gap < program["cooldown_minutes"]:
                    raise DomainError(f"与上一期节目间隔不足冷却时间 {program['cooldown_minutes']} 分钟")
        if program["sponsor"]:
            policy = self.store.conn.execute(
                "SELECT min_gap_minutes FROM sponsor_policies WHERE sponsor=?", (program["sponsor"],)
            ).fetchone()
            if policy:
                gap = policy["min_gap_minutes"]
                all_sponsored = self.store.conn.execute(
                    "SELECT s.*, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id "
                    "WHERE s.air_date=? AND s.region=? AND s.status NOT IN ('cancelled','pending') "
                    "AND p.sponsor=? AND s.id!=?",
                    (air_date, region, program["sponsor"], ignore_slot_id or -1),
                ).fetchall()
                for other in all_sponsored:
                    if _overlap(start_time, duration, other["start_time"], other["duration_minutes"]):
                        raise DomainError(f"与赞助商 {program['sponsor']} 的其他节目冲突")
                    distance = abs(_minutes(start_time) - (_minutes(other["start_time"]) + other["duration_minutes"]))
                    if distance < gap:
                        raise DomainError(f"与赞助商 {program['sponsor']} 的节目间隔不足 {gap} 分钟")

    # ---- 排期操作 ----
    def schedule_slot(self, air_date: str, start_time: str, program_id: int, region: str) -> int:
        program = self.store.conn.execute("SELECT duration_minutes FROM programs WHERE id=?", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在")
        with self.store.transaction():
            self._validate_slot(air_date, start_time, int(program["duration_minutes"]), program_id, region)
            cur = self.store.conn.execute(
                "INSERT INTO slots(air_date,start_time,duration_minutes,program_id,region,created_at) VALUES(?,?,?,?,?,?)",
                (air_date, start_time, int(program["duration_minutes"]), program_id, region, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def replace_slot(self, slot_id: int, new_program_id: int) -> dict:
        """Replace a planned/pending item and revalidate the resulting plan atomically."""
        with self.store.transaction():
            slot = self.store.conn.execute(
                "SELECT * FROM slots WHERE id=? AND status IN ('planned','pending')", (slot_id,)
            ).fetchone()
            if not slot:
                raise DomainError("只能替换尚未播出且状态为 planned 或待改的排期")
            program = self.store.conn.execute("SELECT * FROM programs WHERE id=?", (new_program_id,)).fetchone()
            if not program:
                raise DomainError("替换节目不存在")
            self._validate_slot(slot["air_date"], slot["start_time"], int(program["duration_minutes"]),
                                new_program_id, slot["region"], slot_id)
            self.store.conn.execute(
                "UPDATE slots SET program_id=?, duration_minutes=?, replaced_from=?, status='replaced', "
                "review_reason=NULL, next_playable_start=NULL WHERE id=?",
                (new_program_id, int(program["duration_minutes"]), slot["program_id"], slot_id),
            )
        return self.get_slot(slot_id)

    def reschedule_slot(self, slot_id: int, air_date: str, start_time: str) -> dict:
        """把 planned/待改 排期调整到新时段；校验通过后回到 planned。"""
        with self.store.transaction():
            slot = self.store.conn.execute(
                "SELECT * FROM slots WHERE id=? AND status IN ('planned','pending')", (slot_id,)
            ).fetchone()
            if not slot:
                raise DomainError("只能调整 planned 或待改状态的排期")
            self._validate_slot(air_date, start_time, int(slot["duration_minutes"]),
                                slot["program_id"], slot["region"], slot_id)
            self.store.conn.execute(
                "UPDATE slots SET air_date=?, start_time=?, status='planned', "
                "review_reason=NULL, next_playable_start=NULL WHERE id=?",
                (air_date, start_time, slot_id),
            )
        return self.get_slot(slot_id)

    # ---- 版本重查 ----
    def recheck_after_version_change(self, old_program_id: int, new_program_id: int) -> dict:
        """节目出新版本后，立即重查所有仍引用旧版本且未播出的排期。

        校验通过的改指新版本；失败的退回待改并写清原时段、失败原因和仍可播的
        起点；已替换或已有实播记录的排期保持当时的版本。
        """
        program = self.store.conn.execute("SELECT * FROM programs WHERE id=?", (new_program_id,)).fetchone()
        if not program:
            raise DomainError("新版本不存在")
        duration = int(program["duration_minutes"])
        report: dict = {"old_version": old_program_id, "new_version": new_program_id,
                        "migrated": [], "restored": [], "pending": [], "kept": []}
        with self.store.transaction():
            rows = self.store.conn.execute(
                "SELECT * FROM slots WHERE program_id=? AND status IN ('planned','pending','replaced') "
                "ORDER BY air_date, start_time",
                (old_program_id,),
            ).fetchall()
            for slot in rows:
                if slot["status"] == "replaced":
                    report["kept"].append({"slot_id": slot["id"], "reason": "已替换的排期保持当时版本"})
                    continue
                aired = self.store.conn.execute(
                    "SELECT 1 FROM playout_logs WHERE slot_id=? LIMIT 1", (slot["id"],)
                ).fetchone()
                if aired:
                    report["kept"].append({"slot_id": slot["id"], "reason": "已有实播记录，保持当时版本"})
                    continue
                try:
                    self._validate_slot(slot["air_date"], slot["start_time"], duration,
                                        new_program_id, slot["region"], slot["id"])
                except DomainError as exc:
                    next_start = self._find_next_playable_start(slot, program)
                    self.store.conn.execute(
                        "UPDATE slots SET program_id=?, duration_minutes=?, status='pending', "
                        "review_reason=?, next_playable_start=? WHERE id=?",
                        (new_program_id, duration, str(exc), next_start, slot["id"]),
                    )
                    report["pending"].append({
                        "slot_id": slot["id"], "air_date": slot["air_date"], "start_time": slot["start_time"],
                        "region": slot["region"], "reason": str(exc), "next_playable_start": next_start,
                    })
                else:
                    self.store.conn.execute(
                        "UPDATE slots SET program_id=?, duration_minutes=?, status='planned', "
                        "review_reason=NULL, next_playable_start=NULL WHERE id=?",
                        (new_program_id, duration, slot["id"]),
                    )
                    key = "restored" if slot["status"] == "pending" else "migrated"
                    report[key].append(slot["id"])
        return report

    def _find_next_playable_start(self, slot, program) -> str | None:
        """从原时段起点向后找当天仍可播的最早起点；授权本身失效时返回 None。"""
        air_date, region = slot["air_date"], slot["region"]
        if not (program["start_date"] <= air_date <= program["end_date"]):
            return None
        if not self.store.conn.execute(
            "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (program["id"], region)
        ).fetchone():
            return None
        duration = int(program["duration_minutes"])
        latest = 24 * 60 - duration
        for start in range(_minutes(slot["start_time"]), latest + 1):
            try:
                self._validate_slot(air_date, _hm(start), duration, program["id"], region, slot["id"])
            except DomainError:
                continue
            return _hm(start)
        return None

    # ---- 实播与对账 ----
    def record_playout(self, slot_id: int, actual_start: str, actual_duration_minutes: int,
                       actual_program_id: int | None = None, note: str = "") -> int:
        if not self.store.conn.execute("SELECT 1 FROM slots WHERE id=?", (slot_id,)).fetchone():
            raise DomainError("排期不存在")
        if actual_duration_minutes < 0:
            raise DomainError("实际时长不能为负数")
        _minutes(actual_start)
        with self.store.transaction():
            cur = self.store.conn.execute(
                "INSERT INTO playout_logs(slot_id,actual_start,actual_duration_minutes,actual_program_id,note,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (slot_id, actual_start, actual_duration_minutes, actual_program_id, note, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def reconcile_date(self, air_date: str) -> list[dict]:
        """Compare the latest playout per slot with the plan and persist exceptions."""
        try:
            datetime.strptime(air_date, "%Y-%m-%d")
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        with self.store.transaction():
            self.store.conn.execute("DELETE FROM reconciliation_exceptions WHERE air_date=?", (air_date,))
            slots = self.store.conn.execute(
                "SELECT s.*, p.title, p.sponsor, p.kind FROM slots s JOIN programs p ON p.id=s.program_id "
                "WHERE s.air_date=? AND s.status!='cancelled' ORDER BY s.start_time", (air_date,)
            ).fetchall()
            exceptions: list[tuple[int, str, str]] = []
            for slot in slots:
                log = self.store.conn.execute(
                    "SELECT * FROM playout_logs WHERE slot_id=? ORDER BY id DESC LIMIT 1", (slot["id"],)
                ).fetchone()
                if not log:
                    exceptions.append((slot["id"], "missed", "没有实播记录"))
                    continue
                actual_program_id = log["actual_program_id"] or slot["program_id"]
                if actual_program_id != slot["program_id"]:
                    exceptions.append((slot["id"], "wrong_program",
                                       f"计划节目 #{slot['program_id']}，实播节目 #{actual_program_id}"))
                delta = log["actual_duration_minutes"] - slot["duration_minutes"]
                if abs(delta) > 30:
                    kind = "overrun" if delta > 0 else "underrun"
                    exceptions.append((slot["id"], kind, f"与计划相差 {delta:+d} 分钟"))
                actual = self.store.conn.execute(
                    "SELECT p.* FROM programs p WHERE p.id=?", (actual_program_id,)
                ).fetchone()
                if actual:
                    region_ok = self.store.conn.execute(
                        "SELECT 1 FROM program_regions WHERE program_id=? AND region=?",
                        (actual_program_id, slot["region"]),
                    ).fetchone()
                    if not region_ok or not (actual["start_date"] <= air_date <= actual["end_date"]):
                        exceptions.append((slot["id"], "out_of_license", "实播节目超出地区或日期授权"))
            for slot_id, kind, detail in exceptions:
                self.store.conn.execute(
                    "INSERT INTO reconciliation_exceptions(air_date,slot_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                    (air_date, slot_id, kind, detail, datetime.now().isoformat()),
                )
        return self.get_exceptions(air_date)

    def get_slot(self, slot_id: int) -> dict:
        row = self.store.conn.execute(
            "SELECT s.*, p.title, p.kind, p.sponsor, p.version AS program_version "
            "FROM slots s JOIN programs p ON p.id=s.program_id WHERE s.id=?",
            (slot_id,),
        ).fetchone()
        if not row:
            raise DomainError("排期不存在")
        return dict(row)

    def get_exceptions(self, air_date: str) -> list[dict]:
        return [dict(row) for row in self.store.conn.execute(
            "SELECT * FROM reconciliation_exceptions WHERE air_date=? ORDER BY slot_id, kind", (air_date,)
        ).fetchall()]

    def snapshot(self) -> dict:
        conn = self.store.conn
        programs = [dict(row) for row in conn.execute(
            "SELECT * FROM programs ORDER BY root_id, version, id"
        ).fetchall()]
        regions: dict[int, list[str]] = {}
        for row in conn.execute("SELECT program_id, region FROM program_regions ORDER BY region").fetchall():
            regions.setdefault(row["program_id"], []).append(row["region"])
        for program in programs:
            program["regions"] = regions.get(program["id"], [])
        slots = [dict(row) for row in conn.execute(
            "SELECT s.*, p.title, p.kind, p.version AS program_version "
            "FROM slots s JOIN programs p ON p.id=s.program_id ORDER BY s.air_date, s.start_time"
        ).fetchall()]
        return {"programs": programs, "slots": slots, "exceptions": [dict(row) for row in conn.execute(
            "SELECT * FROM reconciliation_exceptions ORDER BY id DESC LIMIT 50"
        ).fetchall()]}
